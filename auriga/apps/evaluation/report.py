"""Summarize recorded pilots without starting a robot or model."""
import argparse
from collections import Counter
import json
from pathlib import Path
import sqlite3
import tomllib

from auriga.apps.library.scoring import load_effective_summary
from auriga.core.storage.store import open_read_store
from auriga.cortex.session.usage import account_usage, aggregate_accounting, load_price_card


def _recovery_delivery(root, manifest, store):
    """Separate configured guidance, recorded attempts and exact native submissions."""
    from auriga.core.presentation.actor_presentation import ActorPresentation

    guidance = manifest.get("completion_guidance")
    configured = guidance.get("recovery_guidance") if isinstance(guidance, dict) else None
    version = configured.get("version") if isinstance(configured, dict) else None
    if version is not None and (not isinstance(version, str) or not version):
        raise ValueError("invalid_report_recovery_version")
    thread_id = manifest.get("thread_id")
    if thread_id is not None and (not isinstance(thread_id, str) or not thread_id):
        raise ValueError("invalid_report_main_thread")
    presentation = ActorPresentation(store)
    attempts, after = [], 0
    while True:
        events = store.events_by_type(("benchmark.actor_turn_continuation",), after=after, limit=1000)
        if not events:
            break
        for event in events:
            payload = event.get("payload")
            if (not isinstance(payload, dict) or not isinstance(payload.get("thread_id"), str)
                    or not payload["thread_id"] or not isinstance(payload.get("prompt"), str)
                    or not payload["prompt"]):
                raise ValueError("invalid_report_continuation_event")
            context = payload.get("recovery_context")
            if context is not None and not isinstance(context, dict):
                raise ValueError("invalid_report_recovery_context")
            if context is not None and context.get("version") is not None and (
                    not isinstance(context["version"], str) or not context["version"]):
                raise ValueError("invalid_report_recovery_version")
            if thread_id and payload["thread_id"] == thread_id:
                attempts.append((presentation.restore(payload["prompt"]), context))
        after = events[-1]["server_seq"]

    path = root / "codex-raw.jsonl"
    sent = []
    if path.exists():
        with path.open() as stream:
            for line in stream:
                record = json.loads(line)
                if (not isinstance(record, dict) or not isinstance(record.get("message"), dict)
                        or record.get("direction") not in ("sent", "received")):
                    raise ValueError("invalid_native_rpc_record")
                message = record["message"]
                if "params" in message and not isinstance(message["params"], dict):
                    raise ValueError("invalid_native_rpc_record")
                if record["direction"] != "sent" or message.get("method") != "turn/start":
                    continue
                params = message.get("params", {})
                inputs = params.get("input")
                if (not isinstance(params.get("threadId"), str) or not params["threadId"]
                        or not isinstance(inputs, list) or any(not isinstance(item, dict)
                            or not isinstance(item.get("type"), str)
                            or (item["type"] == "text" and not isinstance(item.get("text"), str))
                            for item in inputs)):
                    raise ValueError("invalid_native_turn_start")
                if thread_id and params["threadId"] == thread_id:
                    # restore only reads saved aliases; never allocate new references
                    # or rerun today's producer projection against an old archive.
                    sent.append({presentation.restore(item["text"]) for item in inputs if item["type"] == "text"})

    available = path.exists() and thread_id is not None
    matched, recovery_sent, versions = set(), 0, {}
    for prompt, context in attempts:
        index = next((index for index, texts in enumerate(sent) if index not in matched and prompt in texts), None)
        if index is not None:
            matched.add(index)
        if context is not None:
            entry = versions.setdefault(context.get("version"), dict(attempt_count=0, native_sent_count=0))
            entry["attempt_count"] += 1
            entry["native_sent_count"] += index is not None
            recovery_sent += index is not None
    return dict(configured_version=version,
        status="main_thread_unknown" if thread_id is None else "available" if available else "native_log_missing",
        continuation_attempt_count=len(attempts) if thread_id else None,
        recovery_attempt_count=sum(context is not None for _, context in attempts) if thread_id else None,
        native_turn_start_count=len(sent) if available else None,
        native_sent_continuation_count=len(matched) if available else None,
        native_sent_recovery_count=recovery_sent if available else None,
        unmatched_continuation_attempt_count=len(attempts)-len(matched) if available else None,
        versions=[dict(version=key, attempt_count=value["attempt_count"],
            native_sent_count=value["native_sent_count"] if available else None)
            for key, value in sorted(versions.items(), key=lambda item: item[0] or "")] if thread_id else None)


