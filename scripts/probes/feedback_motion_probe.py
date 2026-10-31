"""Three fixed native EE requests, without a model or task-success claim.

GPU admission belongs to the caller. Run each mode in a fresh process/directory;
all targets derive once from that run's initial pose, never from an earlier move.
"""

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from auriga.core.config import Config
from auriga.core.contracts import new_id
from auriga.apps.library.experiments import register_run
from auriga.robot.motion.feedback_motion import pose_error
from auriga.runtime.harness import Harness
from auriga.robot.backends.robodojo.robodojo import RobotBackend
from auriga.core.storage.store import Store


def now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def read_state(store, observation):
    return json.loads(store.resolve_artifact(observation["robot_state_ref"], ("actor",))[1])


def fixed_targets(initial_state):
    initial = {key: deepcopy(initial_state["arms"]["left"]["ee_pose"][key])
               for key in ("position_m", "quaternion_xyzw")}
    raised = deepcopy(initial)
    raised["position_m"][2] += .005
    forward = deepcopy(raised)
    forward["position_m"][0] += .03
    return [("raise_5mm", raised), ("forward_30mm_at_raised_height", forward),
            ("return_initial", initial)]


def run(args):
    root = args.run_dir.expanduser().resolve()
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise ValueError("choose a fresh run directory")
    # Explicit config plus overrides, without unrelated ambient AURIGA_* values.
    config = Config.load(args.config, environ={}, overrides={
        "run": {"run_dir": str(root), "task": args.task, "seed": args.seed,
                "layout_id": args.layout_id, "gpu": args.gpu, "benchmark": False,
                "resume": False, "replay": False},
        "features": {"motion_execution_mode": args.mode, "motion_review": False,
                     "tracking_guard": False, "joint_replan": False,
                     "observation_profile": "rgb_proprio_calibrated_v1",
                     "depth_skills": False, "depth_auto_feedback": False,
                     "depth_event_payload": False, "pi05": False}})
    config.validate_launch()
    register_run(root, category="validation", task=args.task,
                 title=f"Feedback motion native probe: {args.mode}",
                 tags=["feedback-motion-probe", args.mode], kind="validation")
    write_json(root / "effective_config.json", config.to_dict())
    report = dict(status="running", started_at=now(), mode=args.mode,
                  task=args.task, seed=args.seed, layout_id=args.layout_id, gpu=args.gpu,
                  model_calls=0, scope="fixed movement diagnostic; not a task-success benchmark",
                  gripper_policy="no gripper events; preserve both initially latched goals",
                  target_policy="initial left EE plus [0,0,.005], [.03,0,.005], [0,0,0] metres",
                  cases=[], cleanup={"steps": {}, "errors": [], "robot": None})
    source = Path(__file__).resolve().parents[2]
    report["source_sha256"] = {str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in sorted((source / "auriga").rglob("*.py"))}
    report["source_sha256"]["scripts/probes/feedback_motion_probe.py"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    store = backend = harness = None

    def save():
        write_json(root / "report.json", report)

    try:
        save()
        identity = dict(run_id=root.name, episode_id=new_id("ep"),
                        env_instance_id=new_id("native-env"), env_generation=1)
        store = Store(root, identity)
        backend = RobotBackend(config=config, run_dir=root / "private" / "robot")
        harness = Harness(store, backend, config=config, motion_review=False)
        observation = harness.start()
        initial = read_state(store, observation)
        report.update(initial_observation=observation, initial_state=initial,
                      capabilities=harness.capabilities, targets=fixed_targets(initial))
        save()
        for label, target in report["targets"]:
            if not harness.accepting or harness.episode_done.is_set():
                report["cases"].append(dict(label=label, target=target, status="not_submitted",
                                            reason="motion_admission_closed_or_native_terminal"))
                continue
            observation = harness.latest
            before = read_state(store, observation)
            action = dict(schema_version="0.1", **harness.identity,
                          action_id=new_id("probe"), based_on_observation_id=observation["observation_id"],
                          mode="absolute", world_kind="observed",
                          frame_id=harness.capabilities["frame_id"],
                          tcp_config_id=harness.capabilities["tcp_config_id"],
                          limits_profile=harness.capabilities.get("limits_profile", "robodojo-native-v1"),
                          timeout_s=120,
                          path=dict(kind="backend_free", waypoints=[dict(left=deepcopy(target))],
                                    position_tolerance_m=.005, orientation="backend_free", blend_radius_m=0),
                          gripper_events=[])
            case = dict(label=label, target=target, request=action, before_state=before,
                        native_count_before=harness.native_action_count, status="submitted")
            report["cases"].append(case)
            save()
            started = time.monotonic()
            receipt = harness.execute(action)["receipt"]
            case.update(receipt=receipt, status=receipt.get("status", "unknown"),
                        execution_wall_s=time.monotonic() - started,
                        native_count_after=harness.native_action_count,
                        native_count_delta=harness.native_action_count - case["native_count_before"])
            case["traces"] = {}
            for kind in ("planned_trace", "dispatched_trace", "actual_trace", "before_state"):
                ref = receipt.get(kind + "_ref")
                if ref:
                    value = json.loads(store.resolve_artifact(ref, ("actor",))[1])
                    filename = label + "-" + kind + ".json"
                    write_json(root / filename, value)
                    case["traces"][kind] = dict(artifact_ref=ref, path=filename)
            # Unknown outcomes must not trigger further robot operations.
            result_id = receipt.get("result_observation_id")
            if result_id:
                result = store.read("observation", result_id)
                after = read_state(store, result)
                position, rotation = pose_error(after["arms"]["left"]["ee_pose"], target)
                case.update(after_state=after, position_error_m=position, orientation_error_rad=rotation,
                            control_step_delta=after["sim_step"] - before["sim_step"])
            else:
                case.update(after_state=None, after_state_unavailable=receipt.get(
                    "observation_unavailable_reason", "receipt_has_no_result_observation"))
            save()
            print(json.dumps({"label": label, "status": case["status"],
                              "native_steps": receipt.get("executed_native_steps"),
                              "position_error_m": case.get("position_error_m")}), flush=True)
        if harness.accepting:
            report["diagnostic_evaluation"] = backend.evaluate(final_check=False)
            report["evaluation_final_check"] = False
        else:
            report["evaluation_skipped_reason"] = "motion_admission_closed; no further backend request"
        report["native_episode"] = getattr(harness, "episode_terminal", None)
        report["status"] = ("completed" if all(c["status"] == "completed" for c in report["cases"])
                            else "completed_with_motion_failures")
    except Exception as error:
        report.update(status="failed", error=dict(type=type(error).__name__, message=str(error),
                                                   traceback=traceback.format_exc()))
    finally:
        # Harness has no close(): close admission first, then native worker, then Store.
        for name, operation in (("motion_admission", harness.request_stop if harness else None),
                                ("robot", backend.close if backend else None),
                                ("store", store.close if store else None)):
            if operation is None:
                report["cleanup"]["steps"][name] = "not_created"
                continue
            try:
                value = operation()
                report["cleanup"]["steps"][name] = "completed"
                if name == "robot":
                    report["cleanup"]["robot"] = value
                    if not (value.get("native_close_acknowledged") and value.get("process_exit_confirmed")
                            and not value.get("forced_termination") and not value.get("errors")):
                        report["cleanup"]["errors"].append(dict(step=name, error="native_close_not_confirmed_clean"))
            except Exception as error:
                report["cleanup"]["steps"][name] = "failed"
                report["cleanup"]["errors"].append(dict(step=name, error=f"{type(error).__name__}: {error}"))
        if report["cleanup"]["errors"]:
            report["status"] = "failed"
        report["finished_at"] = now()
        save()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--gpu", type=int, required=True)
    parser.add_argument("--mode", choices=("resampled", "feedback"), required=True)
    parser.add_argument("--task", default="put_bottles_into_dustbin")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--layout-id", type=int, default=0)
    args = parser.parse_args()
    if min(args.gpu, args.seed, args.layout_id) < 0:
        parser.error("gpu, seed and layout-id must be nonnegative")
    report = run(args)
    print(json.dumps(dict(status=report["status"], report=str(args.run_dir / "report.json"))), flush=True)
    raise SystemExit(0 if report["status"] == "completed" else 1)


if __name__ == "__main__":
    main()
