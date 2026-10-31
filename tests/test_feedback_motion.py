"""Non-instantaneous tracking through the real native worker dispatch loop."""

from copy import deepcopy
import io
import threading
import unittest
from unittest.mock import patch

import numpy as np

from auriga.robot.motion.feedback_motion import ArrivalMonitor, pose_error
from test_joint_replan_runtime import replan_adapter, motion_action, periodic_pose
from test_native_wait import wait_action


def feedback_adapter(*, max_delta=.04, speed_override=None, after_step=None, **options):
    adapter, robot, planner, dispatched = replan_adapter(enabled=False, wound=False, **options)
    adapter.motion_execution_mode = "feedback"
    q = {side: np.zeros(6) for side in ("left", "right")}
    speed = {side: np.zeros(6) for side in q}
    native = adapter.env.take_action

    def take_action(action):
        native(action)
        for side in q:
            delta = np.clip(np.asarray(action[side + "_arm_joint_state"]) - q[side], -max_delta, max_delta)
            q[side] += delta
            speed[side] = delta * 25 if speed_override is None else np.full(6, speed_override)
        if after_step:
            after_step(adapter.env.take_action_cnt[0])

    adapter.env.take_action = take_action
    adapter.state = lambda: {"arms": {side: dict(q_rad=value.tolist(), qdot_rad_s=speed[side].tolist(),
                                                ee_pose=periodic_pose(value)) for side, value in q.items()}}
    adapter.robot.get_joint = lambda robot, ids: [q[robot.arm_name.split("_")[0]].copy()]
    return adapter, planner, dispatched


