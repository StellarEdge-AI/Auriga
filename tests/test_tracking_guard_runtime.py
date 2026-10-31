"""CPU-only regression checks through the real EE/native dispatch loop."""

from copy import deepcopy
from types import SimpleNamespace
import threading
import unittest

import numpy as np

from test_native_wait import fake_adapter, wait_action


POSE = dict(position_m=[0., 0., 1.], quaternion_xyzw=[0., 0., 0., 1.])


def ee_action(arm="left", *, waypoints=1, gripper=False):
    action = wait_action()
    del action["hold_native_steps"]
    action["path"] = dict(kind="backend_free", waypoints=[{arm: deepcopy(POSE)} for _ in range(waypoints)])
    if gripper:
        action["gripper_events"] = [dict(arm=arm, phase="after_motion", goal_normalized=0.)]
    return action


def tracking_adapter(*, positions=None, enabled=True, after_step=None, **native_options):
    measured = {side: [0.] * 6 for side in ("left", "right")}

    def advance(count):
        if after_step is not None:
            after_step(count, dispatched[-1], measured)

    adapter, dispatched = fake_adapter(after_step=advance, **native_options)
    adapter.tracking_guard_enabled = enabled
    adapter.state = lambda: {"arms": {side: dict(q_rad=list(q), ee_pose=deepcopy(POSE))
                                     for side, q in measured.items()}}
    adapter.robot.get_robot_by_arm_name = lambda name: SimpleNamespace(arm_joints_name=[f"q{i}" for i in range(6)])
    adapter.fixture_positions = np.ones((10, 6)) if positions is None else np.asarray(positions, dtype=float)
    # Bypass GPU planning only. Validation, native dispatch, state sampling,
    # endpoint checks, hold behavior and receipt accounting remain production code.
    adapter._plan_segment = lambda robot, target, diagnostics, index: (adapter.fixture_positions.copy(), 1)
    adapter._resample_plan = lambda positions, arm: positions
    return adapter, dispatched


