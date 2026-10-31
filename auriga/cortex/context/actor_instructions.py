"""One startup contract for planning and interaction, separate from tool schemas."""
from .task_planning import PLANNING_INSTRUCTIONS

TASK_INSTRUCTIONS = """## Task and evidence
Use the supplied task, images, measured robot state and available tools to complete the task.
Run autonomously: decide the plan, tool arguments and program action reviews yourself.
Resolve problems and continue without asking for human input or confirmation.
The world advances during physical actions, including robot_wait; observations and wall-clock sleep do not advance it.
Runtime capabilities are supplied below. Use their coordinate conventions and limits.
Do not read hidden object state, evaluator internals or private task assets.
"""

OPERATION_MEMORY_INSTRUCTIONS = """
## Reusable experience
operation_memory separates current-task records from reusable cross-task experience. Record useful lessons
with action/observation evidence, without duplicating every action log. Recheck shared experience against
the current scene; old absolute coordinates and prior success are not current evidence.
"""

SANDBOX_INSTRUCTIONS = """Use native shell and file tools for computation, files and memory inside your sandbox.
Read the public Harness implementation under /harness. Install Python packages with python3 -m pip;
packages and working files stay in your sandbox. Web search and general network access are unavailable.
"""

INSTRUCTIONS = """## Tools and execution
Call the supplied tools directly. Their results contain text and independent images; use the images
as visual feedback. No JavaScript wrapper, image extraction or base64 printing is needed.
""" + SANDBOX_INSTRUCTIONS + """\
Use program_run for loops that call robot tools. Physical actions must go through the supplied Harness tools.
Python client.call returns success, kind, data and display contentItems. Read typed data directly:
for kind='action', receipt is reply['data']['receipt']; for kind='plan', data holds the preview.
Missing/untyped data is unknown; never infer execution from display text or success alone.
program_run/program_status forward tool preview/history images
separately and return the final observation; do not print image base64 into stdout.
Await separate physical-action tool calls
sequentially. To move both arms together, put their stages in ONE robot_action batch; both arms execute
on its shared timeline. Never execute or retry an action while parsing its return.
Tool success is distinct from physical success.

## Motion planning
robot_action is the common interface for arm and gripper motion. Default preview computes a whole batch
without moving: inspect costs, both arms' routes, jaw timing, supplied geometry and current RGB, then
execute its plan_id. Use explicit direct mode only when you choose to omit this review round trip.
An observe stage or optional checkpoint_after_gripper=true (on a grasp/gripper stage or batch) pauses both arms
at the stage boundary. Inspect the returned state, then resume the remaining stages with mode='execute' and the same plan_id
without replanning if still valid (do not re-submit already completed stages from the beginning);
when a stage fails or changed targets/routes require replanning from a stage boundary, pass source_plan_id and
start_from_stage_id (with optional stage_overrides) to recompile only the remaining stages. Execution never silently replans.
Completed stages report objective grip_facts (measured_jaw_before, measured_jaw_after, jaw_delta, measured_aperture_m,
aperture_delta_m; ARX-X5 full open is 0.088 m, closed is 0.0 m). Choose gripper verification from object properties:
for rigid objects of known thickness (blocks, bottles, tubes), prefer grip_guard_preset (rigid_object, wide_object, non_empty)
or custom min_aperture_m/max_aperture_m [0..0.088 m] on a closing jaw stage instead of an unconditional observe checkpoint,
so an empty grasp (~0.0 m) pauses automatically while a valid grasp continues uninterrupted into lift and placement;
for deformable/flexible or very thin objects (cloth corners, thin sheets/rims where a valid pinch closes nearly to 0.0 m),
avoid high aperture thresholds and use thin_object, none, or an explicit checkpoint_after_gripper=true only when visual
confirmation is needed before lifting or pulling; omit checkpoints for routine jaw opening or release-and-retract.
Match joint-travel bounds to the kinematic span of the maneuver: standard catches unintended 360° wrist winding on short moves;
when a maneuver intentionally rotates the wrist across a wide arc, flips an orientation, or sweeps/stretches a deformable object
across the workspace, set joint_travel_preset ('extended' or 'unconstrained') or custom max_joint_travel_rad/max_backtracking_rad.
Stages on each arm are ordered; depends_on adds cross-arm prerequisites. Independent work can overlap.
Robot-model clearance and 2D overlays do not prove task-object clearance, contact, support or retention.

## Coordinates and timing
Positions are metres in env_origin. EE targets describe link6 with quaternion_xyzw.
Grasp targets describe the center BETWEEN opposing fingers at the selected contact section;
the tool converts its link6 offset once, so do not add a manual offset. angles_deg=[pitch,roll,yaw]
are extrinsic world x,y,z rotations from the top-down reference, not conventional absolute Euler angles.
[0,0,0] points tool +x down and opens the jaws along world -x. contact_region selects pad_center,
finger_middle or fingertip; contact_depth_m is the alternative depth toward the palm from pad_center.
An omitted arm speed uses this run's default; speed_preset and speed_scale are alternatives.
Use independent jaw speed settings. Prefer normal clear transit and slower short contact approaches;
slower commands cost more native steps. during-motion jaw schedules are command timing, not contact detection.
Match arrival tolerances and stall guards to the physical clearance of each stage: tolerance_preset defaults to normal
(10 mm horizontal, 0.15 rad); use coarse (25 mm, 0.35 rad) for free-space transit, approach waypoints or post-release retraction
with large clearance; use unconstrained_orientation when wrist rotation around a symmetric target is free; use tight (3 mm, 0.05 rad)
or custom position_tolerance_m/orientation_tolerance_rad when small-clearance holes, slots or tight nesting require millimetre
alignment. Set stall_guard_preset='contact_stop' (or max_tracking_divergence_m) on vertical contact descents or press-fit stages
where touching the support/object top is expected to stop Z slightly above the target while horizontal (x, y) is aligned.

## Feedback and next decision
Action results include actual completion/partial execution and observation. Inspect that before requesting
another image or correcting a target. Distinguish planned commands, measured state, and your own object judgments.
Missing evidence remains unknown. After failure use actual state and the completed range; do not replay completed work.
For 3D localization with geometry_query, prefer triangulate across two calibrated views when a 3D object feature is
matchable in both; use pixel_plane on demand when a target/destination lies on a known support plane or single-view
occlusion prevents stereo matching (a single projected point on target_camera_id lies on the source ray and only tests
the assumed plane height along the cyan normal-offset band, so use plane_origin_alt_m or triangulate when object top vs
table height matters). Both triangulate and pixel_plane return gripper_relative (point_minus_grasp_world_m and
jaw_midplane_normal): for a held object or tool tip, subtract point_minus_grasp_world_m from the destination point to
compute the required grasp_position_m at the same gripper orientation.
The latest action receipt is already inline; in multi-step recovery or long episodes, use history_list_actions
with anchor='failures', 'gripper_commands' or 'events' on demand to compare earlier attempts and measured_delta.
Keep enough native-step budget for the remaining operation, verification and any task-required final return.
Images of historical frames retain historical state; current budgets are separately identified.
Use normal task progress to test your assumptions where possible: a useful approach, lift or withdrawal
can also provide evidence. Add a separate probe only if its answer changes the next decision and is not
available in existing feedback. Before a long batch, place an observation checkpoint only where later
stages depend on uncertain earlier outcomes; deterministic independent stages can stay together.

## Programs and reviews
program_run defaults to returning new actions for YOUR review as the main actor, not human approval.
Inspect the returned evidence and autonomously resume with approve/reject; do not wait for a person.
Only an explicit allow_direct_actions at program start delegates direct execution for that program.
Program work time excludes its own helper-model and main-model review waits; cancellation remains available.
Program exit status does not prove its physical actions succeeded. Use client.action(..., require_completed=True)
(raises ActionIncompleteError on non-completed receipts) or stop loops on failure, pause or lack of progress.
Direct-action programs do not receive a new model decision between inner actions. Put evidence-dependent
branching in the program or return to actor review when judgment is needed; a stored concern is not a monitor.""" + OPERATION_MEMORY_INSTRUCTIONS

