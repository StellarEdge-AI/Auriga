#!/usr/bin/env python3
"""Read-only episode cost report, with actor/helper coverage and scored denominators."""
import argparse
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
import tomllib

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from auriga.apps.library.scoring import classify_episode_result, load_effective_summary
from auriga.core.storage.store import Store
from auriga.cortex.session.usage import account_usage, account_responses_usage, load_price_card, mixed_accounting


def summarize_run(root, card):
    root = Path(root).expanduser().resolve()
    originals = {name: (root / name).read_bytes() if (root / name).exists() else None
                 for name in ("manifest.json", "summary.json", "runtime.json", "experiment.json")}
    manifest, original_summary, runtime, experiment = [json.loads(value) if value is not None else {}
        for value in originals.values()]
    summary = load_effective_summary(root, summary=original_summary)
    if "scored_episode" not in summary:
        if not original_summary:
            summary["infrastructure_error"] = "episode_summary_missing"
        summary = classify_episode_result(summary, summary.get("status", runtime.get("status")))
    provider = manifest.get("provider") or (summary.get("token_accounting") or {}).get("provider")
    if provider is None:
        config_path = root / "private/codex-home/config.toml"
        if config_path.exists():
            provider = tomllib.loads(config_path.read_text()).get("model_provider")
    events = []
    actor_usage = summary.get("token_usage")
    if (root / "events.sqlite").exists():
        store = Store(root, manifest, read_only=True)
        try:
            cursor = store.event_cursor()
            for (body,) in store.db.execute("SELECT body FROM events ORDER BY seq"):
                event = json.loads(body)
                if event["type"].startswith("visual.model_"):
                    events.append(event)
            actor_usage = actor_usage or store.read_state("runtime", "token_usage")
            if cursor != store.event_cursor():
                raise ValueError("mixed_cost_source_changed_during_read")
        finally:
            store.close()
    transport = []
    for path in sorted((root / "private").glob("model-requests*.jsonl")):
        for line in path.read_text().splitlines():
            event = json.loads(line)
            event["metrics_file"] = path.name
            transport.append(event)
    entries = []
    def add(identifier, source, usage, model, **evidence):
        entries.append(dict(accounting_id=identifier, source=source, **evidence,
            accounting=account_responses_usage(usage, model=model, provider=provider, price_card=card)))
    # A proxy attempt ledger covers actor and helper requests. Never add the
    # Codex cumulative snapshot or duplicate visual events on top of that ledger.
    ledger = [e for e in transport if e.get("logical_call_id") and e.get("attempt_index")]
    attempts = [e for e in ledger if e["kind"] == "model_request_finished"]
    if ledger:
        coverage = "proxy_attempt_ledger; cumulative_actor_and_visual_events_excluded"
        seen = {}
        for event in attempts:
            identifier = f"{event['metrics_file']}:{event['logical_call_id']}:{event['attempt_index']}"
            if identifier in seen:
                if seen[identifier] != event:
                    raise ValueError("conflicting_attempt_usage_records")
                continue
            seen[identifier] = event
            add(identifier, event.get("source", "unknown"), event.get("usage"),
                event.get("served_model") or (event.get("requested_model") if not event.get("usage") else None),
                response_id=event.get("response_id"), terminal_status=event.get("http_status"))
        finished = {(e["metrics_file"], e["logical_call_id"], e["attempt_index"]) for e in attempts}
        for event in transport:
            key = (event["metrics_file"], event.get("logical_call_id"), event.get("attempt_index"))
            if event["kind"] == "model_request_started" and key not in finished:
                add(":".join(map(str, key)), event.get("source", "unknown"), None,
                    event.get("requested_model"), terminal_status="missing_terminal_evidence")
    else:
        coverage = "legacy_actor_cumulative_plus_recorded_helpers; per_attempt_coverage_unavailable"
        entries.append(dict(accounting_id="actor-cumulative", source="actor",
            accounting=account_usage(actor_usage, model=manifest.get("model"), provider=provider, price_card=card)))
        terminal_ids = set()
        for event in events:
            if event["type"] not in ("visual.model_reviewed", "visual.model_review_failed", "visual.model_review_cancelled"):
                continue
            payload = event["payload"]
            identifier = payload.get("request_id") or payload.get("response_ref") or event["event_id"]
            if identifier in terminal_ids:
                raise ValueError("duplicate_visual_request_terminal")
            terminal_ids.add(identifier)
            add(identifier, "sol_visual_review", payload.get("usage"), payload.get("served_model", payload.get("model")),
                terminal_status=event["type"], response_id=payload.get("response_id"))
        for event in events:
            payload = event["payload"]
            if event["type"] == "visual.model_review_started" and payload["request_id"] not in terminal_ids:
                add(payload["request_id"], "sol_visual_review", None, payload.get("model"),
                    terminal_status="missing_terminal_evidence")
        if not events:
            for index, payload in enumerate(summary.get("visual_review_usage") or []):
                add("summary-visual-"+str(index), "sol_visual_review", payload.get("usage"), payload.get("model"))
        # Old transport logs cannot attribute failed-attempt usage to the
        # native cumulative snapshot. Preserve one unknown coverage component.
        legacy_failed, legacy_unknown = [], []
        for event in transport:
            if event["kind"] != "model_request_finished":
                continue
            complete = event.get("transport_complete", event.get("response_complete"))
            if complete is False or (event.get("http_status") is not None and event["http_status"] != 200):
                legacy_failed.append(event)
            elif complete is not True or event.get("http_status") is None:
                legacy_unknown.append(event)
        for label, values in (("failed", legacy_failed), ("unknown", legacy_unknown)):
            if values:
                add("legacy-"+label+"-attempts", "unattributed_"+label+"_attempts", None, None,
                    affected_attempts=len(values))
        counts = summary.get("model_requests") or {}
        if (counts.get("http_attempt_count") == 0 and counts.get("completed_calls") == 0
                and not transport and not events and not actor_usage
                and not summary.get("visual_review_usage") and summary.get("status") in ("failed", "stopped", "completed")):
            zero = account_usage(dict(total={name: 0 for name in ("input_tokens", "cached_input_tokens",
                "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")}), price_card=card)
            zero["rate_card_estimate"].update(status="complete", reason="verified_no_model_attempts", total="0",
                uncached_input="0", cached_input="0", cache_write_input="0", output="0", known_subtotal="0")
            entries = [dict(accounting_id="verified-no-attempts", source="verified_no_model_attempts", accounting=zero)]
            coverage = "terminal_summary_explicitly_reports_zero_attempts_and_no_usage_or_transport_evidence"
    for name, before in originals.items():
        after = (root / name).read_bytes() if (root / name).exists() else None
        if before != after:
            raise ValueError("mixed_cost_source_changed_during_read")
    return dict(run_dir=str(root), run_id=manifest.get("run_id") or runtime.get("run_id") or root.name,
        task=manifest.get("task", experiment.get("task")),
        metadata_missing=[name for name in ("manifest.json", "summary.json") if originals[name] is None],
        outcome_classification_basis="verified_historical_reconciliation" if summary.get("scoring_reconciliation", {}).get("applied") else "recorded_summary" if "scored_episode" in original_summary else "offline_current_protocol",
        task_outcome=summary.get("task_outcome"), score_status=summary.get("score_status"),
        failure_reason=summary.get("failure_reason"), scoring_policy=summary.get("scoring_policy"),
        scoring_reconciliation=summary.get("scoring_reconciliation"), native_evaluation_snapshot=summary.get("native_evaluation_snapshot"),
        normalized_score=summary.get("normalized_score"),
        original_native_result={name: original_summary.get(name) for name in
            ("status", "valid_episode", "task_success", "normalized_score", "native_episode", "primary_outcome")},
        primary_outcome=summary.get("primary_outcome"), scored_episode=summary.get("scored_episode"),
        task_success=summary.get("task_success"), coverage=coverage, entries=entries,
        accounting=mixed_accounting(entries), source_sha256={
            name: hashlib.sha256(value).hexdigest() if value is not None else None for name, value in originals.items()})


