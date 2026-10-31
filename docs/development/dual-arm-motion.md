# Synchronized Dual-Arm Motion

> **Status**: The `robot_move_dual_grasp` and `robot_execute_ee` dual-arm request formats described below were removed in the 2026-10 refactor (`trusted_legacy` only; unreachable by actors or programs) and are retained here as historical reference. Dual-arm motion is now specified through `robot_action` by placing left-arm and right-arm stages into the same `stages` list on a shared execution timeline; see [Unified Actions](unified-actions.md).

Under `motion_execution_mode="resampled"`, Astra could specify left-arm and right-arm motions in a single request via `robot_move_dual_grasp`. Both arms shared a single native execution timeline, where each merged command row counted as one official native action step. Although `features.joint_replan` defaults to `true`, dual-arm execution does not depend on joint-space replanning being enabled.

Both `paired` synchronization mode and `independent` progression mode are covered by CPU regressions and native short-motion validation; native validation of `independent` mode verified simultaneous motion across unequal waypoint counts as well as gripper actuation overlapping with opposite-arm movement.

## Model Interface (Historical)

A request took the current `observation_id`, `left`, and `right` arm specifications, plus optional `action_id`, `timeout_s`, and `dual_arm_progression`. The default `"paired"` mode waited for both arms to arrive at each corresponding waypoint pair before advancing; explicit `"independent"` mode allowed each arm to advance immediately to its next waypoint or terminal gripper phase as soon as its own measured endpoint check passed. While `"paired"` required equal-length `via_grasp_poses` lists on both arms, `"independent"` allowed each arm to specify 0–7 intermediate waypoints independently without padding, repeating endpoints, or inserting dummy holds (matching array indices simply indexed each arm's own route rather than acting as synchronization barriers). Each arm object accepted:

| Field | Meaning |
| --- | --- |
| `grasp_position_m`, `angles_deg` | Required. Center of the selected finger contact cross-section and top-down grasp reference angles (matching `robot_move_grasp`; defaults to the distal planar pad center). The tool offset is applied exactly once per target. |
| `contact_region` or `contact_depth_m` | Mutually exclusive per-arm contact reference selector. Defaults to `"pad_center"`; also accepts `"finger_middle"`, `"fingertip"`, or a continuous numeric depth `contact_depth_m` for fine tuning. |
| `via_grasp_poses` | Optional list of up to 7 intermediate poses per arm (`"paired"` requires equal lengths across both arms; `"independent"` allows independent lengths). |
| `speed_preset` or `speed_scale` | Mutually exclusive arm motion speed selector; defaults to the run's default motion speed when omitted. |
| `waypoint_speed_presets` or `waypoint_speed_scales` | Per-incoming-segment speed list for this arm, of length `len(via_grasp_poses) + 1` (independent of the opposite arm's length), ordered from the first via to the final target (`null` inherits this arm's request-level speed). |
| `gripper_goal_normalized` | Optional gripper target (`0` closed, `1` open); omitting keeps this arm's latched gripper command. |
| `gripper_speed_preset` or `gripper_speed_scale` | Gripper speed selector for this arm (requires `gripper_goal_normalized` on the same arm). |
| `gripper_motion` | Optional `{start_fraction, end_fraction}` scheduling a synchronized gripper ramp across this arm's final motion segment; if omitted, the gripper runs after arrival (waiting for both arms in `"paired"` mode, or only this arm in `"independent"` mode). |

Arm speed presets map to `slow=0.8`, `normal=1`, `fast=1.25`, and `faster=1.5`; gripper speed presets map to `slow=0.8` and `normal=1`. Continuous numeric scales remain supported in `[0.25, 2.0]` for arms and `[0.25, 1.0]` for grippers; see [Motion Speed Control](motion-speed.md).

For example, when both arms had one approach waypoint and one final target, per-segment speeds could be specified as:

```json
{
  "left": {"waypoint_speed_presets": ["fast", "slow"]},
  "right": {"waypoint_speed_presets": [null, "slow"]}
}
```

When independent routes had different lengths—for example, zero intermediate waypoints on the left arm and two on the right—the speed configuration took the form:

```json
{
  "dual_arm_progression": "independent",
  "left": {"waypoint_speed_presets": ["slow"]},
  "right": {"waypoint_speed_presets": [null, "normal", "slow"]}
}
```

(Actual requests also required `observation_id` and full pose specifications.) In the second example, the shorter left route entered its gripper phase immediately upon reaching its target and then held its pose until the right arm finished, without replanning the same left endpoint twice just to pad the timeline. Whenever fresh visual evidence was needed to decide subsequent targets or release timing, the actions had to be split into separate requests.

## Contact Reference Locations: Distal Pad Center, Finger Middle, and Fingertip

"Grasping the middle of a bottle" and "grasping with the middle of the fingers" are two distinct concepts. The historical `0.1501 m` offset corresponds to the center of the distal 15 mm planar pad; closer to the palm, the inner finger surface is an approximately 55 mm angled ramp rather than the same planar pad.

Both single-arm grasp stages and dual-arm `left`/`right` specifications support selecting the axial contact reference along the finger:

| Selector | Distance from `link6` along tool $+x$ | Axial Depth Relative to `pad_center` |
| --- | ---: | ---: |
| `contact_region="pad_center"` (default) | `150.1 mm` | `0 mm` |
| `contact_region="finger_middle"` | `122.6 mm` | `+27.5 mm` toward the palm |
| `contact_region="fingertip"` | `157.6 mm` | `-7.5 mm` toward the fingertip |

Alternatively, pass `contact_depth_m` in `[-0.0075, 0.0625]` m for continuous adjustment (mutually exclusive with `contact_region`). These bounds and presets derive from the axial span of the inner finger surface in the public robot mesh (with ~0.03 mm calibration rounding) and do not represent a guaranteed safe insertion depth for arbitrary objects. Note that `"finger_middle"` lies on the ~8.735° inner ramp and is not inherently more stable than the distal planar pad.

`grasp_position_m` specifies the **point midway between the two fingers at the selected axial cross-section**, which should align with the target object's local cross-section rather than its visible top surface. Given target point $C$, unit tool approach axis $u$, and contact depth $d$, the compiler computes `link6 = C - (0.1501 - d) * u`—the model must not subtract the depth offset a second time manually. For a vertical top-down grasp (`angles_deg=[0, 0, 0]`), selecting `"finger_middle"` places `link6` 27.5 mm lower for the same $C$; for a horizontal side grasp, it advances 27.5 mm further along the horizontal tool axis rather than along world $z$.

Within a multi-waypoint request, intermediate `via_grasp_poses` inherit that arm's contact reference by default unless overridden on an individual waypoint; subsequent separate requests do not inherit the previous call's selection. During transport, keep a consistent contact reference or convert explicitly so measured `pad_center` coordinates are never mistaken for `finger_middle` coordinates. Low-level `link6` commands (`position_m + quaternion_xyzw` or legacy `robot_execute_ee`) control `link6` directly and reject grasp contact selectors.

Every grasp action receipt includes `grasp_contact_feedback`, which computes the achieved position of the selected contact cross-section from the terminal measured `link6` pose and reports both the 3D position error relative to the **requested final target** and the signed axial shortfall `approach_shortfall_m` along the requested approach axis (where positive values indicate the fingers stopped short of the requested depth). If an action partially executes, fails, or stops early, the final target may never have been attempted, so this comparison is strictly relative to the requested final target rather than a claim of completion. In observations, `robot_state.arms[arm].grasp_pose` always reports the default `pad_center` reference (explicitly labeled as such). This geometric feedback neither detects physical object contact nor triggers automatic retries, gripper closure, or simulation steps.

When an approach stops short with a tracking error, inspect the measured contact cross-section position alongside the RGB images before deciding whether to adjust the target or adapt the grasp strategy—requesting a descent never proves the descent succeeded. Before commanding a deep grasp, check clearance for the palm, fingertips, support surface, and neighboring objects, and verify actual retention after closing and lifting. Default calibration, speeds, motion planners, and official step counting are unchanged by contact reference selection.

## Planning, Execution, and Step Counting

Under the default `dual_arm_progression="paired"`, each waypoint pair executes as follows:

1. Plan left and right arm trajectories independently from the same unadvanced measured robot state. If either arm fails planning, that waypoint pair is not dispatched (while preserving step counts and feedback from any earlier completed pairs).
2. Resample each arm's trajectory at its requested speed and compile any in-motion gripper schedule. Merge corresponding row indices across both arms; the shorter trajectory holds its terminal joint target and gripper command while the longer trajectory finishes, without speeding up the longer arm or requiring simultaneous arrival.
3. Dispatch row by row, where each row contains 6 left arm joints, 1 left gripper command, 6 right arm joints, and 1 right gripper command (14 values total). Each merged row counts as one native action step; cancellation, timeouts, and native episode termination are handled on the shared timeline, and tracking guards (when enabled) monitor both arms.
4. Check measured endpoints for both arms. If either arm exceeds its position or orientation tolerance, halt the entire request without dispatching post-arrival gripper phases or subsequent waypoints.
5. For the final waypoint pair, `after_motion` gripper ramps begin only after both arms pass their endpoint checks. Standard execution rebuilds slow gripper sequences for both arms from their latest measured openings while enforcing per-arm speed limits and verifying remaining shared step budgets against the merged row count (the shorter gripper sequence holds its terminal goal until the other finishes). This prevents natural passive jaw drift during arm motion from causing a first-step jump against a stale initial gripper command. Executing an explicit cached preview plan dispatches its exact cached rows while enforcing start-state drift guards without silent recompilation.

For a single paired segment, the planned native step count is:

```text
max(left_arm_motion_rows, right_arm_motion_rows)
+ max(left_gripper_after_motion_rows, right_gripper_after_motion_rows)
```

(where any row subdivision needed for `during_motion` gripper schedules is already included in each arm's motion row count). Multi-segment requests sum across pairs and share the existing per-request cap of 10 seconds of simulation time (250 native steps)—the budget is shared, not 250 steps per arm. Actual step consumption is recorded in `executed_native_steps` against the unchanged episode budget.

Under explicit `dual_arm_progression="independent"`, execution begins only after the first segment of both arms compiles successfully; thereafter, each arm tracks its own waypoint index, motion/gripper phase, and local row cursor independently. Each dispatched native row merges the current command from each arm regardless of whether the two arms are in the same phase. As soon as one arm finishes a segment and passes its own measured endpoint check, it immediately plans its next segment from the current measured state while the opposite arm continues dispatching its already compiled rows (simulation pauses during planner calls rather than burning hold steps).

An `after_motion` gripper phase on the final segment requires only its own arm to pass its endpoint check, rebuilding the ramp from current measured opening and overlapping with the opposite arm's ongoing motion. **Note that this semantics allows one arm to open and release an object before the opposite arm subsequently fails, with no physical rollback**—use independent post-motion release only when releasing does not depend on the other arm's arrival or support. When multiple phases finish on the exact same native row, all finishing arms are checked before any new gripper ramp or waypoint is dispatched; if any check fails, execution halts immediately. Once an arm finishes its entire route, it holds its final command, and both arms' endpoints are re-verified at the end of the request so post-completion drift on the earlier-finishing arm is never misreported as `completed`.

When all segments succeed without replanning surprises, total shared rows under `"independent"` are roughly `max(sum(left_motion + left_after_gripper), sum(right_motion + right_after_gripper))` without artificially slowing the shorter arm. Because later waypoints are planned online from live measured states and may still halt early on tracking, contact, or planning failures, arithmetic on static pre-planned lengths is only an estimate, and both arms still share the single 250-step request cap.

Use `"paired"` whenever handoffs, cooperative carrying, or entering a shared workspace requires both arms to reach position together, and split requests whenever visual confirmation of grasp stability is needed before the other arm acts. Independent progression eliminates intra-request synchronization waits inside a single dual-arm call, but it cannot automatically parallelize two separate single-arm tool calls—group independent preparation and transport stages into a single batch at planning time without dropping necessary observations.

Sequential solver calls on CPU/GPU do not advance simulation time and do not prevent simultaneous command dispatch once compiled. Separate single-arm tool invocations remain serialized by Harness's single-writer lock and are never auto-merged.

## Low-Level `EEAction` and Receipt Feedback

`robot_execute_ee` supported dual-arm paths via `path.kind="backend_free"`. In `"paired"` mode, every element of `path.waypoints` specified `link6` poses for both `left` and `right`. In `"independent"` mode, the first element required both arms, while trailing elements after the shorter route finished contained only the longer arm—for example, `[{"left": L0, "right": R0}, {"right": R1}, {"right": R2}]` specified one target for the left arm and three for the right. Each arm's waypoints had to form a contiguous prefix starting at index `0` (gapped lists such as `[{"left": L0, "right": R0}, {"right": R1}, {"left": L2, "right": R2}]` were rejected, and omitting an arm on trailing entries meant its route had finished rather than adding a new hold waypoint).

Top-level speed fields served as shared defaults, while `action.arm_options.left/right` could override motion, gripper, or waypoint speeds per arm. Harness resolved all speed presets into numeric scales before archiving both the raw selections and resolved per-arm values, passing only numeric scales to the worker.

Receipts include `arm_results`, `waypoint_results`, and `failed_arm` / `failed_arms` when applicable. Planned traces archive the merged 14-DOF `actions` array alongside per-arm resampled joint paths, speeds, planned motion/gripper row counts, and peer-wait hold counts. Shared executed step counts are counted once, and full measured trajectories, interpolated actuator commands, and native step counters are archived as usual. Planning failure feedback identifies the failing arm and tags private solver diagnostics accordingly.

In `"independent"` mode, `waypoint_results` records per-arm checks tagged with `arm`, `waypoint_index`, `sim_step`, and `phase` (`"paired"` mode likewise tags residuals with waypoint index and timestep). On an interrupted receipt, the last recorded endpoint check for an arm may reflect an earlier waypoint rather than its final target. In independent traces, top-level `actions` and `row_contexts` record the step-by-step shared commands actually scheduled (never claiming to contain uncompiled future segments), while `arms[arm].segments` retains the compiled per-arm segments, unexecuted remainders, and completed step counts. Native counter deltas remain the authoritative measure of step consumption, distinguishing faulted native calls that consumed budget from completed steps.

In `resampled` mode, `robot_capabilities` advertises `dual_arm=true`, `max_simultaneous_arms=2`, and `dual_arm_progression={"available": true, "modes": ["paired", "independent"], "default": "paired"}`. `feedback` mode rejects dual-arm paths.

## Route and Object Constraints

Dual-arm execution dispatches two independently planned native trajectories on a shared clock. The native motion planner checks only single-arm self-collision and the static tabletop—**it provides no joint inter-arm collision avoidance, dynamic task-object collision avoidance, or closed-chain rigid-body constraint for two arms holding the same object**. The model must account for the full swept volume of both arms, open fingers, held objects, and nearby obstacles; two individually reachable endpoints never guarantee that both arms can pass through the workspace simultaneously without colliding.

Appropriate uses include non-overlapping dual-arm approaches, withdrawing a donor hand while the receiving hand moves in an uncontested region, and other stages with verified spatial separation. During a handoff, whether the receiving hand actually supports the object must be verified from feedback before the donor hand releases—never rely on an unmodeled intra-request assumption. As always, `completed` means only that the commanded schedule finished, not that a grasp, handoff, or task succeeded.

## Joint Replanning and Information Boundaries

When enabled, optional joint-space replanning uses the native cuRobo solver with the requested target, measured joint positions, URDF joint limits, static URDF kinematics, base calibration, and the static tabletop model. It never runs speculative physics rollouts, state rollbacks, or queries against ground-truth object poses, future rewards, or evaluator scores. Planner invocations do not consume native action steps; only dispatched `take_action` rows count against the step budget.

These capabilities constitute a control condition equipped with a robot kinematic model and motion planner rather than a raw pixels-to-torques policy. While the upstream RoboDojo README notes that leaderboard submissions undergo cloud anti-cheat verification, it does not publish a separate track specification for native motion planners; our information-boundary audit enforces strict separation from hidden task state but does not constitute upstream leaderboard certification. Benchmark reports should always disclose whether joint replanning and planner features were enabled (enabled by default in new runs, while explicit configs and frozen historical archives preserve their recorded settings).

Unequal-length independent progression is covered by targeted CPU regressions; those unit tests verify scheduling and accounting invariants rather than claiming end-to-end task success rate gains or physical collision safety.