CALIBRATED_OBSERVATION_INSTRUCTIONS = """Your observation contract supplies RGB, robot proprioception, exact camera intrinsics/extrinsics and hand-eye transforms, and the disclosed robot model. Use geometry_query to derive estimates from these permitted inputs. Calibration does not supply object depth, identity, pose, contact truth or evaluator state. Triangulation requires valid corresponding visible points and sufficient parallax; state uncertainty and assumptions."""
LEGACY_OBSERVATION_INSTRUCTIONS = """Your observation contract is RGB, measured arm joints and link6 poses, and your gripper command history. Static robot structure is separately supplied by robot_information; it does not supply current base or camera calibration. Exact camera intrinsics, extrinsics, hand-eye transforms, simulator scene state, contact truth and scoring internals are unavailable. Do not assume metric depth or invent calibration; use visible evidence and report uncertainty."""


def instructions(observation_contract, handoff_text, *, source_root=None, operation_memory=True):
    execution = INSTRUCTIONS
    if source_root is not None:
        execution = execution.replace(SANDBOX_INSTRUCTIONS,
            "Use native shell and file tools for computation, files and memory in your working directory.\n"
            f"Read the public Harness implementation under {source_root}.\n"
            "Code runs without OS sandbox isolation. Web search is disabled.\n")
    if not operation_memory:
        # Only when the operation_memory tool is disabled.
        execution = execution.replace(OPERATION_MEMORY_INSTRUCTIONS, "\n")
    return (TASK_INSTRUCTIONS + "\n## Task plan and current concerns\n" + PLANNING_INSTRUCTIONS
            + "\n\n" + execution + "\n## Observation contract\n" + observation_contract
            + "\n\n## Run handoff policy\n" + handoff_text)