def summarize(root, price_card=None):
    root = Path(root).expanduser().resolve()
    def read(name):
        path = root / name
        return json.loads(path.read_text()) if path.exists() else {}
    manifest, summary = read("manifest.json"), load_effective_summary(root, summary=read("summary.json"))
    snapshot = summary.get("token_usage")
    usage = (snapshot or {}).get("total") or {}
    counts = Counter()
    deliveries = 0
    native_images = code_mode_failures = 0
    rollouts = list((root / "private" / "codex-home" / "sessions").rglob("*.jsonl"))
    for path in rollouts:
        with path.open() as stream:
            for line in stream:
                message = json.loads(line)
                item = message.get("payload") or {}
                if message.get("type") != "response_item" or item.get("type") not in ("custom_tool_call_output", "function_call_output"): continue
                output = item.get("output")
                if isinstance(output, list): native_images += sum(isinstance(block, dict) and block.get("type") == "input_image" for block in output)
                elif isinstance(output, str) and ("SIGSEGV" in output or "code-mode host closed" in output): code_mode_failures += 1
    with open_read_store(root) as store:
        for value in store.records("tool_call"):
            counts[value["params"]["tool"]] += 1
        deliveries = sum(len(value.get("images", [])) for value in store.records("delivery")
                         if value.get("delivered_to_actor"))
        recovery = _recovery_delivery(root, manifest, store)
    model, provider = manifest.get("model"), manifest.get("provider")
    identity_source = "manifest" if model and provider else None
    saved_accounting = summary.get("token_accounting")
    native = account_usage(snapshot)
    # TUI startup can fail after model usage but before manifest creation. The
    # final summary still records accounting. Require this exact native snapshot,
    # rather than borrowing identity/costs from a stale or foreign thread.
    saved_matches = (isinstance(saved_accounting, dict) and native.keys() <= saved_accounting.keys()
                     and isinstance(native["thread_id"], str)
                     and bool(native["thread_id"])
                     and all(saved_accounting.get(key) == native[key] for key in
                             ("schema_version", "scope", "source", "thread_id", "turn_id", "raw_ref", "tokens", "model_context_window"))
                     and all(value is None or saved_accounting.get(key) == value
                             for key, value in (("model", model), ("provider", provider))))
    if saved_matches and (model is None or provider is None):
        model, provider = saved_accounting.get("model"), saved_accounting.get("provider")
        identity_source = "summary.token_accounting"
    if provider is None:
        # Older manifests omitted provider. Use this run's frozen configuration,
        # never the developer's current global model or credentials.
        try:
            config = tomllib.loads((root / "private/codex-home/config.toml").read_text())
            if model is not None and config.get("model") == model:
                provider = config.get("model_provider")
                identity_source = "private/codex-home/config.toml" if provider else None
        except (OSError, ValueError):
            pass
    if (saved_matches and price_card is None and manifest.get("price_card") is None
            and (saved_accounting.get("model"), saved_accounting.get("provider")) == (model, provider)):
        accounting = dict(saved_accounting)
    else:
        accounting = account_usage(snapshot, model=model, provider=provider,
                                   price_card=price_card if price_card is not None else manifest.get("price_card"))
    accounting["identity_source"] = identity_source
    return dict(run_dir=str(root), run_id=manifest.get("run_id", root.name),
                conditions={**{key: manifest.get(key) for key in ("effort", "codex_sdk", "observation_profile", "clock_policy", "max_actions", "actor_wait_budget_s", "task_notes", "task_notes_source", "task_notes_config", "public_task_rules", "task_planning", "actor_stall_policy", "benchmark_terminal_policy", "runtime_protocol", "model_request_limit", "model_request_timeout_s", "no_progress_timeout_s", "startup_timeout_s", "pi05_enabled", "visual_review_model")}, "model": model, "provider": provider},
                task=manifest.get("task", read("experiment.json").get("task")),
                seed=manifest.get("seed"), layout_id=manifest.get("layout_id"),
                status=summary.get("status"), valid_episode=summary.get("valid_episode"),
                primary_outcome=summary.get("primary_outcome"), scored_episode=summary.get("scored_episode"),
                partial_native_result=summary.get("partial_native_result"),
                task_success=summary.get("task_success"), normalized_score=summary.get("normalized_score"),
                action_count=summary.get("action_count"), elapsed_wall_s=summary.get("elapsed_wall_s"),
                native_episode=summary.get("native_episode"), model_requests=summary.get("model_requests"),
                termination_reason=summary.get("termination_reason"),
                termination_authority=summary.get("termination_authority"),
                actor_stall=summary.get("actor_stall"),
                recovery_guidance=recovery,
                task_outcome=summary.get("task_outcome"), score_status=summary.get("score_status"),
                failure_reason=summary.get("failure_reason"), scoring_policy=summary.get("scoring_policy"),
                scoring_reconciliation=summary.get("scoring_reconciliation"), native_evaluation_snapshot=summary.get("native_evaluation_snapshot"),
                visual_review_usage=summary.get("visual_review_usage"), token_accounting_scope=summary.get("token_accounting_scope"),
                token_usage=usage or None,
                token_accounting=accounting,
                uncached_input_tokens=accounting["tokens"]["uncached_input_tokens"],
                cache_hit_rate=accounting["cache_hit_rate"],
                actor_tools=dict(counts), actor_image_deliveries=deliveries,
                native_model_input_images=native_images if rollouts else None, code_mode_failures=code_mode_failures if rollouts else None,
                infrastructure_error=summary.get("infrastructure_error"), source_sha256=manifest.get("source_sha256"), source_ref=manifest.get("source_ref"))


