"""Replay an archived Astra handoff, execute one bounded pi0.5 prefix, then pause.

Observer diagnostics only: archived targets never become actor tool defaults.
"""

import argparse
from contextlib import closing
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import time
import traceback

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from auriga.runtime.policy.action_policy import OpenPIProcessPolicy
from auriga.core.config import Config
from auriga.core.contracts import new_id
from auriga.apps.library.experiments import register_run
from auriga.runtime.harness import Harness
from auriga.core.observation_policy import ACTOR_PROFILE
from auriga.robot.backends.robodojo.robodojo import RobotBackend
from auriga.core.storage.store import Store
from scripts.benchmark_queue import reserve_gpu, sample_gpus


def state(store, observation):
    return json.loads(store.resolve_artifact(observation["robot_state_ref"], ("actor",))[1])


def center(arm):
    pose = arm["ee_pose"]
    x, y, z, w = pose["quaternion_xyzw"]
    axis = np.array([1 - 2*(y*y + z*z), 2*(x*y + z*w), 2*(x*z - y*w)])
    return np.asarray(pose["position_m"]) + .1501 * axis


def changes(before, after):
    result = {}
    for side in ("left", "right"):
        a, b = before["arms"][side], after["arms"][side]
        delta = center(b) - center(a)
        result[side] = dict(q_delta_rad=(np.asarray(b["q_rad"]) - a["q_rad"]).tolist(),
            max_abs_q_delta_rad=float(np.max(np.abs(np.asarray(b["q_rad"]) - a["q_rad"]))),
            link6_delta_m=(np.asarray(b["ee_pose"]["position_m"]) - a["ee_pose"]["position_m"]).tolist(),
            grasp_center_delta_m=delta.tolist(), grasp_center_distance_m=float(np.linalg.norm(delta)),
            gripper_before=a["gripper"], gripper_after=b["gripper"])
    return result


def rgb(store, observation, camera_id):
    camera = next(c for c in observation["cameras"] if c["camera_id"] == camera_id)
    _, raw = store.resolve_artifact(camera["rgb_ref"], ("actor",))
    image = Image.open(io.BytesIO(raw)); image.load()
    return image


class FixedNoisePolicy:
    """Keep diffusion noise paired while using the existing Harness policy tool."""
    def __init__(self, policy, seed): self.policy, self.seed = policy, seed
    def infer(self, *args, **kwargs):
        return self.policy.infer(*args, **kwargs, noise_seed=self.seed)


