"""Run the official RoboDojo OpenPI checkpoint in its isolated environment."""

import argparse
from contextlib import redirect_stdout
import hashlib
import json
from pathlib import Path
import sys
import time
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from auriga.runtime.policy.action_policy import (CHECKPOINT_REVISION, NORM_SHA256, TRAIN_CONFIG,
                                    load_policy_observation, validate_joint_chunk)
from auriga.core.presentation.actor_presentation import strip_hashes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--stdio", action="store_true")
    parser.add_argument("--run-dir")
    parser.add_argument("--observation-id")
    parser.add_argument("--output")
    parser.add_argument("--prompt", help="Explicit language-granularity experiment; public instruction stays in evidence")
    parser.add_argument("--noise-seed", type=int, help="Use identical diffusion noise for paired prompt comparisons")
    args = parser.parse_args()
    if not args.stdio and not all((args.run_dir, args.observation_id, args.output)):
        parser.error("one-shot mode requires --run-dir, --observation-id and --output")
    checkpoint = Path(args.checkpoint).resolve()
    manifest_path = Path(__file__).with_name("pi05_arx_manifest.json")
    manifest = json.loads(manifest_path.read_text())
    if manifest["revision"] != CHECKPOINT_REVISION or manifest["train_config"] != TRAIN_CONFIG:
        raise ValueError("policy_checkpoint_manifest_identity_mismatch")
    for record in manifest["files"]:
        file = checkpoint / record["path"]
        if file.stat().st_size != record["size_bytes"]:
            raise ValueError(f"checkpoint_size_mismatch: {record['path']}")
        with file.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != record["sha256"]:
                raise ValueError(f"checkpoint_hash_mismatch: {record['path']}")
    stats = checkpoint / "assets" / "arx_x5_sim" / "norm_stats.json"
    if hashlib.sha256(stats.read_bytes()).hexdigest() != NORM_SHA256:
        raise ValueError("ARX X5 normalization statistics do not match the selected official checkpoint")
    with redirect_stdout(sys.stderr):
        from openpi.policies import policy_config
        from openpi.training import config
        started = time.perf_counter()
        policy = policy_config.create_trained_policy(config.get_config(TRAIN_CONFIG), checkpoint)
        load_seconds = time.perf_counter() - started
    metadata = dict(checkpoint=str(checkpoint), checkpoint_revision=CHECKPOINT_REVISION,
        checkpoint_repo_id=manifest["repo_id"], robot_model_id="arx-x5",
        checkpoint_files_verified=len(manifest["files"]),
        checkpoint_manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        train_config=TRAIN_CONFIG, normalization_sha256=NORM_SHA256,
        action_semantics="absolute_joint_rad_and_normalized_gripper_left6_1_right6_1",
        preprocessing="official OpenPI resize-with-pad 224x224, quantile norm, discrete state tokenizer",
        execution_authority="Harness_only", load_seconds=load_seconds)
    if args.stdio:
        print(json.dumps(dict(status="ready", **metadata)), flush=True)
        requests = (json.loads(line) for line in sys.stdin)
    else:
        requests = [dict(run_dir=args.run_dir, observation_id=args.observation_id,
                         prompt=args.prompt, noise_seed=args.noise_seed)]
    for request in requests:
        try:
            with redirect_stdout(sys.stderr):
                obs, evidence = load_policy_observation(request["run_dir"], request["observation_id"])
                if request.get("prompt") is not None:
                    if not isinstance(request["prompt"], str) or not request["prompt"].strip():
                        raise ValueError("policy_prompt_must_be_nonempty_text")
                    obs["prompt"] = request["prompt"]
                obs["prompt"] = strip_hashes(obs["prompt"])
                noise = None
                if request.get("noise_seed") is not None:
                    import numpy as np
                    noise = np.random.default_rng(request["noise_seed"]).standard_normal((50, 32)).astype(np.float32)
                started = time.perf_counter()
                result = policy.infer(obs, noise=noise)
                actions = validate_joint_chunk(result["actions"].tolist())
                elapsed = time.perf_counter() - started
            response = dict(status="inference_completed", **metadata, **evidence, actions=actions,
                            action_shape=[50, 14], inference_seconds=elapsed, executed_steps=0,
                            policy_prompt=obs["prompt"], prompt_source="override" if request.get("prompt") else "public_task",
                            noise_seed=request.get("noise_seed"))
        except Exception as error:
            traceback.print_exc(file=sys.stderr)
            response = dict(status="error", error=str(error))
        if args.stdio:
            print(json.dumps(response, allow_nan=False), flush=True)
        else:
            with Path(args.output).open("x") as output:
                json.dump(response, output, indent=2, allow_nan=False)
            print(json.dumps({key: value for key, value in response.items() if key not in {"actions", "images"}}))
            if response["status"] == "error":
                raise RuntimeError(response["error"])


if __name__ == "__main__":
    main()
