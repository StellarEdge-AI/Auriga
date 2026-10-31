# Joint travel admission and candidate trajectory evidence

`features.motion_safety_guard` and `features.trajectory_preview` are independent switches. The worker accepts `--motion-safety-guard` / `--no-motion-safety-guard` and `--trajectory-preview` / `--no-trajectory-preview`; new worker defaults are enabled. Joint replanning remains separately controlled. Neither feature automatically replans or rewrites a joint trajectory.

## Information boundary

New computation uses the following sources only:

- Current public named arm encoders and measured jaw positions.
- Current measured `link6` pose for each arm in the same declared calibrated frame.
- The named public X5 URDF, its hardware collision-sphere configuration, and its gripper linkage/scale configuration.
- Already compiled candidate joint commands and explicit speed/progression choices.
- Controller action budgets and its local dispatched-command count.

The code does not inspect simulator articulation roots, actor/object poses, world collision queries, contact forces, reward/success flags or evaluator state. Public base transforms are reconstructed as `T_world_link6_measured @ inverse(T_base_link6_URDF_FK(q_measured))`. This is conditional on the declared common frame and measured-pose calibration; fitting one pose does not independently establish calibration accuracy. Missing measured provenance, differing pose frame IDs or missing hardware model yields `geometry.status=unknown`, without substituting simulator transforms.

The existing native planner still supplies candidate joint paths under its existing planner scope. These new features do not request extra scene information from it. The proximity computation reads only the pinned robot hardware files under `Assets/Robots/x5`; it does not read the planner's live world, task geometry, object state or camera pose samples. PyYAML already exists in the native RoboDojo worker environment; if unavailable elsewhere, joint evidence remains available while hardware/FK evidence returns unknown.

## Pre-dispatch admission

After each actual resampled motion segment is compiled, admission compares the measured starting joints with the complete compiled command path. It rejects a revolute/continuous joint when both conditions hold:

1. Accumulated absolute travel is greater than π radians.
2. Either `abs(raw_net_delta) - abs(shortest_periodic_delta)` or `total_travel - abs(raw_net_delta)` is at least π/2 radians.

The first excess identifies conspicuous winding; the second identifies large backtracking. Periodic winding applies only to continuous joints, or bounded revolute joints whose measured start and shortest equivalent endpoint both fall within the public URDF's declared lower/upper limits. For example, a bounded −170°→170° move within [−180°,180°] cannot cross the hard stop via −190° and is not rejected as periodic winding. Unknown limits/types explicitly skip that periodic check; backtracking still applies to known revolute/continuous joints. The periodic delta is diagnostic and does not establish a collision-free alternate path. No modulo wrapping, endpoint substitution, clipping or automatic retry occurs.

Admission also compares the whole nominal segment cost, including its initial jaw forecast, to the remaining request and controller episode budgets. The existing 250-step request cap remains in force. The episode limit is the already disclosed controller limit, and the worker tracks dispatched native commands locally; a faulting attempt is counted conservatively. A rejected over-budget segment spends zero rows of that segment. Astra may submit a shorter alternative. Fresh arrival-jaw rows are budget-checked again because their measured opening can change the row count. Standalone jaw and hold requests also check their complete compiled length before dispatch when the guard is enabled.

The check runs for single-arm, paired dual, independent dual and queue move stages. Every newly ready group is fully admitted before dispatching its next merged row. A later failure preserves previous arm/waypoint work and reports `prior_executed_native_steps`; it cannot roll back a peer's earlier release. Exact cached single-arm previews are also checked before dispatch, without changing their cached rows. This feature's travel scope is resampled motion; it does not change the existing feedback executor or raw joint-prefix policy.

Receipts use `reason=motion_safety_rejected`, `failed_phase=before_segment_dispatch`, `motion_safety`, and `motion_safety_checks`. The structured result includes per-joint travel/net values, threshold witnesses, remaining budgets and the current segment's zero dispatched rows. Passing this policy does not mean collision-free motion or guaranteed physical arrival.

## Candidate evidence from `robot_preview_cost`

> Status: `robot_preview_cost` / `preview_motion_cost` and `robot_inspect_trajectory` were removed in the 2026-10 refactor (trusted_legacy only; unreachable by the actor or programs). The admission policy above and `trajectory_evidence.py` remain; trajectory samples are now read with `robot_action` `mode=inspect` (`trajectory_ref`, `start_native_step`, `max_samples`). The sections below are kept as historical reference.

The worker method is `preview_motion_cost(candidates, *, observation_id=None, include_trajectory=None)`. `None` follows the trajectory feature; `False` omits the extended evidence; `True` when the feature is disabled is rejected. Existing single-segment restrictions remain: 1–3 candidates, one current observation and exactly one waypoint. The function cannot truthfully compute later waypoints that depend on future measured states.

Each successfully compiled candidate gains:

