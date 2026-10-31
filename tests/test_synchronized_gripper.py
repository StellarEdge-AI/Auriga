"""CPU integration coverage of command timing; no physical grasp is simulated."""

from copy import deepcopy
import threading
import unittest
from unittest.mock import Mock, patch

import numpy as np

from auriga.core.contracts import validate_action as validate_contract_action
from auriga.robot.backends.robodojo.robodojo import validate_action
from test_joint_replan_runtime import motion_action, replan_adapter
from test_motion_preview_backend import preview_adapter
from test_native_wait import IDENTITY, fake_adapter, wait_action
from test_tracking_guard_runtime import ee_action, tracking_adapter


def synchronized(action, *, goal=0., start=0., end=1.):
    action = deepcopy(action)
    arm = next(iter(action["path"]["waypoints"][-1]))
    action["gripper_events"] = [dict(arm=arm, phase="during_motion", goal_normalized=goal,
                                     start_fraction=start, end_fraction=end)]
    return action


def jaw_commands(rows, arm):
    return np.array([row[f"{arm}_ee_joint_state"][0] for row in rows])


class SynchronizedGripperRuntimeTests(unittest.TestCase):
    def execute(self, adapter, action, cancellation=None):
        return adapter.execute(action, lambda event: None, cancellation or threading.Event())

    def assert_schedule(self, rows, trace, arm, initial, goal, start, end, original_steps):
        count = len(rows)
        fractions = np.arange(1, count + 1) / count
        expected = initial + (goal - initial) * np.clip((fractions - start) / (end - start), 0, 1)
        actual = jaw_commands(rows, arm)
        np.testing.assert_allclose(actual, expected, atol=1e-12)
        self.assertLessEqual(np.max(np.abs(np.diff(np.r_[initial, actual]))), .18 + 1e-12)
        self.assertAlmostEqual(actual[-1], goal)
        self.assertEqual(len(trace["q_rad"]), count)
        metadata = trace["gripper_motion"]
        np.testing.assert_allclose(metadata["goal_normalized_by_step"], expected, atol=1e-12)
        self.assertEqual(metadata["original_motion_steps"], original_steps)
        self.assertEqual(metadata["scheduled_motion_steps"], count)
        self.assertEqual(metadata["timing_basis"], "final_segment_native_steps")

    def test_left_right_open_close_interpolate_latched_command_without_extra_rows(self):
        for arm in ("left", "right"):
            for initial, goal in ((1., 0.), (0., 1.)):
                with self.subTest(arm=arm, initial=initial):
                    adapter, rows = tracking_adapter(positions=np.zeros((20, 6)))
                    adapter.last_dispatched_goals[arm] = initial
                    action = synchronized(ee_action(arm), goal=goal)
                    result = self.execute(adapter, action)
                    self.assertEqual((result["status"], result["reason"]), ("completed", None))
                    self.assertEqual(len(rows), 20)
                    self.assertEqual(result["executed_native_steps"], 20)
                    self.assert_schedule(rows, result["planned_trace"], arm, initial, goal, 0., 1., 20)
                    inactive = "right" if arm == "left" else "left"
                    np.testing.assert_array_equal(jaw_commands(rows, inactive), .3 if inactive == "right" else 1.)
                    self.assertEqual(adapter.last_dispatched_goals[arm], goal)

    def test_delayed_interval_retains_then_ramps_then_holds_target(self):
        adapter, rows = tracking_adapter(positions=np.zeros((20, 6)))
        result = self.execute(adapter, synchronized(ee_action(), start=.25, end=.75))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(rows), 20)
        self.assert_schedule(rows, result["planned_trace"], "left", 1., 0., .25, .75, 20)
        np.testing.assert_array_equal(jaw_commands(rows[:5], "left"), 1.)
        np.testing.assert_array_equal(jaw_commands(rows[14:], "left"), 0.)

    def test_short_motion_retimes_uniformly_and_preserves_all_original_knots(self):
        original = np.array([[0.] * 6, [.1, 0., .2, 0., 0., 0.], [.2, .1, 0., 0., 0., 0.]])
        adapter, rows = tracking_adapter(positions=original)
        result = self.execute(adapter, synchronized(ee_action(), start=.2, end=.8))
        self.assertEqual(result["status"], "completed")
        self.assertGreater(len(rows), len(original))
        self.assert_schedule(rows, result["planned_trace"], "left", 1., 0., .2, .8, len(original))
        positions = np.array([row["left_arm_joint_state"] for row in rows])
        # The measured start to first commanded row is also an original interval.
        factor, remainder = divmod(len(positions), len(original))
        self.assertEqual(remainder, 0)
        np.testing.assert_allclose(positions[factor - 1::factor], original)
        knots = np.vstack([np.zeros((1, 6)), original])
        expected = np.vstack([np.linspace(a, b, factor + 1)[1:] for a, b in zip(knots, knots[1:])])
        np.testing.assert_allclose(positions, expected)

    def test_real_resampling_and_replanning_still_meet_synchronized_rate(self):
        for arm in ("left", "right"):
            with self.subTest(arm=arm):
                adapter, _, planner, rows = replan_adapter(arm=arm)
                initial = adapter.last_dispatched_goals[arm]
                action = synchronized(motion_action(arm=arm), goal=0.)
                result = self.execute(adapter, action)
                self.assertEqual(result["status"], "completed")
                self.assertEqual(len(planner.joint_calls), 1)
                self.assert_schedule(rows, result["planned_trace"], arm, initial, 0., 0., 1., 3)
                np.testing.assert_allclose(rows[-1][f"{arm}_arm_joint_state"], [-.125] + [0.] * 5)

    def test_single_arm_row_can_schedule_bounded_gripper_commands(self):
        original = np.array([[.1, .2, .1, 0., 0., 0.]])
        adapter, rows = tracking_adapter(positions=original)
        result = self.execute(adapter, synchronized(ee_action()))
        self.assertEqual(result["status"], "completed")
        self.assert_schedule(rows, result["planned_trace"], "left", 1., 0., 0., 1., 1)
        np.testing.assert_allclose([row["left_arm_joint_state"] for row in rows],
                                   np.linspace(np.zeros(6), original[0], len(rows) + 1)[1:])

    def test_unchanged_gripper_goal_needs_no_retiming_even_for_narrow_interval(self):
        adapter, rows = tracking_adapter(positions=np.zeros((3, 6)))
        result = self.execute(adapter, synchronized(ee_action(), goal=1., start=.999))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(rows), 3)
        self.assert_schedule(rows, result["planned_trace"], "left", 1., 1., .999, 1., 3)

    def test_only_final_waypoint_segment_changes_jaw_command(self):
        adapter, rows = tracking_adapter(positions=np.zeros((10, 6)))
        result = self.execute(adapter, synchronized(ee_action(waypoints=3)))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(rows), 30)
        np.testing.assert_array_equal(jaw_commands(rows[:20], "left"), 1.)
        segments = result["planned_trace"]["segments"]
        self.assertEqual(len(segments), 3)
        for segment in segments[:-1]:
            self.assertNotIn("gripper_motion", segment)
        self.assert_schedule(rows[20:], segments[-1], "left", 1., 0., 0., 1., 10)

    def test_duration_limit_rejects_entire_oversized_segment_before_dispatch(self):
        adapter, rows = tracking_adapter(positions=np.zeros((3, 6)), step_limit=1000)
        result = self.execute(adapter, synchronized(ee_action(), start=.999, end=1.))
        self.assertEqual((result["status"], result["reason"]), ("failed", "motion_duration_limit"))
        self.assertEqual(result["executed_native_steps"], 0)
        self.assertEqual(rows, [])

    def test_duration_cap_counts_preceding_waypoint_segments(self):
        adapter, rows = tracking_adapter(positions=np.zeros((125, 6)), step_limit=1000)
        result = self.execute(adapter, synchronized(ee_action(waypoints=2), start=.98, end=1.))
        self.assertEqual((result["status"], result["reason"]), ("failed", "motion_duration_limit"))
        self.assertEqual(result["failed_waypoint_index"], 1)
        self.assertEqual(len(rows), 125)
        self.assertEqual(result["executed_native_steps"], 125)
        np.testing.assert_array_equal(jaw_commands(rows, "left"), 1.)

    def test_native_terminal_stops_mid_schedule_without_hidden_gripper_steps(self):
        for success_at, reason, count in ((None, "native_step_limit", 3), (2, "native_success", 2)):
            with self.subTest(reason=reason):
                adapter, rows = tracking_adapter(positions=np.zeros((20, 6)), step_limit=3, success_at=success_at)
                result = self.execute(adapter, synchronized(ee_action()))
                self.assertEqual((result["status"], result["reason"]), ("stopped", "episode_ended"))
                self.assertEqual(result["episode_terminal"]["reason"], reason)
                self.assertEqual(len(rows), count)
                self.assertEqual(result["executed_native_steps"], count)
                self.assertEqual(result["executed_control_steps"], count * 10)
                self.assertEqual(result["stop_confirmation"]["settling_steps"], 0)
                self.assertAlmostEqual(adapter.last_dispatched_goals["left"], 1. - count / 20)

    def test_cancel_before_and_during_schedule_never_finishes_jaw_motion(self):
        for count in (0, 3):
            with self.subTest(count=count):
                cancellation = threading.Event()
                if count == 0:
                    cancellation.set()
                adapter, rows = tracking_adapter(positions=np.zeros((20, 6)),
                    after_step=lambda index, row, measured: cancellation.set() if index == count else None)
                result = self.execute(adapter, synchronized(ee_action()), cancellation)
                self.assertEqual((result["status"], result["reason"]), ("stopped", "cancelled"))
                self.assertEqual(len(rows), count)
                self.assertEqual(result["executed_native_steps"], count)
                self.assertEqual(result["stop_confirmation"]["settling_steps"], 0)
                self.assertAlmostEqual(adapter.last_dispatched_goals["left"], 1. - count / 20)

    def test_timeout_mid_schedule_stops_without_hidden_jaw_rows(self):
        clock = [0.]
        adapter, rows = tracking_adapter(positions=np.zeros((20, 6)),
            after_step=lambda count, row, measured: clock.__setitem__(0, float(count)))
        action = synchronized(ee_action())
        action["timeout_s"] = 2.5
        with patch("auriga.robot.backends.robodojo.robot_worker.time.monotonic", side_effect=lambda: clock[0]):
            result = self.execute(adapter, action)
        self.assertEqual((result["status"], result["reason"]), ("failed", "execution_timeout"))
        self.assertEqual(len(rows), 3)
        self.assertEqual(result["executed_native_steps"], 3)
        self.assertEqual(result["completed_native_steps"], 3)
        self.assertEqual(result["executed_control_steps"], 30)
        self.assertEqual(result["stop_confirmation"]["settling_steps"], 0)
        np.testing.assert_allclose(jaw_commands(rows, "left"), [.95, .9, .85])
        self.assertAlmostEqual(adapter.last_dispatched_goals["left"], .85)

    def test_legacy_after_motion_keeps_jaw_constant_until_arm_finishes(self):
        adapter, rows = tracking_adapter(positions=np.zeros((20, 6)))
        result = self.execute(adapter, ee_action(gripper=True))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(rows), 26)
        np.testing.assert_array_equal(jaw_commands(rows[:20], "left"), 1.)
        self.assertAlmostEqual(jaw_commands(rows, "left")[-1], 0.)
        self.assertNotIn("gripper_motion", result["planned_trace"])


