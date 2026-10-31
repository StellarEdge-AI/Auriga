"""Prepare/verify frozen paired motion-review pilots and summarize saved evidence.

No command in this script starts a model, simulator, queue, or GPU workload.
"""

import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auriga.apps.library.scoring import load_effective_summary
from auriga.core import legacy_names as legacy

EXPERIMENT = "calibrated-motion-review-20261004"
ORDER_SEED = 2026100308
PROFILE = "rgb_proprio_calibrated_v1"
ANALYZER_VERSION = 5
CASES = (
    ("classify_objects_by_language", "standard", 2, 0, 1100, "grasp and retention"),
    ("organize_table", "standard", 0, 4, 1000, "grasp and placed-object disturbance"),
    ("pack_objects_into_box", "standard", 0, 0, 1300, "box lid/rim interference"),
    ("pack_objects_into_box_random", "random", 0, 2, 1300, "box lid/rim interference"),
    ("build_tower", "standard", 0, 2, 1050, "damage to assembled structure"),
    ("put_bottles_into_dustbin", "standard", 0, 1, 700, "approach disturbs neighboring bottles"),
)
TRACKED_TOOLS = ("grasp_plan", "grasp_memory", "robot_preview_motion", "robot_execute_plan",
                 "geometry_query", "scene_update", "scene_query", "robot_move_grasp",
                 "robot_move_ee", "robot_gripper", "robot_execute_ee")


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def analyzer_identity():
    return dict(version=ANALYZER_VERSION, script="scripts/motion_review_eval.py",
                sha256=sha(__file__), role="offline_analysis; independent of frozen model/runtime source")


def tool_attempt_counts(db):
    """Merge evidence by invocation identity, never by tool name or arguments."""
    calls = {"actor": {}, "program": {}}
    evidence = Counter()
    unidentified_events = 0

    def add(source, identifier, tool):
        tool = tool or "unknown"
        previous = calls[source].get(identifier, "unknown")
        if previous != "unknown" and tool != "unknown" and previous != tool:
            raise ValueError("conflicting_tool_attempt_evidence")
        calls[source][identifier] = previous if tool == "unknown" else tool

    for identifier, body in db.execute("SELECT id,body FROM records WHERE kind='tool_call'"):
        value = json.loads(body)
        correlation, params = value.get("correlation") or {}, value.get("params") or {}
        source = "program" if correlation.get("program_id") else "actor"
        add(source, correlation.get("tool_call_id") or params.get("callId") or identifier, params.get("tool"))
        evidence["tool_call_records"] += 1
    for identifier, body in db.execute("SELECT id,body FROM state WHERE kind='program_call'"):
        value = json.loads(body)
        # Current state records omit the tool name; the event provides it.
        add("program", identifier, value.get("tool"))
        evidence["program_call_states"] += 1
    for seq, body in db.execute("SELECT seq,body FROM events WHERE json_extract(body,'$.type') IN ('program.call_started','program.call_completed')"):
        value = json.loads(body)
        correlation, payload = value.get("correlation") or {}, value.get("payload") or {}
        identifier = correlation.get("tool_call_id")
        if not identifier:
            # Do not silently merge unrelated calls when identity evidence is absent.
            unidentified_events += 1
            identifier = "unidentified-event:" + str(seq)
        add("program", identifier, payload.get("tool"))
        evidence[value["type"]] += 1
    by_source = {source: Counter(values.values()) for source, values in calls.items()}
    return (by_source["actor"] + by_source["program"], by_source,
            dict(evidence_rows=dict(evidence), unidentified_program_events=unidentified_events,
                 unnamed_program_attempts=by_source["program"]["unknown"],
                 identity_coverage="complete" if not unidentified_events else "incomplete; unidentified events may overlap other evidence",
                 semantics="Unique actor call IDs and program operation IDs, separated by source. Program state/start/completion evidence is merged. Outer program_run and inner tool calls are distinct attempts. Counts include failed/in-flight calls and do not imply action receipts or native execution."))


def write_new(path, value):
    path = Path(path)
    with path.open("x") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")


def pair_name(spec):
    task, variant, seed, layout, *_ = spec
    base = task.removesuffix("_random") if variant == "random" else task
    return f"{base}__{variant}__g{seed}__l{layout}"


