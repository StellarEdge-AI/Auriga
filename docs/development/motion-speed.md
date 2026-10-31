# Per-Action Motion Speed Control

> **Status**: `robot_move_grasp`, `robot_move_ee`, `robot_move_dual_grasp`, `robot_execute_ee`, `robot_gripper`, `robot_preview_motion`, `robot_execute_plan`, and `depth_move` mentioned below were removed in the 2026-10 refactor (`trusted_legacy` only; unreachable by actors or programs). Speed presets and numeric scales are now configured per stage in `robot_action`: `move` stages accept `speed_preset` / `speed_scale` and `gripper_speed_preset` / `gripper_speed_scale`, while `gripper` stages accept the latter two (replacing per-waypoint speed lists with per-stage settings). See [Unified Actions](unified-actions.md) for the active `robot_action` schema.

New runs default to `features.motion_execution_mode="resampled"`. Astra specifies speed per request (or per stage) without setting a global speed state or inheriting the previous action's speed. The adapter still plans the trajectory with the native motion planner and then adjusts the joint-command resampling spacing according to the selected speed.

Prefer named speed presets for routine control; use continuous numeric scales when finer adjustment is needed. Presets map to fixed resampling multipliers relative to the nominal baseline rather than compounding on top of `features.default_motion_speed_scale`.

| Control Target | Named Presets & Fixed Multipliers | Numeric Scale Range | Default When Both Are Omitted |
| --- | --- | --- | --- |
| Robot arm | `speed_preset`: `slow` → `0.8`, `normal` → `1.0`, `fast` → `1.25`, `faster` → `1.5` | `speed_scale`: `0.25–2.0` | Uses `features.default_motion_speed_scale` (config default `1.0`) |
| Gripper | `gripper_speed_preset`: `slow` → `0.8`, `normal` → `1.0` | `gripper_speed_scale`: `0.25–1.0` | `1.0` (standard opening/closing rate) |

Passing both a preset and a numeric scale for the same target is rejected even if their numeric values match. Arm and gripper speeds can be chosen independently using different forms—for example, combining `speed_preset="slow"` with `gripper_speed_scale=0.5`. The gripper does not support `fast` or `faster` presets.

In legacy single-call tools, `robot_move_grasp`, `robot_move_ee`, `robot_preview_motion`, and RGB-D `depth_move` accepted all four parameters, `robot_gripper` accepted the two gripper speed parameters, and `robot_execute_ee` accepted them at the top level of `action`. Specifying a gripper speed requires a gripper goal or event on the same stage/request.

In legacy multi-waypoint requests, `waypoint_speed_presets` or `waypoint_speed_scales` (mutually exclusive) specified per-segment speeds ordered along `via_grasp_poses` plus the final target (`len(via_grasp_poses) + 1`), where entry `i` governed the **entire incoming segment arriving at waypoint `i`** and `null` inherited the request-level arm speed (or the run default if top-level speed was also omitted). In legacy `robot_move_dual_grasp`, `left` and `right` accepted independent speed fields while sharing the 14-DOF timeline; see [Synchronized Dual-Arm Motion](dual-arm-motion.md).

For example, moving at the default speed to a high-clearance pre-contact waypoint before slowing down for final contact and gripper closure took the form:

```json
{
  "waypoint_speed_presets": [null, "slow"],
  "gripper_goal_normalized": 0,
  "gripper_speed_preset": "slow"
}
```

When top-level arm speed was omitted in a run with `default_motion_speed_scale=1.25`, `[null, "slow"]` resolved to `[1.25, 0.8]` (if top-level `speed_preset="slow"` was also set, `null` inherited `0.8`; passing `["fast", "slow"]` overrode the first segment explicitly, and `waypoint_speed_scales=[1.3, 0.55]` provided continuous numeric control).

