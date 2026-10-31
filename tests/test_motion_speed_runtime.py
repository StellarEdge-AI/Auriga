"""Speed requests through real worker compilation and native dispatch fixtures."""

from copy import deepcopy
import threading
import unittest

import numpy as np

from auriga.robot.motion.motion_speed import bounded_resample
from test_joint_replan_runtime import motion_action, replan_adapter
from test_motion_preview_backend import preview_adapter
from test_native_wait import wait_action
from test_synchronized_gripper import synchronized


def speed_adapter(*, preview=False, measured_opening=.6, **kwargs):
    if preview:
        adapter, planner, dispatched = preview_adapter(**kwargs)
    else:
        adapter, _, planner, dispatched = replan_adapter(enabled=False, wound=False, **kwargs)
    adapter.arm_velocity_step_limits = {side: np.array([.2] * 6) for side in ("left", "right")}
    robot = adapter.robot.robot_list[0]
    robot.gripper_joints_name = ["jaw1", "jaw2"]
    robot.gripper_move = {"base": "jaw1", "sign": 1}
    robot.gripper_scale = [-.01, .044]
    original_state = adapter.state

    def state():
        value = original_state()
        for side in value["arms"]:
            value["arms"][side]["gripper"] = dict(position_source_kind="measurement",
                position_m=[-.01 + .054 * measured_opening] * 2)
        return value

    adapter.state = state
    return adapter, planner, dispatched


