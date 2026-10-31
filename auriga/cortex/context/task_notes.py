"""Versioned operation guidance; injected once, never an action or online memory."""

LEGACY_TASK_NOTES_VERSION = "operation-notes-v11"
LEGACY_TASK_NOTES_SOURCE = "auriga:operation-notes-v11 (docs/development/task-notes.md)"
PREVIOUS_TASK_NOTES_VERSION = "operation-notes-v14"
PREVIOUS_TASK_NOTES_SOURCE = "auriga:operation-notes-v14 (docs/development/task-notes.md)"
TASK_NOTES_VERSION = "operation-notes-v16"
TASK_NOTES_SOURCE = "auriga:operation-notes-v16 (docs/development/task-notes.md)"
MAX_CUSTOM_TASK_NOTES = 8000

GRASP_APPROACH_GUIDANCE = (
    "Compare grasp directions relative to the object's own shape and resting orientation: "
    "across, along or oblique to a suitable section, not only world-horizontal or world-vertical. "
    "Include an inclined gripper posture among the first candidates when it improves stable "
    "engagement, access and eventual placement; there is no fixed default tilt. With equally "
    "stable grasps and sufficient reach/overhead clearance, prefer a top-down approach and "
    "initial upward lift to reduce disturbance to neighbors. Use world-horizontal side approaches "
    "cautiously: fingers, palm, wrist and forearm may sweep a broad horizontal area, including "
    "during turns. Grasp stability takes priority; weigh shape, balance, slip, contact section, "
    "whole-arm clearance and placement together. Neither vertical nor inclined guarantees "
    "clearance; do not rotate a securely held object in clutter merely to follow a preference. "
    "Tool positions and angles still use their documented coordinate conventions."
)

ROUTE_PLANNING_GUIDANCE = (
    "Choose the arm by the entire approach, lift, transport, placement and exit route, not the "
    "object's side alone; opposite-side or diagonal access can avoid an occluded or blocked "
    "same-side route. Check the stationary peer arm too, and clear it before the moving arm "
    "enters a shared corridor; retain simultaneous movement where both swept volumes permit. "
    "Plan placement, separation, withdrawal and turning together, including fingers, palm, "
    "wrist, forearm and held-object sweep. When reachable, overhead space is clear and the "
    "remaining budget permits, prefer a sufficiently high clearance corridor before crossing "
    "clutter and rise clear after separation before a large lateral withdrawal or turn. "
    "Do not automatically use the absolute highest point or release/drop from transit height: "
    "lower slowly to the intended supported placement. Container walls, rims and folded/open "
    "flaps have volume; clear them with the object's bottom and the whole gripper, then allow "
    "exit space. If access remains poor, consider first repositioning or reorienting the "
    "container when that preserves its contents and task constraints. After a substantial "
    "disturbance, re-estimate the scene and full route, not just the landing coordinate."
)

TASK_SCHEDULING_GUIDANCE = (
    "Task instruction lists and plan array order do not impose execution order. Add a dependency "
    "only for an explicit ordering requirement, physical prerequisite or needed observation, and "
    "briefly identify that reason. Choose attainable progress from dependencies, difficulty, "
    "whole-operation budget and both arms' commitments; easy-first is conditional. After failure, "
    "compare a recovery that changes the suspected failure mechanism with an independent subtask, "
    "not another near-identical attempt. Recheck current RGB: if one arm already holds an object, "
    "consider delivering it while the peer recovers, or use observed stable support if useful; "
    "do not release without support. Overlap useful independent delivery or preparation where "
    "both swept routes are clear; unequal routes can use independent progression without waiting "
    "at every paired waypoint. Clear shared corridors before crossing. Before switching targets, "
    "check withdrawal and the next approach against already placed objects and their supports. "
    "After disturbance, update the route before resuming; do not sacrifice verified progress for "
    "an unconfirmed posture correction. A safe idle arm need not return home between stages. "
    "Record only material "
    "changes, optionally with task_plan scheduling(choice_reason, independent_work, held_or_busy_arms, "
    "reconsider_when); no per-action plan update or fixed arm preference is required."
)

