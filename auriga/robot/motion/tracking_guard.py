"""Detect sustained following error for one arm without issuing robot commands."""

import math
from numbers import Integral, Real


def _finite_real(value, name):
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite real number")
    try:
        result = float(value)
    except (OverflowError, ValueError) as error:
        raise ValueError(f"{name} must be a finite real number") from error
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite real number")
    return result


def _joints(values, name):
    try:
        if len(values) != 6:
            raise ValueError(f"{name} must contain exactly six arm joints")
        result = [_finite_real(value, name) for value in values]
    except TypeError as error:
        raise ValueError(f"{name} must contain exactly six arm joints") from error
    if len(result) != 6:
        raise ValueError(f"{name} must contain exactly six arm joints")
    return result


class TrackingGuard:
    """Observe one measured sample after each native row for the active arm.

    Create a fresh instance for each action. Inputs are the six commanded and
    measured arm joints in radians, with no gripper value. Joint coordinates
    are compared directly, without wrapping rotations. A diagnostic does not
    stop or correct motion; the caller owns stopping and returning feedback.
    """

    def __init__(self, threshold_rad=0.5, consecutive_steps=5):
        self.threshold_rad = _finite_real(threshold_rad, "threshold_rad")
        if self.threshold_rad <= 0:
            raise ValueError("threshold_rad must be positive")
        if (isinstance(consecutive_steps, bool) or not isinstance(consecutive_steps, Integral)
                or consecutive_steps < 1):
            raise ValueError("consecutive_steps must be a positive integer")
        self.consecutive_steps = int(consecutive_steps)
        self._streak = 0

    def observe(self, command, measured):
        commanded = _joints(command, "command")
        actual = _joints(measured, "measured")
        residuals = [abs(goal - value) for goal, value in zip(commanded, actual)]
        if not all(math.isfinite(value) for value in residuals):
            raise ValueError("joint residual must be finite")
        joint_index = max(range(6), key=residuals.__getitem__)
        error = residuals[joint_index]
        self._streak = self._streak + 1 if error > self.threshold_rad else 0
        if self._streak < self.consecutive_steps:
            return None
        return {"max_joint_error_rad": error, "threshold_rad": self.threshold_rad,
                "consecutive_steps": self._streak, "joint_index": joint_index,
                "commanded_q_rad": commanded, "measured_q_rad": actual}
