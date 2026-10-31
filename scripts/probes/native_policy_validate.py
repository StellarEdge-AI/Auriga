"""Physical native-EvalEnv validation, with every write owned by Harness."""

import argparse
from copy import deepcopy
import itertools
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from auriga.runtime.policy.action_policy import OpenPIProcessPolicy, arx_state
from auriga.core.config import Config
from auriga.core.contracts import new_id
from auriga.apps.library.experiments import register_run
from auriga.runtime.harness import Harness
from auriga.core.observation_policy import ACTOR_PROFILE
from auriga.robot.backends.robodojo.robodojo import RobotBackend
from auriga.core.storage.store import Store


def state(store, observation):
    return json.loads(store.resolve_artifact(observation["robot_state_ref"], ("actor",))[1])


def grasp_center(arm):
    pose = arm["ee_pose"]
    x, y, z, w = pose["quaternion_xyzw"]
    axis = (1 - 2 * (y*y + z*z), 2 * (x*y + z*w), 2 * (x*z - y*w))
    return [p + .1501 * a for p, a in zip(pose["position_m"], axis)]


def main():
    policy = Config.load().policy
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--task", default="stack_blocks")
    parser.add_argument("--gpu", type=int, default=7)
    parser.add_argument("--hold-limit", action="store_true")
    parser.add_argument("--ee-smoke", action="store_true")
    parser.add_argument("--episode", action="store_true", help="Re-observe and infer until the official native terminal")
    parser.add_argument("--episode-prefix", type=int, choices=range(1, 51), default=50)
    parser.add_argument("--noise-seed", type=int, help="Optional diagnostic inference noise seed; increments once per chunk")
    parser.add_argument("--prefixes", type=int, nargs="+", default=[1, 5, 10])
    parser.add_argument("--prompts", nargs="+", choices=["task", "subtask", "fine"], default=["task", "subtask", "fine"])
    parser.add_argument("--policy-python", default=policy.python, help="OpenPI interpreter; defaults to the unified config policy.python")
    parser.add_argument("--checkpoint", default=policy.checkpoint, help="pi0.5 checkpoint; defaults to the unified config policy.checkpoint")
    args = parser.parse_args()
    root = Path(args.run_dir).resolve()
    if root.exists() and any(root.iterdir()):
        parser.error("choose a fresh run directory")
    register_run(root, category="validation", task=args.task, title="Native π0.5 prefix validation", kind="validation")
    identity = dict(run_id=root.name, episode_id=new_id("ep"), env_instance_id=new_id("native-env"), env_generation=1)
    store = Store(root, identity)
    backend = RobotBackend(gpu_id=args.gpu, task=args.task, run_dir=root / "private" / "robot", observation_profile=ACTOR_PROFILE)
    harness = Harness(store, backend, observation_profile=ACTOR_PROFILE, motion_review=False)
    policy = None
    report = dict(task=args.task, cases=[], status="running",
                  observation_profile=ACTOR_PROFILE, motion_review=False,
                  execution_role="fixed_controller_diagnostic_without_model_review", scope="development prefix execution; not a task-success benchmark")
    report.update(episode_prefix=args.episode_prefix if args.episode else None, noise_seed=args.noise_seed)
    def save():
        (root / "native-policy-report.json").write_text(json.dumps(report, indent=2, allow_nan=False))
    try:
        observation = harness.start()
        report["capabilities"] = harness.capabilities
        save()
        if args.ee_smoke:
            before = state(store, observation)
            pose = deepcopy(before["arms"]["left"]["ee_pose"])
            pose["position_m"][2] += .005
            action = dict(schema_version="0.1", **harness.identity, action_id=new_id("ee-smoke"),
                based_on_observation_id=observation["observation_id"], mode="absolute", world_kind="observed",
                frame_id="env_origin", tcp_config_id="arx-x5-link6-v1", limits_profile="robodojo-native-v1", timeout_s=120,
                path=dict(kind="backend_free", waypoints=[dict(left={key: pose[key] for key in ("position_m", "quaternion_xyzw")})],
                          position_tolerance_m=.005, orientation="backend_free", blend_radius_m=0),
                gripper_events=[dict(arm="left", phase="after_motion", goal_normalized=.5)])
            receipt = harness.execute(action)["receipt"]
            report["cases"].append(dict(receipt=receipt, evaluation=backend.evaluate(final_check=False)))
            assert receipt["status"] == "completed", receipt
            assert receipt["executed_native_steps"] > 0
            assert receipt["executed_control_steps"] == 10 * receipt["executed_native_steps"]
        elif args.hold_limit:
            while not harness.episode_done.is_set():
                before = state(store, observation)
                row = arx_state(before)
                action = dict(schema_version="0.1", **harness.identity, type="JointPrefixAction", world_kind="observed",
                    action_id=new_id("hold"), based_on_observation_id=observation["observation_id"],
                    actions=[row] * 50, timeout_s=300, policy_provenance={"purpose": "native_action_limit_hold_probe"})
                receipt = harness.execute(action)["receipt"]
                observation = harness.latest
                report["cases"].append(dict(receipt=receipt, native_action_count=harness.native_action_count))
                save()
                if receipt["status"] not in {"completed", "stopped"}:
                    raise RuntimeError(receipt)
                print(json.dumps(dict(native_action_count=harness.native_action_count, status=receipt["status"])), flush=True)
            report["episode_terminal"] = harness.episode_terminal
            report["evaluation"] = backend.evaluate()
            assert harness.episode_terminal["reason"] == "native_step_limit"
            assert harness.native_action_count == harness.episode_terminal["native_step_limit"]
            before_count = harness.native_action_count
            action["action_id"] = new_id("after-terminal")
            action["based_on_observation_id"] = observation["observation_id"]
            report["after_terminal_refusal"] = harness.execute(action)["receipt"]
            assert report["after_terminal_refusal"]["status"] == "rejected"
            assert harness.native_action_count == before_count
        else:
            policy = OpenPIProcessPolicy(args.policy_python, args.checkpoint, gpu=args.gpu, log_path=root / "policy.log")
            conditions = [(prompt_kind, prefix) for prompt_kind in args.prompts for prefix in args.prefixes]
            if args.episode:
                conditions = itertools.repeat(("task", args.episode_prefix))
            for prompt_kind, prefix in conditions:
                if harness.episode_done.is_set() and args.episode:
                    break
                if report["cases"] and not args.episode:
                    observation = harness.reset(seed=0, layout_id=0)
                before = state(store, observation)
                prompt = {"task": None, "subtask": "Pick up one block." if args.task == "stack_blocks" else "Grasp the scissors.",
                          "fine": "Move the left gripper down by 1 cm."}[prompt_kind]
                prediction = policy.infer(root, observation["observation_id"], prompt=prompt,
                    noise_seed=args.noise_seed + len(report["cases"]) if args.noise_seed is not None else None if args.episode else 42)
                prediction_ref = store.put_artifact(json.dumps(prediction).encode(), "policy_prediction", "application/json", access_scope="observer")
                action = dict(schema_version="0.1", **harness.identity, type="JointPrefixAction", world_kind="observed",
                    action_id=new_id("pi05-prefix"), based_on_observation_id=observation["observation_id"],
                    actions=prediction["actions"][:prefix], timeout_s=120,
                    policy_provenance={"prediction_ref": prediction_ref, "prefix": prefix})
                started = time.monotonic()
                count_before = harness.native_action_count
                receipt = harness.execute(action)["receipt"]
                observation = harness.latest
                after = state(store, observation)
                delta = {side: [b-a for a,b in zip(grasp_center(before["arms"][side]), grasp_center(after["arms"][side]))]
                         for side in ("left", "right")}
                result = dict(prompt_kind=prompt_kind, prompt=prediction["policy_prompt"], prefix=prefix,
                    inference_seconds=prediction["inference_seconds"], execution_wall_seconds=time.monotonic()-started,
                    grasp_center_delta_m=delta, before_state=before, after_state=after, prediction_ref=prediction_ref,
                    receipt=receipt, evaluation=backend.evaluate(final_check=False))
                if not args.episode:
                    result["videos"] = backend.save_video()
                report["cases"].append(result)
                save()
                print(json.dumps({key: result[key] for key in ("prompt_kind", "prefix", "grasp_center_delta_m", "execution_wall_seconds")}), flush=True)
                if receipt["status"] not in {"completed", "stopped"}:
                    raise RuntimeError(receipt)
                if args.episode and not harness.episode_done.is_set() and harness.native_action_count <= count_before:
                    raise RuntimeError("native_policy_action_returned_without_progress_or_terminal")
            if args.episode:
                if not harness.episode_done.is_set():
                    raise RuntimeError("episode_guard_reached_without_native_terminal")
                report["episode_terminal"] = harness.episode_terminal
                report["evaluation"] = backend.evaluate()
        report["videos"] = backend.save_video()
        report["status"] = "completed"
    except Exception as error:
        report.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        if policy:
            policy.close()
        backend.close()
        save()
        store.close()


if __name__ == "__main__":
    main()
