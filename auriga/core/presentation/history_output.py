"""Read-only history presentation with explicit access to original evidence."""

from copy import deepcopy
import json

from .actor_payload import lean_action_result
from ..tool_output import resolve_detail, select_fields, validate_page


def _detail(arguments, correlation):
    selectors = any(key in arguments for key in ("start", "limit", "path"))
    return resolve_detail(arguments.get("detail"), program=bool(correlation.get("program_id")) and not selectors)


def action_status(harness, arguments, correlation):
    """A status read retains the submitted request and every receipt diagnostic."""
    result = harness.action(arguments["action_id"])
    if _detail(arguments, correlation) == "full":
        return result
    projected = lean_action_result(result)
    projected["full_result"] = dict(tool="robot_action_status", action_id=arguments["action_id"], detail="full")
    return projected


_INDEX_RECEIPT_FIELDS = (
    "status", "reason", "error", "accepted_at", "started_at", "finished_at",
    "executed_steps", "executed_native_steps", "completed_native_steps", "executed_control_steps",
    "step_semantics", "execution_interval", "execution_start_observation_id", "result_observation_id",
    "observation_unavailable_reason", "stop_confirmation", "stop_confirmed", "episode_terminal",
    "failed_waypoint_index", "failed_stage_id", "failed_arm", "failed_arms", "failed_phase",
    "planner_failure_stage", "position_error_m", "orientation_error_rad",
)


def list_actions(harness, arguments, correlation):
    """Return a paged status index, or the original full list on explicit request."""
    if "anchor" in arguments:
        return interaction_blocks(harness, arguments)
    detail = _detail(arguments, correlation)
    requests = harness.store.records("action_request")
    if detail == "full":
        if any(key in arguments for key in ("start", "limit")):
            raise ValueError("full_action_history_cannot_be_paged")
        return [harness.action(request["action_id"]) for request in requests]
    start, limit = validate_page(arguments.get("start"), arguments.get("limit"))
    actions = []
    for request in requests[start:start + limit]:
        result = harness.action(request["action_id"])
        receipt = result.get("receipt", {})
        entry = select_fields(request, ("action_id", "based_on_observation_id", "created_at"))
        entry.update(select_fields(receipt, _INDEX_RECEIPT_FIELDS))
        for key in ("failure_feedback", "motion_safety", "waypoint_results"):
            if key in receipt:
                entry[key + "_available"] = receipt[key] is not None
        entry["full_result"] = dict(tool="robot_action_status", action_id=request["action_id"], detail="full")
        actions.append(entry)
    end = min(start + limit, len(requests))
    return dict(detail="summary", actions=actions, total=len(requests), start=start, limit=limit,
                next_start=end if end < len(requests) else None,
                order="recorded_action_request_order",
                full_result=dict(tool="history_list_actions", detail="full"))


def _gripper_commands(request):
    """Index command locations, including unexecuted stages; never infer grasp."""
    locations = ["/gripper_events"] if request.get("gripper_events") else []
    stages = request.get("stages")
    for index, stage in enumerate(stages if isinstance(stages, list) else []):
        if (isinstance(stage, dict) and isinstance(stage.get("action"), dict)
                and stage["action"].get("gripper_events")):
            locations.append(f"/stages/{index}/action/gripper_events")
    queues = request.get("arm_queues")
    for arm, stages in queues.items() if isinstance(queues, dict) else []:
        for index, stage in enumerate(stages if isinstance(stages, list) else []):
            if isinstance(stage, dict) and (stage.get("kind") == "gripper" or stage.get("gripper_goal_normalized") is not None):
                locations.append(f"/arm_queues/{arm}/{index}")
    return locations


def _interaction_observation(harness, observation_id):
    from ..contracts import IDENTITY
    from .observation_output import saved_observation_state
    from ..observation_policy import require_actor_observation

    try:
        observation = harness.store.read("observation", observation_id)
        if any(observation.get(key) != harness.identity[key] for key in IDENTITY):
            raise ValueError("observation_outside_current_episode")
        require_actor_observation(observation, harness.observation_profile)
    except (KeyError, TypeError, ValueError):
        return dict(observation_id=observation_id, status="unavailable",
                    reason="observation_not_permitted_in_current_episode")
    result = saved_observation_state(harness.store, observation_id)
    result["observation_is_current"] = observation_id == (harness.latest or {}).get("observation_id")
    result["image_lookup"] = dict(tool="history_view_observation", observation_id=observation_id)
    return result