class TrackingGuardRuntimeTests(unittest.TestCase):
    def execute(self, adapter, action=None):
        return adapter.execute(action or ee_action(), lambda event: None, threading.Event())

    def assert_counts(self, result, count):
        self.assertEqual(result["executed_steps"], count)
        self.assertEqual(result["executed_native_steps"], count)
        self.assertEqual(result["completed_native_steps"], count)
        self.assertEqual(result["executed_control_steps"], count * 10)

    def test_fifth_sustained_row_stops_without_gripper_or_settling_commands(self):
        for arm in ("left", "right"):
            with self.subTest(arm=arm):
                positions = np.zeros((12, 6))
                positions[:, 3] = .9
                adapter, dispatched = tracking_adapter(positions=positions)
                result = self.execute(adapter, ee_action(arm, gripper=True))
                self.assertEqual((result["status"], result["reason"]), ("failed", "tracking_divergence"))
                self.assert_counts(result, 5)
                self.assertEqual(len(dispatched), 5)
                self.assertEqual(result["failed_waypoint_index"], 0)
                self.assertEqual(result["waypoint_results"], [])
                self.assertEqual(result["tracking_guard"], dict(
                    arm=arm, max_joint_error_rad=.9, threshold_rad=.5, consecutive_steps=5,
                    joint_index=3, commanded_q_rad=positions[4].tolist(), measured_q_rad=[0.] * 6))
                self.assertEqual(result["stop_confirmation"]["settling_steps"], 0)
                self.assertEqual(result["terminal_observation"]["sim_step"], 50)
                self.assertEqual(result["execution_interval"], dict(
                    start_sim_step=0, end_sim_step=50, step_domain="adapter_control_step"))
                self.assertEqual(len(result["planned_trace"]["q_rad"]), 12)
                self.assertEqual({sample["sim_step"] for sample in result["actual_trace"]}, set(range(0, 51, 10)))
                for row in dispatched:
                    self.assertEqual(row["left_ee_joint_state"], [1.])
                    self.assertEqual(row["right_ee_joint_state"], [.3])

    def test_disabled_or_absent_flag_dispatches_entire_plan(self):
        for absent in (False, True):
            with self.subTest(absent=absent):
                adapter, dispatched = tracking_adapter(enabled=False)
                if absent:
                    del adapter.tracking_guard_enabled
                result = self.execute(adapter)
                self.assertEqual((result["status"], result["reason"]), ("completed", None))
                self.assert_counts(result, 10)
                self.assertEqual(len(dispatched), 10)
                self.assertNotIn("tracking_guard", result)

    def test_native_success_or_step_limit_wins_on_fifth_divergent_row(self):
        for success_at, reason in ((None, "native_step_limit"), (5, "native_success")):
            with self.subTest(success_at=success_at):
                adapter, dispatched = tracking_adapter(step_limit=5, success_at=success_at)
                result = self.execute(adapter)
                self.assertEqual((result["status"], result["reason"]), ("stopped", "episode_ended"))
                self.assertEqual(result["episode_terminal"]["reason"], reason)
                self.assert_counts(result, 5)
                self.assertEqual(len(dispatched), 5)
                self.assertNotIn("tracking_guard", result)
                self.assertEqual(result["stop_confirmation"]["settling_steps"], 0)

    def test_inactive_arm_large_error_is_ignored_for_both_active_arms(self):
        for arm in ("left", "right"):
            with self.subTest(arm=arm):
                inactive = "right" if arm == "left" else "left"

                def follow_active(count, row, measured):
                    measured[arm] = list(row[f"{arm}_arm_joint_state"])
                    measured[inactive] = [10.] * 6

                # Current commands initially differ substantially from the final
                # target; following each dispatched row must remain acceptable.
                positions = np.repeat(np.linspace(.05, 1.2, 12)[:, None], 6, axis=1)
                adapter, dispatched = tracking_adapter(positions=positions, after_step=follow_active)
                result = self.execute(adapter, ee_action(arm))
                self.assertEqual(result["status"], "completed")
                self.assert_counts(result, 12)
                self.assertEqual(len(dispatched), 12)
                self.assertNotIn("tracking_guard", result)
                self.assertEqual(result["actual_trace"][-1]["state"]["arms"][inactive]["q_rad"], [10.] * 6)

    def test_fresh_guard_for_each_action_including_after_a_trigger(self):
        adapter, dispatched = tracking_adapter(positions=np.ones((4, 6)))
        for _ in range(2):
            result = self.execute(adapter)
            self.assertEqual(result["status"], "completed")
            self.assert_counts(result, 4)
            self.assertNotIn("tracking_guard", result)
        adapter.fixture_positions = np.ones((10, 6))
        for _ in range(2):
            result = self.execute(adapter)
            self.assertEqual(result["reason"], "tracking_divergence")
            self.assert_counts(result, 5)
            self.assertEqual(result["tracking_guard"]["consecutive_steps"], 5)
        self.assertEqual(len(dispatched), 18)

    def test_recovery_at_threshold_resets_native_row_streak(self):
        positions = np.ones((12, 6))
        positions[4] = .5
        adapter, dispatched = tracking_adapter(positions=positions)
        result = self.execute(adapter)
        self.assertEqual(result["reason"], "tracking_divergence")
        self.assert_counts(result, 10)
        self.assertEqual(len(dispatched), 10)
        self.assertEqual(result["tracking_guard"]["consecutive_steps"], 5)

    def test_failure_retains_completed_waypoint_and_partial_action_count(self):
        adapter, dispatched = tracking_adapter()
        adapter._plan_segment = lambda robot, target, diagnostics, index: (
            np.zeros((3, 6)) if index == 0 else np.ones((10, 6)), 1)
        result = self.execute(adapter, ee_action(waypoints=3, gripper=True))
        self.assertEqual(result["reason"], "tracking_divergence")
        self.assertEqual(result["failed_waypoint_index"], 1)
        self.assertEqual([value["waypoint_index"] for value in result["waypoint_results"]], [0])
        self.assertEqual(len(result["planned_trace"]["segments"]), 2)
        self.assert_counts(result, 8)
        self.assertEqual(len(dispatched), 8)
        self.assertEqual(result["stop_confirmation"]["settling_steps"], 0)

    def test_wait_standalone_gripper_and_pi_prefix_ignore_tracking_guard(self):
        gripper = wait_action()
        del gripper["hold_native_steps"]
        gripper["gripper_events"] = [dict(arm="left", phase="after_motion", goal_normalized=0.)]
        prefix = dict(type="JointPrefixAction", actions=[[0.] * 14 for _ in range(10)],
                      policy_provenance={"source": "pi05"})

        def diverge(count, row, measured):
            for side in measured:
                measured[side] = [10.] * 6

        for name, action, expected in (("wait", wait_action(10), 10), ("gripper", gripper, 6),
                                       ("pi_prefix", prefix, 10)):
            with self.subTest(name=name):
                adapter, dispatched = tracking_adapter(after_step=diverge)
                result = self.execute(adapter, action)
                self.assertEqual((result["status"], result["reason"]), ("completed", None))
                self.assert_counts(result, expected)
                self.assertEqual(len(dispatched), expected)
                self.assertNotIn("tracking_guard", result)
                for row in dispatched:
                    self.assertEqual(row["left_arm_joint_state"], [0.] * 6)
                    self.assertEqual(row["right_arm_joint_state"], [0.] * 6)


if __name__ == "__main__":
    unittest.main()