def build_report(run_dirs, price_card=None):
    """Retain unreadable runs in the denominator and keep their usage unknown."""
    roots = [Path(root).expanduser().resolve() for root in run_dirs]
    if len(set(roots)) != len(roots):
        raise ValueError("duplicate_run_directory")
    rows = []
    for root in roots:
        try:
            row = {**summarize(root, price_card), "report_status": "readable"}
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
            row = dict(run_dir=str(root), run_id=root.name, report_status="unreadable",
                       report_error={"code": "run_evidence_unreadable", "message": str(error),
                                     "type": type(error).__name__},
                       primary_outcome=None, task_success=None, scored_episode=None,
                       token_accounting=account_usage(None, price_card=price_card))
        rows.append(row)
    # Directory is the requested evidence unit, even if archived run IDs collide.
    identities = Counter(row["run_id"] for row in rows if row["report_status"] == "readable")
    return dict(scope="Recorded runs; missing evidence is unknown, not a task failure or zero cost",
                runs=rows, coverage=dict(requested=len(rows), readable=sum(row["report_status"] == "readable" for row in rows),
                    unreadable=sum(row["report_status"] == "unreadable" for row in rows),
                    duplicate_run_ids=sorted(key for key, count in identities.items() if count > 1)),
                outcomes=dict(Counter(row.get("primary_outcome") or "unknown" for row in rows)),
                token_accounting=aggregate_accounting([{**row, "run_id": row["run_dir"]} for row in rows]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dirs", nargs="+")
    parser.add_argument("--output", help="New JSON file; existing evidence is never overwritten")
    parser.add_argument("--price-card", help="JSON price schedule; recomputes estimates without changing original runs")
    args = parser.parse_args()
    card = load_price_card(args.price_card) if args.price_card else None
    report = build_report(args.run_dirs, card)
    encoded = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        with Path(args.output).expanduser().open("x") as stream: stream.write(encoded)
    print(encoded, end="")


if __name__ == "__main__":
    main()