class MotionSpeedRuntimeTests(unittest.TestCase):
    def execute(self, adapter, action):
        return adapter.execute(action, lambda event: None, threading.Event())

    def test_scale_one_is_exact_legacy_and_slower_faster_preserve_endpoint(self):
        counts = []
        for scale in (.5, 1., 2.):
            adapter, _, rows = speed_adapter()
            action = motion_action(angles=(-.4,))
            action["speed_scale"] = scale
            result = self.execute(adapter, action)
            self.assertEqual(result["status"], "completed")
            counts.append(len(rows))
            q = np.array([row["left_arm_joint_state"] for row in rows])
            self.assertLessEqual(np.abs(np.diff(np.vstack([np.zeros(6), q]), axis=0)).max(), .05 * scale + 1e-12)
            np.testing.assert_allclose(q[-1], [-.4, 0, 0, 0, 0, 0])
            self.assertEqual(result["speed_scale"], scale)
            self.assertEqual(result["executed_control_steps"], 10 * len(rows))
            self.assertEqual(result["planned_trace"]["completed_motion_steps"], len(rows))
            if scale == 1:
                np.testing.assert_array_equal(q, adapter._resample_plan(np.array([[0.] * 6, [-.4, 0, 0, 0, 0, 0]]), "left"))
        self.assertEqual(counts, [16, 8, 4])

    def test_native_velocity_cap_and_low_velocity_joint_slowdown(self):
        adapter, _, _ = speed_adapter()
        adapter.arm_step_limits["left"][0] = .02
        adapter.arm_velocity_step_limits["left"][0] = .02
        self.assertAlmostEqual(adapter._speed_step_limits("left", 2)[0], .02)
        self.assertAlmostEqual(adapter._speed_step_limits("left", .5)[0], .01)
        del adapter.arm_velocity_step_limits
        np.testing.assert_allclose(adapter._speed_step_limits("left", 2), adapter.arm_step_limits["left"])

    def test_bounded_resampling_retains_corner_reversal_and_exact_endpoint(self):
        points = np.array([[0, 0], [.01, 0], [.2, 0], [.2, .15], [.2, .3], [0, .3], [0, 0]])
        rows = bounded_resample(points, [.1, .1])
        for knot in points[[2, 4, 5, 6]]:
            self.assertTrue(np.any(np.all(rows == knot, axis=1)))
        self.assertLessEqual(np.abs(np.diff(np.vstack([points[0], rows]), axis=0)).max(), .1 + 1e-12)
        np.testing.assert_array_equal(rows[-1], points[-1])
        loop = bounded_resample(np.array([[0.], [.2], [0.]]), [.1])
        np.testing.assert_allclose(loop.ravel(), [.1, .2, .1, 0.])

    def test_slow_gripper_ramps_from_measured_not_latched_then_old_dwell(self):
        adapter, _, rows = speed_adapter(measured_opening=.6)
        action = wait_action()
        del action["hold_native_steps"]
        action.update(gripper_events=[dict(arm="left", phase="after_motion", goal_normalized=0.)],
                      gripper_speed_scale=.5)
        result = self.execute(adapter, action)
        self.assertEqual(result["status"], "completed")
        jaw = np.array([row["left_ee_joint_state"][0] for row in rows])
        self.assertEqual(len(jaw), 7 + 6)
        np.testing.assert_allclose(jaw[:7], np.linspace(.6, 0, 8)[1:])
        np.testing.assert_array_equal(jaw[7:], 0.)
        self.assertTrue(all(row["right_ee_joint_state"] == [.3] for row in rows))
        self.assertLessEqual(np.abs(np.diff(np.r_[.6, jaw])).max(), .09)
        self.assertEqual(result["planned_trace"]["completed_gripper_steps"], len(rows))

    def test_default_gripper_remains_old_immediate_goal_and_dwell(self):
        adapter, _, rows = speed_adapter()
        result = self.execute(adapter, motion_action(gripper=True))
        self.assertEqual(result["status"], "completed")
        self.assertEqual([row["left_ee_joint_state"][0] for row in rows], [1.] * 3 + [0.] * 6)

    def test_slow_opening_supports_reversed_native_jaw_axis(self):
        adapter, _, rows = speed_adapter(measured_opening=.6)
        robot = adapter.robot.robot_list[0]
        robot.gripper_move["sign"] = -1
        # Identical measured position means opening .4 on the reversed axis.
        action = wait_action()
        del action["hold_native_steps"]
        action.update(gripper_events=[dict(arm="left", phase="after_motion", goal_normalized=1.)],
                      gripper_speed_scale=.25)
        result = self.execute(adapter, action)
        self.assertEqual(result["status"], "completed")
        values = np.array([row["left_ee_joint_state"][0] for row in rows])
        self.assertGreater(values[0], .4)
        self.assertLessEqual(np.abs(np.diff(np.r_[.4, values])).max(), .045 + 1e-12)
        self.assertEqual(values[-1], 1.)

    def test_waypoints_share_speed_and_each_endpoint_is_retained(self):
        adapter, planner, rows = speed_adapter()
        result = self.execute(adapter, motion_action(angles=(-.2, -.4)) | dict(speed_scale=2.))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(planner.pose_calls), 2)
        self.assertEqual(len(rows), 4)
        np.testing.assert_allclose([row["left_arm_joint_state"][0] for row in rows], [-.1, -.2, -.3, -.4])
        self.assertEqual([segment["speed_scale"] for segment in result["planned_trace"]["segments"]], [2., 2.])

    def test_synchronized_slow_gripper_bounds_ramp_from_measured_opening(self):
        adapter, _, rows = speed_adapter()
        action = synchronized(motion_action())
        action.update(speed_scale=2., gripper_speed_scale=.25)
        result = self.execute(adapter, action)
        self.assertEqual(result["status"], "completed")
        values = np.array([row["left_ee_joint_state"][0] for row in rows])
        self.assertLessEqual(np.abs(np.diff(np.r_[.6, values])).max(), .045 + 1e-12)
        self.assertEqual(values[-1], 0.)
        self.assertEqual(result["planned_trace"]["gripper_motion"]["initial_goal_normalized"], .6)

    def test_preview_execution_uses_identical_speed_rows_and_binds_both_fields(self):
        for field in ("speed_scale", "gripper_speed_scale"):
            adapter, _, rows = speed_adapter(preview=True)
            action = motion_action(gripper=True)
            action.update(speed_scale=2., gripper_speed_scale=.5)
            preview = adapter.preview_motion(action)
            self.assertEqual(preview["status"], "planned")
            altered = deepcopy(action)
            altered.update(preview_plan_id=preview["plan_id"])
            altered[field] = 1.
            result = self.execute(adapter, altered)
            self.assertEqual(result["reason"], "preview_plan_action_mismatch")
            self.assertEqual(rows, [])
        adapter, _, rows = speed_adapter(preview=True)
        action = motion_action(gripper=True) | dict(speed_scale=2., gripper_speed_scale=.5)
        preview = adapter.preview_motion(action)
        result = self.execute(adapter, action | dict(preview_plan_id=preview["plan_id"]))
        self.assertEqual(result["status"], "completed")
        actual = [[*row["left_arm_joint_state"], *row["left_ee_joint_state"],
                   *row["right_arm_joint_state"], *row["right_ee_joint_state"]] for row in rows]
        np.testing.assert_array_equal(actual, preview["native_rows"])

    def test_feedback_rejects_speed_without_planning_or_steps(self):
        adapter, planner, rows = speed_adapter()
        adapter.motion_execution_mode = "feedback"
        result = self.execute(adapter, motion_action() | dict(speed_scale=2.))
        self.assertEqual(result["reason"], "feedback_action_speed_unsupported")
        self.assertEqual(result["executed_native_steps"], 0)
        self.assertFalse(adapter.capabilities()["action_speed"]["available"])
        self.assertEqual(planner.pose_calls, [])
        self.assertEqual(rows, [])

    def test_default_digest_treats_omitted_integer_float_defaults_identically(self):
        adapter, _, _ = speed_adapter()
        action = motion_action(gripper=True)
        digest = adapter._preview_action_digest(action)
        self.assertEqual(digest, adapter._preview_action_digest(action | dict(speed_scale=1, gripper_speed_scale=1)))
        self.assertEqual(digest, adapter._preview_action_digest(action | dict(speed_scale=1., gripper_speed_scale=1.)))

    def test_exact_preview_moved_jaw_start_stops_before_release_then_can_retry(self):
        for cached in (True,):
            adapter, _, rows = speed_adapter(preview=True, measured_opening=.6)
            original_state = adapter.state

            def state():
                value = original_state()
                # A previously opening jaw finishes moving during arm motion.
                if adapter.env.take_action_cnt[0]:
                    value["arms"]["left"]["gripper"]["position_m"] = [.044, .044]
                return value

            adapter.state = state
            action = motion_action(gripper=True) | dict(speed_scale=2., gripper_speed_scale=.5)
            if cached:
                preview = adapter.preview_motion(action)
                action["preview_plan_id"] = preview["plan_id"]
            result = self.execute(adapter, action)
            self.assertEqual(result["reason"], "gripper_start_state_changed")
            self.assertEqual(result["executed_native_steps"], 2)
            self.assertEqual(result["planned_trace"]["completed_motion_steps"], 2)
            self.assertNotIn("completed_gripper_steps", result["planned_trace"])
            self.assertTrue(all(row["left_ee_joint_state"] == [1.] for row in rows))
            self.assertIn("separate gripper request", result["gripper_start_state"]["recovery"])
            retry = wait_action()
            del retry["hold_native_steps"]
            retry.update(gripper_events=[dict(arm="left", phase="after_motion", goal_normalized=0.)],
                         gripper_speed_scale=.5)
            resumed = self.execute(adapter, retry)
            self.assertEqual(resumed["status"], "completed")
            self.assertEqual(resumed["executed_native_steps"], 12 + 6)
            self.assertGreater(rows[2]["left_ee_joint_state"][0], .9)

    def test_missing_planner_start_knot_is_bounded_from_measured_start(self):
        adapter, _, _ = speed_adapter()
        path = np.array([[-.25] + [0.] * 5, [-.4] + [0.] * 5])
        rows = adapter._resample_action_plan(path, "left", dict(speed_scale=2.))
        self.assertLessEqual(np.abs(np.diff(np.vstack([np.zeros(6), rows]), axis=0)).max(), .1 + 1e-12)

    def test_slow_request_exceeding_cap_does_not_dispatch(self):
        adapter, _, rows = speed_adapter(step_limit=1000)
        result = self.execute(adapter, motion_action(angles=(-3.13,)) | dict(speed_scale=.25))
        self.assertEqual(result["reason"], "motion_duration_limit")
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