def _measured_state_delta(before_obs, after_obs):
    """计算执行起点与结果观测之间的实测末端与夹爪位移增量，不做持物或任务成功推断。"""
    import math
    if not (isinstance(before_obs, dict) and isinstance(after_obs, dict)
            and before_obs.get("status") == "available" and after_obs.get("status") == "available"):
        return None
    arms_before = (before_obs.get("robot_state") or {}).get("arms") or {}
    arms_after = (after_obs.get("robot_state") or {}).get("arms") or {}
    deltas = {}
    for arm in ("left", "right"):
        ab, aa = arms_before.get(arm), arms_after.get(arm)
        if not (isinstance(ab, dict) and isinstance(aa, dict)):
            continue
        arm_delta = {}
        pos_b = (ab.get("ee_pose") or {}).get("position_m")
        pos_a = (aa.get("ee_pose") or {}).get("position_m")
        if isinstance(pos_b, list) and isinstance(pos_a, list) and len(pos_b) == 3 and len(pos_a) == 3:
            arm_delta["ee_position_delta_m"] = [round(float(b) - float(a), 6) for a, b in zip(pos_b, pos_a)]
            arm_delta["ee_step_distance_m"] = round(float(math.dist(pos_b, pos_a)), 6)
        grip_b = (ab.get("gripper") or {}).get("position_m")
        grip_a = (aa.get("gripper") or {}).get("position_m")
        if isinstance(grip_b, list) and isinstance(grip_a, list) and len(grip_b) == len(grip_a) and grip_b:
            arm_delta["gripper_position_delta_m"] = [round(float(b) - float(a), 6) for a, b in zip(grip_b, grip_a)]
        if arm_delta:
            deltas[arm] = arm_delta
    return deltas or None


def _interaction_overview(actions, selected_total, episode_total, next_start):
    """汇总当前返回页与本局交互锚点的结构化只读计数。"""
    status_counts = {}
    for block in actions:
        status = (block.get("receipt") or {}).get("status", "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    return dict(
        returned_count=len(actions),
        selected_count=selected_total,
        episode_action_total=episode_total,
        status_counts=status_counts,
        execution_issue_count=sum(1 for b in actions if "execution_issue" in b.get("anchor_reasons", ())),
        gripper_command_count=sum(1 for b in actions if "gripper_command" in b.get("anchor_reasons", ())),
        evidence_gap_count=sum(1 for b in actions if b.get("evidence_gaps")),
        next_start=next_start,
    )


def interaction_blocks(harness, arguments):
    """Join current-episode action evidence at explicit, reproducible anchors.

    No additional capture or inferred boundary: decision, execution start and
    result may be different observations. An absent result stays absent.
    """
    from ..contracts import IDENTITY
    from .feedback_delivery import execution_unconfirmed

    anchor = arguments["anchor"]
    if anchor not in ("events", "failures", "gripper_commands", "all"):
        raise ValueError("invalid_history_anchor")
    if arguments.get("detail") == "full":
        raise ValueError("interaction_history_requires_summary")
    start, limit = validate_page(arguments.get("start"), arguments.get("limit", 5))
    requests = [request for request in harness.store.records("action_request")
                if all(request.get(key) == harness.identity[key] for key in IDENTITY)]
    selected = []
    for index, request in enumerate(requests):
        result = harness.action(request["action_id"])
        receipt = result.get("receipt") or {"status": "unknown"}
        reasons = []
        stages = receipt.get("stage_results")
        if (receipt.get("status") != "completed" or execution_unconfirmed(receipt)
                or (isinstance(stages, dict) and any(not isinstance(stage, dict)
                    or stage.get("status") != "completed" for stage in stages.values()))):
            reasons.append("execution_issue")
        commands = _gripper_commands(request)
        if commands:
            reasons.append("gripper_command")
        if index == 0:
            reasons.append("first_action")
        if index == len(requests) - 1:
            reasons.append("latest_action")
        if (anchor == "all" or (anchor == "events" and reasons)
                or (anchor == "failures" and "execution_issue" in reasons)
                or (anchor == "gripper_commands" and commands)):
            selected.append((index, result, receipt, reasons, commands))

    if arguments.get("start") is None and anchor in ("failures", "gripper_commands"):
        start = max(0, len(selected) - limit)
    actions, observations = [], {}
    previous_index = -1
    for index, result, receipt, reasons, commands in selected[start:start + limit]:
        # The tool call already carries the command. Keep only identity in the
        # echoed request and retain typed receipt diagnostics; full evidence is
        # still addressable through the action lookup and history trace tools.
        compact = dict(result)
        compact["receipt"] = receipt
        block = lean_action_result(compact)
        block["request"] = select_fields(block.get("request", {}),
                                          ("action_id", "based_on_observation_id"))
        block.update(episode_action_index=index, anchor_reasons=reasons,
                     gripper_command_locations=commands,
                     omitted_actions_before=index - previous_index - 1)
        previous_index = index
        boundaries = dict(decision=result["request"].get("based_on_observation_id"),
                          execution_start=receipt.get("execution_start_observation_id"),
                          result=receipt.get("result_observation_id"))
        block["observation_ids"] = boundaries
        block["evidence_gaps"] = []
        for boundary, observation_id in boundaries.items():
            if not observation_id:
                block["evidence_gaps"].append(boundary + "_observation_not_recorded")
                continue
            if observation_id not in observations:
                observations[observation_id] = _interaction_observation(harness, observation_id)
            if observations[observation_id]["status"] != "available":
                block["evidence_gaps"].append(boundary + "_state_unavailable")
        block["measured_delta"] = _measured_state_delta(
            observations.get(boundaries["execution_start"]),
            observations.get(boundaries["result"]))
        block["full_result"] = dict(tool="robot_action_status", action_id=result["request"]["action_id"], detail="full")
        actions.append(block)
    end = min(start + limit, len(selected))
    next_start = end if end < len(selected) else None
    return dict(detail="summary", anchor=anchor, scope="current_episode",
                overview=_interaction_overview(actions, len(selected), len(requests), next_start),
                actions=actions, observations=observations, total=len(selected),
                episode_action_total=len(requests), start=start, limit=limit,
                next_start=next_start, omitted_actions_after=len(requests) - previous_index - 1,
                order="recorded_action_request_order",
                semantics="Each block preserves request and receipt. Observation IDs distinguish decision, execution start and result; missing boundaries are not filled from later observations. Shared observations appear once. Omitted counts cover unreturned requests on this page, not successful or continuous execution. Gripper commands, measured finger positions and completed motion do not establish object retention or task success.")


