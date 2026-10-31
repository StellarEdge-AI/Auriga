"""Bounded phase verification from actor RGB and recorded execution evidence.

This module prepares and constrains a visual judgment. It never executes an
action, chooses a motion, or consults native task truth.
"""
import json

from ...core.contracts import IDENTITY, digest
from ...core.observation_policy import actor_robot_state, reject_privileged


PHASES = ("approach", "grasp", "short_lift", "transport", "place", "release", "retreat")
TOOL_PROPERTIES = dict(
    phase=dict(type="string", enum=list(PHASES)), target=dict(type="string", minLength=1, maxLength=1000),
    expected_result=dict(type="string", minLength=1, maxLength=1000), destination=dict(type="string", minLength=1, maxLength=1000),
    action_ids=dict(type="array", items=dict(type="string", minLength=1, maxLength=200), minItems=1, maxItems=8, uniqueItems=True),
    before_observation_id=dict(type="string"), after_observation_id=dict(type="string"), camera_id=dict(type="string"),
    roi=dict(type="array", items=dict(type="integer"), minItems=4, maxItems=4))
TOOL_REQUIRED = ("phase", "target", "expected_result", "action_ids", "before_observation_id", "after_observation_id", "camera_id", "roi")
INSTRUCTIONS = (
    "Use Sol operation_verify to verify the full operation at meaningful manipulation outcomes: approach when target alignment is "
    "uncertain, grasp and short lift before committing to transfer, the transfer outcome, and placement, release and retreat outcomes. "
    "Do not leave verification unused or substitute your own confidence or a completed command for Sol's review. "
    "You may combine adjacent caller-chosen moves in one checkpoint when their intermediate outcomes would not change your next "
    "decision; supply their actual action IDs in execution order and frame the expected visible result around that checkpoint. "
    "Skip a truly irrelevant phase with a brief reason in your task plan; choose checkpoints according to the actual operation, "
    "not a fixed phase sequence. Supply saved BEFORE/AFTER RGB, the visible target and intended visible result. "
    "Sol verifies evidence and gives feedback; you remain the action planner and choose recovery. Inspect both the model assessment "
    "and execution status: visual met does not make a failed/unknown command completed, and a completed receipt does not prove grasp "
    "or task success. On uncertainty or failure, resolve the missing evidence or choose a changed recovery; do not loop reviews on "
    "unchanged images. Budget reviews together with actor and compaction calls under the shared total model-call budget; combine "
    "useful adjacent checkpoints and prioritize unresolved outcomes instead of exhausting the budget on repeated reviews."
)
PHASE_CRITERIA = {
    "approach": "The same visible target and gripper show the caller's intended approach relation. Image proximity does not prove metric clearance or contact.",
    "grasp": "Positive visible evidence supports the target being engaged and retained with the gripper. Jaw closure alone does not establish a grasp; a separate short-lift check is useful when retention is ambiguous.",
    "short_lift": "The same visible target moves with the gripper and is visibly separated from its prior support. Image motion alone does not establish retention or metric lift height.",
    "transport": "The same visible target remains held with the gripper while changing position. No collision clearance, motion safety or metric route claim follows.",
    "place": "The same visible target reaches the described visible destination and has positive visible support evidence. Image overlap alone does not establish placement or physical stability.",
    "release": "The same visible target has positive visible separation from the gripper; if a destination is specified it remains visibly at that destination. Absence of retention evidence alone does not establish release.",
    "retreat": "The gripper visibly withdraws from the same target, which remains identifiable and visibly separated; if a destination is specified the target remains there. No metric clearance or long-term stability claim follows.",
}

ANSWER_SCHEMA = dict(type="object", additionalProperties=False, properties=dict(
    assessment=dict(type="string", enum=["met", "not_met", "uncertain"]),
    same_target_identified=dict(type="string", enum=["supported", "not_supported", "uncertain"]),
    visibility_after=dict(type="string", enum=["visible", "partly_visible", "not_visible", "uncertain"]),
    target_motion=dict(type="string", enum=["displaced", "roughly_stationary", "uncertain"]),
    gripper_relation=dict(type="string", enum=["held", "separated", "near", "uncertain"]),
    support_relation=dict(type="string", enum=["visibly_supported", "visibly_separated", "uncertain", "not_applicable"]),
    destination_relation=dict(type="string", enum=["visibly_at_destination", "visibly_not_at_destination", "uncertain", "not_applicable"]),
    gripper_withdrawal=dict(type="string", enum=["visually_supported", "not_visually_supported", "uncertain", "not_applicable"]),
    before_evidence=dict(type="string", minLength=1, maxLength=1200),
    after_evidence=dict(type="string", minLength=1, maxLength=1200),
    execution_evidence=dict(type="string", minLength=1, maxLength=1200),
    uncertainties=dict(type="array", items=dict(type="string", minLength=1, maxLength=500), maxItems=6),
    follow_up=dict(type="string", enum=["none", "inspect_fresh_rgb", "inspect_another_camera", "inspect_action_status", "return_to_actor"]),
    follow_up_reason=dict(type="string", maxLength=800),
    recovery_feedback=dict(type="string", maxLength=1200)),
    required=["assessment", "same_target_identified", "visibility_after", "target_motion", "gripper_relation",
              "support_relation", "destination_relation", "gripper_withdrawal", "before_evidence", "after_evidence",
              "execution_evidence", "uncertainties", "follow_up", "follow_up_reason", "recovery_feedback"])


