"""Worker-side restoration of recorded native state: actuator command replay and MuJoCo snapshots."""

import json
from pathlib import Path


def capture_mujoco_snapshot(adapter, snapshot_path: str | Path) -> dict:
    """将当前 MuJoCo 物理状态（qpos/qvel/act/ctrl/mocap）保存为快照文件，供后续毫秒级瞬移恢复。"""
    import numpy as np

    snapshot_path = Path(snapshot_path)
    sim = getattr(adapter.env, "sim", None) or getattr(adapter.robot, "sim", None)
    data = getattr(sim, "data", None) if sim is not None else getattr(adapter.env, "data", None)
    if data is None:
        return {"saved": False, "reason": "mujoco_data_unavailable"}
    arrays = {
        "qpos": np.array(data.qpos, dtype=np.float64),
        "qvel": np.array(data.qvel, dtype=np.float64),
        "ctrl": np.array(data.ctrl, dtype=np.float64),
        "time": np.array([float(getattr(data, "time", 0.0))], dtype=np.float64),
        "meta": np.array([int(getattr(adapter, "control_step", 0)), int(getattr(adapter, "native_actions_dispatched", 0))], dtype=np.int64),
    }
    if getattr(data, "act", None) is not None and len(data.act):
        arrays["act"] = np.array(data.act, dtype=np.float64)
    if getattr(data, "mocap_pos", None) is not None and len(data.mocap_pos):
        arrays["mocap_pos"] = np.array(data.mocap_pos, dtype=np.float64)
        arrays["mocap_quat"] = np.array(data.mocap_quat, dtype=np.float64)
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(snapshot_path, **arrays)
    return {"saved": True, "snapshot_path": str(snapshot_path)}

def restore_mujoco_snapshot(adapter, snapshot_path: str | Path) -> dict:
    """从快照文件直接恢复 MuJoCo 物理状态（qpos/qvel/act/ctrl/mocap）并执行 mj_forward。"""
    import numpy as np

    snapshot_path = Path(snapshot_path)
    if not snapshot_path.exists():
        return {"restored": False, "reason": "snapshot_not_found"}
    sim = getattr(adapter.env, "sim", None) or getattr(adapter.robot, "sim", None)
    data = getattr(sim, "data", None) if sim is not None else getattr(adapter.env, "data", None)
    model = getattr(sim, "model", None) if sim is not None else getattr(adapter.env, "model", None)
    if data is None:
        return {"restored": False, "reason": "mujoco_data_unavailable"}
    with np.load(snapshot_path) as snap:
        data.qpos[:] = snap["qpos"]
        data.qvel[:] = snap["qvel"]
        data.ctrl[:] = snap["ctrl"]
        if "act" in snap and getattr(data, "act", None) is not None and len(data.act):
            data.act[:] = snap["act"]
        if "mocap_pos" in snap and getattr(data, "mocap_pos", None) is not None and len(data.mocap_pos):
            data.mocap_pos[:] = snap["mocap_pos"]
            data.mocap_quat[:] = snap["mocap_quat"]
        if "time" in snap:
            data.time = float(snap["time"][0])
        if "meta" in snap:
            adapter.control_step = int(snap["meta"][0])
            adapter.native_actions_dispatched = int(snap["meta"][1])
    if sim is not None and hasattr(sim, "forward"):
        sim.forward()
    elif model is not None:
        import mujoco
        mujoco.mj_forward(model, data)
    return {
        "restored": True,
        "snapshot_path": str(snapshot_path),
        "sim_step": int(getattr(adapter, "control_step", 0)),
    }