OPERATION_CANDIDATE_GUIDANCE = (
    "For a difficult or revised manipulation, compare complete operation candidates, not just "
    "a cheap reachable entry: entry, contact, transport, placement, separation, withdrawal, "
    "verification and any required return. Choose the object contact section, object-relative "
    "grasp direction and finger region together with the intended release/exit. Account for "
    "held-object, fingers (including opening width), palm, wrist, forearm and peer sweep, plus "
    "container walls/flaps and existing work. Opening the jaw does not establish separation. "
    "If a high route is unreachable, compare another corridor, orientation, arm or supported "
    "regrasp; merely lowering the same crossing can hit the obstacle. Separate a large turn from "
    "cluttered lowering when this creates a clear route, and inspect after a support-changing "
    "phase. Optionally preserve a concise operation_context.candidate with from_observation_id "
    "matching its evidence, contact_section, object_relative_grasp, swept_clearance and phases "
    "(each route/native_steps), plus reserve_native_steps. Omitted phases and 'unknown' costs "
    "remain unknown, never zero; use zero only for a phase known unnecessary. Re-estimate after "
    "disturbance. This is actor judgment, not collision certification, a mandatory form or "
    "automatic concatenation across observation-dependent phases."
)


def handoff_policy(airborne_handoff=False, *, handoff_preference=None):
    """Explicit run policy, separate from optional heuristic task notes."""
    if type(airborne_handoff) is not bool:
        raise ValueError("invalid_handoff_policy")
    if handoff_preference is None:
        handoff_preference = 'airborne' if airborne_handoff else 'surface'
    if handoff_preference not in ('surface', 'airborne') or (not airborne_handoff and handoff_preference == 'airborne'):
        raise ValueError("invalid_handoff_preference")
    text = (
        "For a useful cross-arm transfer, prefer direct airborne handoff when the visible grasp, "
        "receiving section and both-arm clearance support it. Do not add a handoff to an object "
        "that can already be delivered well. Before any giver release, inspect fresh evidence of "
        "receiver support, then verify retention during separation. A closed jaw is insufficient."
        if airborne_handoff else
        "Do not hand the same object directly between two arms in the air. For a cross-arm transfer, "
        "use a visible stable table/platform or other suitable existing support: choose enough footprint "
        "and clearance, lower and release the object, inspect stable support, clear the giver, then "
        "regrasp with the receiver and verify a short lift. Do not begin receiver contact while the "
        "giver still supports or grips that object. If no suitable support is observed, choose another "
        "route or report the missing support instead of attempting an airborne handoff."
    )
    if airborne_handoff and handoff_preference == 'surface':
        text = (
            "Choose whether a useful cross-arm transfer should use a stable physical support surface or a direct "
            "airborne handoff from current geometry, grasp stability, reachability, both-arm clearance and whole-task cost. "
            "Prefer an observed stable table/platform or other suitable physical support when practical; this is a "
            "recommendation, not a requirement to place the object down. Airborne handoff remains available when it "
            "better fits the visible conditions. Do not add a handoff when the holding arm can already deliver the object well. "
            "For supported transfer, lower and release onto sufficient footprint, inspect stable support, clear the giver, "
            "then regrasp with the receiver and verify a short lift. For airborne transfer, keep the giver supporting the "
            "object through receiver approach/close, inspect fresh evidence of receiver support before any separate giver "
            "release, and verify retention during separation. A closed jaw is insufficient."
        )
    text += (
        " Simultaneous dual-arm movement, independent progression and per-arm queues remain available. "
        "Overlap work on different objects or independent preparation when swept volumes are clear; "
        "this handoff policy does not impose serial arms or a single-arm preference."
    )
    text += " Use existing move/gripper/queue tools and matching bounded templates; inspect current " \
            "RGB between support-changing phases. This is a planning strategy, not a new motion Skill."
    if airborne_handoff:
        text += (
            " Transfer preferences concern relocating an object between arms, for example when the working arm "
            "cannot reach it. For cooperative manipulation of one difficult object, such as stabilizing or "
            "orienting an irregular object, choose table-supported or airborne cooperation from current evidence "
            "with no inherent preference; a transfer preference does not prescribe the cooperation method."
        )
    return dict(version="handoff-policy-v3" if airborne_handoff else "handoff-policy-v2", airborne_handoff=airborne_handoff,
                handoff_preference=handoff_preference,
                strategy=("prefer_" + handoff_preference) if airborne_handoff else "surface_only",
                simultaneous_dual_arm="unchanged", text=text,
                enforcement="actor_instruction_and_named_template_gate; raw pose tools do not infer object identity or support")

