"""Bounded command scheduling; no contact, grasp or physical-arrival inference."""

import numpy as np


def synchronize(positions, start_q, initial_goal, event, max_step, max_native_steps):
    """Return a slowed polyline and jaw targets, or None if they exceed the cap.

    Each original interval is subdivided by the same integer factor. Keeping all
    original knots avoids cutting corners while slowing the path. Fractions
    refer to commanded native-step progress, not measured Cartesian distance.
    """
    count = len(positions)
    if not count or count > max_native_steps:
        return None
    start, end = event["start_fraction"], event["end_fraction"]
    goal = event["goal_normalized"]
    for factor in range(1, max_native_steps // count + 1):
        steps = count * factor
        progress = np.arange(1, steps + 1, dtype=float) / steps
        fraction = np.clip((progress - start) / (end - start), 0., 1.)
        targets = initial_goal + (goal - initial_goal) * fraction
        targets[progress >= end] = goal
        if np.max(np.abs(np.diff(np.r_[initial_goal, targets]))) <= max_step + 1e-12:
            break
    else:
        return None
    if factor > 1:
        previous = np.vstack([start_q, positions[:-1]])
        fractions = np.arange(1, factor + 1, dtype=float) / factor
        interpolated = previous[:, None, :] + (positions - previous)[:, None, :] * fractions[None, :, None]
        interpolated[:, -1, :] = positions  # Preserve every original knot exactly.
        positions = interpolated.reshape(-1, positions.shape[1])
    metadata = dict(phase="during_motion", start_fraction=start, end_fraction=end,
        initial_goal_normalized=initial_goal, goal_normalized=goal,
        goal_normalized_by_step=targets.tolist(), original_motion_steps=count,
        scheduled_motion_steps=steps, subdivision_factor=factor,
        timing_basis="final_segment_native_steps", max_goal_step=max_step,
        completion_semantics="command_schedule_only; inspect measured gripper and RGB")
    return positions, targets, metadata
