"""Run one adapted pinned RoboProbe RGB API episode; this starts a real worker and model."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from auriga.apps.reference.reference_api import MAX_MODEL_CALLS, ResponsesClient, run_episode, upstream
from auriga.core.config import Config
from auriga.robot.backends.robodojo.robodojo import RobotBackend
from auriga.apps.library.experiments import allocate_run, register_run
from auriga.apps.evaluation.recording import ReferenceRecorder


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Unified experiment configuration")
    parser.add_argument("--model", help="Explicit model override")
    parser.add_argument("--effort", help="Explicit reasoning effort override")
    parser.add_argument("--codex-home", help="Explicit provider configuration/auth home")
    parser.add_argument("--print-config", action="store_true", help="Print resolved settings without reading credentials or starting workers")
    parser.add_argument("--run-dir")
    parser.add_argument("--experiment-root")
    parser.add_argument("--category", default="reference")
    parser.add_argument("--title")
    parser.add_argument("--tag", action="append", default=[])
    parser.add_argument("--gpu", type=int, default=4)
    parser.add_argument("--task", default="stack_bowls")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--layout-id", type=int, default=0)
    parser.add_argument("--max-calls", type=int, default=MAX_MODEL_CALLS,
                        help="logical complete() returns including model truncation; HTTP retries do not consume this limit")
    parser.add_argument("--action-timeout-s", type=float, default=180)
    args = parser.parse_args(argv)
    model_overrides = {key: value for key, value in
                       (("name", args.model), ("effort", args.effort), ("codex_home", args.codex_home))
                       if value is not None}
    config = Config.load(args.config, overrides={"model": model_overrides})
    if args.print_config:
        print(json.dumps(config.to_dict(), indent=2, ensure_ascii=False))
        return
    if not 1 <= args.max_calls <= MAX_MODEL_CALLS or not 0.1 <= args.action_timeout_s <= 300:
        parser.error("model call limit must be 1–170; action timeout must be 0.1–300 seconds")
    if args.task.removesuffix("_random") == "imitate_sorting_sequence":
        parser.error("reference adapter does not support the upstream timed demonstration-watch action chunks")
    directory = Path(args.run_dir).expanduser().resolve() if args.run_dir else allocate_run(
        args.experiment_root, category=args.category, task=args.task, title=args.title, tags=args.tag, kind="reference")
    if directory.exists() and any(path.name != "experiment.json" for path in directory.iterdir()):
        parser.error("choose a fresh run directory")
    register_run(directory, category=args.category, task=args.task, title=args.title, tags=args.tag, kind="reference")
    upstream()  # Verify pinned source/dependencies before native startup.
    recorder = ReferenceRecorder(directory)
    backend = None
    try:
        client = ResponsesClient.from_codex_config(directory / "api-calls", config=config)
        backend = RobotBackend(gpu_id=args.gpu, task=args.task, seed=args.seed, layout_id=args.layout_id,
                               run_dir=directory / "robot", observation_profile="rgb_proprio_v1", config=config)
        backend.validate_reset(args.seed, args.layout_id)
        report = run_episode(backend, client, directory, max_calls=args.max_calls,
                             action_timeout_s=args.action_timeout_s, recorder=recorder)
        recorder.finish(report)
        print(json.dumps({"report": str(directory / "reference-episode.json"), "loop_verified": report["loop_verified"],
                          "episode_verified": report["episode_verified"],
                          "termination_reason": report["termination_reason"], "llm_calls": report.get("llm_calls"),
                          "http_attempt_count": report.get("http_attempt_count"),
                          "action_count": report.get("action_count"), "task_success": report.get("evaluation", {}).get("task_success"),
                          "normalized_score": report.get("evaluation", {}).get("normalized_score")}, ensure_ascii=False))
        if not report["episode_verified"]:
            raise SystemExit(1)
    finally:
        try:
            if backend: backend.close()
        finally:
            recorder.close()


if __name__ == "__main__":
    main()
