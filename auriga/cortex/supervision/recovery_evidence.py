"""Read-only recovery evidence from public previews and delivered RGB metadata.

No evaluator records, scene-object state, images or robot calls are read here.
Candidate costs reject only that nominal candidate, never the whole task.
"""

import hashlib
import json

from .actor_stall import recovery_fingerprints, recovery_request


VERSION = "recovery-evidence-v1"


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _budget_rejected(candidate, remaining):
    """Trust only a current, typed episode-budget rejection, not another failure."""
    cost = candidate.get("nominal_native_steps")
    if type(cost) is not int or cost < 0 or cost <= remaining:
        return False
    if candidate.get("status") == "planned" and candidate.get("feasible") is True:
        return True
    if (candidate.get("status") != "rejected" or candidate.get("feasible") is not False
            or candidate.get("reason") != "motion_safety_rejected"):
        return False
    safety = candidate.get("motion_safety")
    arms = safety.get("arms") if isinstance(safety, dict) else None
    if not isinstance(arms, dict) or safety.get("status") != "reject":
        return False
    for arm in arms.values():
        exceeded = arm.get("exceeded_budgets") if isinstance(arm, dict) else None
        if (isinstance(arm, dict) and arm.get("status") == "reject"
                and "insufficient_remaining_native_budget" in arm.get("reasons", [])
                and type(arm.get("episode_remaining_native_steps")) is int
                and arm["episode_remaining_native_steps"] == remaining
                and type(arm.get("nominal_native_steps")) is int
                and arm["nominal_native_steps"] == cost
                and isinstance(exceeded, dict) and type(exceeded.get("episode")) is int
                and exceeded["episode"] == remaining):
            return True
    return False


def _batch_budget_rejected(payload, remaining):
    if payload.get("reason") != "motion_safety_rejected":
        return False
    cost = payload.get("planned_native_steps")
    for check in payload.get("motion_safety_checks", []):
        safety = check.get("arms") if isinstance(check, dict) else None
        if not isinstance(safety, dict) or check.get("enabled") is not True or check.get("status") != "reject":
            continue
        if _budget_rejected(dict(status="rejected", feasible=False, reason="motion_safety_rejected",
                nominal_native_steps=cost, motion_safety=dict(status="reject", arms=safety)), remaining):
            return True
    return False


