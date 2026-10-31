# Joint Travel Admission and Candidate Trajectory Evidence

`features.motion_safety_guard` and `features.trajectory_preview` are independent switches. The native worker accepts `--motion-safety-guard` / `--no-motion-safety-guard` and `--trajectory-preview` / `--no-trajectory-preview`, both enabled by default in new runs. Joint-space replanning (`features.joint_replan`) remains controlled separately, and neither safety admission nor trajectory preview automatically replans or mutates a compiled joint trajectory.

## Information Boundary

All computations in these modules rely strictly on permitted public sources:

- Current public named arm joint encoders and measured jaw apertures.
- Current measured `link6` pose for each arm in the declared calibrated frame.
- The public ARX X5 URDF, its hardware collision-sphere configuration, and its gripper linkage/scale configuration.
- Already compiled candidate joint commands and explicit speed/progression settings.
- Controller action budgets and local dispatched-command counters.

The implementation never inspects simulator articulation roots, actor/object ground-truth poses, world collision queries, contact forces, reward/success flags, or evaluator state. Public base transforms are reconstructed as `T_world_link6_measured @ inverse(T_base_link6_URDF_FK(q_measured))`, which is conditional on the declared common frame and measured-pose calibration (fitting a single pose does not independently certify calibration accuracy). Missing measurement provenance, mismatched pose frame IDs, or missing hardware model files yield `geometry.status="unknown"` without falling back to simulator transforms.

While the native planner still generates candidate joint paths under its standard scope, these modules request no extra scene information from it. Cross-arm proximity checks read only the pinned robot hardware files under `Assets/Robots/x5` and never inspect the planner's live world, task geometry, object states, or camera pose samples. If `PyYAML` is unavailable outside the native RoboDojo worker environment, joint-space evidence remains available while hardware/FK evidence reports `unknown`.

## Pre-Dispatch Joint Travel and Budget Admission

Once a `resampled` motion segment is compiled, the admission guard compares the measured starting joints against the full compiled command sequence and rejects any revolute or continuous joint where **both** of the following hold:

1. Accumulated absolute joint travel exceeds $\pi$ radians: `total_travel > pi`.
2. Either periodic excess `abs(raw_net_delta) - abs(shortest_periodic_delta) >= pi / 2` or backtracking excess `total_travel - abs(raw_net_delta) >= pi / 2`.

The first excess catches full-turn periodic winding; the second catches large out-and-back excursions. Periodic winding checks apply only to continuous joints or bounded revolute joints where both the measured start and the shortest $2\pi$-equivalent endpoint lie within the public URDF's declared lower and upper limits—for example, a bounded $-170^\circ \to +170^\circ$ rotation on a $[-180^\circ, +180^\circ]$ joint cannot physically pass through $-190^\circ$ across the hard stop and is therefore not rejected as periodic winding. Unknown joint limits or types explicitly skip the periodic check while still enforcing the backtracking check on known revolute/continuous joints. The periodic delta is purely diagnostic and does not imply that the shorter wrap-around direction is collision-free. No modulo wrapping, endpoint substitution, clipping, or automatic retry is performed.

Admission also checks the full nominal segment cost—including its initial gripper ramp forecast—against both the remaining per-request step cap (250 native steps) and the disclosed episode action budget. The worker tracks dispatched native commands locally (counting faulted native calls conservatively), so a segment rejected for exceeding the budget dispatches zero rows of that segment, allowing the actor to submit a shorter alternative. When post-motion gripper ramps are rebuilt from live measured aperture upon arm arrival, their updated row count is budget-checked again before dispatch. Standalone gripper and hold requests also check their full compiled length before dispatch when the guard is enabled.

This admission check runs across single-arm, paired dual-arm, independent dual-arm, and batch/queue motion stages. Every newly compiled segment group must pass admission before its next merged row is dispatched; if a later segment fails admission, work completed by earlier waypoints or the opposite arm is preserved and reported via `prior_executed_native_steps` (it cannot physically roll back an earlier gripper release on the opposite arm). Executing a cached preview plan runs the same pre-dispatch check without mutating its cached rows. This guard governs `resampled` trajectories and does not alter the diagnostic `feedback` executor or raw joint-prefix policy actions.

