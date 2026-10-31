"""Evidence and bookkeeping for fresh-episode retries after native GPU OOM."""
import hashlib
import json
import math
from pathlib import Path
import re
import time
import uuid

from auriga.core.config import OomRetryConfig
from auriga.robot.backends.robodojo.robot_process import native_gpu_oom_signature
from auriga.apps.library.scoring import load_effective_summary
from auriga.apps.evaluation.provenance import CHECKOUT_LAYOUTS
from auriga.core import legacy_names as legacy


DEFAULT_POLICY = OomRetryConfig().to_dict()
LOG_TAIL_BYTES = 1024 * 1024


def retry_policy(manifest):
    supplied = manifest.get("oom_retry", {})
    if not isinstance(supplied, dict) or supplied.keys() - DEFAULT_POLICY.keys():
        raise ValueError("oom_retry accepts max_retries, initial_delay_s, backoff_multiplier, max_delay_s")
    policy = {**DEFAULT_POLICY, **supplied}
    if type(policy["max_retries"]) is not int or not 0 <= policy["max_retries"] <= 20:
        raise ValueError("oom_retry.max_retries must be an integer between 0 and 20")
    for name in ("initial_delay_s", "backoff_multiplier", "max_delay_s"):
        value = policy[name]
        if type(value) not in (int, float) or not math.isfinite(value) or value < 1:
            raise ValueError("oom_retry." + name + " must be finite and >= 1")
    return policy


def retry_delay(policy, attempt_number):
    delay = min(policy["max_delay_s"], policy["initial_delay_s"])
    for _ in range(attempt_number - 1):
        if delay >= policy["max_delay_s"] / policy["backoff_multiplier"]:
            return policy["max_delay_s"]
        delay *= policy["backoff_multiplier"]
    return delay


def logical_id(run_dir):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, str(Path(run_dir).resolve())))


def retry_run_dir(root_run_dir, attempt_number):
    root = Path(root_run_dir)
    return str(root.with_name(root.name + f".oom-retry-{attempt_number - 1:02d}"))


def recovered_command(entry, gpu):
    """Replay frozen gateway options; only a new episode directory/GPU changes."""
    args = entry.get("retry_command_template")
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        raise ValueError("OOM recovery requires the original gateway command evidence")
    if (len(args) < 5 or not Path(args[0]).is_absolute() or args[1:5] not in [["-u", "-m", layout["module"], "--benchmark"] for layout in CHECKOUT_LAYOUTS]
            or any(item in args for item in ("--resume", "--replay", "--max-actions", "--wall-budget-s"))):
        raise ValueError("OOM recovery requires a fixed-budget benchmark gateway command")
    args = list(args)
    for flag, value in (("--run-dir", entry["case"]["run_dir"]), ("--gpu", str(gpu))):
        if args.count(flag) != 1 or args.index(flag) + 1 >= len(args):
            raise ValueError("OOM recovery command lacks a unique " + flag)
        args[args.index(flag) + 1] = value
    return args


def native_oom_signature(text):
    return "robot_gpu_out_of_memory:" if "robot_gpu_out_of_memory:" in text else native_gpu_oom_signature(text)