Pre-contact waypoints must leave sufficient clearance for the entire arm and any held object; the harness never auto-selects speeds based on obstacle distance or contact detection. Because no model reasoning occurs between waypoints or stages inside a single uninterrupted execution, split actions or insert an observation checkpoint whenever fresh visual feedback is needed to adjust targets, and avoid setting an entire long-distance free-space transit to `slow` merely because the final few centimeters involve contact. Typical per-stage settings in `robot_action` (with poses derived from the current observation):

| Manipulation Phase | `robot_action` Stage Kind | Example Speed Parameters |
| --- | --- | --- |
| Routine transit with ample clearance | `move` stage | Omit speed (uses run default) or set `speed_preset: "normal"` |
| Long free-space transit suitable for acceleration | `move` stage | `speed_preset: "fast"` (or `"faster"` when appropriate) |
| Final pre-contact approach, initial short lift, or in-container descent | `move` stage | `speed_preset: "slow"` |
| Controlled grasp closure | `gripper` stage | `goal_normalized: 0, gripper_speed_preset: "slow"` |
| Controlled release after verifying support | `gripper` stage | `goal_normalized: 1, gripper_speed_preset: "slow"` |

Slowing the arm never automatically slows the gripper; specify both explicitly when needed. Gripper speed scaling modifies the intermediate command ramp dispatched at each native step rather than merely adding a post-actuation wait. It is not closed-loop force control and does not guarantee an object will not slip.

## Command Resampling and Budget Semantics

- When the effective arm speed multiplier is `1.0`, the historical resampled command rows are preserved verbatim; similarly, omitting gripper speed or setting `"normal"` / `1.0` preserves the standard gripper ramp. For any other arm multiplier (including when speed is omitted and `default_motion_speed_scale != 1.0`), bounded path resampling preserves sharp corners and exact endpoints while bounding path simplification error to at most `0.001 rad` per joint and enforcing per-step joint delta limits (including from the measured start state to the first command row). Resampling does not perform additional collision checking.
- Resolved arm multipliers (`speed_scale`) scale joint-space command spacing per native step—they are not Cartesian end-effector speeds in m/s and do not guarantee wall-clock or simulation duration scales linearly, since integer step rounding, path curvature, native joint velocity limits, physical tracking error, and planner variance all affect step counts.
- When the resolved gripper speed multiplier is `< 1.0`, the maximum normalized aperture change per native step is `0.18 * gripper_speed_scale`. Actual finger motion depends on the native PD actuator and physical contact; completing the command ramp does not prove the fingers reached the target opening or secured an object.
- In standard `after_motion` execution, once the arm passes its measured endpoint check, the slow gripper ramp is generated from the **live measured jaw aperture at arrival**, respecting speed limits, per-step aperture bounds, and remaining request step budgets (stopping before dispatching new gripper rows if the budget is exhausted). Executing an explicit cached `plan_id` dispatches its exact cached rows without recompiling, returning `gripper_start_state_changed` if passive jaw drift exceeds the first-step guard.
- In `during_motion` synchronized schedules, slow gripper ramps start from the initial command state across the specified fractional window, subdividing arm rows as needed to satisfy the per-step gripper delta cap.
- Every dispatched native `take_action` row counts as one official action step. Slower speeds generally increase step counts and faster speeds generally reduce transit steps, while terminal gripper dwell steps remain unchanged. The per-request cap (10 s / 250 native steps) and official episode step budgets (e.g., 700 native steps for `put_bottles_dustbin`) remain strictly enforced with zero uncounted background stepping.
- Cached preview plans bind the resolved speed values and exact command rows; changing speeds requires compiling a fresh preview.
- `robot_capabilities.action_speed` advertises availability, valid bounds, and semantics. The diagnostic `feedback` mode supports only default speed `1.0`, and `robot_wait` and policy prefixes do not accept speed parameters.

Action receipts and execution traces record the effective speed scales, planned motion/gripper step counts, and actual executed step counts alongside endpoint errors, cancellations, native terminations, and partial execution details. When speeds vary across segments, `waypoint_speed_scales` records the full resolved list, each segment trace records its own `speed_scale`, and the raw caller input is preserved under `action.speed_resolved.input`. The 250-step per-action cap accumulates across all stages and gripper ramps in the batch rather than resetting per stage.

