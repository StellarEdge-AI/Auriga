"""One task-result policy for runtime, catalog and newly generated reports.

Native evaluation is evidence, task termination determines success eligibility.
Historical corrections are derived views; archived files remain original evidence.
"""
from copy import deepcopy
import json
import math
from pathlib import Path
import sqlite3

from ...core.storage.store import open_read_store

TASK_LIMIT_REASONS = {
    "official_startup_no_action_timeout": "启动后无动作超时",
    "official_no_action_progress_timeout": "动作进展超时",
    "model_request_limit": "模型调用额度耗尽",
    "actor_repeated_inability": "模型连续 5 次声明无法继续完成",
    "actor_no_progress": "模型在有限恢复机会后仍无动作进展",
    "development_wall_budget": "实验时间预算耗尽",
}
FAILURE_LABELS = {**TASK_LIMIT_REASONS, "native_step_limit": "原生任务步数耗尽",
                  "native_task_failure": "原生任务判定失败", "native_failure": "原生任务判定失败"}
SCORING_POLICY = dict(version="native_terminal_and_task_limits_v3",
    task_limits="confirmed_task_limit_stops_are_failures_with_native_process_score",
    native_terminal="native_terminal_result_wins_supervisor_races",
    exclusions="infrastructure_unconfirmed_stop_invalid_or_missing_evaluation")
NATIVE_KEYS = ("task_success", "normalized_score", "native_reward", "native_process_score",
               "valid_episode", "success_authority", "authority")
CLASSIFICATION_KEYS = ("primary_outcome", "scored_episode", "task_success", "normalized_score", "success_authority")


def _validate_summary(summary):
    if not isinstance(summary, dict):
        raise ValueError("summary_requires_object")
    for key in ("scoring_policy", "native_episode", "native_evaluation_snapshot", "partial_native_result",
                "token_usage", "token_accounting"):
        if key in summary and not isinstance(summary[key], dict) and (key == "scoring_policy" or summary[key] is not None):
            raise ValueError("summary_field_requires_object:" + key)


def _number(value, maximum=1):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= maximum


def _describe_failure(summary):
    if summary.get("task_success") is False and summary.get("scored_episode") is True and not summary.get("failure_reason"):
        terminal = summary.get("native_episode") or {}
        ended = terminal.get("ended") is True
        code = (terminal.get("reason") or "native_task_failure") if ended else summary.get("termination_reason") or "native_task_failure"
        summary["failure_reason"] = dict(code=code, label=FAILURE_LABELS.get(code, "原生任务判定失败"),
            authority="robodojo_environment" if ended else "harness")
    return summary


def classify_episode_result(value, final_status):
    result = deepcopy(value)
    native = result.get("native_evaluation_snapshot")
    if not isinstance(native, dict):
        native = {key: result[key] for key in NATIVE_KEYS if key in result}
    result["native_evaluation_snapshot"] = deepcopy(native)
    terminal = result.get("native_episode") or {}
    ended = terminal.get("ended") is True
    reason = result.get("termination_reason")
    task_limit = reason in TASK_LIMIT_REASONS and not ended
    expected_budget = reason == "model_request_limit" and result.get("model_budget_exhausted") is True
    score, success = native.get("normalized_score"), native.get("task_success")
    valid = native.get("valid_episode") is True and type(success) is bool and _number(score)
    process = native.get("native_process_score")
    if "native_process_score" in native and process is not None and not _number(process, 100):
        valid = False
    if task_limit and valid:
        if _number(process, 100):
            score = process / 100
        elif "native_process_score" in native and process is None:
            # The evaluator explicitly records no process-score mechanism.
            score = 0.0
        elif success is True:
            valid = False  # A success-normalized 1 cannot become a failure score.
        success = False
    clean_task_stop = (task_limit and final_status in {"stopped", "completed", "failed"}
        and (final_status != "failed" or expected_budget) and not result.get("infrastructure_error")
        and result.get("cleanup_status") in (None, "completed"))
    confirmed_failure = clean_task_stop and native.get("valid_episode") is True
    if not ended and (final_status == "unknown" or result.get("infrastructure_error")
            or (final_status == "failed" and not expected_budget)
            or result.get("cleanup_status") not in (None, "completed")):
        outcome = "unscored_infrastructure"
    elif not valid:
        outcome = "unscored_evaluation"
    elif ended or clean_task_stop or (not result.get("benchmark") and final_status == "completed"):
        if ended and type(terminal.get("success")) is bool:
            success = terminal["success"]
            if success != native.get("task_success"):
                outcome = "unscored_evaluation"
            else:
                outcome = "scored_success" if success else "scored_failure"
        else:
            outcome = "scored_success" if success else "scored_failure"
    else:
        outcome = "unscored_interrupted"
    scored = outcome.startswith("scored_")
    result.update(primary_outcome=outcome, scored_episode=scored,
                  task_success=success if scored else False if confirmed_failure else None, normalized_score=score if scored else None,
                  task_outcome="success" if outcome == "scored_success" else "failure" if outcome == "scored_failure" or confirmed_failure else "unknown",
                  score_status="scored" if scored else "unavailable", scoring_policy=deepcopy(SCORING_POLICY),
                  termination_authority="robodojo_environment" if ended else "harness")
    result.pop("failure_reason", None)
    if outcome == "scored_failure" or confirmed_failure:
        code = (terminal.get("reason") or "native_task_failure") if ended else reason or "native_task_failure"
        result["failure_reason"] = dict(code=code, label=FAILURE_LABELS.get(code, "原生任务判定失败"),
                                       authority="robodojo_environment" if ended else "harness")
    if confirmed_failure:
        result["success_authority"] = "harness_task_limit"
    else:
        result["success_authority"] = native.get("success_authority") if scored else None
    if not scored and not isinstance(result.get("partial_native_result"), dict):
        result["partial_native_result"] = deepcopy(native)
    if "score" in result:
        result["score"] = result["normalized_score"]
    return result


