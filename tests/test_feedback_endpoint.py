"""Feedback endpoint selection and rejection at the native dispatch boundary."""
import math
import threading
import unittest

import numpy as np

from test_feedback_motion import feedback_adapter
from test_joint_replan_runtime import motion_action, periodic_pose, replan_adapter


class FeedbackEndpointTests(unittest.TestCase):
    def execute(self, adapter, action=None):
        return adapter.execute(action or motion_action(gripper=True), lambda event: None, threading.Event())

    def test_winding_pose_plan_selects_legal_short_endpoint_once_on_both_arms(self):
        for arm in ("left", "right"):
            with self.subTest(arm=arm):
                adapter, planner, rows = feedback_adapter(arm=arm)
                planner.wound = True
                receipt = self.execute(adapter, motion_action(arm=arm, gripper=True))
                self.assertEqual(receipt["status"], "completed")
                self.assertEqual(len(planner.pose_calls), 1)
                self.assertEqual(planner.joint_calls, [])
                self.assertEqual(receipt["planned_trace"]["executed_motion_steps"], 6)
                self.assertEqual(receipt["executed_native_steps"], 12 if arm == "left" else 8)
                np.testing.assert_allclose([row[arm + "_arm_joint_state"][0] for row in rows], -.125)
                diagnostic = receipt["planned_trace"]["feedback_endpoint"]
                self.assertEqual(diagnostic["turn_shifts"], [-1, 0, 0, 0, 0, 0])
                self.assertTrue(diagnostic["validated"])
                self.assertTrue(diagnostic["changed"])
                self.assertLess(diagnostic["all_link_fk_max_abs_error"], 1e-12)
                self.assertGreater(receipt["planned_trace"]["reference_resampled_steps"], 100)
                self.assertAlmostEqual(planner.originals[0][-1, 0], math.tau - .125)

    def test_nearest_endpoint_uses_measured_unwrapped_state_and_respects_joint_six_limit(self):
        adapter, robot, planner, _ = replan_adapter()
        current = np.array([6., 0., 0., 0., 0., -1.])
        goal = np.array([-.125, 0., 0., 0., 0., 2.8])
        diagnostic = {}
        selected = adapter._feedback_endpoint(robot, periodic_pose(goal), current, goal, .005, diagnostic)
        self.assertAlmostEqual(selected[0], math.tau - .125)
        self.assertEqual(selected[5], 2.8)  # -3.483... would be closer but illegal.
        self.assertEqual(diagnostic["turn_shifts"], [1, 0, 0, 0, 0, 0])

    def test_geometry_failure_rejects_before_any_native_step_or_jaw_command(self):
        adapter, planner, rows = feedback_adapter()
        planner.wound = True
        def invalid(*args):
            raise ValueError("unsupported_joint_replan_geometry")
        adapter._joint_replan_geometry = invalid
        receipt = self.execute(adapter)
        self.assertEqual((receipt["status"], receipt["reason"]), ("failed", "feedback_endpoint_invalid"))
        self.assertEqual(receipt["executed_native_steps"], 0)
        self.assertEqual(rows, [])
        self.assertFalse(getattr(adapter, "evaluator_failure", None))

    def test_fk_mismatch_nonrevolute_and_invalid_pose_reject_without_fallback(self):
        for failure in ("fk", "nonrevolute", "position", "orientation", "nan"):
            with self.subTest(failure=failure):
                adapter, planner, rows = feedback_adapter()
                planner.wound = True
                original = adapter._joint_replan_geometry
                def geometry(*args):
                    kinds, transforms, pose_error = original(*args)
                    if failure == "nonrevolute":
                        kinds[0] = "prismatic"
                    if failure == "fk":
                        def transforms(q):
                            result = np.eye(4)
                            result[0, 3] = q[0]
                            return {"tool": result}
                    if failure in {"position", "orientation", "nan"}:
                        pose_error = lambda q: {"position": (.0051, 0), "orientation": (0, .101),
                                                "nan": (float("nan"), 0)}[failure]
                    return kinds, transforms, pose_error
                adapter._joint_replan_geometry = geometry
                receipt = self.execute(adapter)
                self.assertEqual(receipt["reason"], "feedback_endpoint_invalid")
                self.assertEqual(receipt["executed_native_steps"], 0)
                self.assertEqual(rows, [])

    def test_requested_tighter_position_tolerance_is_validated(self):
        adapter, planner, rows = feedback_adapter()
        original = adapter._joint_replan_geometry
        def geometry(*args):
            kinds, transforms, _ = original(*args)
            return kinds, transforms, lambda q: (.002, 0.)
        adapter._joint_replan_geometry = geometry
        action = motion_action()
        action["path"]["position_tolerance_m"] = .001
        self.assertEqual(self.execute(adapter, action)["reason"], "feedback_endpoint_invalid")
        self.assertEqual(rows, [])

    def test_resampled_mode_keeps_original_wound_trajectory_without_geometry_lookup(self):
        adapter, robot, planner, rows = replan_adapter(enabled=False, wound=True, step_limit=1000)
        adapter.motion_execution_mode = "resampled"
        receipt = self.execute(adapter, motion_action())
        self.assertEqual(receipt["status"], "completed")
        self.assertGreater(receipt["executed_native_steps"], 100)
        self.assertAlmostEqual(rows[-1]["left_arm_joint_state"][0], math.tau - .125)
        self.assertEqual(adapter.geometry_calls, [])


if __name__ == "__main__":
    unittest.main()
