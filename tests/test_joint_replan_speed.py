"""Joint replan cost must match the actual segment's speed and dispatch rows."""

import threading
import unittest
from unittest.mock import patch

import numpy as np

from auriga.robot.motion.joint_replan import select_joint_replan
from test_feedback_motion import feedback_adapter
from test_joint_replan_runtime import motion_action, replan_adapter
from test_motion_preview_backend import preview_adapter


class JointReplanSpeedTests(unittest.TestCase):
    @staticmethod
    def capture(diagnostics):
        def select(**kwargs):
            result = select_joint_replan(**kwargs)
            diagnostics.append(kwargs["diagnostic"])
            return result
        return select

    @staticmethod
    def velocity_limits(adapter):
        adapter.arm_velocity_step_limits = {side: np.full(6, .2) for side in ("left", "right")}

    def test_each_waypoint_cost_matches_its_dispatched_rate_and_step_count(self):
        for arm in ("left", "right"):
            for overrides, speeds, counts in (
                    ({"speed_scale": .8}, [.8, .8], [5, 6]),
                    ({"speed_scale": .8, "waypoint_speed_scales": [1.25, .8]}, [1.25, .8], [3, 6])):
                with self.subTest(arm=arm, speeds=speeds):
                    adapter, _, planner, dispatched = replan_adapter(arm)
                    self.velocity_limits(adapter)
                    action = motion_action(arm, (-.18, -.41)) | overrides
                    diagnostics = []
                    with patch("auriga.robot.motion.joint_replan.select_joint_replan", side_effect=self.capture(diagnostics)):
                        result = adapter.execute(action, lambda event: None, threading.Event())
                    self.assertEqual(result["status"], "completed")
                    self.assertEqual(len(planner.joint_calls), 2)
                    segments = result["planned_trace"]["segments"]
                    self.assertEqual([d["speed_scale"] for d in diagnostics], speeds)
                    self.assertEqual([d["candidate"]["resampled_rows"] for d in diagnostics], counts)
                    self.assertEqual([s["planned_motion_steps"] for s in segments], counts)
                    self.assertEqual(len(dispatched), sum(counts))
                    self.assertEqual(result["executed_native_steps"], sum(counts))
                    for d, speed in zip(diagnostics, speeds):
                        self.assertTrue(d["selected"])
                        self.assertLessEqual(d["candidate"]["max_dispatch_delta_rad"], .05 * speed + 1e-12)
                    np.testing.assert_allclose(planner.joint_calls[1][0], [-.18] + [0.] * 5)

    def test_preview_uses_override_for_replan_and_reuses_exact_selected_rows(self):
        adapter, planner, dispatched = preview_adapter()
        adapter.joint_replan_enabled = True
        planner.wound = True
        self.velocity_limits(adapter)
        action = motion_action(angles=(-.18,)) | {"speed_scale": .8, "waypoint_speed_scales": [1.25]}
        diagnostics = []
        with patch("auriga.robot.motion.joint_replan.select_joint_replan", side_effect=self.capture(diagnostics)):
            preview = adapter.preview_motion(action)
        self.assertEqual(preview["status"], "planned")
        self.assertEqual(dispatched, [])
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0]["speed_scale"], 1.25)
        self.assertEqual(diagnostics[0]["candidate"]["resampled_rows"], 3)
        self.assertEqual(preview["planned_native_steps"], 3)
        result = adapter.execute(action | {"preview_plan_id": preview["plan_id"]},
                                 lambda event: None, threading.Event())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(planner.joint_calls), 1)
        np.testing.assert_array_equal([r["left_arm_joint_state"] for r in dispatched],
                                      np.array(preview["native_rows"])[:, :6])

    def test_feedback_does_not_optimize_a_trajectory_it_will_not_execute(self):
        adapter, planner, dispatched = feedback_adapter()
        adapter.joint_replan_enabled = True
        planner.wound = True
        result = adapter.execute(motion_action(), lambda event: None, threading.Event())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(planner.joint_calls, [])
        self.assertTrue(result["planned_trace"]["feedback_endpoint"]["changed"])
        self.assertEqual(result["executed_native_steps"], 6)
        np.testing.assert_allclose([r["left_arm_joint_state"][0] for r in dispatched], -.125)


if __name__ == "__main__":
    unittest.main()
