"""Real simulator matrix: reset, calibrated capture, EE/reference and cancellation."""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import threading

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from auriga.robot.backends.robodojo.robodojo import RobotBackend
from robot_smoke import action_from_observation
from reference_baseline import run_reference
from auriga.apps.library.experiments import allocate_run, register_run
from auriga.apps.evaluation.recording import ReferenceRecorder


def pose_difference(first, second):
    return max(abs(a-b) for arm in ("left", "right") for a,b in zip(first["arms"][arm]["q_rad"], second["arms"][arm]["q_rad"]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--gpu", type=int, default=4)
    parser.add_argument("--run-dir")
    parser.add_argument("--experiment-root")
    parser.add_argument("--category", default="validation")
    parser.add_argument("--title")
    parser.add_argument("--tag", action="append", default=[])
    parser.add_argument("--matrix", choices=("legacy", "current"), default="legacy")
    parser.add_argument("--task", default="stack_bowls")
    parser.add_argument("--displacement-m", type=float, default=.005)
    parser.add_argument("--jaw-goal", type=float, default=.8)
    args = parser.parse_args()
    if args.matrix == 'current':
        from auriga.apps.evaluation.robot_matrix import run_validation, validate_options
        try:
            validate_options(args.displacement_m, args.jaw_goal)
        except ValueError as error:
            parser.error(str(error))
    elif args.task != 'stack_bowls':
        parser.error('legacy matrix is pinned to stack_bowls; use --matrix current for another task')
    directory = Path(args.run_dir).expanduser().resolve() if args.run_dir else allocate_run(
        args.experiment_root, category=args.category, task=args.task, title=args.title, tags=args.tag, kind="validation")
    if directory.exists() and any(path.name != "experiment.json" for path in directory.iterdir()):
        parser.error("choose a fresh run directory")
    register_run(directory, category=args.category, task=args.task, title=args.title, tags=args.tag, kind="validation")
    if args.matrix == 'current':
        report = run_validation(directory, gpu=args.gpu, task=args.task,
            displacement_m=args.displacement_m, jaw_goal=args.jaw_goal)
        print(json.dumps(dict(passed=report['passed'], report=str(directory / 'validation-report.json'))), flush=True)
        if not report['passed']:
            raise SystemExit(1)
        return
    recorder = ReferenceRecorder(directory, kind="validation")
    progress = recorder.progress
    backend = RobotBackend(gpu_id=args.gpu, run_dir=directory, observation_profile="rgb_proprio_calibrated_v1")
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "checks": {}, "data_origin": "robodojo_runtime"}
    allowed_observations = []
    generation = None
    results_recorded = False

    def remember(observation):
        allowed = {key: observation[key] for key in ("raw_observation_id", "data_origin", "world_kind", "observation_profile",
            "robot_model_id", "cameras", "robot_state", "sim_step", "step_domain", "control_dt_s", "sim_time_s",
            "captured_at", "camera_calibration", "camera_pose_sample", "robot_model")}
        allowed.update(env_generation=generation, episode_id=f"native-episode-{generation}", env_instance_id="robot-validation-env")
        allowed_observations.append(allowed)
        recorder.identity.update(env_generation=generation, episode_id=f"native-episode-{generation}")
        recorder.store.set_state("runtime", "identity", dict(recorder.identity))
        recorder.observe(observation)
        (directory / "calibrated-observations.jsonl").write_text("".join(json.dumps(value, allow_nan=False)+"\n" for value in allowed_observations))
        progress.update(status="running", phase="robot_matrix", observation_count=len(allowed_observations), checks=report["checks"])
    try:
        report["capabilities"] = backend.start()
        recorder.start(report["capabilities"])
        initial = backend.observe()
        (directory / "initial-calibrated.json").write_text(json.dumps(initial, indent=2))
        print("initial calibrated recording:", directory / "initial-calibrated.json", flush=True)
        first = backend.reset(seed=0, layout_id=0)
        private_first = backend.observer_snapshot()
        second = backend.reset(seed=0, layout_id=0)
        private_second = backend.observer_snapshot()
        report["resets"] = [first, second]
        report["private_reset_snapshots"] = [private_first, private_second]
        report["reset_joint_max_difference_rad"] = pose_difference(first["robot_state"], second["robot_state"])
        object_delta = max(float(np.linalg.norm(np.asarray(a["position_m"])-b["position_m"])) for a,b in zip(private_first["objects"], private_second["objects"]))
        report["reset_object_max_position_difference_m"] = object_delta
        report["checks"]["repeatable_reset"] = report["reset_joint_max_difference_rad"] < 1e-5 and object_delta < 1e-4
        observation = second["observation"]
        generation = second["episode_index"]
        remember(observation)
        calibration = observation["camera_calibration"]
        sample = observation["camera_pose_sample"]
        report["checks"]["camera_synchronized"] = sample["sim_step"] == observation["sim_step"] == observation["robot_state"]["sim_step"]
        camera_checks = {}
        for camera in observation["cameras"]:
            name = camera["camera_id"]
            item, current = calibration["cameras"][name], sample["cameras"][name]
            K, transform = np.array(item["K"]), np.array(current["T_world_camera"])
            camera_checks[name] = {"fx": float(K[0,0]), "fy": float(K[1,1]), "orthogonal_residual": float(np.linalg.norm(transform[:3,:3].T@transform[:3,:3]-np.eye(3))),
                                  "image_shape": list(cv2.imread(camera.get("rgb_path") or camera["png_path"]).shape), "frame_id_match": current["frame_id"] == camera["frame_id"]}
            if "T_link_camera" in item:
                camera_checks[name]["link_composition_residual"] = float(np.linalg.norm(np.array(current["T_world_link"]) @ np.array(item["T_link_camera"])-transform))
        report["camera_checks"] = camera_checks
        report["checks"]["camera_calibration"] = all(value["fx"]>1 and value["fy"]>1 and value["orthogonal_residual"]<1e-5 and value["frame_id_match"] and value.get("link_composition_residual",0)<1e-4 for value in camera_checks.values())
        backend.record()
        action = action_from_observation(observation, 0.005)
        action["timeout_s"] = 180
        extra = deepcopy(action["path"]["waypoints"][0])
        extra["left"]["position_m"][2] += 0.005
        action["path"]["waypoints"].append(extra)
        report["multi_waypoint_action"] = action
        multi = recorder.execute(backend, action, progress_callback=lambda update: remember(update["observation"]) if update.get("observation") else None)
        report["multi_waypoint_motion"] = multi
        report["checks"]["multiple_waypoints"] = multi["status"] == "completed" and len(multi["waypoint_results"]) == 2
        print("multiple waypoints:", multi["status"], multi["reason"], flush=True)
        observation = multi["terminal_observation"]
        remember(observation)
        standalone = deepcopy(action)
        standalone["path"] = {"kind":"hold","waypoints":[]}
        standalone["gripper_events"] = [{"arm":"left","phase":"after_motion","goal_normalized":0.5}]
        grip = recorder.execute(backend, standalone)
        report["standalone_gripper"] = grip
        remember(grip["terminal_observation"])
        report["checks"]["standalone_gripper"] = grip["status"]=="completed" and grip["terminal_observation"]["robot_state"]["arms"]["left"]["gripper"]["goal_normalized"]==0.5
        report["checks"]["gripper_preserves_arms"] = pose_difference(observation["robot_state"],grip["terminal_observation"]["robot_state"])<1e-4
        geometry_observation = grip["terminal_observation"]
        report["geometry_motions"] = []
        for purpose, delta_x in (("train_active_view", 0.03), ("heldout_active_view", 0.015)):
            geometry_action = action_from_observation(geometry_observation, 0)
            geometry_action["timeout_s"] = 180
            geometry_action["path"]["waypoints"][0]["left"]["position_m"][0] += delta_x
            motion = recorder.execute(backend, geometry_action)
            report["geometry_motions"].append({"purpose": purpose, "action": geometry_action, "receipt": motion})
            geometry_observation = motion["terminal_observation"]
            remember(geometry_observation)
        report["checks"]["active_geometry_views"] = all(value["receipt"]["status"] == "completed" for value in report["geometry_motions"])
        for name, kind, x in (("linear_refusal","linear",None),("bounds_refusal",None,10)):
            invalid = action_from_observation(observation)
            if kind: invalid["path"]["kind"] = kind
            if x: invalid["path"]["waypoints"][0]["left"]["position_m"][0] = x
            try: recorder.execute(backend, invalid)
            except ValueError as error:
                report[name]=str(error);report["checks"][name]=True
            else: report["checks"][name]=False
        unreachable = action_from_observation(observation)
        unreachable["path"]["waypoints"][0]["left"]["position_m"]=[1.5,1.5,1.5]
        failure = recorder.execute(backend, unreachable)
        report["unreachable"] = failure
        remember(failure["terminal_observation"])
        report["checks"]["unreachable_failure"] = failure["status"]=="failed" and failure["reason"]=="path_infeasible" and failure["executed_steps"]==0 and bool(failure["terminal_observation"]["cameras"])
        cancellation = threading.Event()
        cancel_action = action_from_observation(geometry_observation,0.03)
        cancel_action["timeout_s"] = 180
        cancelled = recorder.execute(backend, cancel_action,progress_callback=lambda update:cancellation.set() if update.get("phase")=="executing" else None,cancel_flag=cancellation)
        report["cancelled"] = cancelled
        remember(cancelled["terminal_observation"])
        report["checks"]["cancellation"] = cancelled["status"]=="stopped" and cancelled["stop_confirmation"]["clock"]=="paused" and cancelled["stop_confirmation"]["max_joint_velocity_rad_s"]<1e-3
        report["checks"]["cancel_preserves_gripper_goal"] = cancelled["terminal_observation"]["robot_state"]["arms"]["left"]["gripper"]["goal_normalized"]==0.5
        timeout_action = action_from_observation(cancelled["terminal_observation"],0.005)
        timeout_action["timeout_s"] = 0.1
        timed = recorder.execute(backend, timeout_action)
        report["timeout"] = timed
        remember(timed["terminal_observation"])
        report["checks"]["timeout_failure_terminal"] = timed["status"]=="failed" and timed["reason"].endswith("timeout") and bool(timed["terminal_observation"]["cameras"])
        report["evaluation"] = backend.evaluate()
        report["videos"] = backend.save_video()
        recorder.record_results(report)
        results_recorded = True
        report["checks"]["native_evaluator_failure"] = not report["evaluation"]["task_success"] and report["evaluation"]["valid_episode"]
        report["checks"]["video_nonempty"] = all(Path(video["video_path"]).stat().st_size>1000 and video["frames"]>1 for video in report["videos"]["videos"])
        print("validation checks:",json.dumps(report["checks"]),flush=True)
        reference = run_reference(backend, recorder=recorder)
        report["reference"] = reference
        (directory / "reference-report.json").write_text(json.dumps(reference,indent=2))
        report["checks"]["pinned_reference_golden_fixture"] = reference["passed"]
        report["passed"] = all(report["checks"].values())
    except Exception as error:
        report.update(passed=False,error=f"{type(error).__name__}: {error}")
        raise
    finally:
        try:
            backend.close()
            report["finished_at"] = datetime.now(timezone.utc).isoformat()
            (directory / "validation-report.json").write_text(json.dumps(report,indent=2,allow_nan=False))
            print("report:",directory / "validation-report.json",flush=True)
        finally:
            try:
                recorder.finish(report, status="completed" if report.get("passed") else "failed", record_results=not results_recorded)
            finally:
                recorder.close()
    if not report["passed"]: raise SystemExit(1)


if __name__ == "__main__":
    main()
