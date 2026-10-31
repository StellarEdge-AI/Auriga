"""Pinned RoboProbe grasp-EE golden fixture through installed RoboDojo planning.

It uses upstream pose.py byte-for-byte, native plan_path and joint replay. The
bounded sequence verifies frame/TCP/gripper semantics; it is not an LLM policy
reproduction or a benchmark success claim. Observer scores remain private.
"""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from auriga.robot.model import roboprobe_pose as pose
from auriga.robot.backends.robodojo.robodojo import FRAME_ID, TCP_CONFIG_ID, RobotBackend
from auriga.apps.library.experiments import allocate_run, register_run
from auriga.apps.evaluation.recording import ReferenceRecorder
from auriga.core.observation_policy import LEGACY_ACTOR_PROFILE


def reference_action(observation, *, delta_z_m=0.005, yaw_delta_deg=0.0, gripper=None):
    measured = observation["robot_state"]["arms"]["left"]["ee_pose"]
    native = np.array(measured["position_m"] + measured["quaternion_xyzw"][3:] + measured["quaternion_xyzw"][:3])
    values = pose.pose_to_values(native)
    requested = {**values, "z": values["z"] + delta_z_m, "yaw_deg": values["yaw_deg"] + yaw_delta_deg}
    flange = pose.values_to_pose(requested)
    roundtrip = pose.values_to_pose(values)
    if np.linalg.norm(roundtrip[:3] - native[:3]) > 1e-9 or pose.orientation_error_deg(roundtrip[3:], native[3:]) > 1e-5:
        raise ValueError("pinned_reference_pose_roundtrip_failed")
    action = {"mode": "absolute", "frame_id": FRAME_ID, "tcp_config_id": TCP_CONFIG_ID,
              "limits_profile": "robodojo-native-v1", "timeout_s": 180,
              "path": {"kind": "backend_free", "waypoints": [{"left": {
                  "position_m": flange[:3].tolist(), "quaternion_xyzw": np.r_[flange[4:], flange[3]].tolist()}}],
                  "position_tolerance_m": 0.005}, "gripper_events": []}
    if gripper is not None:
        action["gripper_events"] = [{"arm": "left", "phase": "after_motion", "goal_normalized": gripper}]
    return action, {"measured_flange_wxyz": native.tolist(), "measured_grasp_values": values,
                    "requested_grasp_values": requested, "planned_flange_wxyz": flange.tolist(),
                    "grasp_point_offset_m": pose.GRASP_POINT_OFFSET_M,
                    "flange_to_grasp_offset_m": (pose.GRASP_POINT_OFFSET_M * pose.tool_axis(flange[3:])).tolist()}