def _pointer(value, path):
    if not isinstance(path, str) or (path and not path.startswith("/")):
        raise ValueError("invalid_trace_path")
    for token in path.split("/")[1:] if path else ():
        # JSON Pointer only admits ~0 and ~1 escape sequences.
        index = 0
        while index < len(token):
            if token[index] == "~":
                if index + 1 == len(token) or token[index + 1] not in "01":
                    raise ValueError("invalid_trace_path")
                index += 1
            index += 1
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and token in value:
            value = value[token]
        elif isinstance(value, list) and token.isascii() and token.isdecimal() and str(int(token)) == token:
            offset = int(token)
            if offset >= len(value):
                raise ValueError("trace_path_not_found")
            value = value[offset]
        else:
            raise ValueError("trace_path_not_found")
    return value


def _child_path(path, key):
    return path + "/" + str(key).replace("~", "~0").replace("/", "~1")


def read_trace(harness, arguments, correlation):
    """Window saved JSON sequences while preserving exact values and ordering."""
    detail = _detail(arguments, correlation)
    kind = arguments["kind"]
    if kind not in ("planned_trace", "dispatched_trace", "actual_trace", "before_state"):
        raise ValueError("invalid_trace_kind")
    selectors = any(key in arguments for key in ("path", "start", "limit"))
    if detail == "full" and selectors:
        raise ValueError("full_trace_cannot_have_page_selectors")
    reference = harness.action(arguments["action_id"])["receipt"].get(kind + "_ref")
    if reference is None:
        return dict(status="unavailable", reason="trace_not_recorded")
    _, raw = harness.store.resolve_artifact(reference, allowed_scopes=("actor", "internal"))
    value = json.loads(raw)
    if detail == "full":
        return value
    start, limit = validate_page(arguments.get("start"), arguments.get("limit"))
    path = arguments.get("path", "")
    selected = _pointer(value, path)
    windows = []
    lookup = dict(tool="history_read_trace", action_id=arguments["action_id"], kind=kind)

    def page_sequence(node, current_path, offset=0, force=False):
        if len(node) > limit or offset or force:
            end = min(offset + limit, len(node))
            windows.append(dict(path=current_path, total=len(node), start=offset, limit=limit,
                                next_start=end if end < len(node) else None,
                                retrieval={**lookup, "detail": "summary", "path": current_path}))
            # A page contains COMPLETE samples. Applying limit recursively here
            # would silently turn q6/xyz3/quaternion4 into malformed vectors.
            return deepcopy(node[offset:offset + limit])
        return deepcopy(node)

    def known_sequences(node, current_path):
        if not isinstance(node, dict):
            return deepcopy(node)
        projected = {}
        for key, item in node.items():
            child_path = _child_path(current_path, key)
            if key == "segments" and isinstance(item, list):
                if len(item) > limit:
                    projected[key] = page_sequence(item, child_path)
                else:
                    projected[key] = [known_sequences(segment, _child_path(child_path, i))
                                      for i, segment in enumerate(item)]
            elif isinstance(item, list) and (key in ("samples", "actions", "feedback_samples")
                    or (key == "q_rad" and item and all(isinstance(row, list) for row in item))):
                projected[key] = page_sequence(item, child_path)
            elif isinstance(item, dict):
                projected[key] = known_sequences(item, child_path)
            else:
                # Unknown arrays, joint vectors and matrices are not assumed to
                # be time series. An explicit path may select an array itself.
                projected[key] = deepcopy(item)
        return projected

    if "start" in arguments and not isinstance(selected, list):
        raise ValueError("trace_page_requires_sequence_path")
    projected = (page_sequence(selected, path, start, force=selectors) if isinstance(selected, list)
                 else known_sequences(selected, path))
    if not windows and not selectors:
        return projected
    return dict(action_id=arguments["action_id"], kind=kind, detail="summary", trace=projected,
                path=path, windows=windows, trace_ref=reference,
                full_result={**lookup, "detail": "full"})
