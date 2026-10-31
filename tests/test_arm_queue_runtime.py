"""CPU-native dispatch boundary checks for bounded dependency queues and jaws."""

import io
import threading
import unittest
from unittest.mock import Mock, patch

import numpy as np

from test_dual_arm_runtime import dual_adapter, native_rows
from test_joint_replan_runtime import motion_action, periodic_pose
from test_native_wait import wait_action


def move(identifier, angle, **fields):
    return dict(id=identifier, kind="move", target=periodic_pose([angle]), **fields)


def jaw(identifier, goal=0., **fields):
    return dict(id=identifier, kind="gripper", goal_normalized=goal, **fields)


def queue_action(left=(), right=(), **fields):
    action = wait_action(timeout_s=30)
    for key in ("hold_native_steps", "path", "gripper_events"):
        action.pop(key)
    action.update(type="ArmQueueAction", arm_queues=dict(left=list(left), right=list(right)), **fields)
    return action


class ArmQueueRuntimeTests(unittest.TestCase):
    def execute(self, adapter, action, event=None):
        return adapter.execute(action, lambda event: None, event or threading.Event())

    def assert_count(self, result, rows, count):
        self.assertEqual(len(rows), count)
        self.assertEqual(result["executed_native_steps"], count)
        self.assertEqual(result["completed_native_steps"], count)
        self.assertEqual(result["executed_control_steps"], count*10)
        trace = result["planned_trace"]
        self.assertEqual(trace.get("completed_shared_steps", 0), count)
        for arm in ("left", "right"):
            self.assertEqual(sum(trace["arms"][arm].get("completed_"+phase+"_steps", 0)
                for phase in ("motion", "gripper", "hold")), count)

    def test_cost_capability_requires_calibrated_profile_and_resampled_mode(self):
        adapter, _, _ = dual_adapter()
        self.assertTrue(adapter.capabilities()["motion_cost_preview"]["available"])
        adapter.observation_profile = "rgb_proprio_v1"
        self.assertFalse(adapter.capabilities()["motion_cost_preview"]["available"])
        self.assertTrue(adapter.capabilities()["arm_queue"]["available"])
        adapter.observation_profile = "rgb_proprio_calibrated_v1"
        adapter.motion_execution_mode = "feedback"
        self.assertFalse(adapter.capabilities()["motion_cost_preview"]["available"])

    def test_contact_feedback_attaches_measured_checks_and_marks_unattempted_target_at_checkpoint(self):
        adapter, _, rows = dual_adapter()
        metadata = dict(position_m=[1., 0., 1.], angles_deg=[0., 0., 0.])
        calls = []
        def feedback(action, state):
            calls.append((action, state))
            return dict(target_scope="requested_final_pose_not_completion_claim", source_kind="fixture_measurement")
        with patch("auriga.robot.model.grasp_contact.contact_target_feedback", side_effect=feedback):
            result = self.execute(adapter, queue_action([move("done", .125, input_grasp_pose=metadata),
                dict(id="look", kind="observe", reason="check before next"),
                move("pending", .25, input_grasp_pose=metadata)], []))
        self.assertEqual(len(calls), 2)
        self.assertIn("grasp_contact_feedback", result["stage_results"]["done"]["endpoint_checks"][0])
        final = result["queue_contact_feedback"]["arms"]["left"]
        self.assertEqual((final["stage_id"], final["stage_status"]), ("pending", "pending"))
        self.assertEqual(final["feedback"]["target_scope"], "requested_final_pose_not_completion_claim")

    def test_contact_diagnostic_failure_cannot_fail_successful_motion(self):
        adapter, _, rows = dual_adapter()
        with patch("auriga.robot.model.grasp_contact.contact_target_feedback", side_effect=RuntimeError("bad diagnostic input")):
            result = self.execute(adapter, queue_action([move("l", .125, input_grasp_pose={"test": True})], []))
        self.assertEqual(result["status"], "completed")
        contact = result["stage_results"]["l"]["endpoint_checks"][0]["grasp_contact_feedback"]
        self.assertFalse(contact["available"])

    def test_unequal_lengths_and_crossed_progress_preserve_own_command_rows(self):
        adapter, _, rows = dual_adapter()
        result = self.execute(adapter, queue_action([move("l0", .125), move("l1", .55)], [move("r0", .425)]))
        self.assertEqual((result["status"], result["queue_status"]), ("completed", "completed"))
        self.assert_count(result, rows, 12)
        self.assertEqual([(a, c) for a, c, _ in adapter.planning_starts], [("left", 0), ("right", 0), ("left", 3)])
        self.assertEqual(result["completed_stage_ids"], ["l0", "l1", "r0"])
        self.assertEqual(result["pending_stage_ids"], [])
        np.testing.assert_allclose(native_rows(rows), result["planned_trace"]["actions"])
        self.assertEqual(result["planned_trace"]["arms"]["right"]["completed_hold_steps"], 3)
        self.assertTrue(all(value["phase"] == "final_held" for value in result["arm_results"].values()))

    def test_initial_jaw_overlaps_peer_move_without_motion_padding(self):
        adapter, _, rows = dual_adapter()
        result = self.execute(adapter, queue_action([jaw("lclose")], [move("rmove", .425)]))
        self.assertEqual(result["status"], "completed")
        self.assert_count(result, rows, 9)
        self.assertEqual(result["stage_results"]["lclose"]["completed_steps"], 6)
        contexts = result["planned_trace"]["row_contexts"]
        self.assertTrue(all(c["left"]["phase"] == "gripper" and c["right"]["phase"] == "motion" for c in contexts[:6]))

    def test_cross_arm_dependencies_gate_jaw_without_extra_idle_rows(self):
        adapter, _, rows = dual_adapter()
        result = self.execute(adapter, queue_action([move("lmove", .125), jaw("lclose", depends_on=["rmove"])],
                                                    [move("rmove", .425)]))
        self.assertEqual(result["status"], "completed")
        self.assert_count(result, rows, 15)
        self.assertTrue(all(row["left_ee_joint_state"] == [1.] for row in rows[:9]))
        self.assertTrue(all(row["left_ee_joint_state"] == [0.] for row in rows[9:]))
        self.assertEqual(result["stage_results"]["lclose"]["started_sim_step"], 90)

    def test_observe_checkpoint_stops_peer_mid_stage_and_never_releases(self):
        adapter, _, rows = dual_adapter()
        result = self.execute(adapter, queue_action([move("lmove", .125),
            dict(id="inspect", kind="observe", reason="verify support"), jaw("release", goal=1.)], [move("rmove", .425)]))
        self.assertEqual((result["status"], result["reason"], result["queue_status"]),
                         ("stopped", "observation_checkpoint", "awaiting_observation"))
        self.assert_count(result, rows, 3)
        self.assertEqual(result["completed_stage_ids"], ["lmove", "inspect"])
        self.assertEqual(result["pending_stage_ids"], ["release", "rmove"])
        self.assertEqual(result["checkpoint_stage_ids"], ["inspect"])
        self.assertEqual(result["stage_results"]["rmove"]["completed_steps"], 3)
        self.assertEqual(result["stage_results"]["release"]["completed_steps"], 0)

    def test_initial_checkpoint_spends_zero_steps_and_does_not_plan_peer(self):
        adapter, planners, rows = dual_adapter()
        result = self.execute(adapter, queue_action([dict(id="look", kind="observe", reason="inspect first")], [move("r", .425)]))
        self.assertEqual(result["reason"], "observation_checkpoint")
        self.assert_count(result, rows, 0)
        self.assertTrue(all(not p.pose_calls for p in planners.values()))

    def test_initial_failed_move_prevents_ready_peer_jaw(self):
        adapter, _, rows = dual_adapter(fail_at={"right": 1})
        result = self.execute(adapter, queue_action([jaw("lclose")], [move("bad", .425)]))
        self.assertEqual((result["reason"], result["failed_stage_id"]), ("path_infeasible", "bad"))
        self.assert_count(result, rows, 0)

    def test_initial_ready_group_cancel_timeout_and_exception_prevent_all_dispatch(self):
        for kind in ("cancel", "timeout", "exception"):
            with self.subTest(kind=kind):
                adapter, planners, rows = dual_adapter()
                event, clock = threading.Event(), [0.]
                native = planners["right"].plan_path
                def stop(*args):
                    result = native(*args)
                    if kind == "cancel":
                        event.set()
                    elif kind == "timeout":
                        clock[0] = 31.
                    else:
                        raise RuntimeError("ready-group planning fault")
                    return result
                planners["right"].plan_path = stop
                with patch("auriga.robot.backends.robodojo.robot_worker.time.monotonic", side_effect=lambda: clock[0]), \
                     patch("auriga.robot.backends.robodojo.robot_worker.sys.stderr", new=io.StringIO()):
                    result = self.execute(adapter, queue_action([jaw("lclose")], [move("r", .425)]), event)
                self.assert_count(result, rows, 0)
                self.assertEqual(result["completed_stage_ids"], [])

    def test_same_row_arrival_failure_blocks_all_next_stage_jaws(self):
        adapter, _, rows = dual_adapter()
        adapter.pose_offsets["right"][2] = .02
        result = self.execute(adapter, queue_action([move("l", .125), jaw("lc")], [move("r", .125), jaw("rc")]))
        self.assertEqual((result["reason"], result["failed_stage_id"]), ("tracking_error", "r"))
        self.assert_count(result, rows, 3)
        self.assertEqual(result["completed_stage_ids"], ["l"])
        self.assertTrue(all(row["left_ee_joint_state"] == [1.] and row["right_ee_joint_state"] == [.3] for row in rows))

    def test_dependency_wait_rechecks_own_pose_before_release(self):
        def drift(count, row, measured):
            if count == 9:
                measured["left"][0] += .02
        adapter, _, rows = dual_adapter(after_step=drift)
        result = self.execute(adapter, queue_action([move("l", .125), jaw("lc", depends_on=["r"])], [move("r", .425)]))
        self.assertEqual((result["reason"], result["failed_stage_id"], result["failed_phase"]),
                         ("tracking_error", "lc", "before_gripper"))
        self.assert_count(result, rows, 9)
        self.assertTrue(all(row["left_ee_joint_state"] == [1.] for row in rows))

    def test_final_hold_checks_empty_peer_arm_and_finished_arm(self):
        def drift(count, row, measured):
            if count == 5:
                measured["right"][0] += .02
        adapter, _, rows = dual_adapter(after_step=drift)
        result = self.execute(adapter, queue_action([move("l", .225)], []))
        self.assertEqual((result["reason"], result["failed_arm"], result["failed_phase"]),
                         ("tracking_error", "right", "final_held"))
        self.assert_count(result, rows, 5)

    def test_later_planning_failure_retains_peer_remaining_rows_and_completed_ids(self):
        adapter, _, rows = dual_adapter(fail_at={"left": 2})
        result = self.execute(adapter, queue_action([move("l0", .125), move("l1", .55)], [move("r", .425)]))
        self.assertEqual(result["failed_stage_id"], "l1")
        self.assert_count(result, rows, 3)
        self.assertEqual(result["completed_stage_ids"], ["l0"])
        right = result["planned_trace"]["arms"]["right"]["segments"][0]
        self.assertEqual((right["planned_steps"], right["completed_steps"]), (9, 3))

    def test_later_stage_failure_keeps_solver_diagnostics_and_bad_metadata_cannot_replace_reason(self):
        adapter, _, rows = dual_adapter(fail_at={"left": 2})
        original = adapter._plan_segment
        def plan(robot, target, diagnostics, index, **options):
            diagnostics.append(dict(waypoint_index=index, stage="ik", status="fixture_no_ik", success=[False]))
            return original(robot, target, diagnostics, index, **options)
        adapter._plan_segment = plan
        result = self.execute(adapter, queue_action([move("l0", .125), move("bad", .55, input_grasp_pose={})], []))
        self.assertEqual((result["reason"], result["failed_stage_id"]), ("path_infeasible", "bad"))
        feedback = result["failure_feedback"]
        self.assertEqual((feedback["stage_index"], feedback["stage_id"]), (1, "bad"))
        self.assertEqual(feedback["solver_summary"][0]["native_statuses"], ["fixture_no_ik"])
        self.assertTrue(feedback["grasp_metadata_unavailable"])

    def test_stage_local_motion_jaw_speeds_and_contact_metadata_preserved(self):
        adapter, _, rows = dual_adapter()
        metadata = dict(position_m=[1., 0., 1.], angles_deg=[0., 0., 0.], contact_region="finger_middle")
        result = self.execute(adapter, queue_action([move("l", .125, speed_scale=.5, input_grasp_pose=metadata),
                                                     jaw("lc", gripper_speed_scale=.8)], [move("r", .425, speed_scale=1.5)]))
        self.assertEqual(result["status"], "completed")
        own = result["planned_trace"]["arms"]["left"]["segments"]
        self.assertEqual(own[0]["input_grasp_pose"], metadata)
        self.assertEqual(own[0]["speed_scale"], .5)
        self.assertEqual(own[1]["gripper_speed_scale"], .8)
        self.assertLessEqual(np.max(np.abs(np.diff([1.] + [r["left_ee_joint_state"][0] for r in rows]))), .18*.8+1e-12)

    def test_cycle_rejected_before_observation_or_any_native_work(self):
        adapter, _, rows = dual_adapter()
        adapter.observe = Mock(side_effect=AssertionError("must validate first"))
        with self.assertRaisesRegex(ValueError, "dependency_cycle"):
            self.execute(adapter, queue_action([move("l", .1, depends_on=["r"])], [move("r", .2, depends_on=["l"])]))
        self.assertFalse(rows)

    def test_shared_cap_exact_250_and_no_dispatch_for_overlong_initial_stage(self):
        for count in (250, 251):
            with self.subTest(count=count):
                adapter, _, rows = dual_adapter()
                adapter._resample_plan = lambda q, arm: np.linspace(q[0], q[-1], count+1)[1:]
                result = self.execute(adapter, queue_action([move("l", .125)], []))
                self.assertEqual(result["status"], "completed" if count == 250 else "failed")
                self.assert_count(result, rows, count if count == 250 else 0)

    def test_predispatch_cap_defense_does_not_allow_251st_row_from_overlong_compiler(self):
        adapter, _, rows = dual_adapter()
        adapter._resample_plan = lambda q, arm: np.linspace(q[0], q[-1], 252)[1:]
        original = adapter._motion_segment
        adapter._motion_segment = lambda a, state, q, arm, remaining, **kw: original(a, state, q, arm, 1000, **kw)
        result = self.execute(adapter, queue_action([move("l", .125)], []))
        # Queue admission validates stage length independently of the compiler.
        self.assertEqual(result["reason"], "motion_duration_limit")
        self.assert_count(result, rows, 0)

    def test_jaw_completion_drift_rejects_before_dependent_motion(self):
        def drift(count, row, measured):
            if count == 6:
                measured["left"][0] += .02
        adapter, planners, rows = dual_adapter(after_step=drift)
        result = self.execute(adapter, queue_action([jaw("lc"), move("next", .125)], []))
        self.assertEqual((result["reason"], result["failed_stage_id"]), ("tracking_error", "lc"))
        self.assert_count(result, rows, 6)
        self.assertTrue(result["stage_results"]["lc"]["command_complete"])
        self.assertFalse(planners["left"].pose_calls)

    def test_cancel_timeout_native_terminal_count_completed_last_row_once(self):
        for kind in ("cancel", "timeout", "terminal"):
            with self.subTest(kind=kind):
                event, clock = threading.Event(), [0.]
                def step(count, row, measured):
                    clock[0] = count
                    if count == 3 and kind == "cancel":
                        event.set()
                adapter, _, rows = dual_adapter(after_step=step, step_limit=3 if kind == "terminal" else 1000)
                with patch("auriga.robot.backends.robodojo.robot_worker.time.monotonic", side_effect=lambda: clock[0]):
                    result = self.execute(adapter, queue_action([move("l", .425)], [move("r", .425)],
                        timeout_s=2.5 if kind == "timeout" else 30), event)
                self.assert_count(result, rows, 3)
                self.assertEqual(result["stage_results"]["l"]["completed_steps"], 3)
                self.assertEqual(result["completed_stage_ids"], [])

    def test_faulting_native_call_retains_partial_counts(self):
        def fault(count, row, measured):
            if count == 3:
                raise RuntimeError("synthetic physics fault")
        adapter, _, rows = dual_adapter(after_step=fault)
        with patch("auriga.robot.backends.robodojo.robot_worker.sys.stderr", new=io.StringIO()):
            result = self.execute(adapter, queue_action([move("l", .425)], []))
        self.assertEqual((result["status"], result["executed_native_steps"], result["completed_native_steps"]), ("unknown", 3, 2))
        self.assertEqual(result["stage_results"]["l"]["completed_steps"], 2)
        self.assertEqual(result["stage_results"]["l"]["dispatch_attempted_steps"], 3)
        self.assertTrue(result["stage_results"]["l"]["execution_uncertain"])
        self.assertEqual(len(result["planned_trace"]["row_contexts"]), 3)

    def test_feedback_rejects_queue_without_observation_or_physics(self):
        adapter, _, rows = dual_adapter()
        adapter.motion_execution_mode = "feedback"
        adapter.observe = Mock(side_effect=AssertionError("unsupported before observation"))
        result = self.execute(adapter, queue_action([move("l", .1)], []))
        self.assertEqual(result["reason"], "feedback_arm_queue_unsupported")
        self.assertFalse(rows)