- `motion_safety`: the same admission policy evaluated on its nominal path. If the guard is enabled and rejects it, candidate status is `rejected`, `feasible=false` and reason is `motion_safety_rejected`. Native planner feasibility and nominal row costs remain separate fields.
- `trajectory_evidence`: at most six synchronized key poses (start, quarter, midpoint, three-quarter, target and minimum model clearance), with exact sampled `q_rad`, per-arm `joint_names` and `q_unit="rad"`. It also includes a compact minimum-clearance link/sphere pair, near-boundary count and first/last near boundary, geometry provenance and explicit unknown/near-approach anomalies. Display FK poses are rounded to four decimal places; sampled joint values are unrounded. Full per-link maps, sphere centers/radii and unbounded near-step lists remain in the artifact. Cost, feasibility and `motion_safety` remain unchanged.
- `trajectory_data`: the complete artifact-ready nominal timeline and public model. The harness stores this separately rather than repeatedly putting every row into model context.

`trajectory_data.samples` contains initial `native_step=0` and one sample per shared native boundary. Each sample has `arms.left/right.{q_rad,jaw_goal_normalized,phase,link_positions_m,link6_pose}`. Link positions and the link6 pose (position plus rotation matrix) are present only when public geometry is available. The full artifact retains exact command values, every link map, the full near-step list and detailed minimum sphere witness. Historical v1 artifacts that predate orientation storage still expose their recorded link6 positions. The initial arm q is measured; future q and every future FK pose are candidates, not measurements. The initial FK uses measured finger positions; later finger positions follow nominal gripper commands. Real fingers may be blocked by an object, and arrival ramps will be recompiled from the then-current measurement.

Paired timing is `max(arm motion lengths) + max(arm jaw lengths)`, padding the shorter motion and jaw phases with holds. Independent timing is `max(each arm motion + own jaw)`, with immediate own-jaw progression and final holds. The nonparticipating arm is held. Thus displayed overlap follows actual scheduler semantics, not unrelated equal-duration interpolation. Actual execution replans and checks its newly compiled path; the preview is not an executable commitment.

## Robot-model proximity limits

Both arm models are placed using the public inferred bases. The check computes all cross-arm hardware sphere clearances at every native boundary, records the minimum pair and classifies clearance ≤ 30 mm as `model_near_approach`. It never rejects execution for sphere proximity. This is a conservative warning threshold on an approximate model, not a claim that the sphere set conservatively encloses every robot surface.

The native public sphere file currently covers base, arm links and wrist cameras, but not `link7`/`link8` fingers; `missing_finger_coverage=true` is returned. Held objects, table/task objects, actual contact deformation, calibration error and movement between samples are not tested. `no_near_approach_at_sampled_boundaries` is deliberately narrower than “safe” or “collision-free.” Missing sphere metadata leaves FK evidence available but proximity unknown. Images projected by a separate tool must likewise identify nominal geometry and avoid treating 2D overlap as physical collision.

## Verification

`test_motion_safety.py` checks a saved 70-row right return-home path from a recorded run (297.706° first-joint commanded travel), no mutation/wrapping, bounded hard-stop and unknown-limit cases, initial and later admission, all four execution modes, peer gating, cached paths, standalone jaw/hold and fresh-jaw budget growth. Its historical fixture contains the recorded encoder start, candidate q commands and public URDF joint limits, with action/segment and hardware provenance.

`test_trajectory_evidence.py` compares complete preview rows to the real CPU dispatch scheduler for paired and independent jaw timing, verifies public FK and sphere witnesses, and checks unknown calibration/model handling and zero-step rejected previews. Its geometry adapter intentionally has no simulator/world attributes.

A separate read-only CPU check using the public X5 files and the saved measured state of a recorded winding failure produced 71 samples with available FK, without starting Isaac or advancing physics. For a right-home candidate with the left arm held at that particular initial state, the sampled sphere minimum was 40.856 mm. This is not the full multi-waypoint executed request, nor evidence that the real failure was collision-free; the guard correctly rejects the winding independently of this advisory proximity result.


## Actor access and calibrated RGB overlay

`features.preview_intent` (still accepted, but a no-op since the 2026-10 refactor) defaults to `false`. The original cost preview, `include_trajectory` option, sampled joints/images and automatic post-action trajectory feedback remain available. With an explicit `features.preview_intent=true`, `robot_preview_cost` exposes `purpose="route_review"` (the default when trajectory evidence is enabled) and `purpose="budget_only"`. Route review automatically supplies sampled q and a `cam_head` overlay per available candidate. Budget-only compares numbers; it is not a route review. The opt-in instructions recommend route review for contact, container entry/withdrawal, held-object turns, shared-arm clearance and repeated failures, while leaving the decision to preview to the actor. `preview_scope` reports whether the joints and images were actually available; it never claims that the actor inspected them or that a complete operation is verified.

The existing `include_trajectory=false/true` interface remains valid; contradictory explicit purpose/boolean pairs receive a field-specific correction instead of a silent override. Setting `preview_intent=false` restores the earlier schema, instructions and result without removing the underlying trajectory tools. Requesting trajectory evidence while that feature is disabled is rejected. Each candidate returns the compact evidence above and `trajectory_ref`; full candidate data is stored as an actor artifact bound to the episode and source observation. `detail="summary"` is the actor default and adds `full_result_ref` for the complete cost result; program calls default to `detail="full"`. Both details retain the same sampled q, anomalies and image evidence. Old observations remain explicitly historical; their preview cannot be overlaid as current. All reads require the same environment/episode identity and actor access scope, and reject privileged data before projecting a summary.