def source_files(root):
    paths = []
    for directory in ("auriga", "scripts", "tests", "configs", "third_party"):
        paths.extend(p for p in (root / directory).rglob("*")
                     if p.is_file() and "__pycache__" not in p.parts and p.suffix in (".py", ".json", ".toml", ".yaml", ".yml", ".md", ".txt"))
    for name in ("pyproject.toml", "requirements.txt", "requirements.lock.txt", "environment.yml", "uv.lock", "AGENTS.md"):
        if (root / name).is_file():
            paths.append(root / name)
    return {str(path.relative_to(root)): sha(path) for path in sorted(set(paths))}


def prepare(source_root, experiment_root, robodojo_root, config_path=None):
    source_root, experiment_root, robodojo_root = (Path(p).expanduser().resolve()
                                                 for p in (source_root, experiment_root, robodojo_root))
    for name in ("auriga/apps/gateway/cli.py", "auriga/apps/gateway/workbench.py", "auriga/robot/motion/motion_review.py"):
        if not (source_root / name).is_file():
            raise ValueError("implementation_missing:" + name)
    from scripts.benchmark_queue import CASE_MEMORY_MIB, command, queue_config
    from auriga.core.config import Config
    from auriga.cortex.session.model_proxy import DEFAULT_MAX_RETRIES, RETRY_POLICY
    defaults = Config.from_dict({})
    # Preserve the selected model, provider home and image settings; the paired
    # protocol fixes run controls and feature conditions before queue reloads.
    config = queue_config({}, path=config_path).with_overrides({
        "run": {**defaults.run.to_dict(), "benchmark": True},
        "features": {**defaults.features.to_dict(), "actor_payload_profile": "full_v1",
                     "completion_guidance": "both"},
        "backend": {"root": str(robodojo_root)},
        "queue": {"max_parallel": 1, "memory_per_case_mib": CASE_MEMORY_MIB,
                  "gpu_headroom_mib": 0, "oom_retry": {"max_retries": 0}},
    }).validate_launch()
    pairs = [CASES[0]]
    remaining = list(CASES[1:])
    rng = random.Random(ORDER_SEED)
    rng.shuffle(remaining)
    pairs.extend(remaining)
    first_conditions = ["control", "control", "review", "review", "review"]
    rng.shuffle(first_conditions)
    first_conditions.insert(0, "control")
    cases, pair_records = [], []
    for spec, first in zip(pairs, first_conditions):
        task, _, seed, layout, native_limit, mechanism = spec
        name = pair_name(spec)
        pair_records.append(dict(pair=name, task=task, seed=seed, layout_id=layout,
                                 native_step_limit=native_limit, historical_mechanism=mechanism,
                                 order=[first, "review" if first == "control" else "control"]))
        for condition in pair_records[-1]["order"]:
            case_id = condition + "--" + name
            case = dict(cwd=str(source_root), run_dir=str(experiment_root / "runs" / case_id),
                        task=task, seed=seed, layout_id=layout, title=EXPERIMENT + ": " + case_id,
                        tag=[EXPERIMENT, condition, name, "targeted-development"],
                        observation_profile=PROFILE, motion_review=condition == "review",
                        grasp_memory_dir=str(experiment_root / "memory" / case_id),
                        actor_payload_profile="full_v1", completion_guidance="both",
                        pi05=False, enable_wait=True, tracking_guard=False, joint_replan=False)
            command(case, 0, config=config)  # Pure contract validation; does not launch.
            if Path(case["grasp_memory_dir"]).exists():
                raise ValueError("evaluation_memory_must_start_fresh:" + case["grasp_memory_dir"])
            cases.append(case)
    native_files = {}
    for task, _, seed, layout, *_ in CASES:
        for relative in (f"task/RoboDojo/tasks/{task}.py", f"Assets/Eval_Layout/RoboDojo/arx_x5/{seed}/{task}_{layout}.json"):
            native_files[relative] = sha(robodojo_root / relative)
    protocol = dict(schema_version=1, experiment=EXPERIMENT, created_at=now(),
        status="prepared_not_launched", auriga_config=config.to_dict(), source_root=str(source_root), source_sha256=source_files(source_root),
        robodojo_root=str(robodojo_root), native_input_sha256=native_files,
        source_snapshot_limit="Hashes bind supplied source; use an isolated checkout and verify before each phase. Shared USD/textures/runtime dependencies are not fully snapshotted.",
        comparison="calibrated control versus same calibration plus grasp reasoning/memory and motion preview/execution bundle",
        interpretation="Six selected development pairs, one episode per condition/input; no general benchmark or population-effect claim. This does not isolate calibration or individual bundle components.",
        model=dict(model=config.model.name, effort=config.model.effort, codex_home=config.model.codex_home,
                   helper=config.features.visual_review_model, observation_profile=PROFILE,
                   completed_call_limit=170, attempt_timeout_s=None, max_retries=DEFAULT_MAX_RETRIES,
                   retry_policy=RETRY_POLICY,
                   completion_guidance="both", actor_payload_profile="full_v1", enable_wait=True),
        order=dict(seed=ORDER_SEED, method="Language g2 pilot fixed control then review; remaining five blocks shuffled, first condition balanced overall 3/3",
                   actual_admission="Queue events determine actual admission and overlaps"),
        memory="Every case starts with a distinct empty configured memory directory; no cross-case or historical lessons. Cross-episode learning is not evaluated here.",
        execution=dict(pilot_pairs=1, remaining_pairs=5, max_parallel=1, memory_per_case_mib=CASE_MEMORY_MIB,
                       gpu_headroom_mib=0, native_budgets="unchanged official per-task limits",
                       oom_retry=dict(max_retries=0),
                       model_auth="Selected provider configuration/auth in frozen model.codex_home; never record credentials",
                       startup_timeout_s=900, no_native_progress_timeout_s=1500,
                       progress_clock="wall_time_excluding_pending_model_requests", whole_episode_wall_budget_s=None),
        pilot_gate="Before remaining phase, review actual profile/tools/model/budgets, calibration geometry, stable preview/execute binding, and fresh memory paths. Fix implementation failures only in a newly identified experiment; do not silently mix source revisions.",
        scoring="Primary: native terminal results and confirmed task-limit failures with valid native process scores. Timeout, model-call budget, development time budget and five consecutive actor inability answers are task failures; retain their current process score and exact failure reason. Native terminal results win supervisor races. Infrastructure, unconfirmed stops and invalid evidence remain unknown/unscored. Retain all planned episodes, distinguish score availability from task outcome, and preserve historical source evidence with verified policy reconciliation. No replacement of failed episodes.",
        mechanisms="Count recorded tool attempts, saved plans, plan statuses, action failures/IK reasons, native rows, model calls and token coverage. Visual empty lifts, carried-object drops and object disturbance require observation/action-linked review; tool counts do not infer them.",
        efficiency="Compare tokens at matched success only for both-success valid pairs with complete usage coverage. Report failures and unknown token units separately.",
        pairs=pair_records)
    manifest = dict(schema_version=1, experiment=EXPERIMENT, protocol_file="protocol.json",
                    max_parallel=1, memory_per_case_mib=CASE_MEMORY_MIB, gpu_headroom_mib=0, oom_retry=dict(max_retries=0),
                    auriga_config=config.to_dict(), cases=cases)
    experiment_root.mkdir(parents=True, exist_ok=True)
    outputs = [experiment_root / name for name in ("protocol.json", "manifest.json", "pilot-manifest.json", "remaining-manifest.json", "freeze.json")]
    if any(path.exists() for path in outputs):
        raise FileExistsError("prepared_evidence_already_exists")
    write_new(outputs[0], protocol)
    write_new(outputs[1], manifest)
    write_new(outputs[2], {**manifest, "phase": "pilot", "cases": cases[:2]})
    write_new(outputs[3], {**manifest, "phase": "remaining", "cases": cases[2:]})
    write_new(outputs[4], dict(created_at=now(), files={path.name: sha(path) for path in outputs[:4]}))
    return dict(experiment_root=str(experiment_root), prepared_cases=len(cases), pilot_cases=2,
                protocol_sha256=sha(outputs[0]), manifest_sha256=sha(outputs[1]), launched=False)