class SingleArmJawGateTests(unittest.TestCase):
    def execute(self, adapter, action=None):
        return adapter.execute(action or motion_action(angles=(.125,), gripper=True), lambda event: None, threading.Event())

    def test_failed_arrival_spends_only_motion_rows_and_never_closes(self):
        adapter, _, rows = dual_adapter(frozen=("left",))
        result = self.execute(adapter)
        self.assertEqual((result["reason"], result["failed_phase"]), ("tracking_error", "before_gripper"))
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row["left_ee_joint_state"] == [1.] for row in rows))
        jaw_result = result["gripper_execution"]["left"]
        self.assertEqual((jaw_result["planned_steps"], jaw_result["completed_steps"], jaw_result["started"]), (6, 0, False))

    def test_successful_jaw_has_pre_and_post_pose_checks_and_command_counts(self):
        adapter, _, rows = dual_adapter()
        result = self.execute(adapter)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(rows), 9)
        self.assertEqual(result["motion_arrival_results"][0]["sim_step"], 30)
        self.assertEqual(result["motion_arrival_results"][0]["phase"], "before_gripper")
        self.assertEqual(result["waypoint_results"][0]["sim_step"], 90)
        self.assertEqual(result["waypoint_results"][0]["phase"], "after_gripper")
        self.assertTrue(result["gripper_execution"]["left"]["command_complete"])

    def test_after_jaw_drift_failure_reports_that_jaw_did_execute(self):
        def drift(count, row, measured):
            if count == 9:
                measured["left"][0] += .02
        adapter, _, rows = dual_adapter(after_step=drift)
        result = self.execute(adapter)
        self.assertEqual((result["reason"], result["failed_phase"]), ("tracking_error", "after_gripper"))
        self.assertEqual(result["gripper_execution"]["left"]["completed_steps"], 6)
        self.assertTrue(result["gripper_execution"]["left"]["command_complete"])

    def test_standalone_jaw_remains_allowed_without_a_motion_request(self):
        adapter, _, rows = dual_adapter()
        action = wait_action()
        action.pop("hold_native_steps")
        action["gripper_events"] = [dict(arm="left", phase="after_motion", goal_normalized=0.)]
        result = self.execute(adapter, action)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(rows), 6)
        self.assertTrue(result["gripper_execution"]["left"]["command_complete"])

    def test_nonfinite_pose_cannot_bypass_before_jaw_gate(self):
        adapter, _, rows = dual_adapter()
        original = adapter.state
        def state():
            value = original()
            if len(rows) >= 3:
                value["arms"]["left"]["ee_pose"]["position_m"][0] = float("nan")
            return value
        adapter.state = state
        with patch("auriga.robot.backends.robodojo.robot_worker.sys.stderr", new=io.StringIO()):
            result = self.execute(adapter)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(rows), 3)
        self.assertFalse(result["gripper_execution"]["left"]["started"])

    def test_faulting_first_jaw_call_reports_possible_execution_even_without_returned_jaw_row(self):
        def fault(count, row, measured):
            if count == 4:
                raise RuntimeError("jaw physics fault")
        adapter, _, rows = dual_adapter(after_step=fault)
        with patch("auriga.robot.backends.robodojo.robot_worker.sys.stderr", new=io.StringIO()):
            result = self.execute(adapter)
        self.assertEqual(result["status"], "unknown")
        jaw_result = result["gripper_execution"]["left"]
        self.assertEqual((jaw_result["completed_steps"], jaw_result["dispatch_attempted_steps"]), (0, 1))
        self.assertTrue(jaw_result["execution_uncertain"])


if __name__ == "__main__":
    unittest.main()