## Runtime Default Speed and Ablations

`features.default_motion_speed_scale` sets the arm speed multiplier used when a move omits both `speed_preset` and `speed_scale` (defaulting to `1.0` in production configs). It changes only the fallback for omitted arm speed without altering named preset values or the default gripper speed (`1.0`). For example, when configured to `1.25`:

| Request Speed Specification | Effective Arm Multiplier |
| --- | ---: |
| Omitted | `1.25` |
| `speed_preset="normal"` | `1.0` |
| `speed_preset="slow"` | `0.8` |
| `speed_preset="fast"` | `1.25` |
| `speed_preset="faster"` | `1.5` |
| `speed_scale=0.8` | `0.8` |

Higher runtime defaults (such as `1.25` or `1.5`) should be used only in explicit experiment configurations; explicit preset or numeric selections on a stage always override the runtime default. When comparing against historical runs, verify that tool catalogs, prompts, and other conditions match, and audit how often the default fallback vs. explicit presets were actually used alongside task success and native step counts—never assume a higher config multiplier automatically speeds up an entire episode or saves steps (failing faster is not an efficiency gain).

Note that `slow` currently maps to `0.8` for both arms and grippers (compared to `0.6` in early revisions); passing an explicit numeric `speed_scale=0.6` still delivers exactly `0.6`, and historical archives retain their original recorded values.

## Integration with Operational Experience (Task Notes)

Historical default Task Notes (`operation-notes-v8`) paired speed control with manipulation heuristics: centering placements inside containers; slowing down for pre-contact approaches, grasp closure, initial lift, in-container descent, and release; choosing top-down vs. side grasps based on object geometry (uprighting supported objects first when needed); selecting fingertip vs. mid-finger contact depth based on shape, contact surface, estimated center of mass, and clearance without altering the tool-center convention; checking 3D clearance between the object bottom, overall envelope, fingers, and container rim when crossing container walls; and combining fast free-space transit with slow contact segments in a single batch unless intermediate visual feedback is required. Versioned Task Notes affect only new benchmarks with default notes enabled, while historical archives retain their exact recorded prompt text; see [Task Notes](task-notes.md).

## Speed Control and Joint-Space Winding Replanning

Larger resampling steps cannot eliminate a $2\pi$ full-turn detour produced by the native IK/trajectory planner. When `features.joint_replan` is enabled (the default), after native Cartesian planning succeeds, the worker searches for a closer $2\pi$-equivalent joint configuration within URDF limits, verifies full-link forward-kinematics equivalence and the original Cartesian target, and calls native `plan_joint` once. The replanned joint trajectory is adopted only if it is valid and strictly reduces **both** the resampled row count and total joint travel—never by wrapping or mutating raw trajectory angles in place.

Cost comparison between the original Cartesian plan and the joint-space candidate evaluates both trajectories at the **segment's actual resolved speed scale** via `_resample_action_plan` (in both preview and execution) rather than assuming a uniform scale of `1.0`. The comparison measures arm motion rows, while synchronized or post-motion gripper phases are compiled afterward. Joint replanning applies only to unconstrained `resampled` moves (not `feedback` mode).

`features.joint_replan` defaults to `true` and can be toggled via `--joint-replan` / `--no-joint-replan`. If native joint planning fails, offers no strict reduction, or has less than 15 seconds of remaining action timeout, the original Cartesian plan is retained; native exceptions, timeouts, and cancellations still halt dispatch immediately. Joint replanning does not guarantee every detour is eliminated and does not model dynamic task objects, held objects, or inter-arm collisions.

Private `planning_diagnostics` for executed actions are archived across all observation profiles to track how often joint replanning is triggered and adopted; under the calibrated profile, only permitted metadata enters the actor reply without altering action execution or step counts.