# Evidence and counterexamples live in docs/development/task-notes.md. Keep this
# actor-facing text free of historical coordinates, evaluator facts and recipes.
LEGACY_DEFAULT_TASK_NOTES = """Operation experience: choose relevant checks from current evidence. These are fallible heuristics, not a mandatory sequence or extra completion requirements; tool help defines parameters and coordinate conventions.

1. Plan the whole operation: compare grasp, lift, direct transport, supported regrasp/transfer, placement, verification and required final return costs, including likely recovery. Choose order from dependencies, current arm/object poses, reachability, occlusion and clearance. Prefer easier attainable subtasks when they support overall progress; clearing an obstruction or establishing a prerequisite can come first. Reassess after the scene changes. Choose either or both arms according to the work, without a single-arm preference. Reserve the advertised remaining budget for unresolved objects, verification and required final return; do not invent an unknown limit. Before committing to a difficult posture change or repeating an expensive approach, use available cost preview to compare plausible candidates with the intended speeds. A small Cartesian correction can entail a long joint path. Compare the whole remaining operation, not just the next move; preview timeout_s is at most 60 and preview does not prove object retention.

2. Preserve simultaneous independent work: use both arms together for different objects or independent preparation when their swept volumes and releases are independently valid. Paired waypoints impose joint arrival barriers; independent progression permits each arm to advance after its own measured endpoint passes. Follow this run's handoff policy for transferring the same object between arms; the handoff restriction does not disable dual-arm motion. A common central crossing corridor may require one arm to clear before the other enters even for different objects. Use queue dependencies for known ordering and separate requests or observe checkpoints for fresh evidence. For supported transfer, select a stable visible surface reachable by both arms, with enough object footprint and approach/withdrawal clearance; confirm release onto it, then clear the giver before the receiver approaches. Do not substitute a rim, uncertain ledge or unsupported pose for a stable surface. Available manipulation templates reduce repeated parameter assembly and return observation barriers; prefer a matching bounded template when it fits, without treating its completion as retention or task success.

3. Choose the object section and finger contact section separately. Seek sturdy, accessible opposing surfaces and a grasp that also permits the intended placement. Compare top and side approaches, balance, slip/rotation risk and jaw clearance; a convenient protrusion or image center need not be stable. Tip, distal-pad and deeper finger contact can each be appropriate; deeper is not automatically better. Place the selected reference between both intended contact surfaces, with palm, fingertip, support and neighbor clearance. If fingers meet an object end/bottom before reaching the selected section, reconsider the exposed body section, insertion direction and supported object orientation; changing contact_region alone does not create insertion clearance. Keep contact references consistent through transport. If an object needs straightening, consider a supported regrasp. After an empty lift, revise the observed offset, depth, orientation or obstructed approach rather than applying an automatic lowering or rotation rule.

4. Use clear-space transit speed and slow short contact approaches, initial lifts and placement. Arm and jaw slow are both 0.8; slow arm motion does not slow the fingers. Use separate incoming-segment speeds for clear transit and contact, without slowing a long clear route just because its last segment contacts. Keep jaw intent explicit, use slow contact closing and release, and inspect which phase actually ran after a partial failure. Requested closure or completed motion does not prove retention. When needed, make a bounded short lift and check that the object follows; recheck after turns or long transfers. A small measured-correction skill is for local recovery, not repeated 1 cm traversal of an ordinary route.

5. Place the whole object: plan its resting position and orientation, offset from the fingers, support and withdrawal route. For containers, aim within the usable interior center with room from the rim and existing contents. Clear the rim with the object's bottom before crossing, then lower slowly to the intended supported/release position. Account for the whole object's tilt and offset, not only the fingertip projection. Image overlap does not establish 3D clearance. After slow opening, inspect separation and landing before a large withdrawal, then check that the object remains in place. If a rim or arm hides the result, retain 'released, placement unverified' and seek a useful view while recovery is still affordable. Maintain a concise per-object distinction between held, released-unverified and visibly retained; preserve it in material plan updates and summaries. Verify placements before the final home phase instead of repeatedly returning home and going back to inspect. Distinguish a jaw phase that never ran from a caught object or occlusion, and do not impose a universal EE-error rule for release.

6. Recover from measured state: read the inner action receipt, pose and RGB even inside programs. Distinguish zero-step planning rejection, partial motion, suppressed jaws and executed jaws. After tracking failure, compare the selected contact reference with the actual measured pose before closing; requesting deeper contact does not mean it was reached. Correct the observed mismatch or choose a viable alternative, without blindly closing or repeating the same descent. Reconsider both position and orientation for infeasible targets. Waypoints must change a feasible route; they cannot make an unreachable endpoint reachable. Stop loops on failure or unexpected lack of progress.

7. Preserve clearance and useful posture: consider fingers, held objects, both arms and nearby objects throughout translation and turning. A reachable endpoint or accepted preview does not certify a clear swept volume. If contact moves something, update later targets. At intermediate stages do not return home or make a large empty-hand turn unless needed for the next operation, clearance or observation. Save and verify initial poses when final return is required; an old return route may no longer be clear. Reuse action-returned images first; seek another permitted view for an actual ambiguity. Use short simulation-advancing waits only when external motion or settling changes the decision. With optional local policy, choose a bounded prefix and inspect both arms afterward; text naming an arm is not a hard movement constraint."""