def run_reference(backend, *, reset=True, recorder=None):
    if reset:
        reset_result = backend.reset(seed=0, layout_id=0)
        initial = reset_result["observation"]
        if recorder:
            generation = reset_result["episode_index"]
            recorder.identity.update(env_generation=generation, episode_id=f"native-episode-{generation}")
            recorder.store.set_state("runtime", "identity", dict(recorder.identity))
    else:
        initial = backend.observe()
    if recorder:
        recorder.observe(initial)
    # The fixture consumes only measured robot state; calibrated data is explicitly removed.
    initial_policy_input = {key: deepcopy(initial[key]) for key in ("raw_observation_id", "cameras", "robot_state")}
    backend.record()
    receipts = []
    observation = initial_policy_input
    try:
        for index, specification in enumerate((dict(delta_z_m=0.005, gripper=0.5), dict(delta_z_m=0.005))):
            action, reference = reference_action(observation, **specification)
            if recorder:
                receipt = recorder.execute(backend, action)
            else:
                receipt = backend.execute(action)
            receipts.append({"index": index, "reference": reference, "action": action, "receipt": receipt})
            observation = {key: deepcopy(receipt["terminal_observation"][key])
                           for key in ("raw_observation_id", "cameras", "robot_state")}
            if receipt["status"] != "completed" or receipt.get("episode_terminal", {}).get("ended"):
                break
        terminal = observation["robot_state"]
        checks = {"actual_motion": len(receipts) == 2 and all(item["receipt"]["executed_steps"] > 0 for item in receipts),
                  "both_moves_completed": len(receipts) == 2 and all(item["receipt"]["status"] == "completed" for item in receipts),
                  "gripper_latched_across_second_motion": terminal["arms"]["left"]["gripper"]["goal_normalized"] == 0.5,
                  "other_arm_held": max(abs(a-b) for a,b in zip(initial["robot_state"]["arms"]["right"]["q_rad"], terminal["arms"]["right"]["q_rad"])) < 1e-4}
        if receipts and receipts[0]["receipt"]["planned_trace"].get("q_rad"):
            receipt = receipts[0]["receipt"]
            arm_steps = len(receipt["planned_trace"]["q_rad"])
            # Every native action has one measured boundary sample; the first is pre-action.
            samples = receipt["actual_trace"][1:]
            commands = [sample["state"]["arms"]["left"]["gripper"]["goal_normalized"] for sample in samples]
            checks["gripper_only_after_arm_phase"] = bool(commands[arm_steps:]) and all(
                command == initial["robot_state"]["arms"]["left"]["gripper"]["goal_normalized"] for command in commands[:arm_steps]
            ) and all(command == 0.5 for command in commands[arm_steps:])
        evaluation = backend.evaluate()
    finally:
        videos = backend.save_video()
    if recorder: recorder.record_results({"evaluation": evaluation, "videos": videos})
    provenance = json.loads((Path(__file__).resolve().parent.parent / "third_party/roboprobe/provenance.json").read_text())
    return {"reference_id": "roboprobe-eef-golden-fixture-v1", "provenance": provenance,
            "llm_calls": 0, "http_attempt_count": 0, "automatic_action_retries": False,
            "full_llm_policy_reproduction": False, "benchmark_result": False, "observation_profile": "rgb_proprio_v1",
            "recording_observation_profile": initial["observation_profile"],
            "motion_review": False, "execution_role": "fixed_upstream_reference_fixture",
            "policy_inputs": "RGB files and measured proprioception; golden fixture itself uses measured proprioception only; no hidden task state or calibration",
            "shared_semantics": ["upstream grasp-point-to-link6 conversion", "native CuRobo plan_path with current measured q and robot entity origin", "native joint command replay", "arm phase before gripper phase", "latched requested gripper goal during subsequent motion", "unrequested arm held"],
            "adaptations": ["single arm", "official 25 Hz native action steps and live embodiment step limits", "bounded two-motion fixture instead of upstream LLM decisions", "independent native predicate evaluation and observer video"],
            "initial_observation": initial, "receipts": receipts, "checks": checks, "passed": all(checks.values()),
            "evaluation": evaluation, "videos": videos}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--gpu", type=int, default=4)
    parser.add_argument("--task", default="stack_bowls")
    parser.add_argument("--run-dir")
    parser.add_argument("--experiment-root")
    parser.add_argument("--category", default="reference")
    parser.add_argument("--title")
    parser.add_argument("--tag", action="append", default=[])
    args = parser.parse_args()
    directory = Path(args.run_dir).expanduser().resolve() if args.run_dir else allocate_run(args.experiment_root,
        category=args.category, task=args.task, title=args.title, tags=args.tag, kind="validation")
    if directory.exists() and any(path.name != "experiment.json" for path in directory.iterdir()): parser.error("choose a fresh run directory")
    register_run(directory, category=args.category, task=args.task, title=args.title, tags=args.tag, kind="validation")
    backend = RobotBackend(gpu_id=args.gpu, task=args.task, run_dir=directory, observation_profile=LEGACY_ACTOR_PROFILE)
    recorder = ReferenceRecorder(directory, kind="golden_fixture", observation_profile=LEGACY_ACTOR_PROFILE)
    try:
        recorder.start(backend.start())
        result = run_reference(backend, recorder=recorder)
        path = directory / "reference-report.json"
        path.write_text(json.dumps(result, indent=2, allow_nan=False))
        recorder.finish(result, status="completed" if result["passed"] else "failed", record_results=False)
        print(json.dumps({"report": str(path), "fixture_passed": result["passed"], "task_success": result["evaluation"]["task_success"], "normalized_score": result["evaluation"]["normalized_score"]}))
        if not result["passed"]:
            raise SystemExit(1)
    finally:
        try:
            backend.close()
        finally:
            recorder.close()


if __name__ == "__main__":
    main()
