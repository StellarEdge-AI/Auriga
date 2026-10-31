"""Per-waypoint rates through native worker compilation and dispatch."""

from copy import deepcopy
import hashlib
import json
import threading
import unittest

import numpy as np

from test_joint_replan_runtime import motion_action
from test_motion_speed_runtime import speed_adapter


class WaypointSpeedRuntimeTests(unittest.TestCase):
    def execute(self, adapter, action):
        return adapter.execute(action, lambda event: None, threading.Event())

    def test_fast_approach_slow_contact_share_one_request_and_preserve_knots(self):
        adapter, planner, rows = speed_adapter()
        action = motion_action(angles=(-.3, -.6), gripper=True)
        action.update(speed_scale=.6, waypoint_speed_scales=[1.5, .6], gripper_speed_scale=.6)
        result = self.execute(adapter, action)
        self.assertEqual(result["status"], "completed")
        segments = result["planned_trace"]["segments"]
        self.assertEqual([part["speed_scale"] for part in segments], [1.5, .6])
        self.assertEqual(result["waypoint_speed_scales"], [1.5, .6])
        self.assertEqual([part["speed_scale"] for part in result["waypoint_results"]], [1.5, .6])
        self.assertEqual(segments[0]["planned_motion_steps"], 4)
        self.assertEqual(segments[0]["planned_gripper_steps"], 0)
        self.assertGreaterEqual(segments[1]["planned_motion_steps"], 10)
        self.assertGreater(segments[1]["completed_gripper_steps"], 0)
        self.assertEqual(len(planner.pose_calls), 2)
        self.assertAlmostEqual(planner.pose_calls[1][0][0], -.3)
        start = np.zeros(6)
        for part in segments:
            q = np.array(part["q_rad"])
            bound = np.array(part["command_step_limits_rad"])
            self.assertTrue((np.abs(np.diff(np.vstack([start, q]), axis=0)) <= bound + 1e-12).all())
            start = q[-1]
        self.assertAlmostEqual(segments[0]["q_rad"][-1][0], -.3)
        self.assertAlmostEqual(segments[1]["q_rad"][-1][0], -.6)
        self.assertEqual(result["executed_native_steps"], len(rows))
        self.assertEqual(result["executed_control_steps"], len(rows) * 10)

    def test_omitted_list_matches_explicit_uniform_list_without_changing_legacy_rows(self):
        for top in (1., .6, 1.5):
            adapter, _, implicit = speed_adapter()
            other, _, explicit = speed_adapter()
            action = motion_action(angles=(-.2, -.4)) | dict(speed_scale=top)
            expected = self.execute(adapter, action)
            actual = self.execute(other, action | dict(waypoint_speed_scales=[top, top]))
            self.assertEqual(expected["status"], "completed")
            self.assertEqual(actual["status"], "completed")
            self.assertEqual(implicit, explicit)
            self.assertEqual(expected["waypoint_speed_scales"], [top, top])

    def test_preview_binds_single_segment_override_and_reuses_exact_rows(self):
        adapter, _, rows = speed_adapter(preview=True)
        action = motion_action() | dict(speed_scale=.6, waypoint_speed_scales=[1.5])
        preview = adapter.preview_motion(action)
        self.assertEqual(preview["status"], "planned")
        self.assertEqual(preview["planned_trace"]["speed_scale"], 1.5)
        self.assertEqual(preview["waypoint_speed_scales"], [1.5])
        np.testing.assert_allclose(preview["planned_trace"]["command_step_limits_rad"], [.075] * 6)
        result = self.execute(adapter, action | dict(preview_plan_id=preview["plan_id"]))
        self.assertEqual(result["status"], "completed")
        actual = [[*r["left_arm_joint_state"], *r["left_ee_joint_state"],
                   *r["right_arm_joint_state"], *r["right_ee_joint_state"]] for r in rows]
        np.testing.assert_array_equal(actual, preview["native_rows"])
        self.assertEqual(result["planned_trace"]["speed_scale"], 1.5)

    def test_modified_preview_segment_speed_rejected_without_native_steps(self):
        adapter, _, rows = speed_adapter(preview=True)
        action = motion_action() | dict(speed_scale=.6, waypoint_speed_scales=[1.5])
        preview = adapter.preview_motion(action)
        changed = deepcopy(action)
        changed.update(preview_plan_id=preview["plan_id"], waypoint_speed_scales=[.6])
        result = self.execute(adapter, changed)
        self.assertEqual(result["reason"], "preview_plan_action_mismatch")
        self.assertEqual(result["executed_native_steps"], 0)
        self.assertEqual(rows, [])

    def test_omitted_list_keeps_existing_digest_algorithm(self):
        adapter, _, _ = speed_adapter()
        action = motion_action()
        motion = {key: action.get(key) for key in (
            "type", "mode", "frame_id", "tcp_config_id", "limits_profile", "path", "gripper_events")}
        motion.update(speed_scale=1., gripper_speed_scale=1.)
        previous = hashlib.sha256(json.dumps(motion, sort_keys=True, allow_nan=False).encode()).hexdigest()
        self.assertEqual(adapter._preview_action_digest(action), previous)

    def test_feedback_rejects_any_explicit_waypoint_speed_before_planning(self):
        for scales in ([1.], [.6]):
            adapter, planner, rows = speed_adapter()
            adapter.motion_execution_mode = "feedback"
            result = self.execute(adapter, motion_action() | dict(waypoint_speed_scales=scales))
            self.assertEqual(result["reason"], "feedback_action_speed_unsupported")
            self.assertEqual(result["executed_native_steps"], 0)
            self.assertEqual(planner.pose_calls, [])
            self.assertEqual(rows, [])
            self.assertFalse(adapter.capabilities()["action_speed"]["waypoint_speed_scales"]["available"])

    def test_total_request_cap_is_shared_across_different_segment_rates(self):
        adapter, _, rows = speed_adapter(step_limit=1000)
        result = self.execute(adapter, motion_action(angles=(-.5, 2.9), gripper=True) |
                              dict(waypoint_speed_scales=[2., .25]))
        self.assertEqual(result["reason"], "motion_duration_limit")
        self.assertEqual(result["failed_waypoint_index"], 1)
        self.assertEqual(result["executed_native_steps"], 5)
        self.assertEqual(result["waypoint_speed_scales"], [2., .25])
        self.assertTrue(all(row["left_ee_joint_state"] == [1.] for row in rows))


if __name__ == "__main__":
    unittest.main()