def oom_evidence(entry):
    """Read one bounded native-log tail only after an unscored attempt ends.

    Actor transcripts, queue stdout, arbitrary 'oom' text and exit codes are not
    evidence. A native terminal result always wins over later export/cleanup OOM.
    """
    summary = entry.get("summary") or {}
    outcome = summary.get("primary_outcome", "")
    if ((summary.get("native_episode") or {}).get("ended") is True
            or str(outcome).startswith("scored_") or summary.get("task_success") is True
            or summary.get("termination_reason") in {"native_step_limit", "model_request_limit",
                "official_startup_no_action_timeout", "official_no_action_progress_timeout"}):
        return None
    reason = summary.get("infrastructure_error")
    if not (reason or outcome == "unscored_infrastructure" or summary.get("status") in {"failed", "unknown"}):
        return None
    run = Path(entry["case"]["run_dir"])
    # Only exception-shaped native failures qualify here. Codex failures may
    # quote model text and therefore require independent native-log evidence.
    if isinstance(reason, str) and (reason.startswith("robot_gpu_out_of_memory:")
            or re.match(r"^(?:bootstrap_failed: )?(?:RuntimeError|(?:torch\.)?(?:cuda\.)?OutOfMemoryError):", reason)):
        signature = native_oom_signature(reason)
        if signature:
            return {"source": str(run / "summary.json"), "field": "infrastructure_error",
                    "signature": signature, "value": reason[:4096],
                    "sha256": hashlib.sha256(reason.encode()).hexdigest()}
    log = run / "private" / "robot" / "robot-worker.log"
    try:
        with log.open("rb") as stream:
            size = stream.seek(0, 2)
            offset = max(0, size - LOG_TAIL_BYTES)
            stream.seek(offset)
            tail = stream.read(LOG_TAIL_BYTES)
    except OSError:
        return None
    position = offset
    for raw_line in tail.splitlines(keepends=True):
        line = raw_line.decode("utf-8", errors="replace")
        signature = native_oom_signature(line)
        if signature and not line.lstrip().startswith(("AURIGA_JSON", legacy.WORKER_WIRE_PREFIX, "{")):
            return {"source": str(log), "signature": signature, "byte_offset": position,
                    "excerpt": line.strip()[:1024], "file_size": size,
                    "scan_offset": offset, "scan_bytes": len(tail),
                    "sha256": hashlib.sha256(raw_line).hexdigest()}
        position += len(raw_line)
    return None


def cleanup_confirmed(entry):
    report = entry.get("queue_cleanup") or {}
    return (entry.get("status") == "exited" and report.get("gateway_exit_confirmed") is True
            and report.get("worker_exit_confirmed") is True
            and report.get("process_exit_confirmed") is not False)


def logical_tasks(entries):
    """Keep first-attempt and final-attempt outcomes separate in queue reports."""
    tasks = {}
    for entry in entries:
        task = tasks.setdefault(entry["logical_task_id"], {"attempts": []})
        task["attempts"].append({key: entry.get(key) for key in
            ("attempt_number", "case", "status", "returncode", "summary", "effective_summary", "oom_evidence", "retry_status",
             "retry_delay_s", "retry_not_before", "source_queue", "source_state_sha256", "source_summary_sha256")})
        attempt = task["attempts"][-1]
        if attempt.get("summary") and not attempt.get("effective_summary"):
            attempt["effective_summary"] = load_effective_summary(
                Path(entry["case"]["run_dir"]), summary=attempt["summary"])
    for task in tasks.values():
        task["attempts"].sort(key=lambda item: item["attempt_number"])
        first, last = task["attempts"][0], task["attempts"][-1]
        task.update(first_attempt_outcome=(first.get("effective_summary") or first.get("summary") or {}).get("primary_outcome"),
                    final_attempt_outcome=(last.get("effective_summary") or last.get("summary") or {}).get("primary_outcome"),
                    had_oom=any(item.get("oom_evidence") for item in task["attempts"]),
                    retries_started=sum(item["attempt_number"] > 1 and item["status"] != "not_started" for item in task["attempts"]),
                    status=last.get("retry_status") if last.get("retry_status") in
                        {"pending", "exhausted", "blocked_cleanup", "cancelled"} else last["status"])
    return tasks