`robot_inspect_trajectory` always returns exact, unrounded `q_rad`, nominal jaw goals and phases for the selected synchronized boundaries:

- With no selector it reads start, midpoint and target, deduplicating short paths.
- `native_steps=[0, 10, 20]` reads 1–25 specific existing boundaries in the requested order.
- `start_native_step=0, max_samples=25` reads a chronological page. Either pagination field activates this mode; defaults are step 0 and 25 samples. Follow `next_native_step` until it is null to retrieve every row, including the initial measured boundary. Pagination fields cannot be combined with `native_steps`. The response includes `selection_mode` and `total_samples`.
- `detail="full"` additionally returns selected full sample link/FK data, public model/base provenance and detailed proximity metadata; the default `detail="summary"` returns exact joint commands without geometry. `include_geometry=true/false` remains an alias for full/summary. Conflicting explicit settings are rejected. The artifact remains complete in both cases.

For example, call `robot_inspect_trajectory(trajectory_ref=ref, start_native_step=0, max_samples=25)`, append `samples`, then repeat with the returned `next_native_step` until it is null. A 63-sample trajectory yields pages of 25, 25 and 13 samples. Explicit indices and pagination never round command values.

Either tool optionally accepts `camera_id`: it overrides the preview's automatic head camera, or requests an inspection image. An overlay contains at most three configurations, each drawn on the same original RGB and camera pose, with the complete stored link6 path connected by thin lines. Each panel retains the original image dimensions; a three-panel 640×480 source produces a 1920×480 image. The original, unannotated RGB remains referenced separately. Inspection requests with a camera must therefore select at most three samples (including when using pagination). Overlay rendering reads full artifact geometry independently of `include_geometry`. Blue is left, orange is right; skeleton lines represent joint frames, with no volume or visibility inference. Object collision, held-object sweep, fingers and unknown geometry still require actor visual reasoning. Overlay failure is local: cost and joint evidence remain available.

The overlay camera builder does not call the older dynamic `GeometryTools.camera` path and never reads pose-sample artifacts. Fixed cameras use static intrinsics/extrinsics; wrists use the static mount plus URDF FK from measured joints and the measurement-derived base already disclosed by the preview. Missing frame calibration yields unavailable. The simulation adapter's static camera calibration stands in for hardware calibration; deployment must provide measured calibration. Dynamic simulator camera/base/link transforms, residuals and object state are not used.

## Automatic action feedback

`build_action_trajectory_feedback` reads only the saved actor `before_state`, `planned_trace`, `actual_trace`, public robot model and the original execution-start observation. It never calls a robot backend, replans, obtains a new observation or advances simulation. The model still decides whether a cost preview is useful; execution does not require a prior preview.

Action feedback presents bounded exact q samples for recorded plans and measured boundary states, plus the largest recorded command/encoder difference when complete native-boundary sampling supports that comparison. It deduplicates repeated terminal measurements, preserves original `sim_step` values, includes the final completed and first incomplete planned boundaries, and retains all samples in `trajectory_feedback_ref`. The original trace references remain available. Planned jaw geometry stops at link6 when the artifacts lack the public normalized-jaw conversion; measured finger positions can still be shown in measured FK.

`features.action_trajectory_delivery="automatic"` is the default: routine completed actions retain sampled planned/measured joints and one measured-route image at original panel resolution. A separate planned image is available on demand, avoiding a second largely duplicate image on routine success. Failures, uncertainty and explicit diagnostics retain available planned/measured/local images. The optional `"diagnostic"` policy preserves the compact no-image routine summary. Both policies retain the full recorded paths, and neither can reconstruct a missing trace.

Plan and measurement images are explicitly labelled and projected onto the same execution-start RGB and camera pose. They do not reconstruct later scene appearance. Recorded paired rows and actually scheduled independent rows have shared timing. An independent arm's compiled remainder is separate evidence with local row indices, no invented peer trajectory or shared future timing. At most one relevant local candidate per arm is shown immediately; every retained local candidate remains in the full feedback artifact. A faulting row may have partially executed, so “not recorded completed” is distinct from a rejected solver endpoint explicitly marked “not executed.” When only an endpoint was retained, its image shows only that configuration, without inventing a path. Zero-step guard rejection without retained q reports the missing plan rather than regenerating one.

Missing trace, calibration or public geometry is a local diagnostic failure and cannot change the action's status or retry it. Diagnostic overlays are separate from ordinary camera RGB and must not advance the adaptive RGB delivery baseline. Full-resolution automatic images add image transport/context cost; compact text alone does not establish lower total token use.

`test_trajectory_feedback.py` covers exact commands, synchronized versus local paths, historical source-frame binding, original image dimensions, zero-step missing plans, rejected endpoints, actor scope, stale identity, missing calibration and unknown sampling. A read-only replay over saved actions from recorded runs exercised the same builder with no backend instance or new native step, checking image dimensions, source references and sampled-q equality.