def reconcile_summary(summary, evaluation=None, *, evidence_verified=False):
    """Expose corrected historical task-limit outcomes only with verified evidence."""
    original = deepcopy({} if summary is None else summary)
    _validate_summary(original)
    if original.get("scoring_policy", {}).get("version") == SCORING_POLICY["version"]:
        if isinstance(original.get("native_evaluation_snapshot"), dict) and original.get("status"):
            return classify_episode_result(original, original["status"])
        return _describe_failure(original)
    if original.get("termination_reason") not in TASK_LIMIT_REASONS:
        return _describe_failure(original)
    if not evidence_verified or original.get("cleanup_status") != "completed" or original.get("infrastructure_error"):
        return _describe_failure(original)
    if original.get("status") not in {"completed", "stopped", "failed"}:
        return _describe_failure(original)
    native = evaluation.get("result", evaluation) if isinstance(evaluation, dict) else original.get("partial_native_result")
    if not isinstance(native, dict):
        return _describe_failure(original)
    candidate = {**original, **{key: native[key] for key in NATIVE_KEYS if key in native},
                 "native_evaluation_snapshot": deepcopy(native)}
    # Missing legacy fields may be present in the outer, validated summary.
    candidate["native_evaluation_snapshot"].setdefault("valid_episode", original.get("valid_episode"))
    effective = classify_episode_result(candidate, original["status"])
    effective["scoring_reconciliation"] = dict(applied=True,
        reason="apply_current_task_limit_policy_to_verified_saved_native_evaluation",
        original={key: original.get(key) for key in CLASSIFICATION_KEYS},
        evaluation_id=native.get("evaluation_id"), result_ref=evaluation.get("result_ref") if isinstance(evaluation, dict) else None)
    return effective


def _object(path, default=None):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("symlink_metadata_denied")
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return default
    if not isinstance(value, dict):
        raise ValueError("metadata_requires_object")
    return value


def _verified_evaluation(directory, native_count):
    root = Path(directory)
    evaluation = _object(root / "evaluation.json", {})
    native = evaluation.get("result")
    if not isinstance(native, dict) or not evaluation.get("result_ref"):
        return None
    try:
        with open_read_store(root) as store:
            identity = store.read_state("runtime", "identity", {})
            keys = ("run_id", "episode_id", "env_instance_id", "env_generation")
            if any(key not in identity or evaluation.get(key) != identity[key] for key in keys):
                return None
            if native_count is not None and native.get("evaluator_actions") != native_count:
                return None
            _, content = store.resolve_artifact(evaluation["result_ref"], allowed_scopes=("observer",))
            recorded = json.loads(content)
            if not isinstance(recorded, dict):
                return None
            if recorded.get("result") != native or any(recorded.get(key) != identity[key] for key in keys):
                return None
            # Only these events can invalidate the evaluator anchor. Unrelated
            # terminal/log events cannot push valid evidence out of a tail limit.
            rows = store.events_by_type(("observer.evaluated", "action.requested",
                "environment.reset_requested", "environment.recovery_reset"), limit=1, newest_first=True)
            if not rows:
                return None
            event = rows[0]
            if event.get("type") != "observer.evaluated":
                return None
            if event.get("payload", {}).get("result_ref") != evaluation["result_ref"]:
                return None
            if any(event.get(key) != identity[key] for key in keys):
                return None
            return evaluation
    except (sqlite3.Error, ValueError, OSError, KeyError):
        return None
    return None


def load_effective_summary(root, evidence_root=None, *, summary=None):
    root = Path(root).resolve()
    evidence = Path(evidence_root).resolve() if evidence_root is not None else root
    original = _object(root / "summary.json", {}) if summary is None else deepcopy(summary)
    _validate_summary(original)
    if original.get("scoring_policy", {}).get("version") == SCORING_POLICY["version"]:
        return reconcile_summary(original)
    if original.get("termination_reason") not in TASK_LIMIT_REASONS:
        return _describe_failure(original)
    evaluation = _verified_evaluation(evidence, (original.get("native_episode") or {}).get("native_action_count"))
    return reconcile_summary(original, evaluation, evidence_verified=evaluation is not None)