Rejected actions return `reason="motion_safety_rejected"`, `failed_phase="before_segment_dispatch"`, `motion_safety`, and `motion_safety_checks`, detailing per-joint travel and net deltas, threshold witnesses, remaining budgets, and zero dispatched rows for the rejected segment. Passing admission does not guarantee collision-free execution or physical arrival.

## Candidate Trajectory Evidence in `robot_preview_cost` (Historical)

> **Status**: `robot_preview_cost` / `preview_motion_cost` and `robot_inspect_trajectory` were removed in the 2026-10 refactor (`trusted_legacy` only; unreachable by actors or programs). The pre-dispatch admission guard above and `trajectory_evidence.py` remain active; trajectory samples are now inspected via `robot_action` with `mode="inspect"` (`trajectory_ref`, `start_native_step`, `max_samples`). The sections below are retained as historical reference.

The worker method `preview_motion_cost(candidates, *, observation_id=None, include_trajectory=None)` followed the trajectory preview toggle when `include_trajectory=None`, omitted extended evidence when `False`, and rejected `True` when the feature was disabled. It evaluated 1–3 single-waypoint candidates bound to the current observation (refusing multi-waypoint lookahead that would depend on unmeasured future states).

Each compiled candidate included:

- `motion_safety`: The pre-dispatch admission policy evaluated on the candidate's nominal path (setting candidate `status="rejected"`, `feasible=false`, and `reason="motion_safety_rejected"` on violation while keeping native planner feasibility and nominal step costs in separate fields).
- `trajectory_evidence`: Up to six synchronized key poses (`start`, `quarter`, `midpoint`, `three_quarter`, `target`, and `min_clearance`) with exact unrounded `q_rad`, per-arm `joint_names`, `q_unit="rad"`, the minimum-clearance link/sphere pair, near-approach boundary counts and first/last steps, geometry provenance, and explicit `unknown` or `model_near_approach` anomaly flags (display FK positions/rotations were rounded to 4 decimal places while joint angles remained exact).
- `trajectory_data`: The complete nominal timeline and public model stored in a separate artifact rather than inlined repeatedly into model context.

`trajectory_data.samples` recorded initial `native_step=0` (using measured arm joints and measured finger positions) followed by one sample per shared native boundary (using candidate joint commands and nominal gripper commands), exposing `arms.left/right.{q_rad, jaw_goal_normalized, phase, link_positions_m, link6_pose}` whenever public geometry was available. Paired timing followed `max(arm_motion_lengths) + max(arm_jaw_lengths)` and independent timing followed `max(each_arm_motion + own_jaw)`, matching actual scheduler overlap rather than equal-time interpolation.

## Robot-Model Cross-Arm Proximity Limits

Placing both arms via their measurement-derived base transforms, the proximity check evaluates all cross-arm hardware collision-sphere pairs at each native step boundary, records the minimum-clearance pair, and flags clearances $\le 30\text{ mm}$ as `model_near_approach`. Sphere proximity is strictly advisory and never rejects execution, and the sphere set is an approximate model rather than a guaranteed outer bounding volume of every surface.

Because the upstream X5 sphere configuration covers the base, arm links, and wrist camera brackets but omits `link7`/`link8` fingers, `missing_finger_coverage=true` is always reported. Held objects, tabletop/task objects, contact deformation, calibration error, and inter-sample continuous motion are not modeled; `no_near_approach_at_sampled_boundaries` is therefore deliberately narrower than "collision-free." Missing sphere metadata leaves FK evidence available while marking proximity `unknown`, and 2D image overlays must never treat 2D pixel overlap as 3D physical collision.

## Actor Access and Calibrated RGB Trajectory Overlays

In the legacy `robot_preview_cost` / `robot_inspect_trajectory` workflow (now consolidated into `robot_action` preview and `mode="inspect"`):