def completed_turn_evidence(store, *, thread_id, turn_id, episode_id,
                            native_action_count, native_steps_remaining=None):
    """Return bounded credit and a candidate ledger, scoped to this completed turn.

    Missing or stale preview results never cancel normal investigation credit.
    SQL selects the public preview event type before decoding: evaluator events
    are not scanned into Python. Delivery metadata verifies actual RGB delivery.
    """
    context = dict(thread_id=thread_id, turn_id=turn_id, episode_id=episode_id)
    observations = {value["observation_id"]: value for value in store.records("observation")
                    if value.get("episode_id") == episode_id and value.get("observation_id")}
    calls, activity = {}, set()
    for record in store.records("tool_call"):
        correlation = record.get("correlation", {})
        if any(correlation.get(key) != context[key] for key in ("thread_id", "turn_id")):
            continue
        params = record.get("params", {})
        arguments, plans = recovery_request(store, params.get("tool"), params.get("arguments"))
        params = {**params, "arguments": arguments}
        calls[(correlation.get("program_id"), correlation.get("tool_call_id"))] = params
        activity.update(recovery_fingerprints(params.get("tool"), arguments,
            observations=observations, episode_id=episode_id, plans=plans))
    result = dict(version=VERSION, status="available", activity=sorted(activity), candidates=[],
                  budget_rejected_count=0, delivered_rgb_count=0,
                  native_action_count=native_action_count,
                  native_steps_remaining=native_steps_remaining)
    candidate_results = set()
    budget_rejected_ids = set()
    try:
        events = store.turn_preview_events(thread_id=thread_id, turn_id=turn_id, episode_id=episode_id)
    except (AttributeError, TypeError):
        result["status"] = "unavailable"
        return result
    for event in events:
        payload = event.get("payload", {})
        correlation = event.get("correlation", {})
        call_key = (correlation.get("program_id"), correlation.get("tool_call_id"))
        if event.get("type") == "program.call_started":
            if all(isinstance(value, str) and value for value in call_key):
                arguments, _ = recovery_request(store, payload.get("tool"), payload.get("arguments"))
                calls[call_key] = {**payload, "arguments": arguments}
            continue
        if event.get("type") == "action.batch_preview":
            params = calls.get(call_key, {})
            arguments = params.get("arguments") or {}
            budget = payload.get("action_budget", {})
            observation = observations.get(payload.get("observation_id"), {})
            status = payload.get("status")
            failed = status == "failed"
            binding = payload.get("state_binding")
            state_bound = (isinstance(binding, dict)
                and binding.get("observation_id") == payload.get("observation_id")
                and isinstance(binding.get("robot_state_ref"), str)
                and binding.get("robot_state_ref") == observation.get("robot_state_ref")
                and binding.get("sim_step") == observation.get("sim_step")
                and binding.get("step_domain") == observation.get("step_domain"))
            if (params.get("tool") != "robot_action" or not isinstance(arguments, dict)
                    or arguments.get("mode", "preview") not in ("preview", "direct")
                    or arguments.get("observation_id") != payload.get("observation_id")
                    or status not in ("planned", "failed") or payload.get("physics_advanced") is not False
                    or type(payload.get("executed_native_steps")) is not int or payload["executed_native_steps"] != 0
                    or not observation or type(native_action_count) is not int or native_action_count < 0
                    or type(budget.get("native_steps_used")) is not int
                    or budget["native_steps_used"] != native_action_count
                    or not (payload.get("status") == "planned" or (failed
                        and payload.get("zero_step_check_passed") is True and state_bound))):
                continue
            fingerprints = recovery_fingerprints("robot_action", arguments,
                observations=observations, episode_id=episode_id)
            budget_known = (type(native_steps_remaining) is int and native_steps_remaining >= 0
                and type(budget.get("native_steps_remaining")) is int
                and budget["native_steps_remaining"] == native_steps_remaining)
            cost = payload.get("planned_native_steps")
            known_cost = type(cost) is int and cost >= 0
            rejected = (budget_known and known_cost and cost > native_steps_remaining
                and (status == "planned" or _batch_budget_rejected(payload, native_steps_remaining)))
            candidate_id = _digest(sorted(fingerprints))
            state_reference = observation.get("robot_state_ref")
            evidence = dict(candidate_id=candidate_id,
                label=str(payload.get("plan_id", "batch"))[:80], observation_id=payload.get("observation_id"),
                measured_state_sha256=_digest(state_reference) if isinstance(state_reference, str) else None,
                native_action_count=native_action_count,
                native_steps_remaining=native_steps_remaining if budget_known else None,
                nominal_native_steps=cost if known_cost else None,
                status=status,
                feasible=True if status == "planned" else False if payload.get("reason") in
                    ("path_infeasible", "motion_safety_rejected") else None,
                reason=payload.get("reason"), exceeds_episode_budget=rejected,
                cost_scope="complete_submitted_batch_excluding_optional_settle",
                trajectory_available=isinstance(payload.get("trajectory_ref"), str)
                    and payload["trajectory_ref"].startswith("artifact:"))
            result_id = _digest({key: value for key, value in evidence.items()
                                 if key not in ("label", "observation_id")})
            if (candidate_id, result_id) in candidate_results:
                activity.difference_update(fingerprints)
                continue
            candidate_results.add((candidate_id, result_id))
            result["candidates"].append(evidence)
            activity.difference_update(fingerprints)
            if rejected:
                budget_rejected_ids.add(candidate_id)
            else:
                activity.add("preview_result:" + _digest({key: value for key, value in evidence.items()
                    if key not in ("label", "observation_id")}))
            continue
        binding, budget = payload.get("state_binding"), payload.get("action_budget")
        # Unknown and mismatched accounting is not evidence of budget failure.
        if (not isinstance(binding, dict) or not isinstance(budget, dict)
                or type(native_action_count) is not int or native_action_count < 0
                or type(binding.get("native_action_count")) is not int
                or binding.get("native_action_count") != native_action_count
                or payload.get("status") != "completed"
                or payload.get("same_measured_state_verified") is not True
                or payload.get("zero_step_check_passed") is not True
                or payload.get("state_binding_after") != binding):
            continue
        params = calls.get(call_key, {})
        arguments = params.get("arguments") or {}
        request_candidates = (arguments.get("candidates") or []) if params.get("tool") == "robot_preview_cost" else []
        by_label = {value.get("label"): value for value in request_candidates if isinstance(value, dict)}
        budget_known = (type(native_steps_remaining) is int and native_steps_remaining >= 0
                        and type(budget.get("native_steps_remaining")) is int
                        and budget["native_steps_remaining"] == native_steps_remaining
                        and type(budget.get("native_steps_used")) is int
                        and budget.get("native_steps_used") == native_action_count)
        if (not isinstance(payload.get("observation_id"), str)
                or payload.get("observation_id") not in observations
                or arguments.get("observation_id") != payload.get("observation_id")):
            continue
        for candidate in payload.get("candidates", []):
            if not isinstance(candidate, dict):
                continue
            request = by_label.get(candidate.get("label"))
            # Native/program previews without a matching public request retain
            # their original bounded credit; generated action IDs are not novel.
            if request is None:
                continue
            candidate_activity = recovery_fingerprints("robot_preview_cost",
                {**arguments, "candidates": [request]}, observations=observations, episode_id=episode_id)
            candidate_id = _digest(sorted(recovery_fingerprints("robot_preview_cost",
                {"candidates": [request]}, observations=observations, episode_id=episode_id)))
            cost = candidate.get("nominal_native_steps")
            known_cost = type(cost) is int and cost >= 0
            rejected = (budget_known and _budget_rejected(candidate, native_steps_remaining))
            evidence = dict(candidate_id=candidate_id,
                label=str(candidate.get("label", ""))[:80],
                observation_id=payload.get("observation_id"),
                measured_state_sha256=binding.get("sha256"),
                native_action_count=native_action_count,
                native_steps_remaining=native_steps_remaining if budget_known else None,
                nominal_native_steps=cost if known_cost else None,
                status=candidate.get("status"), feasible=candidate.get("feasible"),
                reason=candidate.get("reason"), exceeds_episode_budget=rejected,
                trajectory_available=isinstance(candidate.get("trajectory_ref"), str)
                    and candidate["trajectory_ref"].startswith("artifact:"))
            result_id = _digest({key: value for key, value in evidence.items()
                                 if key not in ("label", "observation_id")})
            if (candidate_id, result_id) in candidate_results:
                activity.difference_update(candidate_activity)
                continue
            candidate_results.add((candidate_id, result_id))
            result["candidates"].append(evidence)
            activity.difference_update(candidate_activity)
            if rejected:
                budget_rejected_ids.add(candidate_id)
            else:
                # Same request can expose a genuinely changed feasible result;
                # labels, elapsed planning time and observation UUID churn cannot.
                outcome = {key: value for key, value in evidence.items()
                           if key not in ("label", "observation_id")}
                activity.add("preview_result:" + _digest(outcome))
    result["budget_rejected_count"] = len(budget_rejected_ids)
    for record in store.records("delivery"):
        correlation = record.get("correlation", {})
        observation = observations.get(record.get("observation_id"), {})
        if (any(correlation.get(key) != context[key] for key in ("thread_id", "turn_id"))
                or record.get("delivered_to_actor") is not True or not observation):
            continue
        for frame in record.get("images", []):
            sha = frame.get("source_sha256")
            if isinstance(sha, str) and len(sha) == 64 and frame.get("camera_id"):
                activity.add("delivered_rgb:" + _digest([frame["camera_id"], sha]))
                result["delivered_rgb_count"] += 1
    result["activity"] = sorted(activity)
    return result
