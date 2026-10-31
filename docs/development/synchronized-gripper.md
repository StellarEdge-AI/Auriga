# Synchronized gripper commands during an arm move

> Status: `robot_move_ee`, `robot_move_grasp`, `robot_preview_motion` and `robot_execute_plan` named below were removed in the 2026-10 refactor (trusted_legacy only; unreachable by the actor or programs). The same `gripper_motion={"start_fraction", "end_fraction"}` field with `gripper_goal_normalized` is now set on a `robot_action` move stage (preview, then execute the returned `plan_id`); the semantics below are unchanged. See [unified actions](unified-actions.md).

`robot_move_ee`, `robot_move_grasp`, and `robot_preview_motion` accept
`gripper_motion={"start_fraction": ..., "end_fraction": ...}` together with
`gripper_goal_normalized`. The gripper command changes while the arm follows its
planned route. A normalized goal of `0` means closed and `1` means open.

Both fractions must be finite numbers satisfying
`0 <= start_fraction < end_fraction <= 1`. `gripper_motion` requires a gripper
goal. Omitting `gripper_motion` preserves the existing behavior: the optional
gripper command is applied after arm motion. Omitting both fields retains the
latched gripper goal.

For `robot_move_grasp` with `via_grasp_poses`, the fractions apply only to the
final segment, from the last intermediate waypoint to the final target. Earlier
segments retain the existing gripper goal. Preview accepts one pose; review a
multi-segment route one segment at a time using fresh actual feedback.

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

The event arm must match the moving arm. This adds no multi-arm scheduling or
contact trigger.

## Timing and completion

Fractions refer to the final segment's scheduled native steps, not Cartesian
distance, measured arm progress, or wall-clock time. For a segment with `N`
scheduled rows, command row `i` uses progress `p = i/N`, for `i = 1, ..., N`.
Starting from the latched goal `g0`, the commanded value is:

```text
u = clamp((p - start_fraction) / (end_fraction - start_fraction), 0, 1)
gripper_command = g0 + u * (goal_normalized - g0)
```

The initial goal is command state; it is not inferred from measured finger
position. Before the window the command stays at `g0`; after the window it stays
at the requested goal. The first dispatched row may already contain a small
change when `start_fraction=0`.

If the original arm rows are too short for this schedule, the adapter uniformly
subdivides their joint intervals, including the interval from the measured
starting pose to the first row. Original joint knots remain in the route. It
adds enough rows to keep each gripper command increment within
`eef_gripper_dwell_step`. The existing 10-second motion-duration cap still
applies; an infeasible schedule is rejected. The added steps consume the normal
native action budget. Changing the fractions can therefore change the duration
of the arm motion.

There is no hidden settling phase after the synchronized schedule. A completed
action means the commanded schedule completed. The physical fingers can lag,
meet an object, or fail to reach the commanded opening. Inspect the action
receipt, measured gripper positions, and final RGB before deciding what happened.
Completion does not establish grasp retention, contact, release, or task success.

## Preview, review, and execution

Preview computes the same scheduled rows that execution will use. Its raw
`native_rows` contain exact 14-value commands, including both gripper columns.
The final segment's `planned_trace.gripper_motion` records the fractions,
initial and final goals, per-step goal values, original and scheduled step
counts, `timing_basis="final_segment_native_steps"`, and command-only completion
semantics. `robot_execute_plan` reuses those cached rows. The action digest binds
the event and its fractions; changing the schedule requires a fresh preview.
Stale state or scene evidence also requires a fresh preview.

The numerical collision review constructs a conservative finger envelope from
the measured opening, the latched command, and every command in cached
`native_rows`. It checks the entire range between those openings along the arm
trajectory; it does not assume that the measured fingers track each scheduled
command. A partial-opening schedule can therefore have a smaller envelope than
the full nominal 0–44 mm range. Missing command provenance falls back to that
full range. Schedule metadata alone does not replace the cached command rows.

A `contact_entity_id` permits only active-finger contact during the final
contiguous approach within 60 mm and 20 degrees of the final tool pose. The
permission can span several native intervals. Subdividing the scheduled rows
does not restrict contact to the last interval, and starting or ending the
gripper ramp earlier does not expand the geometric permission. An earlier visit
to the target followed by an excursion outside that final approach still
blocks execution. Wrist and forearm intersections remain checked throughout.

An unblocked review remains conditional geometry evidence. Missing finger
geometry, unseen or omitted scene geometry, physical tracking error, contact
dynamics, and released-object motion remain unknown. A held-object proxy assumes
the object stays attached to the tool; it cannot predict an object released by
opening during motion. Reobserve after release or slip before planning its next
interaction.

## Example: open an empty gripper during the first half of an approach

Choose the target pose and conservative obstacle proxies from the current
observation. This is an empty-gripper approach, so no held-object proxy is needed.
The tool arguments are:

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

Submit these to `robot_preview_motion`. The coordinates are illustrative, and
the empty obstacle list provides no task-object geometry; replace both with
current evidence. Inspect the returned risks, unknowns, and overlays. If the
review permits execution, call `robot_execute_plan` with
`{"plan_id": "<returned plan_id>"}`. Inspect the resulting receipt, measured
state, and RGB. To issue the same motion directly where review is not required,
use `robot_move_grasp` with the motion fields above and omit `obstacles`.

CPU regression coverage was in `tests/test_synchronized_motion_review.py` (removed with the legacy tools in 2026-10); runtime
scheduling and preview reuse have their own adapter and gateway tests. These
checks do not establish native simulator tracking or grasp performance.

## Verification

Run the full CPU suite from the repository with:

```bash
mamba run -n Auriga python -m unittest discover -s tests -q
```

The web phase model is checked with `npm --prefix web run check`; after
installing the locked dependencies with `npm --prefix web ci`,
`npm --prefix web run build` checks the production build. These checks
establish CPU behavior and build compatibility; they do not run a native
simulator, live browser integration, or task-success experiment.