class SynchronizedGripperPreviewTests(unittest.TestCase):
    def test_preview_caches_exact_retimed_rows_and_execution_reuses_them(self):
        for arm in ("left", "right"):
            with self.subTest(arm=arm):
                adapter, _, rows = preview_adapter(arm=arm)
                action = synchronized(motion_action(arm=arm), goal=0., start=.2, end=.8)
                preview = adapter.preview_motion(action)
                self.assertEqual(preview["status"], "planned")
                self.assertEqual(rows, [])
                expected = deepcopy(preview["native_rows"])
                self.assertEqual(len(expected), len(preview["planned_trace"]["q_rad"]))
                adapter._plan_segment = Mock(side_effect=AssertionError("cached plan must not replan"))
                adapter._resample_plan = Mock(side_effect=AssertionError("cached plan must not resample"))
                preview["native_rows"][0][0] = 999.
                result = adapter.execute(dict(action, preview_plan_id=preview["plan_id"]), lambda event: None, threading.Event())
                self.assertEqual(result["status"], "completed")
                actual = [row["left_arm_joint_state"] + row["left_ee_joint_state"] +
                          row["right_arm_joint_state"] + row["right_ee_joint_state"] for row in rows]
                np.testing.assert_array_equal(actual, expected)

    def test_preview_rejects_changed_schedule_and_duration_overflow_without_motion(self):
        adapter, _, rows = preview_adapter()
        action = synchronized(motion_action())
        preview = adapter.preview_motion(action)
        changed = synchronized(motion_action(), start=.25)
        result = adapter.execute(dict(changed, preview_plan_id=preview["plan_id"]), lambda event: None, threading.Event())
        self.assertEqual(result["status"], "rejected")
        self.assertIn("action_mismatch", result["reason"])
        overflow = adapter.preview_motion(synchronized(motion_action(), start=.999))
        self.assertEqual((overflow["status"], overflow["reason"], overflow["plan_id"]), ("failed", "motion_duration_limit", None))
        self.assertEqual(rows, [])