def verify(experiment_root):
    root = Path(experiment_root).expanduser().resolve()
    freeze = json.loads((root / "freeze.json").read_text())
    differences = []
    for relative, expected in freeze["files"].items():
        path = root / relative
        if not path.is_file() or sha(path) != expected:
            differences.append("prepared_file:" + relative)
    protocol = json.loads((root / "protocol.json").read_text())
    actual_source = source_files(Path(protocol["source_root"]))
    for relative in set(actual_source) | set(protocol["source_sha256"]):
        if actual_source.get(relative) != protocol["source_sha256"].get(relative):
            differences.append("source:" + relative)
    for relative, expected in protocol["native_input_sha256"].items():
        path = Path(protocol["robodojo_root"]) / relative
        if not path.is_file() or sha(path) != expected:
            differences.append("native:" + relative)
    return dict(observed_at=now(), valid=not differences, differences=sorted(differences),
                limitation=protocol["source_snapshot_limit"])


def read_json(path, errors):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as error:
        errors.append(dict(source=str(path), error=type(error).__name__))
        return {}


def token_report(root, manifest, summary):
    """Use the shared accounting arithmetic without importing gateway/runtime."""
    from auriga.cortex.session.usage import account_usage, account_responses_usage, mixed_accounting
    starts, finishes, malformed = {}, {}, []
    files = sorted((root / "private").glob("model-requests*.jsonl"))
    for path in files:
        for number, line in enumerate(path.read_text().splitlines(), 1):
            try:
                event = json.loads(line)
            except ValueError:
                malformed.append(f"{path.name}:{number}")
                continue
            if not event.get("logical_call_id") or not event.get("attempt_index"):
                continue
            key = f"{path.name}:{event['logical_call_id']}:{event['attempt_index']}"
            target = starts if event.get("kind") == "model_request_started" else finishes if event.get("kind") == "model_request_finished" else None
            if target is not None:
                if key in target and target[key] != event:
                    raise ValueError("conflicting_attempt_records")
                target[key] = event
    entries = []
    if starts or finishes:
        coverage = "proxy_attempt_ledger; cumulative_snapshot_excluded"
        for key in sorted(starts.keys() | finishes.keys()):
            event = finishes.get(key, starts.get(key))
            usage = event.get("usage") if key in finishes else None
            source = event.get("source", "unknown")
            if "compact" in event.get("endpoint", ""):
                source = "compaction"
            entries.append(dict(accounting_id=key, source=source,
                                accounting=account_responses_usage(usage,
                                    model=event.get("served_model") or event.get("requested_model"),
                                    provider=manifest.get("provider"))))
    else:
        coverage = "cumulative_snapshot_only; per_attempt_coverage_unknown"
        entries.append(dict(accounting_id="cumulative", source="actor",
                            accounting=account_usage(summary.get("token_usage"),
                                model=manifest.get("model"), provider=manifest.get("provider"))))
        if files or (summary.get("model_requests") or {}).get("http_attempt_count"):
            entries.append(dict(accounting_id="unknown_attempt_coverage", source="unknown_attempt_usage",
                                accounting=account_usage(None)))
    for key in malformed:
        entries.append(dict(accounting_id="malformed:" + key, source="unreadable_attempt_evidence",
                            accounting=account_usage(None)))
    return dict(coverage=coverage, accounting=mixed_accounting(entries),
                malformed_lines=malformed, attempt_ledger_files=[p.name for p in files])


