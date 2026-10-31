"""Derived, non-blocking reminders for repeated unsuccessful action mechanisms.

The helper reads stored action requests and terminal receipts. It never changes a
receipt, infers object state, or treats a command failure as task failure.
"""
from ...core.contracts import IDENTITY

_TERMINAL_EVENTS = (
    "action.completed", "action.failed", "action.partial", "action.stopped", "action.cancelled",
    "action.rejected", "action.status_unknown",
)
_FAILURE_EVENTS = frozenset((
    "action.failed", "action.partial", "action.rejected", "action.status_unknown", "action.stopped",
))
_NORMAL_STOP_REASONS = frozenset(("observation_checkpoint", "episode_ended"))
CHECKPOINT_TRIGGER_COUNT = 2


def _stage_family(stage):
    if not isinstance(stage, dict):
        return ("unknown", "unknown", "unknown", "unknown", 0)
    action = stage.get("action") if isinstance(stage.get("action"), dict) else stage
    path = action.get("path") if isinstance(action.get("path"), dict) else {}
    pose = ("grasp" if "grasp_position_m" in path or "angles_deg" in path
            else "link6" if "position_m" in path or "quaternion_xyzw" in path
            else "other")
    contact = ("region:" + str(path.get("contact_region"))
               if path.get("contact_region") is not None
               else "depth" if path.get("contact_depth_m") is not None else "none")
    route = len(path.get("waypoints", [])) if isinstance(path.get("waypoints"), list) else 0
    jaw = "jaw" if "goal_normalized" in path or "gripper_goal_normalized" in path else "no_jaw"
    return (str(stage.get("arm", action.get("arm", "unknown"))),
            str(stage.get("kind", action.get("type", "unknown"))), pose, contact, route, jaw)


def mechanism_family(action):
    """Return an internal family key without exposing IDs or coordinates."""
    if not isinstance(action, dict):
        return None
    stages = action.get("stages")
    if isinstance(stages, list):
        return tuple(_stage_family(stage) for stage in stages)
    path = action.get("path")
    if isinstance(path, dict):
        return (_stage_family({"action": action}),)
    return (str(action.get("type", "unknown")),)


def _identity_matches(value, identity):
    return isinstance(value, dict) and all(value.get(key) == identity.get(key) for key in IDENTITY)


def _terminal_action_records(store, identity):
    requested = {}
    # The checkpoint is about the current tail of an episode. Reading the
    # oldest bounded window made it silently stop working after long runs.
    for event in store.events_by_type(("action.requested",), limit=1000, newest_first=True):
        action = event.get("payload", {}).get("request")
        if not _identity_matches(action, identity):
            continue
        action_id = action.get("action_id") if isinstance(action, dict) else None
        if isinstance(action_id, str):
            requested[action_id] = action
    terminal = {}
    for event in store.events_by_type(_TERMINAL_EVENTS, limit=1000, newest_first=True):
        action_id = event.get("correlation", {}).get("action_id")
        if action_id in requested:
            terminal[action_id] = (event.get("server_seq", 0), event.get("type"),
                                   requested[action_id], event.get("payload", {}))
    return sorted(terminal.values(), key=lambda item: item[0])


def _counts_as_failure(event_type, receipt):
    """Keep normal observation and episode stops out of failure repetition."""
    if event_type not in _FAILURE_EVENTS:
        return False
    return not (event_type == "action.stopped"
                and receipt.get("reason") in _NORMAL_STOP_REASONS)


def adaptive_action_checkpoint(store, identity):
    """Return a short actor-facing reminder after repeated failed families.

    Only failed/partial/rejected/unknown actions and abnormal stops count. A
    completed action, normal observation checkpoint, episode stop, cancellation,
    or different mechanism clears the reminder. IDs, hashes and evaluator data
    never enter the returned display text.
    """
    records = _terminal_action_records(store, identity)
    if len(records) < 2 or not _counts_as_failure(records[-1][1], records[-1][3]):
        return None
    latest_family = mechanism_family(records[-1][2])
    if latest_family is None:
        return None
    count = 0
    for _, event_type, action, receipt in reversed(records):
        if (not _counts_as_failure(event_type, receipt)
                or mechanism_family(action) != latest_family):
            break
        count += 1
    if count < CHECKPOINT_TRIGGER_COUNT:
        return None
    status = records[-1][3].get("status")
    reason = records[-1][3].get("reason")
    result = dict(
        repeated_failed_mechanism_count=count,
    )
    if isinstance(status, str):
        result["last_outcome"] = status
    if isinstance(reason, str) and reason and len(reason) <= 240:
        result["last_reason"] = reason
    return result


def checkpoint_triggered(checkpoint):
    """Whether an advisory should be delivered for this failure edge.

    The detector keeps returning the current count for diagnostics, but the
    action feedback adds a reminder only at the threshold; the request hook
    does not inject a second copy.
    """
    return (isinstance(checkpoint, dict)
            and checkpoint.get("repeated_failed_mechanism_count") == CHECKPOINT_TRIGGER_COUNT)
