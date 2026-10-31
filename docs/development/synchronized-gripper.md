# Synchronized Gripper Commands During Arm Motion

> **Status**: `robot_move_ee`, `robot_move_grasp`, `robot_preview_motion`, and `robot_execute_plan` named below were removed in the 2026-10 refactor (`trusted_legacy` only; unreachable by actors or programs). The same `gripper_motion={"start_fraction", "end_fraction"}` field paired with `gripper_goal_normalized` is now set on a `robot_action` `move` stage (preview first, then execute the returned `plan_id`), and the scheduling semantics below remain unchanged. See [Unified Actions](unified-actions.md).

`robot_move_ee`, `robot_move_grasp`, and `robot_preview_motion` accept `gripper_motion={"start_fraction": ..., "end_fraction": ...}` together with `gripper_goal_normalized`. This schedules the gripper command to ramp while the arm follows its planned trajectory, where a normalized goal of `0` means closed and `1` means open.

Both fractions must be finite numbers satisfying `0 <= start_fraction < end_fraction <= 1`, and `gripper_motion` requires `gripper_goal_normalized`. Omitting `gripper_motion` preserves the default post-motion behavior (applying the gripper command after arm arrival), while omitting both fields retains the latched gripper goal.

For multi-waypoint moves (`via_grasp_poses`), the fractional window applies only to the final segment—from the last intermediate waypoint to the final target—while earlier segments retain the existing gripper goal. Legacy single-pose preview accepted one target pose at a time; in `robot_action`, each `move` stage is its own segment and can carry its own `gripper_motion` window.

The equivalent canonical `EEAction.gripper_events` entry is:

```json
{
  "arm": "left",
  "phase": "during_motion",
  "goal_normalized": 1.0,
  "start_fraction": 0.0,
  "end_fraction": 0.5
}
```

The event's `arm` must match the moving arm; this mechanism adds no cross-arm trigger or physical contact trigger.

## Timing and Completion Semantics

Fractions are defined over the final segment's scheduled native steps—not Cartesian arc length, measured arm progress, or wall-clock time. For a segment with `N` scheduled rows, command row `i` (`i = 1, ..., N`) has normalized progress `p = i / N`. Starting from the latched command `g0`, the scheduled command is:

```text
u = clamp((p - start_fraction) / (end_fraction - start_fraction), 0, 1)
gripper_command = g0 + u * (goal_normalized - g0)
```

Here `g0` is the prior command state rather than an inference from measured finger position. Before `start_fraction`, the command stays at `g0`; after `end_fraction`, it stays at `goal_normalized`. When `start_fraction=0`, the very first dispatched row already includes the first incremental step.

If the arm trajectory has too few rows to satisfy the gripper rate limit across the requested window, the adapter uniformly subdivides the arm's joint intervals—including the first interval from the measured starting joints to row 1—while keeping all original joint knots on the path. It inserts just enough rows to keep every per-step gripper delta within `eef_gripper_dwell_step`. Because added rows count against both the 10-second per-action duration cap (250 native steps) and the episode action budget, narrowing `[start_fraction, end_fraction]` can lengthen the scheduled duration of the arm move; schedules exceeding the cap are rejected.

No hidden post-motion settling phase is appended after a synchronized schedule. Action `completed` means only that the commanded schedule finished dispatching: physical fingers may lag, encounter an obstacle, or stop short of the commanded aperture. Always inspect the action receipt, measured finger positions, and terminal RGB frame before concluding whether a grasp, release, or task goal succeeded.

## Preview, Review, and Execution

Preview compiles the exact scheduled rows that execution will dispatch. Its raw `native_rows` contain the full 14-DOF commands (including both gripper columns), and `planned_trace.gripper_motion` on the final segment records the fractions, initial and final goals, per-step command values, original and subdivided step counts, `timing_basis="final_segment_native_steps"`, and command-only completion semantics. Executing the cached `plan_id` dispatches those exact rows; because the action digest binds the gripper event and its fractions, changing the schedule or invalidating the observation/state requires a fresh preview.

Numerical collision review constructs a conservative finger envelope spanning the measured opening, the latched command, and every scheduled command in `native_rows`, checking the full range across the arm trajectory rather than assuming the physical fingers track each step without lag. A partial-opening schedule therefore produces a tighter envelope than the full nominal `0–44 mm` span (falling back to the full range if command provenance is missing).

A `contact_entity_id` permits active-finger contact only during the final contiguous approach window within `60 mm` and `20°` of the final tool pose. Subdividing rows for a gripper ramp does not shrink that geometric window to a single step, nor does starting or ending the gripper ramp early widen the permitted contact zone; visiting the target zone early and then departing outside the approach window still blocks execution, and wrist/forearm links remain checked throughout.

Passing numerical collision review remains conditional geometric evidence: unmodeled finger details, occluded or omitted scene geometry, physical tracking error, contact dynamics, and post-release object motion remain unknown. In particular, a held-object bounding box assumes rigid attachment to the hand and cannot predict the trajectory of an object released mid-motion—always re-observe after releasing or slipping before planning subsequent interactions.

## Example: Opening an Empty Gripper During the First Half of an Approach

Select the target pose and conservative obstacle bounding boxes from the current observation (no held-object box is needed for an empty-gripper approach):

```json
{
  "observation_id": "<current observation ID>",
  "arm": "left",
  "grasp_position_m": [0.20, 0.10, 0.30],
  "angles_deg": [0, 0, 0],
  "gripper_goal_normalized": 1.0,
  "gripper_motion": {"start_fraction": 0.0, "end_fraction": 0.5},
  "obstacles": []
}
```

In the current unified interface, pass the same motion and `gripper_motion` fields inside a `kind="move"` stage of `robot_action` (replacing the illustrative coordinates and empty obstacle list above with live scene evidence), inspect the returned preview cost, trajectory, unknowns, and overlays, and then call `robot_action(mode="execute", plan_id="<returned plan_id>")`.

CPU regression coverage for runtime scheduling and preview reuse lives in the adapter and gateway test suites (legacy `tests/test_synchronized_motion_review.py` was removed with the legacy tools in 2026-10). Those CPU checks verify scheduling invariants rather than live simulator tracking or grasp success.

## Verification

Run the full CPU test suite from the repository root:

```bash
mamba run -n Auriga python -m unittest discover -s tests -q
```

Verify the web workbench type check with `npm --prefix web run check` and the production bundle build with `npm --prefix web run build` (after `npm --prefix web ci`). These checks validate CPU logic and build compatibility without launching the native simulator, live browser sessions, or benchmark evaluations.