PREVIOUS_DEFAULT_TASK_NOTES = LEGACY_DEFAULT_TASK_NOTES.replace(
    "Tip, distal-pad and deeper finger contact can each be appropriate;",
    GRASP_APPROACH_GUIDANCE + " Tip, distal-pad and deeper finger contact can each be appropriate;",
).replace(
    "A reachable endpoint or accepted preview does not certify a clear swept volume.",
    "A reachable endpoint or accepted preview does not certify a clear swept volume. " + ROUTE_PLANNING_GUIDANCE,
) + """

8. Use a compact operation_context for a near-term manipulation, not invented geometry for every future stage. Cite a current actor observation and the RGB/proprioception/calibration/public-URDF basis plus uncertainty; object labels, shape and support are actor RGB estimates, never simulator object coordinates or contact/score truth. Choose pad_center, fingertip or finger_middle explicitly with a reason; the template applies this reference to move/lift, while no-contact peer transit needs none. Before placement, state the whole object's resting pose, bottom relative to the fingers, rim/boundary clearance, release gap/drop distance and withdrawal route; qualitative estimates or 'unknown' are acceptable. Plan supported-transfer landing, giver exit and receiver entry together, with clear independent parallel work. After deposit review, clear the giver and review a later observation before receiver contact; giver home is not a prerequisite and can occur later or independently. Placement templates return released_unverified with the observation id. Record retained only with cited RGB separation and stable landing evidence, retaining uncertainty for occlusion. If actual settling time is needed, use an explicit bounded native-step wait and inspect the result; never an automatic model-verification loop or unaccounted wall-clock delay."""


def default_notes_for(manipulation_protocol=True, *, task_scheduling=True, operation_candidates=False):
    if not manipulation_protocol:
        return LEGACY_DEFAULT_TASK_NOTES
    text = PREVIOUS_DEFAULT_TASK_NOTES
    if task_scheduling:
        text = text.replace("Reassess after the scene changes.", "Reassess after the scene changes. " + TASK_SCHEDULING_GUIDANCE)
    if operation_candidates:
        text += "\n\n9. " + OPERATION_CANDIDATE_GUIDANCE
    return text


DEFAULT_TASK_NOTES = default_notes_for()


def version_for(manipulation_protocol=True, *, task_scheduling=True, operation_candidates=False):
    if not manipulation_protocol:
        return LEGACY_TASK_NOTES_VERSION
    return TASK_NOTES_VERSION if task_scheduling or operation_candidates else PREVIOUS_TASK_NOTES_VERSION


def resolve_task_notes(default_enabled=True, custom_text=None, custom_source=None, *, manipulation_protocol=True,
                       task_scheduling=True, operation_candidates=False):
    """Return the exact effective text and provenance for a fresh episode."""
    if type(default_enabled) is not bool:
        raise ValueError("invalid_default_task_notes")
    if type(manipulation_protocol) is not bool:
        raise ValueError("invalid_manipulation_protocol")
    if type(task_scheduling) is not bool or type(operation_candidates) is not bool:
        raise ValueError("invalid_operation_planning_flags")
    if custom_text is not None or custom_source is not None:
        if (not isinstance(custom_text, str) or not custom_text.strip()
                or len(custom_text) > MAX_CUSTOM_TASK_NOTES
                or not isinstance(custom_source, str) or not custom_source.strip()):
            raise ValueError("task_notes requires nonempty text and source; maximum 8000 characters")
    default_text = default_notes_for(manipulation_protocol, task_scheduling=task_scheduling,
                                    operation_candidates=operation_candidates)
    default_source = (TASK_NOTES_SOURCE if task_scheduling or operation_candidates else PREVIOUS_TASK_NOTES_SOURCE) if manipulation_protocol else LEGACY_TASK_NOTES_SOURCE
    texts = [default_text] if default_enabled else []
    sources = [default_source] if default_enabled else []
    if custom_text is not None:
        texts.append(custom_text)
        sources.append(custom_source)
    return {
        "text": "\n\n".join(texts) or None,
        "source": "; ".join(sources) or None,
        "config": {
            "default_enabled": default_enabled,
            "default_version": version_for(manipulation_protocol, task_scheduling=task_scheduling,
                                           operation_candidates=operation_candidates) if default_enabled else None,
            "task_scheduling": task_scheduling,
            "operation_candidates": operation_candidates,
            "custom_text": custom_text,
            "custom_source": custom_source,
        },
    }