def summarize_case(case, *, expected_model=None):
    root = Path(case["run_dir"])
    errors = []
    manifest, summary = (read_json(root / name, errors) for name in ("manifest.json", "summary.json"))
    summary = load_effective_summary(root, summary=summary)
    tools = receipts = record_kinds = preview_statuses = motion_events = None
    tools_by_source = tool_count_evidence = None
    ik_failures, ik_unresolved, ik_preview_failures = None, None, None
    evidence_cursor = None
    database = root / "events.sqlite"
    if database.is_file():
        try:
            uri = database.as_uri() + "?mode=ro" + ("&immutable=1" if not Path(str(database) + "-wal").exists() else "")
            with closing(sqlite3.connect(uri, uri=True)) as db:
                db.execute("PRAGMA query_only=ON")
                db.execute("BEGIN")
                evidence_cursor = db.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
                tools, receipts, record_kinds, preview_statuses, motion_events = Counter(), Counter(), Counter(), Counter(), Counter()
                tools, tools_by_source, tool_count_evidence = tool_attempt_counts(db)
                ik_failures, ik_unresolved, ik_preview_failures = 0, 0, 0
                for kind, body in db.execute("SELECT kind,body FROM records"):
                    value = json.loads(body)
                    record_kinds[kind] += 1
                    if kind == "action_receipt":
                        status = value.get("status", "unknown")
                        receipts[status] += 1
                        if status != "completed":
                            reason = value.get("reason") or value.get("error")
                            if value.get("planner_failure_stage") == "inverse_kinematics":
                                ik_failures += 1
                            elif reason is None:
                                ik_unresolved += 1
                            elif any(token in json.dumps(reason).lower() for token in ("ik_", "_ik", "inverse_kinematic")):
                                ik_failures += 1
                    if kind in ("motion_plan", "motion_preview", "preview_plan"):
                        preview_statuses[(value.get("review") or {}).get("status", value.get("status", "unknown"))] += 1
                for (body,) in db.execute("SELECT body FROM events WHERE json_extract(body,'$.type') LIKE 'motion.%'"):
                    event = json.loads(body)
                    motion_events[event["type"]] += 1
                    if event["type"] == "motion.preview_failed":
                        payload = event.get("payload") or {}
                        preview_statuses["planning_failed:" + str(payload.get("status", "unknown"))] += 1
                        if payload.get("planner_failure_stage") == "inverse_kinematics":
                            ik_preview_failures += 1
        except (sqlite3.Error, ValueError, TypeError) as error:
            errors.append(dict(source=str(database), error=type(error).__name__))
            tools = receipts = record_kinds = preview_statuses = motion_events = None
            tools_by_source = tool_count_evidence = None
            ik_failures = ik_unresolved = ik_preview_failures = None
    else:
        errors.append(dict(source=str(database), error="missing"))
    accounting = None
    try:
        accounting = token_report(root, manifest, summary)
    except (OSError, ValueError, KeyError, sqlite3.Error) as error:
        errors.append(dict(source="mixed_usage", error=type(error).__name__))
    # Standalone old callers retain the original paired protocol. Full reports
    # always pass expectations from their saved configuration, never live defaults.
    expected_model = expected_model if expected_model is not None else dict(name="gpt-6-astra", effort="high")
    model_name = expected_model.get("name", expected_model.get("model"))
    checks = {key: (manifest.get(key) == expected if key in manifest else None)
              for key, expected in dict(model=model_name, effort=expected_model.get("effort"), observation_profile=PROFILE,
                                       motion_review=case["motion_review"], actor_payload_profile="full_v1",
                                       enable_wait=case["enable_wait"], model_request_limit=170).items()}
    guidance = manifest.get("completion_guidance")
    checks["completion_guidance"] = ((guidance.get("mode") if isinstance(guidance, dict) else guidance) == "both"
                                     if guidance is not None else None)
    if case["motion_review"]:
        checks["grasp_memory_dir"] = (manifest["grasp_memory_dir"] == case["grasp_memory_dir"]
                                     if "grasp_memory_dir" in manifest else None)
    native = summary.get("native_episode")
    expected_limit = next((item[4] for item in CASES if item[0] == case["task"]), None)
    checks["native_step_limit"] = (native.get("native_step_limit") == expected_limit
                                    if isinstance(native, dict) and "native_step_limit" in native else None)
    condition = "review" if case["motion_review"] else "control"
    return dict(run_dir=str(root), condition=condition, pair=Path(case["run_dir"]).name.split("--", 1)[-1],
        expected=dict(task=case["task"], seed=case["seed"], layout_id=case["layout_id"], grasp_memory_dir=case["grasp_memory_dir"],
                      model=model_name, effort=expected_model.get("effort")),
        observed_at=now(), analyzer=analyzer_identity(), configuration_checks=checks, status=summary.get("status"),
        valid_episode=summary.get("valid_episode"), scored_episode=summary.get("scored_episode"),
        primary_outcome=summary.get("primary_outcome"), task_success=summary.get("task_success"),
        normalized_score=summary.get("normalized_score"), native_process_score=summary.get("native_process_score"),
        partial_native_result=summary.get("partial_native_result"), termination_authority=summary.get("termination_authority"),
        actor_stall=summary.get("actor_stall"),
        task_outcome=summary.get("task_outcome"), score_status=summary.get("score_status"),
        failure_reason=summary.get("failure_reason"), scoring_policy=summary.get("scoring_policy"),
        scoring_reconciliation=summary.get("scoring_reconciliation"), native_evaluation_snapshot=summary.get("native_evaluation_snapshot"),
        native_episode=native, model_requests=summary.get("model_requests"),
        action_count=summary.get("action_count"), elapsed_wall_s=summary.get("elapsed_wall_s"),
        termination_reason=summary.get("termination_reason"), infrastructure_error=summary.get("infrastructure_error"),
        tools={name: tools[name] for name in TRACKED_TOOLS} if tools is not None else None,
        all_tool_attempts=dict(tools) if tools is not None else None,
        tool_attempts_by_source={source: dict(counts) for source, counts in tools_by_source.items()} if tools_by_source is not None else None,
        tool_count_evidence=tool_count_evidence,
        record_counts=dict(record_kinds) if record_kinds is not None else None,
        receipt_statuses=dict(receipts) if receipts is not None else None,
        preview_statuses=dict(preview_statuses) if preview_statuses is not None else None,
        motion_event_counts=dict(motion_events) if motion_events is not None else None,
        explicit_ik_failure_receipts=ik_failures, failed_receipts_missing_reason=ik_unresolved,
        explicit_ik_failure_previews=ik_preview_failures,
        ik_count_limit="Only explicit planner_failure_stage=inverse_kinematics or receipt IK error labels; no inference from tool calls or generic planner failures.",
        visual_mechanisms=dict(empty_lifts=None, carried_object_drops=None, object_disturbances=None,
                               reason="requires_observation_linked_visual_review"),
        token_usage=accounting, evidence_cursor=evidence_cursor, evidence_errors=errors,
        source_sha256={name: sha(root / name) if (root / name).is_file() else None for name in ("manifest.json", "summary.json")})