- With no sample selector, inspection returns `start`, `midpoint`, and `target` (deduplicating short paths).
- `native_steps=[0, 10, 20]` selects 1–25 specific boundaries in the requested order.
- `start_native_step=0, max_samples=25` paginates chronologically; follow `next_native_step` until `null` to read every boundary (`native_steps` cannot be combined with pagination fields).
- `detail="full"` (or alias `include_geometry=true`) includes full per-link FK poses, base provenance, and sphere proximity witnesses, whereas default `detail="summary"` returns exact joint commands without geometry.

Passing `camera_id` renders a trajectory overlay image containing up to three sampled arm configurations drawn onto the original RGB frame and camera pose, with the full `link6` path connected by thin curves. Each panel preserves full source resolution (e.g., three panels of a $640 \times 480$ camera produce a $1920 \times 480$ composite image) while referencing the unannotated original RGB separately; requesting a camera overlay therefore requires selecting at most three samples. Left-arm links are drawn in blue and right-arm links in orange as kinematic stick skeletons without volume rendering or occlusion inference. If overlay rendering fails, cost and joint evidence remain intact.

The overlay camera projection uses static intrinsics/extrinsics for fixed cameras and composes static hand-eye mounts with URDF FK and the measurement-derived base transform for wrist cameras—never reading privileged simulator camera/base/link transforms or object states.

## Automatic Post-Action Trajectory Feedback

`build_action_trajectory_feedback` reads only the saved actor `before_state`, `planned_trace`, `actual_trace`, public robot model, and execution-start observation—never calling a robot backend, replanning, capturing a new frame, or advancing physics.

Action feedback delivers bounded exact joint samples for planned commands and measured boundary states, plus the maximum command-vs-encoder deviation when complete native-boundary sampling is present. Repeated terminal measurements are deduplicated, original `sim_step` values are preserved, the final completed and first incomplete planned boundaries are included, and the full sequence is saved in `trajectory_feedback_ref`.

Under the default `features.action_trajectory_delivery="automatic"`, routine completed actions return sampled planned/measured joints and a single measured-trajectory overlay image at full panel resolution (with the planned-trajectory overlay available on demand via `robot_action(mode="inspect", ...)`), whereas failures, uncertain outcomes, and handoff diagnostics automatically include available planned, measured, and local-candidate overlays. The optional `"diagnostic"` policy omits the overlay image on routine unambiguous completions while keeping full paths in `trajectory_feedback_ref`.

Both planned and measured overlays are projected onto the **execution-start** RGB frame and camera pose and explicitly labeled as historical rather than reconstructing post-action scene appearance. Synchronized paired rows and scheduled independent rows share the common timeline, whereas an independent arm's unexecuted remainder is reported separately with local row indices rather than inventing a shared future timeline. Faulted rows that may have partially executed are distinguished from rejected solver endpoints that never executed; if only a candidate endpoint was retained, only that single configuration is drawn without inventing an connecting path, and zero-step guard rejections without a retained plan report the missing plan rather than replanning.

Missing traces, calibration, or public geometry are local diagnostic omissions that never alter the action's execution status, trigger retries, or advance the adaptive RGB delivery baseline.

## Verification

- `test_motion_safety.py`: Verifies a recorded 70-row right-arm return-home winding trajectory ($297.706^\circ$ commanded travel on joint 1), zero mutation/wrapping, bounded hard-stop vs. unknown-limit handling, initial and mid-sequence admission across all execution modes, peer gating, cached plans, standalone gripper/hold checks, and arrival-jaw budget growth.
- `test_trajectory_evidence.py`: Compares complete preview timelines against the real CPU dispatch scheduler for both paired and independent gripper timing, verifies public FK and sphere proximity witnesses, and checks unknown calibration/model fallbacks and zero-step rejected previews using a simulator-free geometry adapter.
- `test_trajectory_feedback.py`: Covers exact command preservation, synchronized vs. local paths, historical source-frame bindings, panel dimensions, zero-step missing plans, rejected endpoints, actor scope enforcement, stale identities, missing calibration, and read-only replay over archived run actions.
