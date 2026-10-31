"""Arrival-state jaw regressions from four observed bottle-run boundary failures.

The native fixture follows joint commands; measured jaw changes at arrival model
pending opening / contact-constrained closing, without privileged object state.
"""

import threading
import unittest

import numpy as np

from test_arm_queue_runtime import jaw, move, queue_action
from test_dual_arm_runtime import dual_action, dual_adapter, native_rows
from test_joint_replan_runtime import motion_action
from test_motion_speed_runtime import speed_adapter


CASES = (
    # name, arm, compile opening, arrival opening, latched command, new goal
    ("queue_A8", "right", .8556675552218049, .9999900614773786, 1., 0.),
    ("queue_A25", "left", .9919885518374267, .998718081801026, 1., 0.),
    ("full_A42", "right", .7280534243694059, .7165037136938838, 0., 1.),
    ("adaptive_A5", "left", .9985987345377605, .9999999955848412, 1., 0.),
)


class ArrivalGripperRuntimeTests(unittest.TestCase):
    def execute(self, adapter, action):
        return adapter.execute(action, lambda event: None, threading.Event())

    def fixture(self, mode, case, *, arrival_step=None):
        name, arm, initial, arrival, latched, goal = case
        own_steps = 5 if mode == "paired" else 3
        arrival_step = own_steps if arrival_step is None else arrival_step
        def drift(count, row, measured):
            if count == arrival_step:
                adapter.measured_jaws[arm] = arrival
        adapter, _, dispatched = dual_adapter(after_step=drift)
        adapter.measured_jaws[arm] = initial
        adapter.goals[arm] = adapter.last_dispatched_goals[arm] = latched
        event = dict(arm=arm, phase="after_motion", goal_normalized=goal)
        if mode == "single":
            action = motion_action(arm=arm, angles=(.125,))
            action.update(gripper_events=[event], gripper_speed_scale=.8)
        elif mode in ("paired", "independent"):
            pairs = ((.125, .225),) if arm == "left" else ((.225, .125),)
            action = dual_action(pairs=pairs, dual_arm_progression=mode,
                gripper_events=[event], arm_options={arm: dict(gripper_speed_scale=.8)})
        else:
            queues = {arm: [move("approach", .125), jaw("contact", goal, gripper_speed_scale=.8)],
                "right" if arm == "left" else "left": [move("peer", .225)]}
            action = queue_action(queues["left"], queues["right"])
        return adapter, dispatched, action, own_steps

    def test_four_observed_opening_changes_rebase_all_direct_execution_modes(self):
        for mode in ("single", "paired", "independent", "queue"):
            for case in CASES:
                with self.subTest(mode=mode, case=case[0]):
                    adapter, rows, action, start = self.fixture(mode, case)
                    _, arm, initial, arrival, latched, goal = case
                    result = self.execute(adapter, action)
                    self.assertEqual((result["status"], result["reason"]), ("completed", None))
                    trace = result["planned_trace"]
                    if mode == "single":
                        own = trace
                    elif mode == "paired":
                        own = trace["arms"][arm]
                        np.testing.assert_allclose(native_rows(rows), trace["actions"])
                    else:
                        own = trace["arms"][arm]["segments"][-1]
                        np.testing.assert_allclose(native_rows(rows), trace["actions"])
                    compilation = own["gripper_compilation"]
                    self.assertEqual(compilation["mode"], "arrival_state")
                    self.assertEqual(compilation["compiled_sim_step"], start * 10)
                    self.assertAlmostEqual(compilation["measured_opening_normalized"][arm], arrival)
                    self.assertEqual(compilation["max_goal_step"], .144)
                    ramp_count = int(np.ceil(abs(goal - arrival) / .144))
                    dwell_count = max(1, int(np.ceil(abs(goal - latched) / .18)))
                    self.assertEqual(compilation["planned_steps"], ramp_count + dwell_count)
                    jaw_values = np.array([r[f"{arm}_ee_joint_state"][0] for r in rows[start:]])
                    self.assertLessEqual(np.abs(np.diff(np.r_[arrival, jaw_values])).max(), .144 + 1e-12)
                    self.assertAlmostEqual(jaw_values[-1], goal)
                    expected_first = arrival + (goal - arrival) / ramp_count
                    self.assertAlmostEqual(jaw_values[0], expected_first)
                    # Jaw dispatch retains the arrived commanded arm endpoint.
                    target = rows[start - 1][f"{arm}_arm_joint_state"]
                    self.assertTrue(all(row[f"{arm}_arm_joint_state"] == target for row in rows[start:]))
                    self.assertEqual(result["executed_native_steps"], len(rows))
                    self.assertEqual(result["executed_control_steps"], 10 * len(rows))

    def test_fresh_ramp_growth_respects_shared_cap_before_any_jaw_dispatch(self):
        # Forecast: 242 motion + (1 ramp + 6 dwell) = 249 rows.
        # Arrival at 1 needs 7 ramp + 6 dwell = 13; remaining cap is only 8.
        case = ("cap_growth", "left", .1, 1., 1., 0.)
        for mode in ("single", "paired", "independent", "queue"):
            with self.subTest(mode=mode):
                adapter, rows, action, _ = self.fixture(mode, case, arrival_step=242)
                resample = adapter._resample_action_plan
                def near_cap(plan, arm, action, index=0):
                    endpoint = resample(plan, arm, action, index)[-1:]
                    return np.repeat(endpoint, 242 if arm == "left" or mode == "paired" else 245, axis=0)
                adapter._resample_action_plan = near_cap
                result = self.execute(adapter, action)
                self.assertEqual(result["reason"], "motion_duration_limit")
                self.assertEqual(result["executed_native_steps"], 242)
                self.assertTrue(all(row["left_ee_joint_state"] == [1.] for row in rows))
                self.assertLessEqual(len(rows), 250)
                if mode == "independent":
                    self.assertEqual(result["planned_trace"]["arms"]["right"]["completed_motion_steps"], 242)
                if mode == "queue":
                    self.assertEqual(result["stage_results"]["contact"]["completed_steps"], 0)

    def test_fresh_ramp_can_fill_the_last_shared_step_exactly(self):
        case = ("cap_exact", "left", .1, 1., 1., 0.)
        adapter, rows, action, _ = self.fixture("independent", case, arrival_step=237)
        resample = adapter._resample_action_plan
        def near_cap(plan, arm, action, index=0):
            endpoint = resample(plan, arm, action, index)[-1:]
            return np.repeat(endpoint, 237 if arm == "left" else 245, axis=0)
        adapter._resample_action_plan = near_cap
        result = self.execute(adapter, action)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["executed_native_steps"], 250)
        self.assertEqual(result["planned_trace"]["arms"]["left"]["completed_gripper_steps"], 13)
        self.assertEqual(rows[-1]["left_ee_joint_state"], [0.])

    def test_later_peer_failure_does_not_erase_completed_fresh_jaw_work(self):
        adapter, rows, action, _ = self.fixture("independent", CASES[1])
        from test_joint_replan_runtime import periodic_pose
        action["path"]["waypoints"][0]["right"] = periodic_pose([.825])
        adapter.pose_offsets["right"][2] = .02
        result = self.execute(adapter, action)
        self.assertEqual((result["reason"], result["failed_arm"]), ("tracking_error", "right"))
        self.assertEqual(result["planned_trace"]["arms"]["left"]["completed_gripper_steps"], 13)
        self.assertEqual(rows[-1]["left_ee_joint_state"], [0.])
        self.assertEqual(result["executed_native_steps"], 17)

    def test_same_row_peer_endpoint_failure_blocks_fresh_jaw_admission(self):
        for mode in ("paired", "independent", "queue"):
            with self.subTest(mode=mode):
                adapter, rows, action, _ = self.fixture(mode, CASES[1])
                adapter.pose_offsets["right"][2] = .02
                if mode == "queue":
                    action["arm_queues"]["right"][0] = move("peer", .125)
                else:
                    action["path"]["waypoints"][0]["right"] = action["path"]["waypoints"][0]["left"]
                result = self.execute(adapter, action)
                self.assertEqual((result["reason"], result["failed_arm"]), ("tracking_error", "right"))
                self.assertEqual(len(rows), 3)
                self.assertTrue(all(row["left_ee_joint_state"] == [1.] for row in rows))

    def test_queue_dependency_wait_uses_opening_at_actual_jaw_start(self):
        case = CASES[1]
        adapter, rows, action, _ = self.fixture("queue", case, arrival_step=9)
        action["arm_queues"]["left"][1]["depends_on"] = ["peer"]
        action["arm_queues"]["right"][0] = move("peer", .425)
        result = self.execute(adapter, action)
        self.assertEqual(result["status"], "completed")
        stage = result["planned_trace"]["arms"]["left"]["segments"][1]
        self.assertEqual(stage["gripper_compilation"]["compiled_sim_step"], 90)
        self.assertAlmostEqual(stage["gripper_compilation"]["measured_opening_normalized"]["left"], case[3])
        self.assertEqual(result["stage_results"]["contact"]["started_sim_step"], 90)
        self.assertAlmostEqual(rows[9]["left_ee_joint_state"][0], case[3] * 6 / 7)

    def test_exact_preview_keeps_cached_rows_and_rejects_each_observed_stale_jump(self):
        for case in CASES:
            with self.subTest(case=case[0]):
                _, arm, initial, arrival, latched, goal = case
                adapter, _, rows = speed_adapter(preview=True, measured_opening=initial, arm=arm)
                adapter.goals[arm] = adapter.last_dispatched_goals[arm] = latched
                old_state = adapter.state
                def state():
                    value = old_state()
                    if rows:
                        value["arms"][arm]["gripper"]["position_m"] = [-.01 + .054 * arrival] * 2
                    return value
                adapter.state = state
                action = motion_action(arm=arm, angles=(.125,)) | dict(gripper_speed_scale=.8,
                    gripper_events=[dict(arm=arm, phase="after_motion", goal_normalized=goal)])
                preview = adapter.preview_motion(action)
                self.assertIn("exact cached", preview["jaw_execution_semantics"])
                result = self.execute(adapter, action | dict(preview_plan_id=preview["plan_id"]))
                self.assertEqual(result["reason"], "gripper_start_state_changed")
                self.assertEqual(result["executed_native_steps"], 3)
                np.testing.assert_array_equal(native_rows(rows), preview["native_rows"][:3])
                self.assertFalse(result["gripper_execution"][arm]["started"])
                self.assertNotIn("gripper_compilation", result["planned_trace"])


if __name__ == "__main__":
    unittest.main()