def report(runs):
    if len({run["run_id"] for run in runs}) != len(runs):
        raise ValueError("duplicate_run_id")
    entries = [{**entry, "accounting_id": run["run_id"]+":"+entry["accounting_id"]}
               for run in runs for entry in run["entries"]]
    accounting = mixed_accounting(entries)
    valid = sum(run["scored_episode"] is True for run in runs)
    successes = sum(run["scored_episode"] is True and run["task_success"] is True for run in runs)
    costs = {}
    for currency, value in accounting["total"]["rate_card_estimate_by_currency"].items():
        costs[currency] = dict(total_cost_per_scored_success=(str(Decimal(value["total"])/successes)
            if successes and value["total"] is not None else None),
            known_component_cost_per_scored_success=(str(Decimal(value["known_component_subtotal"])/successes)
            if successes else None))
    return dict(scope="Model API reference-price estimate only; excludes robot/GPU/hosting costs; not provider invoice or causal performance comparison",
        runs=runs, accounting=accounting,
        outcomes=dict(episodes=len(runs), scored_episodes=valid, scored_successes=successes,
                      scored_failures=valid-successes, unscored_episodes=len(runs)-valid),
        cost_per_scored_success=dict(numerator="all listed episodes, including failures and unscored runs",
                                    denominator=successes, by_currency=costs))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dirs", nargs="+", type=Path)
    parser.add_argument("--price-card", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    card = load_price_card(args.price_card)
    result = report([summarize_run(path, card) for path in args.run_dirs])
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
    print(json.dumps(dict(output=str(args.output), outcomes=result["outcomes"])))


if __name__ == "__main__":
    main()