def fast_forward_adapter(adapter, checkpoint_spec: dict, *, use_snapshot_cache: bool = True) -> dict:
    """在 RoboDojoAdapter 中恢复切点物理态：优先尝试 qpos/qvel 毫秒级快照恢复，未命中时无渲染重放控制序列并写缓存。"""
    import numpy as np

    prefix_raw = checkpoint_spec.get("prefix_commands_path")
    prefix_path = Path(prefix_raw) if prefix_raw else None
    snapshot_raw = checkpoint_spec.get("mujoco_snapshot_path")
    snapshot_path = Path(snapshot_raw) if snapshot_raw else (prefix_path.with_name("mujoco_snapshot.npz") if prefix_path else None)
    if use_snapshot_cache and snapshot_path and snapshot_path.exists():
        snap_res = restore_mujoco_snapshot(adapter, snapshot_path)
        if snap_res.get("restored"):
            target_goals = (checkpoint_spec.get("target_robot_state") or {}).get("gripper_goals")
            if isinstance(target_goals, dict) and target_goals and hasattr(adapter, "goals"):
                adapter.goals = {side: float(target_goals.get(side, adapter.goals.get(side, 1.0))) for side in ("left", "right")}
                adapter.last_dispatched_goals = dict(adapter.goals)
            score_before = None
            if hasattr(adapter, "evaluate"):
                try:
                    ev = adapter.evaluate(final_check=False)
                    score_before = ev.get("normalized_score", ev.get("score"))
                except Exception:
                    pass
            return {
                "mode": "mujoco_snapshot",
                "fast_forwarded_steps": 0,
                "sim_step": snap_res["sim_step"],
                "native_actions_dispatched": int(getattr(adapter, "native_actions_dispatched", 0)),
                "max_joint_error_rad": 0.0,
                "score_before": score_before,
                "snapshot_path": str(snapshot_path),
            }

    if prefix_path is not None:
        commands = [
            json.loads(line)["commands"]
            for line in prefix_path.read_text().splitlines()
            if line.strip()
        ]
    else:
        commands = [
            item["commands"] if isinstance(item, dict) and "commands" in item else item
            for item in (checkpoint_spec.get("commands") or [])
        ]
    if not commands:
        score_before = None
        if hasattr(adapter, "evaluate"):
            try:
                ev = adapter.evaluate(final_check=False)
                score_before = ev.get("normalized_score", ev.get("score"))
            except Exception:
                pass
        return {
            "mode": "noop",
            "fast_forwarded_steps": 0,
            "sim_step": int(getattr(adapter, "control_step", 0)),
            "native_actions_dispatched": int(getattr(adapter, "native_actions_dispatched", 0)),
            "max_joint_error_rad": 0.0,
            "score_before": score_before,
        }

    ticks = max(1, int(getattr(adapter, "native_step_ticks", 10)))
    if hasattr(adapter, "_native_action"):
        from env.robot_manager.control_manager import MetaControl
        orig_process = adapter.env.process_control_info
        orig_pop = adapter.robot.control_manager.pop
        group = install_recorded_controls(adapter, MetaControl)
        try:
            for offset in range(0, len(commands), ticks):
                chunk = commands[offset : offset + ticks]
                if not chunk:
                    break
                group[:] = list(chunk)
                last_cmd = chunk[-1]
                row = []
                for side in ("left", "right"):
                    arm_pos = last_cmd[f"{side}_arm_joint_state"]["position"]
                    ee_pos = last_cmd[f"{side}_ee_joint_state"]["position"]
                    goal_norm = float(np.clip(float(ee_pos[0]) / 0.044, 0.0, 1.0)) if ee_pos else 1.0
                    row.extend([*arm_pos, goal_norm])
                if not adapter._native_action(row):
                    break
        finally:
            adapter.env.process_control_info = orig_process
            adapter.robot.control_manager.pop = orig_pop
        target_goals = (checkpoint_spec.get("target_robot_state") or {}).get("gripper_goals")
        if isinstance(target_goals, dict) and target_goals:
            adapter.goals = {side: float(target_goals.get(side, adapter.goals.get(side, 1.0))) for side in ("left", "right")}
            adapter.last_dispatched_goals = dict(adapter.goals)
    else:
        orig_control_step = adapter.robot.control_manager.step
        orig_env_step = adapter.env.step
        install_recorded_controls(adapter, commands)
        try:
            while adapter._replay_cursor < len(commands):
                adapter.env.step([0], decimation=1)
        finally:
            adapter.robot.control_manager.step = orig_control_step
            adapter.env.step = orig_env_step
        adapter.native_actions_dispatched = len(commands) // ticks

    if use_snapshot_cache and snapshot_path is not None:
        capture_mujoco_snapshot(adapter, snapshot_path)
    measured = adapter.state() if hasattr(adapter, "state") else adapter.observe(video_only=False)["robot_state"]
    target_joints = (checkpoint_spec.get("target_robot_state") or {}).get("arm_joint_positions") or {}
    max_err = 0.0
    measured_arms = measured.get("arms") or {}
    for side, expected in target_joints.items():
        actual = (measured.get("arm_joint_positions") or {}).get(side)
        if actual is None and isinstance(measured_arms.get(side), dict):
            actual = measured_arms[side].get("q_rad")
        if expected and actual:
            err = float(np.max(np.abs(np.asarray(actual, dtype=float) - np.asarray(expected, dtype=float))))
            max_err = max(max_err, err)
    score_before = None
    if hasattr(adapter, "evaluate"):
        try:
            ev = adapter.evaluate(final_check=False)
            score_before = ev.get("normalized_score", ev.get("score"))
        except Exception:
            pass
    return {
        "mode": "prefix_replay",
        "fast_forwarded_steps": len(commands),
        "sim_step": int(adapter.control_step),
        "native_actions_dispatched": int(getattr(adapter, "native_actions_dispatched", 0)),
        "max_joint_error_rad": max_err,
        "score_before": score_before,
        "snapshot_path": str(snapshot_path) if snapshot_path and snapshot_path.exists() else None,
    }


def install_recorded_controls(adapter, meta_control_type):
    """Keep native take_action scheduling; replace interpolation and jaw clamping."""
    env, manager = adapter.env, adapter.robot.control_manager
    original_process = env.process_control_info
    group = []

    class RecordedControl(meta_control_type):
        def get_action(self, robot_manager, env_idx):
            return self.control_info_dict

    def process(control_info, env_idx):
        original_process(control_info, env_idx)  # Consume native support-arm queues too.
        return group

    def pop(env_idx_list=None):
        if env_idx_list not in (None, [0]):
            raise ValueError("replay_requires_one_environment")
        command = manager.control_queue[0].pop().control_info_dict
        manager.prev_control[0] = command
        return [RecordedControl(command)]

    env.process_control_info, manager.pop = process, pop
    return group
