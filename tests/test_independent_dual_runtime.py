"""Real dispatch-loop checks for independently advancing dual arm routes."""

import io
import threading
import unittest
from unittest.mock import patch

import numpy as np

from test_dual_arm_runtime import ARMS, dual_action, dual_adapter, native_rows


class IndependentDualRuntimeTests(unittest.TestCase):
    def execute(self, adapter, action=None, cancellation=None):
        action = action or dual_action()
        action["dual_arm_progression"] = "independent"
        return adapter.execute(action, lambda event: None, cancellation or threading.Event())

    def assert_counts(self, result, rows, count):
        self.assertEqual(len(rows), count)
        for key in ("executed_steps", "executed_native_steps", "completed_native_steps"):
            self.assertEqual(result[key], count)
        self.assertEqual(result["executed_control_steps"], count * 10)
        trace = result["planned_trace"]
        self.assertEqual(trace.get("completed_shared_steps", 0), count)
        for arm in ARMS:
            own = trace["arms"][arm]
            self.assertEqual(sum(own.get("completed_" + phase + "_steps", 0)
                                 for phase in ("motion", "gripper", "hold")), count)

    def test_crossed_three_nine_routes_take_twelve_instead_of_eighteen_unchanged_rows(self):
        action = dual_action(pairs=((.125, .425), (.55, .55)))
        paired, _, paired_rows = dual_adapter()
        paired_result = paired.execute(action, lambda event: None, threading.Event())
        adapter, planners, rows = dual_adapter()
        result = self.execute(adapter, action)
        self.assertEqual((paired_result["status"], result["status"]), ("completed", "completed"))
        self.assertEqual(len(paired_rows), 18)
        self.assert_counts(result, rows, 12)
        self.assertEqual([(arm, count) for arm, count, _ in adapter.planning_starts],
                         [("left", 0), ("right", 0), ("left", 3), ("right", 9)])
        np.testing.assert_allclose(native_rows(rows), result["planned_trace"]["actions"])
        for arm in ARMS:
            own = result["planned_trace"]["arms"][arm]["segments"]
            for index, item in enumerate(own):
                np.testing.assert_allclose(item["q_rad"],
                    paired_result["planned_trace"]["segments"][index]["arms"][arm]["q_rad"])
                self.assertEqual(item["completed_motion_steps"], item["planned_motion_steps"])
            self.assertEqual(result["arm_results"][arm]["phase"], "final_held")
            self.assertEqual(result["arm_results"][arm]["sim_step"], 120)
            # Replanning really starts at the measured first endpoint.
            self.assertAlmostEqual(planners[arm].pose_calls[1][0][0], [.125, .425][ARMS.index(arm)])

    def test_short_arm_jaw_overlaps_peer_motion_without_stretching_either_path(self):
        adapter, _, rows = dual_adapter()
        action = dual_action(pairs=((.125, .425),), gripper_events=[
            {"arm": "left", "phase": "after_motion", "goal_normalized": 0.}])
        result = self.execute(adapter, action)
        self.assertEqual(result["status"], "completed")
        self.assert_counts(result, rows, 9)  # 3 motion + 6 jaw vs right 9 motion.
        self.assertTrue(all(row["left_ee_joint_state"] == [1.] for row in rows[:3]))
        self.assertTrue(all(row["left_ee_joint_state"] == [0.] for row in rows[3:]))
        for context in result["planned_trace"]["row_contexts"][3:]:
            self.assertEqual((context["left"]["phase"], context["right"]["phase"]), ("gripper", "motion"))

    def test_unequal_waypoint_routes_finish_short_arm_jaw_without_dummy_stages(self):
        for shorter, longer in (("left", "right"), ("right", "left")):
            adapter, planners, rows = dual_adapter()
            action = dual_action(pairs=((.125, .125), (.425, .425), (.55, .55)), gripper_events=[
                {"arm": shorter, "phase": "after_motion", "goal_normalized": 0.}])
            for point in action['path']['waypoints'][1:]:
                point.pop(shorter)
            result = self.execute(adapter, action)
            self.assertEqual(result['status'], 'completed')
            self.assert_counts(result, rows, 12)  # max(short route + jaw, 3 + 6 + 3), no filler segment.
            self.assertEqual(len(planners[shorter].pose_calls), 1)
            self.assertEqual(len(planners[longer].pose_calls), 3)
            contexts = result['planned_trace']['row_contexts']
            self.assertEqual(contexts[3][shorter]['phase'], 'gripper')
            self.assertEqual(contexts[3][longer]['waypoint_index'], 1)
            self.assertEqual(contexts[-1][shorter]['phase'], 'hold')
            self.assertEqual(result['arm_results'][shorter]['waypoint_index'], 0)
            self.assertEqual(result['arm_results'][longer]['waypoint_index'], 2)
            self.assertTrue(all(x['phase']=='final_held' and x['sim_step']==120
                                for x in result['arm_results'].values()))
            self.assertEqual(len(result['arm_options'][shorter]['waypoint_speed_scales']), 1)

    def test_short_route_during_motion_jaw_runs_on_own_final_segment(self):
        adapter, _, rows = dual_adapter()
        action = dual_action(pairs=((.125, .425), (.3, .55)), gripper_events=[
            {'arm': 'left', 'phase': 'during_motion', 'goal_normalized': 0.,
             'start_fraction': 0., 'end_fraction': 1.}])
        action['path']['waypoints'][1].pop('left')
        result = self.execute(adapter, action)
        self.assertEqual(result['status'], 'completed')
        self.assert_counts(result, rows, 12)
        self.assertEqual(result['planned_trace']['arms']['left']['segments'][0]['gripper_motion']['phase'], 'during_motion')
        self.assertEqual(rows[-1]['left_ee_joint_state'], [0.])
        self.assertTrue(any(row['left_ee_joint_state'][0]<1 for row in rows[:9]))

    def test_longer_route_failure_preserves_short_route_executed_jaw_and_partial_steps(self):
        adapter, planners, rows = dual_adapter(fail_at={'right': 2})
        action = dual_action(pairs=((.025, .425), (.025, .55)), gripper_events=[
            {'arm': 'left', 'phase': 'after_motion', 'goal_normalized': 0.}])
        action['path']['waypoints'][1].pop('left')
        result = self.execute(adapter, action)
        self.assertEqual((result['status'], result['reason'], result['failed_arm'], result['failed_waypoint_index']),
                         ('failed', 'path_infeasible', 'right', 1))
        self.assert_counts(result, rows, 9)
        self.assertEqual(len(planners['left'].pose_calls), 1)
        self.assertEqual(result['planned_trace']['arms']['left']['completed_gripper_steps'], 6)
        self.assertEqual(rows[-1]['left_ee_joint_state'], [0.])

    def test_finished_short_arm_is_rechecked_after_long_route_finishes(self):
        def drift(count, row, measured):
            if count == 12:
                measured['left'][0] += .02
        adapter, _, rows = dual_adapter(after_step=drift)
        action = dual_action(pairs=((.125, .425), (.125, .55)))
        action['path']['waypoints'][1].pop('left')
        result = self.execute(adapter, action)
        self.assertEqual((result['reason'], result['failed_arm']), ('tracking_error', 'left'))
        self.assert_counts(result, rows, 12)
        self.assertEqual(result['arm_results']['left']['phase'], 'final_held')

    def test_unequal_final_motion_and_jaws_hold_only_after_own_tail(self):
        adapter, _, rows = dual_adapter()
        action = dual_action(gripper_events=[
            {"arm": arm, "phase": "after_motion", "goal_normalized": 0.} for arm in ARMS])
        result = self.execute(adapter, action)
        self.assertEqual(result["status"], "completed")
        self.assert_counts(result, rows, 9)
        self.assertEqual(result["planned_trace"]["arms"]["right"]["completed_hold_steps"], 2)
        np.testing.assert_allclose([row["right_arm_joint_state"][0] for row in rows[5:]], .225)

    def test_initial_planning_failure_dispatches_neither_arm(self):
        for arm in ARMS:
            with self.subTest(arm=arm):
                adapter, planners, rows = dual_adapter(fail_at={arm: 1})
                result = self.execute(adapter)
                self.assertEqual((result["reason"], result["failed_arm"]), ("path_infeasible", arm))
                self.assert_counts(result, rows, 0)
                self.assertEqual([len(planners[side].pose_calls) for side in ARMS], [1, 1])

    def test_initial_planning_interruptions_never_dispatch_prepared_peer(self):
        for kind in ("cancel", "timeout", "exception"):
            with self.subTest(kind=kind):
                adapter, planners, rows = dual_adapter()
                cancellation, clock = threading.Event(), [0.]
                plan = planners["right"].plan_path
                def interrupt(*args):
                    value = plan(*args)
                    if kind == "cancel":
                        cancellation.set()
                    elif kind == "timeout":
                        clock[0] = 31.
                    else:
                        raise RuntimeError("synthetic independent planner fault")
                    return value
                planners["right"].plan_path = interrupt
                with patch("auriga.robot.backends.robodojo.robot_worker.time.monotonic", side_effect=lambda: clock[0]), \
                     patch("auriga.robot.backends.robodojo.robot_worker.sys.stderr", new=io.StringIO()):
                    result = self.execute(adapter, cancellation=cancellation)
                self.assert_counts(result, rows, 0)
                self.assertEqual([len(planners[arm].pose_calls) for arm in ARMS], [1, 1])
                if kind == "exception":
                    self.assertEqual(result["failed_arm"], "right")
                else:
                    self.assertEqual(result["reason"], "cancelled" if kind == "cancel" else "planning_timeout")

    def test_later_failure_stops_peer_and_retains_compiled_unexecuted_remainder(self):
        adapter, _, rows = dual_adapter(fail_at={"left": 2})
        result = self.execute(adapter, dual_action(pairs=((.125, .425), (.55, .55))))
        self.assertEqual((result["reason"], result["failed_arm"], result["failed_waypoint_index"]),
                         ("path_infeasible", "left", 1))
        self.assert_counts(result, rows, 3)
        trace = result["planned_trace"]
        self.assertEqual(len(trace["actions"]), 3)
        right = trace["arms"]["right"]["segments"][0]
        self.assertEqual((len(right["motion_actions"]), right["completed_motion_steps"]), (9, 3))

    def test_later_peer_failure_can_follow_earlier_own_jaw_release_without_rollback(self):
        adapter, _, rows = dual_adapter()
        adapter.pose_offsets["right"][2] = .02
        result = self.execute(adapter, dual_action(pairs=((.025, .425),), gripper_events=[
            {"arm": "left", "phase": "after_motion", "goal_normalized": 0.}]))
        self.assertEqual((result["reason"], result["failed_arm"]), ("tracking_error", "right"))
        self.assert_counts(result, rows, 9)
        self.assertEqual(result["planned_trace"]["arms"]["left"]["completed_gripper_steps"], 6)
        self.assertTrue(all(row["left_ee_joint_state"] == [0.] for row in rows[1:]))

    def test_same_row_failure_prevents_healthy_peer_jaw_or_next_plan(self):
        for failed in ARMS:
            with self.subTest(failed=failed):
                adapter, planners, rows = dual_adapter()
                adapter.pose_offsets[failed][2] = .02
                action = dual_action(pairs=((.125, .125),), gripper_events=[
                    {"arm": arm, "phase": "after_motion", "goal_normalized": 0.} for arm in ARMS])
                result = self.execute(adapter, action)
                self.assertEqual((result["reason"], result["failed_arm"]), ("tracking_error", failed))
                self.assert_counts(result, rows, 3)
                self.assertEqual(len(result["waypoint_results"]), 2)
                self.assertTrue(all(row["left_ee_joint_state"] == [1.] and row["right_ee_joint_state"] == [.3]
                                    for row in rows))
                self.assertEqual([len(planners[arm].pose_calls) for arm in ARMS], [1, 1])

    def test_final_hold_recheck_rejects_arm_that_drifted_after_earlier_completion(self):
        def drift(count, row, measured):
            if count == 5:
                measured["left"][0] += .02
        adapter, _, rows = dual_adapter(after_step=drift)
        result = self.execute(adapter)
        self.assertEqual((result["reason"], result["failed_arm"]), ("tracking_error", "left"))
        self.assert_counts(result, rows, 5)
        self.assertEqual(result["arm_results"]["left"]["phase"], "final_held")
        self.assertEqual(result["arm_results"]["left"]["sim_step"], 50)

    def test_slow_jaw_rebuilds_at_own_arrival_before_dispatch(self):
        adapter, _, rows = dual_adapter()
        state = adapter.state
        def changed():
            value = state()
            if len(rows) >= 3:
                value["arms"]["left"]["gripper"]["position_m"] = [.1, .1]
            return value
        adapter.state = changed
        result = self.execute(adapter, dual_action(gripper_events=[
            {"arm": "left", "phase": "after_motion", "goal_normalized": 0.}],
            arm_options={"left": {"gripper_speed_scale": .8}}))
        self.assertEqual(result["status"], "completed")
        self.assert_counts(result, rows, 10)
        self.assertTrue(all(row["left_ee_joint_state"] == [1.] for row in rows[:3]))
        self.assertEqual(rows[3]["left_ee_joint_state"], [0.])
        trace = result["planned_trace"]["arms"]["left"]["segments"][0]
        self.assertEqual(trace["gripper_compilation"]["compiled_sim_step"], 30)
        self.assertEqual(trace["planned_gripper_steps"], 7)

    def test_nonfinite_endpoint_rejects_before_own_jaw(self):
        adapter, _, rows = dual_adapter()
        state = adapter.state
        def invalid():
            value = state()
            if len(rows) >= 3:
                value["arms"]["left"]["ee_pose"]["position_m"][0] = float("nan")
            return value
        adapter.state = invalid
        with patch("auriga.robot.backends.robodojo.robot_worker.sys.stderr", new=io.StringIO()):
            result = self.execute(adapter, dual_action(gripper_events=[
                {"arm": "left", "phase": "after_motion", "goal_normalized": 0.}]))
        self.assertIn("nonfinite_endpoint_measurement", result["reason"])
        self.assertEqual(result["failed_arm"], "left")
        self.assert_counts(result, rows, 3)

    def test_independent_speeds_waypoint_overrides_and_final_during_motion_fractions(self):
        adapter, _, rows = dual_adapter()
        action = dual_action(pairs=((.125, .425), (.55, .55)), arm_options={
            "left": {"waypoint_speed_scales": [.5, 2.], "gripper_speed_scale": .8},
            "right": {"waypoint_speed_scales": [2., .5]}}, gripper_events=[
                {"arm": "left", "phase": "during_motion", "goal_normalized": 0.,
                 "start_fraction": .2, "end_fraction": .9}])
        result = self.execute(adapter, action)
        self.assertEqual(result["status"], "completed")
        trace = result["planned_trace"]
        left = trace["arms"]["left"]["segments"]
        self.assertNotIn("gripper_motion", left[0])
        self.assertIn("gripper_motion", left[1])
        self.assertTrue(all(row[-1] == 1. for row in left[0]["motion_actions"]))
        self.assertEqual(left[1]["motion_actions"][-1][-1], 0.)
        for arm, speeds in (("left", [.5, 2.]), ("right", [2., .5])):
            self.assertEqual([s["speed_scale"] for s in trace["arms"][arm]["segments"]], speeds)
        np.testing.assert_allclose(native_rows(rows), trace["actions"])

    def test_native_terminal_cancel_and_timeout_count_last_successful_row_once(self):
        for kind in ("limit", "success", "cancel", "timeout"):
            with self.subTest(kind=kind):
                event, clock = threading.Event(), [0.]
                def after(count, row, measured):
                    clock[0] = count
                    if kind == "cancel" and count == 3:
                        event.set()
                adapter, _, rows = dual_adapter(after_step=after,
                    step_limit=3 if kind == "limit" else 1000, success_at=3 if kind == "success" else None)
                with patch("auriga.robot.backends.robodojo.robot_worker.time.monotonic", side_effect=lambda: clock[0]):
                    result = self.execute(adapter, dual_action(timeout_s=2.5 if kind == "timeout" else 30), event)
                self.assert_counts(result, rows, 3)
                self.assertEqual(len(result["planned_trace"]["actions"]), 3)
                self.assertEqual(result["reason"], {"limit": "episode_ended", "success": "episode_ended",
                    "cancel": "cancelled", "timeout": "execution_timeout"}[kind])

    def test_faulting_native_call_preserves_started_vs_completed_and_row_context(self):
        def fail(count, row, measured):
            if count == 3:
                raise RuntimeError("fault after advancing native physics")
        adapter, _, rows = dual_adapter(after_step=fail)
        with patch("auriga.robot.backends.robodojo.robot_worker.sys.stderr", new=io.StringIO()):
            result = self.execute(adapter)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual((len(rows), result["executed_native_steps"], result["completed_native_steps"]), (3, 3, 2))
        self.assertEqual(result["planned_trace"]["completed_shared_steps"], 2)
        self.assertEqual(len(result["planned_trace"]["row_contexts"]), 3)
        self.assertEqual(result["planned_trace"]["arms"]["left"]["completed_motion_steps"], 2)

    def test_tracking_guard_identifies_own_waypoint_and_counts_completed_row(self):
        adapter, _, rows = dual_adapter(frozen=("right",))
        adapter.tracking_guard_enabled = True
        result = self.execute(adapter, dual_action(pairs=((.125, 1.2), (.8, 1.3))))
        self.assertEqual((result["reason"], result["failed_arm"]), ("tracking_divergence", "right"))
        self.assertEqual(result["failed_waypoint_index"], 0)
        self.assert_counts(result, rows, len(rows))
        self.assertEqual(result["planned_trace"]["row_contexts"][-1]["left"]["waypoint_index"], 1)

    def test_shared_cap_accepts_exact_250_and_rejects_later_segment_before_dispatch(self):
        for lengths, pairs, expected, status in (
                ((250, 1), ((.125, .225),), 250, "completed"),
                ((130, 130), ((.125, .225), (.25, .45)), 130, "failed")):
            with self.subTest(lengths=lengths):
                adapter, _, rows = dual_adapter()
                adapter._resample_plan = lambda q, arm: np.linspace(q[0], q[-1], lengths[ARMS.index(arm)] + 1)[1:]
                result = self.execute(adapter, dual_action(pairs=pairs))
                self.assertEqual(result["status"], status)
                self.assert_counts(result, rows, expected)
                if status == "failed":
                    self.assertEqual(result["reason"], "motion_duration_limit")

    def test_cap_counts_overlapping_jaw_and_peer_motion_once(self):
        adapter, _, rows = dual_adapter()
        adapter._resample_plan = lambda q, arm: np.linspace(q[0], q[-1], 251 if arm == "left" else 2)[1:]
        action = dual_action(gripper_events=[{"arm": "right", "phase": "after_motion", "goal_normalized": 1.}])
        result = self.execute(adapter, action)
        self.assertEqual(result["status"], "completed")
        self.assert_counts(result, rows, 250)
        self.assertEqual(result["planned_trace"]["arms"]["right"]["completed_gripper_steps"], 4)

    def test_predispatch_cap_guard_survives_overlong_segment_from_compiler(self):
        adapter, _, rows = dual_adapter()
        adapter._resample_plan = lambda q, arm: np.linspace(q[0], q[-1], 252)[1:]
        compile_segment = adapter._motion_segment
        adapter._motion_segment = lambda a, state, q, arm, remaining, **options: compile_segment(
            a, state, q, arm, 1000, **options)
        result = self.execute(adapter)
        self.assertEqual(result["reason"], "motion_duration_limit")
        self.assert_counts(result, rows, 250)
        self.assertEqual(len(result["planned_trace"]["actions"]), 250)

    def test_paired_endpoint_results_identify_previous_checked_waypoint_on_later_failure(self):
        adapter, _, rows = dual_adapter(fail_at={"left": 2})
        result = adapter.execute(dual_action(pairs=((.125, .225), (.25, .45))), lambda event: None, threading.Event())
        self.assertEqual(result["failed_waypoint_index"], 1)
        self.assertEqual(result["endpoint_result_waypoint_index"], 0)
        self.assertEqual(result["endpoint_result_sim_step"], 50)
        self.assertTrue(all(value["waypoint_index"] == 0 and value["sim_step"] == 50
                            for value in result["arm_results"].values()))


if __name__ == "__main__":
    unittest.main()