def summarize(experiment_root):
    root = Path(experiment_root).expanduser().resolve()
    manifest = json.loads((root / "manifest.json").read_text())
    protocol = json.loads((root / "protocol.json").read_text())
    expected_model = protocol.get("auriga_config", protocol.get(legacy.MANIFEST_CONFIG_KEY, {})).get("model", protocol.get("model"))
    runs = [summarize_case(case, expected_model=expected_model) for case in manifest["cases"]]
    pairs = []
    for item in protocol["pairs"]:
        values = {row["condition"]: row for row in runs if row["pair"] == item["pair"]}
        valid = all(values[name]["scored_episode"] is True and values[name]["valid_episode"] is True
                    and isinstance(values[name]["task_success"], bool) for name in ("control", "review"))
        outcome = "unresolved"
        if valid:
            a, b = (values[name]["task_success"] for name in ("control", "review"))
            outcome = "both_success" if a and b else "both_failure" if not a and not b else "review_win" if b else "control_win"
        pairs.append(dict(pair=item["pair"], outcome=outcome, native_step_limit=item["native_step_limit"]))
    outcomes = {}
    for condition in ("control", "review"):
        selected = [row for row in runs if row["condition"] == condition]
        scored = [row for row in selected if row["valid_episode"] is True and row["scored_episode"] is True and isinstance(row["task_success"], bool)]
        outcomes[condition] = dict(planned=len(selected), valid_scored=len(scored),
                                   successes=sum(row["task_success"] for row in scored),
                                   unscored_or_unknown=len(selected) - len(scored))
    return dict(observed_at=now(), analyzer=analyzer_identity(),
                frozen_runtime_source=dict(root=protocol["source_root"], analyzer_sha256=protocol["source_sha256"].get("scripts/motion_review_eval.py")),
                scope=protocol["interpretation"], outcomes=outcomes, pairs=pairs, runs=runs,
                freeze_verification=verify(root), conclusion="Mechanism-selected development evidence; n=6 pairs cannot establish a general performance effect.")