class SynchronizedGripperValidationTests(unittest.TestCase):
    def test_capability_and_both_validation_boundaries(self):
        caps = fake_adapter()[0].capabilities()
        self.assertIn("during_motion", caps["gripper_phases"])
        action = synchronized(ee_action())
        validate_action(action)
        validate_contract_action(action, IDENTITY, "observed", caps)
        caps["gripper_phases"] = ["after_motion"]
        with self.assertRaises(ValueError):
            validate_contract_action(action, IDENTITY, "observed", caps)

    def test_invalid_fractions_and_event_context_reject_before_execution(self):
        valid = synchronized(ee_action())
        invalid = []
        for field in ("start_fraction", "end_fraction"):
            for value in (None, True, False, "0.5", float("nan"), float("inf"), -.1, 1.1):
                action = deepcopy(valid)
                action["gripper_events"][0][field] = value
                invalid.append(action)
            action = deepcopy(valid)
            del action["gripper_events"][0][field]
            invalid.append(action)
        invalid.extend([synchronized(ee_action(), start=.5, end=.5), synchronized(ee_action(), start=.75, end=.25)])
        wrong_arm = deepcopy(valid)
        wrong_arm["gripper_events"][0]["arm"] = "right"
        invalid.append(wrong_arm)
        hold = wait_action()
        del hold["hold_native_steps"]
        hold["gripper_events"] = valid["gripper_events"]
        invalid.append(hold)
        legacy = ee_action(gripper=True)
        legacy["gripper_events"][0]["start_fraction"] = 0.
        invalid.append(legacy)
        caps = fake_adapter()[0].capabilities()
        for action in invalid:
            for boundary in (validate_action, lambda value: validate_contract_action(value, IDENTITY, "observed", caps)):
                with self.subTest(event=action["gripper_events"], boundary=boundary), self.assertRaises(ValueError):
                    boundary(action)


if __name__ == "__main__":
    unittest.main()