def recover_oom(source):
    """Prepare only safe OOM retries from a terminal queue; never mutate it.

    Live/crashed queues are deliberately not resumed: gateway ownership cannot
    be safely reconstructed from a stale state snapshot alone.
    """
    source = Path(source).resolve()
    raw = (source / "state.json").read_bytes()
    state = json.loads(raw)
    if state.get("status") not in {"finished", "failed", "interrupted", "gpu_wait_timeout"}:
        raise ValueError("OOM recovery requires a terminal queue; live/crashed queue ownership is unknown")
    config = json.loads((source / "config.json").read_text())
    manifest = config["manifest"]
    policy = retry_policy(manifest)
    entries = [dict(item) for item in state.get("prior_attempts", []) + state["cases"]]
    if any(item.get("status") in {"running", "starting"} for item in entries):
        raise ValueError("OOM recovery refuses a queue with active or unconfirmed attempts")
    grouped = {}
    for entry in entries:
        entry.setdefault("logical_task_id", logical_id(entry["case"]["run_dir"]))
        entry.setdefault("attempt_number", 1)
        entry.setdefault("root_run_dir", entry["case"]["run_dir"])
        grouped.setdefault(entry["logical_task_id"], []).append(entry)
    selected, prior = [], []
    for task_entries in grouped.values():
        launched = [item for item in task_entries if item.get("status") != "not_started"]
        if not launched:
            continue
        entry = max(launched, key=lambda item: item["attempt_number"])
        if not cleanup_confirmed(entry) or entry["attempt_number"] > policy["max_retries"]:
            continue
        # A cached OOM label cannot override a native terminal result. Bind the
        # recovery decision to the exact summary bytes whose hash we preserve.
        summary_path = Path(entry["case"]["run_dir"]) / "summary.json"
        try:
            summary_raw = summary_path.read_bytes()
            summary = json.loads(summary_raw)
        except (OSError, ValueError) as error:
            raise ValueError("OOM recovery requires readable source summary: " + str(summary_path)) from error
        if summary != entry.get("summary"):
            raise ValueError("OOM recovery source summary differs from terminal queue state: " + str(summary_path))
        evidence = oom_evidence(entry)
        if not evidence:
            continue
        entry["oom_evidence"] = evidence
        entry["source_summary_sha256"] = hashlib.sha256(summary_raw).hexdigest()
        delay = retry_delay(policy, entry["attempt_number"])
        # Persisted pending retries retain their deadline. Older queues had no
        # retry clock, so start their backoff when recovery is explicitly invoked.
        due = (entry["retry_not_before"] if entry.get("retry_status") == "scheduled" and "retry_not_before" in entry
               else time.time() + delay)
        for old in launched:
            old.setdefault("source_queue", str(source))
            old.setdefault("source_state_sha256", hashlib.sha256(raw).hexdigest())
            try:
                old.setdefault("source_summary_sha256", hashlib.sha256(
                    (Path(old["case"]["run_dir"]) / "summary.json").read_bytes()).hexdigest())
            except OSError:
                pass
            prior.append(old)
        number = entry["attempt_number"] + 1
        selected.append({"case": {**entry["case"], "run_dir": retry_run_dir(entry["root_run_dir"], number)},
            "logical_task_id": entry["logical_task_id"], "attempt_number": number,
            "root_run_dir": entry["root_run_dir"], "previous_attempt_run_dir": entry["case"]["run_dir"],
            "retry_delay_s": delay, "retry_not_before": due, "retry_status": "pending", "status": "not_started",
            "retry_command_template": entry.get("retry_command_template", entry.get("command")),
            "retry_experiment_settings": entry.get("retry_experiment_settings", entry.get("experiment_settings"))})
        if not isinstance(selected[-1]["retry_experiment_settings"], dict):
            raise ValueError("OOM recovery requires original experiment settings evidence")
        recovered_command(selected[-1], 0)  # Missing provenance fails before creating any output.
    if not selected:
        raise ValueError("No cleanup-confirmed OOM attempts with retries remaining")
    return ({**manifest, "cases": [item["case"] for item in selected]},
            {"source_queue": str(source), "source_state_sha256": hashlib.sha256(raw).hexdigest(),
             "entries": selected, "prior_attempts": prior})