def resources():
    from scripts.benchmark_queue import sample_gpus
    variables = ("CODEX_HOME", "AURIGA_EXPERIMENT_CONFIG", "AURIGA_ROBODOJO_ROOT", "AURIGA_ROBODOJO_PYTHON", "AURIGA_PRICE_CARD")
    paths = {name: dict(path=os.environ.get(name), exists=Path(os.environ[name]).expanduser().exists() if name in os.environ else None)
             for name in variables}
    return dict(observed_at=now(), gpu=sample_gpus(), configured_paths=paths,
                note="Read-only snapshot; no GPU reservation or model/auth request. Credentials were not read.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prep = subparsers.add_parser("prepare")
    prep.add_argument("--source-root", required=True, type=Path)
    prep.add_argument("--experiment-root", required=True, type=Path)
    prep.add_argument("--robodojo-root", required=True, type=Path)
    prep.add_argument("--config", help="Unified model/provider settings; paired protocol overrides run controls and features")
    for name in ("verify", "summarize"):
        command = subparsers.add_parser(name)
        command.add_argument("--experiment-root", required=True, type=Path)
        command.add_argument("--output", type=Path)
    subparsers.add_parser("resources")
    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare(args.source_root, args.experiment_root, args.robodojo_root, args.config)
    elif args.command == "resources":
        result = resources()
    else:
        result = (verify if args.command == "verify" else summarize)(args.experiment_root)
        if args.output:
            write_new(args.output, result)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if args.command == "verify" and not result["valid"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