class FeedbackMotionTests(unittest.TestCase):
    def execute(self, adapter, action=None, cancellation=None, progress=None):
        return adapter.execute(action or motion_action(), progress or (lambda value: None),
                               cancellation or threading.Event())

    def test_fixed_target_repeats_until_measured_arrival_and_velocity_settle(self):
        for arm in ("left", "right"):
            adapter, planner, rows = feedback_adapter(arm=arm)
            receipt = self.execute(adapter, motion_action(arm=arm))
            self.assertEqual(receipt["status"], "completed")
            self.assertEqual(len(planner.pose_calls), 1)
            self.assertEqual(len(rows), 6)
            self.assertEqual(receipt["executed_native_steps"], 6)
            self.assertEqual(receipt["executed_control_steps"], 60)
            np.testing.assert_allclose([row[arm + "_arm_joint_state"][0] for row in rows], [-.125] * 6)
            trace = receipt["planned_trace"]
            self.assertEqual(trace["reference_resampled_steps"], 3)
            self.assertEqual(trace["executed_motion_steps"], 6)
            self.assertEqual(trace["feedback_samples"][-1]["settled_samples"], 2)
            self.assertGreater(trace["feedback_samples"][-3]["max_joint_velocity_rad_s"], .1)

    def test_each_waypoint_plans_from_measured_state_then_jaw_dwell_only_after_last(self):
        adapter, planner, rows = feedback_adapter()
        receipt = self.execute(adapter, motion_action(angles=(-.125, -.25), gripper=True))
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(len(planner.pose_calls), 2)
        self.assertAlmostEqual(planner.pose_calls[1][0][0], -.125)
        self.assertEqual(receipt["executed_native_steps"], 18)
        self.assertEqual([r["left_ee_joint_state"][0] for r in rows], [1.] * 12 + [0.] * 6)
        np.testing.assert_allclose([r["left_arm_joint_state"][0] for r in rows[-6:]], [-.25] * 6)
        self.assertTrue(all(r["right_ee_joint_state"] == [.3] for r in rows))

    def test_stall_spends_bounded_steps_and_never_executes_gripper(self):
        adapter, _, rows = feedback_adapter(max_delta=0)
        receipt = self.execute(adapter, motion_action(gripper=True))
        self.assertEqual((receipt["status"], receipt["reason"]), ("failed", "feedback_stalled"))
        self.assertEqual(receipt["executed_native_steps"], 15)
        self.assertTrue(all(r["left_ee_joint_state"] == [1.] for r in rows))

    def test_slow_real_progress_still_obeys_unconditional_action_cap(self):
        adapter, _, rows = feedback_adapter(step_limit=1000, max_delta=.001)
        receipt = self.execute(adapter, motion_action(angles=(-.9,), gripper=True))
        self.assertEqual(receipt["reason"], "motion_duration_limit")
        self.assertEqual(receipt["executed_native_steps"], 250)
        self.assertTrue(all(row["left_ee_joint_state"] == [1.] for row in rows))

    def test_high_reported_speed_without_progress_stops_and_preserves_grip(self):
        # Real failures included both 3.588 rad/s and a marginal 0.103888 rad/s.
        for speed in (3.588, .103888):
            adapter, _, rows = feedback_adapter(max_delta=0, speed_override=speed)
            receipt = self.execute(adapter, motion_action(gripper=True))
            self.assertEqual((receipt["status"], receipt["reason"]), ("failed", "feedback_stalled"))
            self.assertEqual(receipt["executed_native_steps"], 15)
            self.assertTrue(all(row["left_ee_joint_state"] == [1.] for row in rows))

    def test_pose_arrived_but_never_settled_is_bounded_without_releasing(self):
        adapter, _, rows = feedback_adapter(max_delta=1, speed_override=1)
        receipt = self.execute(adapter, motion_action(gripper=True))
        self.assertEqual(receipt["reason"], "feedback_stalled")
        self.assertEqual(receipt["executed_native_steps"], 16)
        self.assertLess(receipt["position_error_m"], .005)
        self.assertTrue(all(row["left_ee_joint_state"] == [1.] for row in rows))

    def test_high_velocity_with_real_progress_can_run_beyond_stall_window(self):
        adapter, _, _ = feedback_adapter(max_delta=.005, step_limit=1000)
        receipt = self.execute(adapter, motion_action(angles=(-.9,)))
        self.assertEqual(receipt["status"], "completed")
        self.assertGreater(receipt["executed_native_steps"], 100)

    def test_periodic_motion_without_best_error_improvement_is_bounded(self):
        target = periodic_pose([-.9])
        initial = dict(q_rad=[0.] * 6, qdot_rad_s=[2.] * 6, ee_pose=periodic_pose([0.]))
        monitor = ArrivalMonitor(target, [-.9] + [0.] * 5, initial)
        for index in range(15):
            state = deepcopy(initial)
            state["q_rad"][0] = .02 if index % 2 else 0.
            state["ee_pose"] = periodic_pose(state["q_rad"])
            outcome, _ = monitor.observe(state)
            self.assertEqual(outcome, "stalled" if index == 14 else "continue")

    def test_native_terminal_retains_last_completed_sample_and_no_extra_jaw(self):
        for success_at in (None, 2):
            adapter, _, rows = feedback_adapter(step_limit=3, success_at=success_at)
            receipt = self.execute(adapter, motion_action(gripper=True))
            count = success_at or 3
            self.assertEqual((receipt["status"], receipt["reason"]), ("stopped", "episode_ended"))
            self.assertEqual(receipt["executed_native_steps"], count)
            self.assertEqual(receipt["planned_trace"]["executed_motion_steps"], count)
            self.assertEqual(len(receipt["planned_trace"]["feedback_samples"]), count)
            self.assertTrue(all(row["left_ee_joint_state"] == [1.] for row in rows))
            again = self.execute(adapter)
            self.assertEqual(again["executed_native_steps"], 0)

    def test_cancel_during_execution_keeps_counts_and_does_not_release(self):
        cancellation = threading.Event()
        adapter, _, rows = feedback_adapter(after_step=lambda n: cancellation.set() if n == 3 else None)
        receipt = self.execute(adapter, motion_action(gripper=True), cancellation)
        self.assertEqual((receipt["status"], receipt["reason"]), ("stopped", "cancelled"))
        self.assertEqual(receipt["executed_native_steps"], 3)
        self.assertEqual(receipt["planned_trace"]["executed_motion_steps"], 3)
        self.assertEqual(len(rows), 3)

    def test_deadline_after_planning_or_after_native_step_prevents_more_motion(self):
        for planning in (True, False):
            adapter, planner, rows = feedback_adapter()
            expired = [False]
            if planning:
                original = planner.plan_path
                def plan(*args):
                    result = original(*args)
                    expired[0] = True
                    return result
                planner.plan_path = plan
            else:
                original_native = adapter.env.take_action
                def execute(action):
                    original_native(action)
                    expired[0] = True
                adapter.env.take_action = execute
            with patch("auriga.robot.backends.robodojo.robot_worker.time.monotonic", side_effect=lambda: 100 if expired[0] else 0):
                receipt = self.execute(adapter, motion_action(timeout_s=1, gripper=True))
            self.assertEqual(receipt["reason"], "planning_timeout" if planning else "execution_timeout")
            self.assertEqual(len(rows), 0 if planning else 1)

    def test_partial_native_fault_preserves_started_but_incomplete_count(self):
        adapter, _, rows = feedback_adapter()
        def failure(action):
            adapter.env.take_action_cnt[0] += 1
            adapter.control_step += 4
            raise RuntimeError("partial feedback native fault")
        adapter.env.take_action = failure
        with patch("sys.stderr", new=io.StringIO()):
            receipt = self.execute(adapter)
        self.assertEqual(receipt["status"], "unknown")
        self.assertEqual((receipt["executed_native_steps"], receipt["completed_native_steps"], receipt["executed_control_steps"]), (1, 0, 4))

    def test_unsupported_preview_and_synchronized_gripper_reject_without_motion(self):
        adapter, planner, rows = feedback_adapter()
        self.assertFalse(adapter.capabilities()["motion_preview"]["available"])
        self.assertEqual(adapter.capabilities()["gripper_phases"], ["after_motion"])
        with self.assertRaisesRegex(ValueError, "feedback_motion_preview_unsupported"):
            adapter.preview_motion(motion_action())
        synchronized = motion_action(gripper=True)
        synchronized["gripper_events"][0].update(phase="during_motion", start_fraction=0, end_fraction=1)
        for action in (dict(motion_action(), preview_plan_id="existing-plan"), synchronized):
            receipt = self.execute(adapter, action)
            self.assertEqual(receipt["status"], "rejected")
            self.assertEqual(receipt["executed_native_steps"], 0)
        self.assertEqual(rows, [])
        self.assertEqual(planner.pose_calls, [])

    def test_wait_keeps_explicit_native_step_semantics_in_feedback_mode(self):
        adapter, _, rows = feedback_adapter()
        receipt = self.execute(adapter, wait_action(7))
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["executed_native_steps"], 7)

    def test_rotation_sign_and_finite_measured_velocity(self):
        target = periodic_pose([.3])
        negative = deepcopy(target)
        negative["quaternion_xyzw"] = [-v for v in target["quaternion_xyzw"]]
        self.assertAlmostEqual(pose_error(negative, target)[1], 0.)
        state = dict(q_rad=[0.] * 6, qdot_rad_s=[0.] * 6, ee_pose=periodic_pose([0.]))
        monitor = ArrivalMonitor(target, [.3] + [0.] * 5, state)
        state["qdot_rad_s"][0] = float("nan")
        with self.assertRaisesRegex(ValueError, "finite_measured"):
            monitor.observe(state)


if __name__ == "__main__":
    unittest.main()