def main():
    policy = Config.load().policy
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--through-action", required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--intent", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--prompt-kind", choices=("task", "atomic", "coordinates"), required=True)
    parser.add_argument("--prefix", type=int, choices=range(1, 26), default=5)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--layout-id", type=int, default=0)
    parser.add_argument("--noise-seed", type=int, default=42)
    parser.add_argument("--comparison-prompts", type=Path, help="JSON mapping of task/atomic/coordinates texts for paired read-only inference")
    parser.add_argument("--gpus", type=int, nargs="+", default=[4, 7])
    parser.add_argument("--policy-python", default=policy.python, help="OpenPI interpreter; defaults to the unified config policy.python")
    parser.add_argument("--checkpoint", default=policy.checkpoint, help="pi0.5 checkpoint; defaults to the unified config policy.checkpoint")
    args = parser.parse_args()
    root, source = args.run_dir.resolve(), args.source_run.resolve()
    if root.exists() and any(root.iterdir()): parser.error("choose a fresh run directory")
    metadata = json.loads((source / "experiment.json").read_text())
    register_run(root, category="validation", task=metadata["task"], title="Astra to pi0.5 atomic handoff", kind="validation")
    report = dict(status="waiting_gpu", observation_profile=ACTOR_PROFILE, motion_review=False,
        execution_role="fixed_history_replay_and_one_policy_prefix_without_model_review", scope="observer-only one-prefix atomic diagnostic; not a full task episode",
        source_run=str(source), through_action=args.through_action, task=metadata["task"], intent=args.intent,
        prompt_kind=args.prompt_kind, prompt=args.prompt, prefix=args.prefix, noise_seed=args.noise_seed,
        seed=args.seed, layout_id=args.layout_id, replay=[], admission_attempts=[],
        diagnostic_source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    def save(): (root / "atomic-report.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    reservation = policy = backend = store = None
    try:
        # Use the same cross-queue lease as production. Inference plus simulator
        # receive a 48 GiB reservation, including their startup peaks.
        for _ in range(120):
            baseline = sample_gpus()
            for gpu in sorted([g for g in baseline["gpus"] if g["index"] in args.gpus], key=lambda g: -g["free_mib"]):
                reservation, admission = reserve_gpu(gpu, memory_mib=49152, headroom_mib=4096, baseline=baseline)
                report["admission_attempts"].append(admission)
                if reservation:
                    reservation.bind(os.getpid()); report.update(gpu=gpu, lease_id=reservation.metadata["id"])
                    break
            if reservation: break
            save(); time.sleep(15)
        if not reservation: raise RuntimeError("gpu_admission_timeout")
        print("ADMITTED", gpu["index"], flush=True)
        with closing(Store(source, {}, read_only=True)) as old:
            requests = [e["payload"]["request"] for e in old.events(limit=100000) if e["type"] == "action.requested"]
            stop = next(i for i, a in enumerate(requests) if a["action_id"] == args.through_action)
            requests = requests[:stop+1]
            if any(a.get("type") == "JointPrefixAction" for a in requests):
                raise ValueError("handoff_source_must_be_Astra_EE_or_gripper_actions")
            old_receipt = old.read("action_receipt", args.through_action)
            old_obs = old.read("observation", old_receipt["result_observation_id"])
            old_state = state(old, old_obs)
            report["source_handoff"] = dict(observation=old_obs, robot_state=old_state, receipt=old_receipt)
            store = Store(root, dict(run_id=root.name, episode_id=new_id("ep"), env_instance_id=new_id("atomic-env"), env_generation=1))
            backend = RobotBackend(gpu_id=gpu["index"], task=metadata["task"], seed=args.seed,
                                   layout_id=args.layout_id, run_dir=root / "private" / "robot", observation_profile=ACTOR_PROFILE)
            harness = Harness(store, backend, observation_profile=ACTOR_PROFILE, motion_review=False)
            observation = harness.start()
            report.update(status="replaying", capabilities=harness.capabilities); save()
            for original in requests:
                archived_receipt = old.read("action_receipt", original["action_id"])
                action = deepcopy(original)
                action.update(harness.identity, action_id=new_id("astra-replay"), based_on_observation_id=harness.latest["observation_id"])
                receipt = harness.execute(action)["receipt"]
                report["replay"].append(dict(source_action_id=original["action_id"], source_status=archived_receipt["status"],
                    source_native_steps=archived_receipt.get("executed_native_steps"), request=action, receipt=receipt))
                save()
                if receipt["status"] not in ("completed", "failed") or harness.episode_done.is_set():
                    raise RuntimeError("replay_did_not_reach_a_live_handoff")
            before_obs = harness.latest
            before = state(store, before_obs)
            reproducibility = changes(old_state, before)
            report["handoff"] = dict(observation=before_obs, robot_state=before, source_difference=reproducibility,
                                     image_difference={})
            for camera in before_obs["cameras"]:
                name = camera["camera_id"]
                a, b = np.asarray(rgb(old, old_obs, name)).astype(float), np.asarray(rgb(store, before_obs, name)).astype(float)
                report["handoff"]["image_difference"][name] = dict(mean_absolute_pixel_difference=float(np.mean(np.abs(a-b))),
                    identical_pixels=bool(np.array_equal(a,b)))
            # Avoid comparing a policy on a substantially different robot state.
            if any(x["grasp_center_distance_m"] > .005 or x["max_abs_q_delta_rad"] > .03
                   or abs(x["gripper_before"]["command_normalized"] - x["gripper_after"]["command_normalized"]) > .03
                   for x in reproducibility.values()):
                raise RuntimeError("handoff_replay_outside_5mm_0.03rad_0.03gripper_tolerance")
            report["status"] = "handoff_ready"; save()
            policy = OpenPIProcessPolicy(args.policy_python, args.checkpoint, gpu=gpu["index"], log_path=root / "private" / "policy.log")
            if args.comparison_prompts:
                comparisons = json.loads(args.comparison_prompts.read_text())
                if set(comparisons) != {"task", "atomic", "coordinates"} or any(
                        not isinstance(text, str) or not 1 <= len(text) <= 2000 for text in comparisons.values()):
                    raise ValueError("comparison_prompts_require_three_nonempty_texts")
                report["same_observation_predictions"] = {}
                for kind, prompt in comparisons.items():
                    prediction = policy.infer(root, before_obs["observation_id"], prompt=prompt, noise_seed=args.noise_seed)
                    report["same_observation_predictions"][kind] = store.put_artifact(
                        json.dumps(prediction).encode(), "policy_prediction", "application/json", "observer")
                save()
            harness.action_policy = FixedNoisePolicy(policy, args.noise_seed)
            result = harness.policy_step(dict(action_id=new_id("atomic-prefix"), observation_id=before_obs["observation_id"],
                                             prefix=args.prefix, prompt=args.prompt, timeout_s=120), {})
            receipt = result["receipt"]
            after_obs = harness.latest
            after = state(store, after_obs)
            # No second policy call and no actor continuation: freeze and return
            # these observations for Astra to make the next global decision.
            paused = backend.observe()
            if paused["sim_step"] != after["sim_step"]: raise RuntimeError("physics_advanced_after_prefix_return")
            report.update(status="completed", policy_result=result, after_observation=after_obs, after_state=after,
                          motion=changes(before, after), return_to_astra=dict(clock="paused", policy_calls=1,
                          sim_step=after["sim_step"], next_actor_action="not_run_in_observer_diagnostic"), videos=backend.save_video())
            sheet = Image.new("RGB", (960, 3*266), "white"); draw = ImageDraw.Draw(sheet)
            for row, (label, owner, obs) in enumerate((("archived Astra handoff", old, old_obs),
                    ("replayed Astra handoff", store, before_obs), ("after one pi0.5 prefix", store, after_obs))):
                for col, camera in enumerate(obs["cameras"]):
                    image = rgb(owner, obs, camera["camera_id"]).resize((320,240))
                    sheet.paste(image, (320*col,266*row+26)); draw.text((320*col+3,266*row+5), label+" "+camera["camera_id"], fill="black")
            sheet.save(root / "handoff-comparison.jpg")
            report["contact_sheet"] = str(root / "handoff-comparison.jpg")
            print(json.dumps(dict(status=receipt["status"], prefix=args.prefix, motion=report["motion"])), flush=True)
    except Exception as error:
        report.update(status="failed", error=f"{type(error).__name__}: {error}", traceback=traceback.format_exc())
        raise
    finally:
        if policy: policy.close()
        if backend: backend.close()
        if store: store.close()
        if reservation: reservation.close()
        report["resources_released"] = True
        save()


if __name__ == "__main__": main()
