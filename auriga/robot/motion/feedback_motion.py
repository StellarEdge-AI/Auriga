"""Measured arrival and stall detection for bounded native target execution.

This module never advances physics. The worker retains ownership of native
dispatch, cancellation, terminal checks, and the action's total step budget.
"""

import math

import numpy as np


VELOCITY_TOLERANCE_RAD_S = 0.1
SETTLED_SAMPLES = 2
STALL_WINDOW_NATIVE_STEPS = 15


def _vector(value, size):
    result = np.asarray(value, dtype=float)
    if result.shape != (size,) or not np.isfinite(result).all():
        raise ValueError("feedback_requires_finite_measured_state")
    return result


def pose_error(measured, target):
    position = float(np.linalg.norm(_vector(measured["position_m"], 3) - _vector(target["position_m"], 3)))
    actual_q = _vector(measured["quaternion_xyzw"], 4)
    target_q = _vector(target["quaternion_xyzw"], 4)
    norms = float(np.linalg.norm(actual_q)), float(np.linalg.norm(target_q))
    if min(norms) < 1e-12:
        raise ValueError("feedback_requires_nonzero_quaternion")
    rotation = 2 * math.acos(float(np.clip(abs(np.dot(actual_q / norms[0], target_q / norms[1])), 0, 1)))
    return position, rotation


class ArrivalMonitor:
    """Compare fresh post-action measurements with one immutable endpoint.

Joint progress uses unwrapped distance to that endpoint, not arbitrary motion.
Pose progress and joint progress can independently postpone a stall. Instantaneous
velocity is used only for arrival: high velocity can coexist with no net progress
at native sampling boundaries (for example, repeated within-step oscillation).
The worker also retains an unconditional native-step cap.
"""

    def __init__(self, target_pose, target_q, initial_state, position_tolerance_m=0.005, orientation_tolerance_rad=0.1):
        if not math.isfinite(position_tolerance_m) or position_tolerance_m <= 0:
            raise ValueError("feedback_requires_positive_position_tolerance")
        if not math.isfinite(orientation_tolerance_rad) or orientation_tolerance_rad <= 0:
            raise ValueError("feedback_requires_positive_orientation_tolerance")
        self.target_pose = target_pose
        self.target_q = _vector(target_q, 6).copy()
        self.position_tolerance_m = position_tolerance_m
        self.orientation_tolerance_rad = orientation_tolerance_rad
        self.samples = self.settled = self.last_progress = 0
        first = self.measure(initial_state)
        self.best_pose_error = first["normalized_pose_error"]
        self.best_joint_error = first["max_joint_error_rad"]

    def measure(self, state):
        position, rotation = pose_error(state["ee_pose"], self.target_pose)
        speed = float(np.max(np.abs(_vector(state["qdot_rad_s"], 6))))
        joint = float(np.max(np.abs(_vector(state["q_rad"], 6) - self.target_q)))
        return dict(position_error_m=position, orientation_error_rad=rotation,
                    max_joint_velocity_rad_s=speed, max_joint_error_rad=joint,
                    normalized_pose_error=max(position / self.position_tolerance_m, rotation / self.orientation_tolerance_rad))

    def observe(self, state):
        value = self.measure(state)
        self.samples += 1
        quiet = value["max_joint_velocity_rad_s"] <= VELOCITY_TOLERANCE_RAD_S
        arrived = value["normalized_pose_error"] <= 1
        self.settled = self.settled + 1 if arrived and quiet else 0
        # Anchor significant improvements instead of resetting on numerical noise.
        if (value["normalized_pose_error"] < self.best_pose_error - 0.05
                or value["max_joint_error_rad"] < self.best_joint_error - 0.002):
            self.last_progress = self.samples
            self.best_pose_error = min(self.best_pose_error, value["normalized_pose_error"])
            self.best_joint_error = min(self.best_joint_error, value["max_joint_error_rad"])
        value.update(native_samples=self.samples, settled_samples=self.settled,
                     no_progress_native_steps=self.samples - self.last_progress)
        outcome = ("arrived" if self.settled >= SETTLED_SAMPLES else
                   "stalled" if self.samples - self.last_progress >= STALL_WINDOW_NATIVE_STEPS
                   else "continue")
        return outcome, value

    def settings(self):
        return dict(position_tolerance_m=self.position_tolerance_m, orientation_tolerance_rad=self.orientation_tolerance_rad,
                    velocity_tolerance_rad_s=VELOCITY_TOLERANCE_RAD_S,
                    settled_samples=SETTLED_SAMPLES, stall_window_native_steps=STALL_WINDOW_NATIVE_STEPS,
                    pose_progress_threshold=0.05, joint_progress_threshold_rad=0.002,
                    stall_semantics="no significant best pose or joint-target error improvement, regardless of instantaneous velocity or unsettled pose arrival",
                    target_semantics="fixed unwrapped joint endpoint; measured EE arrival")