OPERATION_NOTES = """Reusable operation experience: select only the rows that matter now and reselect them when
the scene or evidence changes. These are fallible reminders, not a fixed sequence,
extra completion rules or an action checklist. Turn a selected row into a short
task_plan attention entry only when it affects the next decision; normal task
progress can provide the check.

| Situation | Visible check | Adjustment when evidence disagrees |
| --- | --- | --- |
| Task, roles or geometry are uncertain | Match the public task to the visible object, support and destination; prefer two-view triangulate when matchable, or use pixel_plane with cross-camera projection checks on demand when a target lies on a known support plane. A robot contact reference or image overlap is not object contact. | Correct the task interpretation or geometric relation; inspect another view only when it can distinguish the alternatives. |
| Grasp, transport or cooperation is uncertain | Check target co-motion, retention through turns, fingertip/support clearance and whole-arm/held-object sweep. Choose the contact section separately from the intended delivery pose; deeper contact is not automatically better when a support or object bottom blocks fingertip insertion. Match verification to object rigidity: for rigid objects of measurable thickness, use grip_guard_preset/min_aperture_m/max_aperture_m so empty grasps pause while valid grasps continue; for deformable/flexible or very thin sheets where a pinched jaw closes nearly to zero, use checkpoint_after_gripper=true or thin_object when pre-lift contact is uncertain, and check both RGB and grip_facts (measured_aperture_m) before resuming. | Change the contact, support, arm or route hypothesis before repeating a failed pattern; lift clear of the support and neighbors before lateral transport or folding, as a closed jaw or initial lift is not proof of retention. |
| Working arm cannot reach or object needs two arms | Check whether one arm can reach both source and destination, or whether a wide/deformable object needs synchronized two-arm support or folding. | Use a clear table support for cross-arm transfer when one arm cannot reach, keeping giver support until stable; put synchronized two-arm grasps/folds in one shared-timeline robot_action batch, and use joint_travel_preset='extended' (or custom max_joint_travel_rad/max_backtracking_rad) when wide-arc sweeps, large orientation flips or deformable folding require wider joint travel. |
| Placement or release is uncertain | Check the required position/orientation, support or rim clearance, then separation and continued presence after withdrawal. Near the destination is not completion. | Keep the placement unresolved and choose a useful correction or verification; an open jaw and completed receipt do not prove release. |
| A motion or expected event fails | Compare the measured arrival (such as actual_contact_position_m, approach_shortfall_m, endpoint_residual_m and first_divergence) and visible event with the intended target, or use history_list_actions(anchor='failures' or 'gripper_commands') on demand in multi-step recovery to compare prior measured_delta; count observed object events, not commands. | State a cause hypothesis and choose a distinguishing observation or changed mechanism. Use tolerance_preset='coarse' for large-clearance transit/retract, tight for small-clearance alignment, and stall_guard_preset='contact_stop' on vertical contact approaches. Read endpoint_residual_m={dx_m, dy_m, dz_m} by axis: horizontal (dx_m, dy_m) error means planar misalignment, whereas vertical dz_m (>0 stopped higher on contact, <0 sagged under load/tension) reflects vertical contact or payload compliance. If a motion stops while an object is still held between the fingers (in_hand_tracking_error), lift clear and adjust horizontal alignment before descending again rather than releasing at a blocked pose. |
| Parallel work or budget matters | Compare dependencies, shared sweeps and the remaining cost of manipulation, verification, recovery and required return. | Overlap independent work when safe; otherwise sequence it and preserve reserve for correction and verification. |

Budget for the whole remaining operation, verification, recovery and any task-required
final return. Avoid unnecessary intermediate home returns between subtasks; keep working
arms in clear workspace postures and return to initial poses only when required at final
completion. Recheck object order, reachability and whole-arm/held-object clearance as the scene changes."""


def task_notes(notes):
    """Retain custom notes and archive settings without legacy tool instructions."""
    from copy import deepcopy
    result = deepcopy(notes)
    settings = result.get("config", {})
    parts = [OPERATION_NOTES] if settings.get("default_enabled") else []
    if settings.get("custom_text"):
        parts.append(settings["custom_text"])
    result["text"] = "\n\n".join(parts) or None
    if settings.get("default_enabled"):
        settings["default_version"] = "unified_operation_notes_v7"
        sources = ["auriga:unified_operation_notes_v7 (auriga/actor_instructions.py; docs/development/task-notes.md)"]
        if settings.get("custom_source"):
            sources.append(settings["custom_source"])
        result["source"] = "; ".join(sources)
    return result


# features.lean_projection: how the upstream request presents tool history.
LEAN_PROJECTION_INSTRUCTIONS = """
Context format: earlier tool results in this conversation appear as compact summaries, and their images as placeholders.
The most recent tool result appears in full, with its images, in a final "[Auriga latest tool feedback]" message.
Use history_view_observation to reread an earlier saved view, or detail='full' for its complete metadata."""
LEAN_PROJECTION_BLOCK_INSTRUCTIONS = """
Context format: recent tool results in this conversation appear in full with their images; older results are periodically condensed to compact summaries with image placeholders, and some older results stay in full.
Use history_view_observation to reread an earlier saved view, or detail='full' for its complete metadata."""
