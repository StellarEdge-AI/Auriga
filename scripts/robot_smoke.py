"""Actual RoboDojo smoke: RGB observation, small EE motion, refusal and cancellation."""

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys
import threading
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from auriga.robot.backends.robodojo.robodojo import FRAME_ID, TCP_CONFIG_ID, RobotBackend
from auriga.apps.library.experiments import allocate_run, register_run
from auriga.apps.evaluation.recording import ReferenceRecorder
from auriga.core.observation_policy import ACTOR_PROFILE


def action_from_observation(observation, delta_m=0.01):
    pose = deepcopy(observation["robot_state"]["arms"]["left"]["ee_pose"])
    pose["position_m"][2] += delta_m
    return {"schema_version": "0.1", "action_id": "smoke-" + str(uuid.uuid4()),
        "run_id": "robot-smoke", "episode_id": "ep-1", "env_instance_id": "env-1", "env_generation": 1,
        "based_on_observation_id": observation["raw_observation_id"], "frame_id": FRAME_ID, "tcp_config_id": TCP_CONFIG_ID,
        "mode": "absolute", "limits_profile": "simulation-slow-v1", "timeout_s": 60,
        "path": {"kind": "backend_free", "waypoints": [{"left": {"position_m": pose["position_m"],
                  "quaternion_xyzw": pose["quaternion_xyzw"]}}], "orientation": "backend_free", "blend_radius_m": 0,
                 "position_tolerance_m": 0.005}, "gripper_events": []}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--gpu", type=int, default=4)
    parser.add_argument("--run-dir")
    parser.add_argument("--experiment-root")
    parser.add_argument("--category", default="acceptance")
    parser.add_argument("--title")
    parser.add_argument("--tag", action="append", default=[])
    parser.add_argument("--task", default="stack_bowls")
    arguments = parser.parse_args()
    directory = Path(arguments.run_dir).expanduser().resolve() if arguments.run_dir else allocate_run(arguments.experiment_root,
        category=arguments.category, task=arguments.task, title=arguments.title, tags=arguments.tag, kind="validation")
    if directory.exists() and any(path.name != "experiment.json" for path in directory.iterdir()): parser.error("choose a fresh run directory")
    register_run(directory, category=arguments.category, task=arguments.task, title=arguments.title, tags=arguments.tag, kind="validation")
    backend = RobotBackend(gpu_id=arguments.gpu, run_dir=directory, task=arguments.task, observation_profile=ACTOR_PROFILE)
    report = {"data_origin": "robodojo_runtime", "task_success": "not_evaluated",
              "observation_profile": ACTOR_PROFILE, "motion_review": False,
              "execution_role": "fixed_robot_interface_smoke_without_model_review"}
    recorder = ReferenceRecorder(directory, kind="smoke", observation_profile=ACTOR_PROFILE)
    try:
        report["capabilities"] = backend.start()
        recorder.start(report["capabilities"])
        print("Worker initialized", flush=True)
        observation = backend.observe()
        report["before_observation"] = observation
        recorder.observe(observation)
        assert len(observation["cameras"]) == 3
        from auriga.core.storage.images import camera_path
        assert all(camera_path(camera).stat().st_size > 1000 for camera in observation["cameras"])
        action = action_from_observation(observation)
        action["gripper_events"] = [{"arm": "left", "phase": "after_motion", "goal_normalized": 0.5}]
        report["action"] = action
        def progress(update):
            print(json.dumps({key: value for key, value in update.items() if key != "observation"}), flush=True)
        receipt = recorder.execute(backend, action, progress_callback=progress)
        report["motion"] = receipt
        print("Motion result:", receipt["status"], receipt["reason"], receipt.get("position_error_m"), flush=True)
        assert receipt["status"] == "completed", receipt["reason"]
        assert receipt["executed_steps"] > 0 and receipt["terminal_observation"]["cameras"]
        after = receipt["terminal_observation"]
        before_pose = observation["robot_state"]["arms"]["left"]["ee_pose"]["position_m"]
        after_pose = after["robot_state"]["arms"]["left"]["ee_pose"]["position_m"]
        assert after_pose[2] - before_pose[2] > 0.005
        gripper = after["robot_state"]["arms"]["left"]["gripper"]
        assert gripper["goal_normalized"] == 0.5
        assert abs(gripper["command_normalized"] - 0.5) < 1e-3
        unsupported = deepcopy(action)
        unsupported["path"]["kind"] = "linear"
        try:
            recorder.execute(backend, unsupported)
            raise AssertionError("unsupported path accepted")
        except ValueError as error:
            report["unsupported_path"] = str(error)
        out_of_bounds = deepcopy(action)
        out_of_bounds["path"]["waypoints"][0]["left"]["position_m"][0] = 10
        try:
            recorder.execute(backend, out_of_bounds)
            raise AssertionError("out of bounds accepted")
        except ValueError as error:
            report["out_of_bounds"] = str(error)
        unreachable = action_from_observation(after)
        unreachable["path"]["waypoints"][0]["left"]["position_m"] = [1.5, 1.5, 1.5]
        report["unreachable_motion"] = recorder.execute(backend, unreachable)
        assert report["unreachable_motion"]["status"] == "failed" and report["unreachable_motion"]["reason"] == "path_infeasible"
        assert report["unreachable_motion"]["executed_steps"] == 0
        assert report["unreachable_motion"]["terminal_observation"]["cameras"]
        cancellation = threading.Event()
        cancel_action = action_from_observation(after, 0.03)
        def cancel_on_dispatch(update):
            if update.get("phase") == "executing":
                cancellation.set()
        report["cancelled_motion"] = recorder.execute(backend, cancel_action, progress_callback=cancel_on_dispatch, cancel_flag=cancellation)
        assert report["cancelled_motion"]["status"] == "stopped", report["cancelled_motion"]
        assert report["cancelled_motion"]["terminal_observation"]["cameras"]
        assert report["cancelled_motion"]["terminal_observation"]["robot_state"]["arms"]["left"]["gripper"]["goal_normalized"] == 0.5
        report["passed"] = True
    except Exception as error:
        report.update(passed=False, error=f"{type(error).__name__}: {error}")
        raise
    finally:
        try:
            backend.close()
            (directory / "smoke-report.json").write_text(json.dumps(report, indent=2, allow_nan=False))
            print(f"Report: {directory / 'smoke-report.json'}", flush=True)
        finally:
            try:
                recorder.finish(report, status="completed" if report.get("passed") else "failed")
            finally:
                recorder.close()


if __name__ == "__main__":
    main()