def validate_request(*, phase, target, expected_result, action_ids, destination=None):
    if phase not in PHASES:
        raise ValueError("operation_verification_unknown_phase")
    for name, value in (("target", target), ("expected_result", expected_result)):
        if not isinstance(value, str) or not value.strip() or len(value) > 1000:
            raise ValueError("operation_verification_requires_" + name)
    if destination is not None and (not isinstance(destination, str) or not destination.strip() or len(destination) > 1000):
        raise ValueError("operation_verification_invalid_destination")
    if (not isinstance(action_ids, list) or not 1 <= len(action_ids) <= 8
            or any(not isinstance(value, str) or not value or len(value) > 200 for value in action_ids)
            or len(set(action_ids)) != len(action_ids)):
        raise ValueError("operation_verification_requires_one_to_eight_unique_actions")


def _state(store, observation, profile):
    reference = observation.get("robot_state_ref")
    if not reference:
        return dict(status="unavailable", reason="robot_state_not_recorded")
    info, content = store.resolve_artifact(reference, allowed_scopes=("actor",))
    if info.get("kind") != "robot_state" or info.get("mime_type") != "application/json":
        raise ValueError("operation_verification_requires_actor_robot_state")
    return dict(status="available", robot_state_ref=reference, sha256=info["sha256"],
                state=actor_robot_state(json.loads(content), profile))


def execution_inputs(store, identity, *, action_ids, before_observation_id, after_observation_id):
    """Read narrow, actor-permitted summaries with immutable record hashes.

    Missing terminal evidence remains unknown. Linking guards prevent an old or
    unrelated completed action from validating a caller's phase.
    """
    before, after = [store.read("observation", value) for value in (before_observation_id, after_observation_id)]
    blockers, actions = [], []
    if before_observation_id == after_observation_id or after.get("sim_step", 0) <= before.get("sim_step", 0):
        blockers.append("no_new_simulation_step")
    expected_before = before_observation_id
    for index, action_id in enumerate(action_ids):
        request = store.read("action_request", action_id)
        if any(request.get(key) != identity[key] for key in IDENTITY):
            raise ValueError("operation_verification_action_identity_mismatch")
        if request.get("action_id") != action_id:
            raise ValueError("operation_verification_action_id_mismatch")
        reject_privileged(request, before["observation_profile"])
        receipt_recorded = True
        try:
            receipt = store.read("action_receipt", action_id)
        except KeyError:
            receipt_recorded = False
            receipt = dict(status="unknown", reason="terminal_receipt_not_recorded", result_observation_id=None)
        reject_privileged(receipt, before["observation_profile"],
            stage_ids=[stage["id"] for stage in request["stages"]]
                if request.get("type") == "BatchAction" and receipt_recorded and receipt.get("status") != "rejected" else None)
        if (request.get("based_on_observation_id") != expected_before
                and not (index == 0 and receipt.get("execution_start_observation_id") == expected_before)):
            blockers.append("action_observation_chain_mismatch:" + action_id)
        if receipt.get("status") != "completed":
            blockers.append("execution_not_completed:" + action_id)
        if not receipt.get("result_observation_id"):
            blockers.append("result_observation_unavailable:" + action_id)
        requested = {key: request[key] for key in ("type", "path", "gripper_events", "hold_native_steps", "timeout_s", "policy_input", "policy_provenance") if key in request}
        feedback = {key: receipt[key] for key in ("status", "reason", "error", "result_observation_id", "execution_start_observation_id",
                    "observation_unavailable_reason", "executed_steps", "executed_control_steps", "executed_native_steps",
                    "completed_native_steps", "execution_interval", "step_semantics", "position_error_m", "orientation_error_rad",
                    "failed_waypoint_index", "tracking_guard", "stop_confirmation") if key in receipt}
        actions.append(dict(action_id=action_id, request_sha256=digest(request), receipt_recorded=receipt_recorded,
                            receipt_sha256=digest(receipt) if receipt_recorded else None,
                            based_on_observation_id=request.get("based_on_observation_id"),
                            requested=requested, feedback=feedback))
        expected_before = receipt.get("result_observation_id")
    if expected_before != after_observation_id:
        blockers.append("final_action_result_observation_mismatch")
    statuses = [item["feedback"].get("status") for item in actions]
    status = ("unknown" if any(value in (None, "unknown", "status_unknown", "accepted", "running") for value in statuses)
              else "completed" if all(value == "completed" for value in statuses) else "incomplete")
    result = dict(source_kind="recorded_action_execution", status=status, blockers=blockers, actions=actions,
                  before_robot_state=_state(store, before, before["observation_profile"]),
                  after_robot_state=_state(store, after, after["observation_profile"]),
                  limitations="Requested paths and gripper goals are commands. Measured robot state and completed receipts do not prove object contact, retention, release or task success.")
    reject_privileged(result, before["observation_profile"])
    return result


def constrain_answer(answer, *, phase, execution, destination=None):
    """Prevent unlinked evidence from validating the reviewed operation.

    Visible phase relations are model judgments, not an encoded physical test or
    a compulsory sequence. Only provenance and execution evidence constrain a
    positive assessment here; the actor decides what to do with the feedback.
    A failed or stopped command can still have a visibly achieved local result.
    Its execution status stays separate and is never upgraded by visual judgment.
    """
    answer = {**answer, "uncertainties": list(answer["uncertainties"])}
    blockers = [value for value in execution["blockers"] if value == "no_new_simulation_step"
                or value == "final_action_result_observation_mismatch" or value.startswith("action_observation_chain_mismatch:")]
    if answer["assessment"] == "met":
        if blockers:
            answer["assessment"] = "uncertain"
            if answer["follow_up"] == "none":
                answer.update(follow_up="return_to_actor", follow_up_reason="The selected images are not confirmed as a fresh linked action result.")
    return answer, blockers
