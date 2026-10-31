"""针对中间态切片（checkpoint slice）、感知重算与 Codex 原生 thread/fork 的单元测试。"""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import numpy as np

from auriga.apps.evaluation.checkpoint import _truncate_sqlite_store, extract_prefix_commands, prepare_checkpoint_slice, slice_rollout_jsonl
from auriga.robot.backends.robodojo.native_replay import capture_mujoco_snapshot, restore_mujoco_snapshot
from auriga.apps.evaluation.turn_probe import (
    ProbeRobotAdapter,
    _check_slice_assertions,
    _extract_tool_error,
    _resolve_cutpoint_ids,
)
from auriga.perception.external.object_table import grasp_hints_from
from auriga.core.storage.store import Store


class CheckpointSliceUnitTests(unittest.TestCase):
    def test_slice_rollout_truncates_and_replaces_output_and_strips_unpaired_calls(self):
        """验证 rollout 裁剪能精确截断到目标 call_id、原地替换输出并剔除同批次未完成的并行调用。"""
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src_rollout.jsonl"
            dst = Path(tmp) / "dst_rollout.jsonl"
            lines = [
                {
                    "timestamp": "2026-10-26T00:00:00Z",
                    "type": "session_meta",
                    "payload": {
                        "id": "019a0000-0000-7000-8000-000000000001",
                        "dynamic_tools": [{"name": "old_tool"}],
                        "base_instructions": {"text": "old instructions"},
                    },
                },
                {
                    "timestamp": "2026-10-26T00:00:01Z",
                    "type": "response_item",
                    "payload": {"type": "function_call", "name": "geometry_query", "arguments": "{}", "call_id": "call_1"},
                },
                {
                    "timestamp": "2026-10-26T00:00:01Z",
                    "type": "response_item",
                    "payload": {"type": "function_call", "name": "robot_observe", "arguments": "{}", "call_id": "call_parallel_unanswered"},
                },
                {
                    "timestamp": "2026-10-26T00:00:02Z",
                    "type": "response_item",
                    "payload": {"type": "function_call_output", "call_id": "call_1", "output": [{"type": "inputText", "text": "old_output"}]},
                },
                {
                    "timestamp": "2026-10-26T00:00:03Z",
                    "type": "response_item",
                    "payload": {"type": "function_call_output", "call_id": "call_parallel_unanswered", "output": [{"type": "inputText", "text": "later"}]},
                },
                {
                    "timestamp": "2026-10-26T00:00:04Z",
                    "type": "response_item",
                    "payload": {"type": "function_call", "name": "robot_action", "arguments": "{}", "call_id": "call_2"},
                },
            ]
            src.write_text("\n".join(json.dumps(x) for x in lines) + "\n")

            new_out = {"success": True, "contentItems": [{"type": "inputText", "text": "recomputed_v2_output"}]}
            info = slice_rollout_jsonl(
                src,
                dst,
                until_call_id="call_1",
                replacement_output=new_out,
                dynamic_tools=[{"name": "new_tool"}],
                developer_instructions="new dev instructions",
            )
            self.assertEqual(info["source_thread_id"], "019a0000-0000-7000-8000-000000000001")
            self.assertTrue(info["replaced_output"])

            kept = [json.loads(line) for line in dst.read_text().splitlines() if line.strip()]
            # 仅保留 session_meta + call_1 + call_1_output（剔除了未在切点前回答的 call_parallel_unanswered 及后续 call_2）
            self.assertEqual(len(kept), 3)
            self.assertEqual(kept[0]["payload"]["dynamic_tools"], [{"name": "new_tool"}])
            self.assertEqual(kept[0]["payload"]["base_instructions"]["text"], "new dev instructions")
            self.assertEqual(kept[1]["payload"]["call_id"], "call_1")
            self.assertEqual(kept[2]["payload"]["call_id"], "call_1")
            self.assertEqual(kept[2]["payload"]["output"], [{"type": "input_text", "text": "recomputed_v2_output"}])

    def test_sqlite_truncation_and_prefix_commands_extraction(self):
        """验证 SQLite 截断与前置控制序列提取。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "run"
            identity = {"run_id": "r1", "episode_id": "ep1", "env_instance_id": "env1", "env_generation": 1}
            store = Store(root, identity)
            store.set_state("runtime", "identity", identity)

            obs1 = {"observation_id": "obs-1", "robot_state": {"sim_step": 0, "arm_joint_positions": {"left": [0.0] * 6}}}
            store.save("observation", "obs-1", obs1)
            store.event("observation.published", {}, {"observation_id": "obs-1"})

            trace_ref = store.put_artifact(
                json.dumps([{"sim_step": 1, "commands": {"left": {"joint_pos": [0.1] * 6}}}]).encode(),
                "dispatched_trace",
                "application/json",
                "observer",
            )
            store.save("action_request", "act-1", {"action_id": "act-1", "based_on_observation_id": "obs-1"})
            store.event("action.requested", {}, {"action_id": "act-1"})
            store.save(
                "action_receipt",
                "act-1",
                {"action_id": "act-1", "status": "completed", "result_observation_id": "obs-2", "dispatched_trace_ref": trace_ref},
            )
            obs2 = {"observation_id": "obs-2", "last_action_id": "act-1", "robot_state": {"sim_step": 1, "arm_joint_positions": {"left": [0.1] * 6}}}
            store.save("observation", "obs-2", obs2)
            store.event("observation.published", {}, {"observation_id": "obs-2", "tool_call_id": "call-cut"})

            # 切点之后的事件与动作
            store.save("action_request", "act-2", {"action_id": "act-2", "based_on_observation_id": "obs-2"})
            store.event("action.requested", {}, {"action_id": "act-2"})
            store.save("observation", "obs-3", {"observation_id": "obs-3", "robot_state": {"sim_step": 2}})
            store.event("observation.published", {}, {"observation_id": "obs-3"})

            prefix = extract_prefix_commands(store, until_action_id="act-1")
            self.assertEqual(prefix["command_count"], 1)
            self.assertEqual(prefix["target_sim_step"], 1)
            self.assertEqual(prefix["boundary_observation_id"], "obs-2")
            self.assertEqual(prefix["target_robot_state"]["arm_joint_positions"]["left"], [0.1] * 6)

            trunc = _truncate_sqlite_store(root / "events.sqlite", until_call_id="call-cut")
            self.assertTrue(trunc["truncated"])
            self.assertEqual(trunc["dropped_observations"], 1)
            self.assertEqual(trunc["dropped_actions"], 1)

    def test_grasp_hints_preserves_floating_object_height_without_silent_reanchor(self):
        """验证悬浮/叠放物体保留真实 3D 高度与 pad_z_m，不做静默桌面重投影。"""
        held_block = {
            "centre_m": [0.345, -0.215, 0.957],
            "centre_xy": [0.345, -0.215],
            "top_z_m": 0.994,
            "bottom_z_m": 0.919,
            "length_m": 0.128,
            "width_m": 0.044,
            "yaw_deg": 31.0,
            "support": "unknown",
            "flags": ["thin_or_on_support", "floating", "low_depth_confidence"],
        }
        hints = grasp_hints_from([held_block], 0.765)
        self.assertEqual(held_block["support"], "unknown")
        self.assertAlmostEqual(held_block["centre_m"][2], 0.957, places=4)
        self.assertGreater(hints["o1"]["pad_z_m"], 0.920)

    def test_prepare_checkpoint_slice_directory(self):
        """验证 prepare_checkpoint_slice 完整生成切片目录、checkpoint_spec.json、prefix_commands.jsonl 与截断后的 rollout。"""
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src_run"
            dst = Path(tmp) / "dst_slice"
            identity = {"run_id": "r1", "episode_id": "ep1", "env_instance_id": "env1", "env_generation": 1}
            store = Store(src, identity)
            store.set_state("runtime", "identity", identity)
            store.save("observation", "obs-1", {"observation_id": "obs-1", "robot_state": {"sim_step": 0}})
            store.event("observation.published", {}, {"observation_id": "obs-1", "tool_call_id": "call_cut"})
            store.close()
            (src / "manifest.json").write_text("{}\n")
            (src / "effective_config.json").write_text("{}\n")
            (src / "scene").mkdir(parents=True, exist_ok=True)
            (src / "actor").mkdir(parents=True, exist_ok=True)
            rollout_dir = src / "private" / "codex-home" / "sessions" / "2026" / "10" / "26"
            rollout_dir.mkdir(parents=True, exist_ok=True)
            rollout_file = rollout_dir / "rollout-2026-10-26T00-00-00-019a0000-0000-7000-8000-000000000001.jsonl"
            rollout_file.write_text(
                "\n".join([
                    json.dumps({"timestamp": "2026-10-26T00:00:00Z", "type": "session_meta", "payload": {"id": "019a0000-0000-7000-8000-000000000001"}}),
                    json.dumps({"timestamp": "2026-10-26T00:00:01Z", "type": "response_item", "payload": {"type": "function_call", "name": "robot_observe", "arguments": "{}", "call_id": "call_cut"}}),
                    json.dumps({"timestamp": "2026-10-26T00:00:02Z", "type": "response_item", "payload": {"type": "function_call_output", "call_id": "call_cut", "output": "ok"}}),
                    json.dumps({"timestamp": "2026-10-26T00:00:03Z", "type": "response_item", "payload": {"type": "function_call", "name": "robot_action", "arguments": "{}", "call_id": "call_later"}}),
                ]) + "\n"
            )

            res = prepare_checkpoint_slice(src, dst, until_call_id="call_cut", recompute_cutpoint=False)
            spec = res["spec"]
            self.assertEqual(spec["source_thread_id"], "019a0000-0000-7000-8000-000000000001")
            self.assertEqual(spec["rollout"]["kept_lines"], 3)
            self.assertTrue((dst / "checkpoint_spec.json").exists())
            self.assertTrue((dst / "prefix_commands.jsonl").exists())

    def test_slice_rollout_closes_open_turn_and_reindexes_ordinal(self):
        """验证在半回合切点处自动补齐 task_complete 闭合事件并重排 ordinal，消除 <turn_aborted> 合成噪音。"""
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.jsonl"
            dst = Path(tmp) / "dst.jsonl"
            lines = [
                {"ordinal": 0, "timestamp": "2026-10-26T00:00:00Z", "type": "session_meta", "payload": {"id": "tid-1"}},
                {"ordinal": 1, "timestamp": "2026-10-26T00:00:01Z", "type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-1", "started_at": 100}},
                {"ordinal": 2, "timestamp": "2026-10-26T00:00:02Z", "type": "response_item", "payload": {"type": "function_call", "name": "geometry_query", "call_id": "call_1"}},
                {"ordinal": 3, "timestamp": "2026-10-26T00:00:03Z", "type": "response_item", "payload": {"type": "function_call_output", "call_id": "call_1", "output": "ok"}},
                {"ordinal": 4, "timestamp": "2026-10-26T00:00:04Z", "type": "response_item", "payload": {"type": "function_call", "name": "robot_action", "call_id": "call_2"}},
            ]
            src.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
            info = slice_rollout_jsonl(src, dst, until_call_id="call_1", close_open_turn=True)
            self.assertTrue(info["closed_open_turn"])
            out_lines = [json.loads(x) for x in dst.read_text().splitlines() if x.strip()]
            self.assertEqual(len(out_lines), 6)
            self.assertEqual([x["ordinal"] for x in out_lines], [0, 1, 2, 3, 4, 5])
            self.assertEqual(out_lines[-1]["payload"]["type"], "task_complete")
            self.assertEqual(out_lines[-1]["payload"]["turn_id"], "turn-1")

    def test_mujoco_snapshot_capture_and_restore(self):
        """验证 MuJoCo 物理状态（qpos/qvel/ctrl/act/mocap）快照保存与毫秒级恢复。"""
        with tempfile.TemporaryDirectory() as tmp:
            snap_file = Path(tmp) / "mujoco_snapshot.npz"
            fake_data = SimpleNamespace(
                qpos=np.array([0.1, 0.2, 0.3], dtype=np.float64),
                qvel=np.array([0.01, 0.02, 0.03], dtype=np.float64),
                ctrl=np.array([1.0, 2.0], dtype=np.float64),
                act=np.array([0.5], dtype=np.float64),
                mocap_pos=np.array([[0.3, -0.1, 0.8]], dtype=np.float64),
                mocap_quat=np.array([[1.0, 0.0, 0.0, 0.0]], dtype=np.float64),
                time=12.5,
            )
            forwarded = []
            fake_sim = SimpleNamespace(data=fake_data, forward=lambda: forwarded.append(True))
            adapter = SimpleNamespace(env=SimpleNamespace(sim=fake_sim), robot=None, control_step=250, native_actions_dispatched=10)

            cap = capture_mujoco_snapshot(adapter, snap_file)
            self.assertTrue(cap["saved"])

            # 清零状态后验证恢复
            fake_data.qpos[:] = 0.0
            fake_data.qvel[:] = 0.0
            fake_data.time = 0.0
            adapter.control_step = 0
            adapter.native_actions_dispatched = 0

            res = restore_mujoco_snapshot(adapter, snap_file)
            self.assertTrue(res["restored"])
            self.assertEqual(res["sim_step"], 250)
            self.assertEqual(adapter.native_actions_dispatched, 10)
            self.assertAlmostEqual(fake_data.time, 12.5)
            np.testing.assert_allclose(fake_data.qpos, [0.1, 0.2, 0.3])
            np.testing.assert_allclose(fake_data.qvel, [0.01, 0.02, 0.03])
            self.assertEqual(len(forwarded), 1)

    def test_probe_robot_adapter_and_slice_assertions(self):
        """验证 ProbeRobotAdapter 的离线批次预检结构与黄金切片断言校验器。"""
        adapter = ProbeRobotAdapter(image_settings={})
        raw = adapter.preview_batch({"stages": [{"id": "approach", "kind": "move", "arm": "right"}, {"id": "close", "kind": "gripper", "arm": "right"}]})
        self.assertEqual(raw["status"], "planned")
        self.assertEqual(raw["planned_native_steps"], 35)
        self.assertEqual(len(adapter.previewed_actions), 1)

        sample_result = {
            "new_summary": {
                "max_stereo_skew_m": 0.008,
                "labelled_count": 6,
                "floating_unknown_count": 1,
                "rows": [
                    {"id": "o1", "support": "table", "flags": []},
                    {"id": "o2", "support": "unknown", "flags": ["floating"]},
                ],
            }
        }
        checks = _check_slice_assertions(
            sample_result,
            {
                "max_skew_below": 0.015,
                "min_labelled_count": 5,
                "max_floating_unknown": 2,
                "expect_support": {"o1": "table"},
                "expect_floating": {"o1": False, "o2": True},
            },
        )
        self.assertEqual(len(checks), 6)
        self.assertTrue(all(c["passed"] for c in checks))

        # 验证双臂过近时 ProbeRobotAdapter 生成 inter_arm_proxy_intersection 风险
        risky_raw = adapter.preview_batch({
            "stages": [{
                "id": "both_close",
                "kind": "move",
                "arm": "both",
                "arms": {
                    "left": {"grasp_position_m": [0.30, 0.01, 0.80]},
                    "right": {"grasp_position_m": [0.30, -0.01, 0.80]},
                },
            }]
        })
        self.assertEqual(risky_raw["geometry_review"]["status"], "risk")
        self.assertIn("inter_arm_proxy_intersection", risky_raw["geometry_review"]["risks"])

        # 验证 turn_fork / live_slice 出现工具调用报错时自动使断言失败（避免假阳性 PASS）
        err_msg = _extract_tool_error({"success": False, "contentItems": [{"type": "inputText", "text": "cached_batch_commands_cannot_be_changed"}]})
        self.assertEqual(err_msg, "cached_batch_commands_cannot_be_changed")
        live_with_err = {
            "mode": "live_slice",
            "tool_error_count": 1,
            "executed_slice_actions": [{"action_id": "act-1", "status": "completed"}],
        }
        err_checks = _check_slice_assertions(live_with_err, {"live_completed_min": 1})
        self.assertFalse(all(c["passed"] for c in err_checks))


if __name__ == "__main__":
    unittest.main()
