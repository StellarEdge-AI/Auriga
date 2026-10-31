"""CPU coverage of paired native planning, merged dispatch and shared accounting."""

from copy import deepcopy
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import numpy as np

from test_joint_replan_runtime import Limits, Planner, periodic_pose
from test_native_wait import fake_adapter, wait_action


ARMS = ("left", "right")


def dual_action(pairs=((.125, .225),), **values):
    action = wait_action(timeout_s=30)
    del action["hold_native_steps"]
    action["path"] = {"kind": "backend_free", "waypoints": [
        {arm: periodic_pose([angle]) for arm, angle in zip(ARMS, pair)} for pair in pairs]}
    action.update(values)
    return action


def dual_adapter(*, fail_at=None, after_step=None, frozen=(), **native_options):
    measured = {arm: [0.] * 6 for arm in ARMS}
    jaws = {"left": 1., "right": .3}
    pose_offsets = {arm: np.zeros(3) for arm in ARMS}

    def follow(count):
        for arm in ARMS:
            if arm not in frozen:
                measured[arm] = list(dispatched[-1][f"{arm}_arm_joint_state"])
            jaws[arm] = dispatched[-1][f"{arm}_ee_joint_state"][0]
        if after_step is not None:
            after_step(count, dispatched[-1], measured)

    adapter, dispatched = fake_adapter(after_step=follow, **({"step_limit": 1000} | native_options))
    adapter.arm_velocity_step_limits = {arm: np.array([.2] * 6) for arm in ARMS}
    adapter.tracking_guard_enabled = False
    adapter.joint_replan_enabled = False

    def state():
        result = {"arms": {}}
        for arm in ARMS:
            pose = periodic_pose(measured[arm])
            pose["position_m"] = (np.asarray(pose["position_m"]) + pose_offsets[arm]).tolist()
            result["arms"][arm] = {"q_rad": list(measured[arm]), "ee_pose": pose,
                "gripper": {"position_source_kind": "measurement", "position_m": [jaws[arm]] * 2}}
        return result

    adapter.state = state
    robots = {arm: SimpleNamespace(robot_name=arm, arm_name=arm + "_arm", type="target",
        arm_joints_name=[f"{arm}_joint{i}" for i in range(1, 7)], arm_joint_indices=list(range(6)),
        entity_origin_pose=object(), gripper_joints_name=["jaw1", "jaw2"],
        gripper_move={"base": "jaw1", "sign": 1}, gripper_scale=[0., 1.]) for arm in ARMS}
    planners = {arm: Planner(wound=False, fail_pose_at=(fail_at or {}).get(arm)) for arm in ARMS}
    adapter.planning_starts = []
    for arm, planner in planners.items():
        native = planner.plan_path

        def plan(current, target, origin, *, _arm=arm, _native=native):
            adapter.planning_starts.append((_arm, len(dispatched), deepcopy(measured)))
            return _native(current, target, origin)

        planner.plan_path = plan
    adapter.robot = SimpleNamespace(planner=planners, robot_list=list(robots.values()),
        robot_key=[SimpleNamespace(data=SimpleNamespace(soft_joint_pos_limits=Limits())) for _ in ARMS],
        get_robot_by_arm_name=lambda name: robots[name.split("_")[0]],
        get_joint=lambda robot, envs: [np.asarray(measured[robot.robot_name], dtype=float)])
    adapter.pose_offsets = pose_offsets
    adapter.measured_jaws = jaws
    return adapter, planners, dispatched


def native_rows(dispatched):
    return [row["left_arm_joint_state"] + row["left_ee_joint_state"] +
            row["right_arm_joint_state"] + row["right_ee_joint_state"] for row in dispatched]


class DualArmRuntimeTests(unittest.TestCase):
    def execute(self, adapter, action=None, cancellation=None):
        return adapter.execute(action or dual_action(), lambda event: None, cancellation or threading.Event())

    def assert_counts(self, result, rows, count):
        self.assertEqual(len(rows), count)
        for field in ("executed_steps", "executed_native_steps", "completed_native_steps"):
            self.assertEqual(result[field], count)
        self.assertEqual(result["executed_control_steps"], count * 10)

    def test_three_and_five_merge_to_five_real_native_calls_and_pad_shorter_endpoint(self):
        adapter, planners, rows = dual_adapter()
        result = self.execute(adapter)
        self.assertEqual((result["status"], result["reason"]), ("completed", None))
        self.assert_counts(result, rows, 5)
        self.assertEqual([(arm, count) for arm, count, state in adapter.planning_starts], [("left", 0), ("right", 0)])
        self.assertEqual(adapter.planning_starts[0][2], adapter.planning_starts[1][2])
        trace = result["planned_trace"]
        np.testing.assert_array_equal(native_rows(rows), trace["actions"])
        np.testing.assert_allclose([row["left_arm_joint_state"][0] for row in rows], [.125 / 3, .25 / 3, .125, .125, .125])
        np.testing.assert_allclose([row["right_arm_joint_state"][0] for row in rows], np.linspace(.045, .225, 5))
        self.assertEqual((trace["planned_motion_steps"], trace["planned_gripper_steps"]), (5, 0))
        self.assertEqual(trace["arms"]["left"]["completed_motion_steps"], 3)
        self.assertEqual(trace["arms"]["left"]["completed_motion_hold_steps"], 2)
        self.assertEqual(trace["arms"]["right"]["completed_motion_steps"], 5)
        self.assertEqual(set(result["waypoint_results"][0]["arms"]), set(ARMS))
        self.assertEqual({sample["sim_step"] for sample in result["actual_trace"]}, set(range(0, 51, 10)))
        self.assertTrue(all(not planner.joint_calls for planner in planners.values()))

    def test_independent_speeds_and_waypoint_overrides_use_actual_resampler(self):
        adapter, planners, rows = dual_adapter()
        action = dual_action(pairs=((.125, .225), (.25, .45)), speed_scale=1., arm_options={
            "left": {"speed_scale": .5, "waypoint_speed_scales": [.5, 2.]},
            "right": {"speed_scale": 2., "waypoint_speed_scales": [2., .5]}})
        result = self.execute(adapter, action)
        self.assertEqual(result["status"], "completed")
        segments = result["planned_trace"]["segments"]
        self.assertEqual([segment["planned_motion_steps"] for segment in segments], [5, 9])
        self.assert_counts(result, rows, 14)
        self.assertEqual([(arm, count) for arm, count, state in adapter.planning_starts],
                         [("left", 0), ("right", 0), ("left", 5), ("right", 5)])
        for arm, scales in (("left", [.5, 2.]), ("right", [2., .5])):
            self.assertEqual(result["arm_options"][arm]["waypoint_speed_scales"], scales)
            for index, segment in enumerate(segments):
                own = segment["arms"][arm]
                self.assertEqual(own["speed_scale"], scales[index])
                points = np.vstack([planners[arm].originals[index][0], own["q_rad"]])
                self.assertLessEqual(np.max(np.abs(np.diff(points, axis=0))), .05 * scales[index] + 1e-12)
        np.testing.assert_allclose(native_rows(rows), segments[0]["actions"] + segments[1]["actions"])

    def test_during_motion_and_after_motion_keep_both_final_arm_targets(self):
        adapter, _, rows = dual_adapter()
        action = dual_action(gripper_events=[
            {"arm": "left", "phase": "during_motion", "goal_normalized": 0., "start_fraction": 0., "end_fraction": 1.},
            {"arm": "right", "phase": "after_motion", "goal_normalized": 1.}])
        result = self.execute(adapter, action)
        self.assertEqual(result["status"], "completed")
        self.assert_counts(result, rows, 10)
        trace = result["planned_trace"]
        self.assertEqual((trace["planned_motion_steps"], trace["planned_gripper_steps"]), (6, 4))
        np.testing.assert_allclose([row["left_ee_joint_state"][0] for row in rows[:6]], np.linspace(1., 0., 7)[1:])
        self.assertTrue(all(row["right_ee_joint_state"] == [.3] for row in rows[:6]))
        self.assertTrue(all(row["left_ee_joint_state"] == [0.] and row["right_ee_joint_state"] == [1.] for row in rows[6:]))
        np.testing.assert_allclose([row["left_arm_joint_state"][0] for row in rows[6:]], .125)
        np.testing.assert_allclose([row["right_arm_joint_state"][0] for row in rows[6:]], .225)
        np.testing.assert_allclose(native_rows(rows), trace["actions"])

    def test_both_after_motion_slow_ramps_share_tail_and_pad_final_jaw(self):
        adapter, _, rows = dual_adapter()
        action = dual_action(gripper_events=[
            {"arm": "left", "phase": "after_motion", "goal_normalized": 0.},
            {"arm": "right", "phase": "after_motion", "goal_normalized": 1.}],
            arm_options={"left": {"gripper_speed_scale": .5}, "right": {"gripper_speed_scale": .25}})
        result = self.execute(adapter, action)
        self.assertEqual(result["status"], "completed")
        self.assert_counts(result, rows, 25)
        trace = result["planned_trace"]
        self.assertEqual((trace["planned_motion_steps"], trace["planned_gripper_steps"]), (5, 20))
        self.assertEqual(trace["arms"]["left"]["planned_gripper_steps"], 18)
        self.assertEqual(trace["arms"]["right"]["planned_gripper_steps"], 20)
        for arm, initial, bound, goal in (("left", 1., .09, 0.), ("right", .3, .045, 1.)):
            jaw = [row[f"{arm}_ee_joint_state"][0] for row in rows]
            np.testing.assert_allclose(jaw[:5], initial)
            self.assertLessEqual(np.max(np.abs(np.diff([initial] + jaw[5:]))), bound + 1e-12)
            self.assertEqual(jaw[-1], goal)
        np.testing.assert_allclose(native_rows(rows), trace["actions"])

    def test_both_during_motion_follow_own_lengths_then_hold(self):
        adapter, _, rows = dual_adapter()
        action = dual_action(gripper_events=[
            {"arm": arm, "phase": "during_motion", "goal_normalized": 0., "start_fraction": 0., "end_fraction": 1.}
            for arm in ARMS])
        result = self.execute(adapter, action)
        self.assertEqual(result["status"], "completed")
        self.assert_counts(result, rows, 6)
        np.testing.assert_allclose([row["left_ee_joint_state"][0] for row in rows], np.linspace(1., 0., 7)[1:])
        np.testing.assert_allclose([row["right_ee_joint_state"][0] for row in rows],
                                   (.3 * np.linspace(1., 0., 6)[1:]).tolist() + [0.])
        self.assertEqual(result["planned_trace"]["planned_gripper_steps"], 0)

    def test_either_measured_endpoint_failure_prevents_all_after_motion_release(self):
        for failed_arm in ARMS:
            with self.subTest(arm=failed_arm):
                adapter, _, rows = dual_adapter()
                adapter.pose_offsets[failed_arm][2] = .02
                action = dual_action(gripper_events=[{"arm": arm, "phase": "after_motion", "goal_normalized": 0.}
                                                     for arm in ARMS])
                result = self.execute(adapter, action)
                self.assertEqual((result["reason"], result["failed_arm"]), ("tracking_error", failed_arm))
                self.assert_counts(result, rows, 5)
                self.assertTrue(all(row["left_ee_joint_state"] == [1.] and row["right_ee_joint_state"] == [.3] for row in rows))
                self.assertAlmostEqual(result["arm_results"][failed_arm]["position_error_m"], .02)

    def test_guard_checks_each_arm_and_stops_without_release(self):
        for failed_arm in ARMS:
            with self.subTest(arm=failed_arm):
                adapter, _, rows = dual_adapter(frozen=(failed_arm,))
                adapter.tracking_guard_enabled = True
                action = dual_action(pairs=((1.2, 1.2),), gripper_events=[
                    {"arm": arm, "phase": "after_motion", "goal_normalized": 0.} for arm in ARMS])
                result = self.execute(adapter, action)
                self.assertEqual((result["reason"], result["failed_arm"]), ("tracking_divergence", failed_arm))
                self.assertEqual(result["tracking_guard"]["consecutive_steps"], 5)
                self.assertLess(len(rows), result["planned_trace"]["planned_motion_steps"])
                self.assertTrue(all(row["left_ee_joint_state"] == [1.] and row["right_ee_joint_state"] == [.3] for row in rows))
                self.assertEqual(result["stop_confirmation"]["settling_steps"], 0)

    def test_failed_native_plan_has_no_pair_dispatch_and_retains_earlier_pairs(self):
        for arm in ARMS:
            for index, expected in ((1, 0), (2, 5)):
                with self.subTest(arm=arm, index=index):
                    adapter, planners, rows = dual_adapter(fail_at={arm: index})
                    result = self.execute(adapter, dual_action(pairs=((.125, .225), (.25, .45))))
                    self.assertEqual((result["reason"], result["failed_arm"]), ("path_infeasible", arm))
                    self.assertEqual(result["failed_waypoint_index"], index - 1)
                    self.assert_counts(result, rows, expected)
                    self.assertEqual(len(result["waypoint_results"]), index - 1)
                    self.assertEqual([len(planners[side].pose_calls) for side in ARMS], [index, index])
                    self.assertEqual(result["failure_feedback"]["arm"], arm)

    def test_planning_interruption_or_exception_never_dispatches_prepared_pair(self):
        for kind in ("cancel", "timeout", "exception"):
            with self.subTest(kind=kind):
                adapter, planners, rows = dual_adapter()
                cancellation, clock = threading.Event(), [0.]
                original = planners["right"].plan_path

                def stop_planning(*args):
                    result = original(*args)
                    if kind == "exception":
                        raise RuntimeError("right native planner failure")
                    if kind == "cancel":
                        cancellation.set()
                    else:
                        clock[0] = 31.
                    return result

                planners["right"].plan_path = stop_planning
                with patch("auriga.robot.backends.robodojo.robot_worker.time.monotonic", side_effect=lambda: clock[0]), \
                     patch("auriga.robot.backends.robodojo.robot_worker.sys.stderr", new=io.StringIO()):
                    result = self.execute(adapter, cancellation=cancellation)
                self.assert_counts(result, rows, 0)
                self.assertEqual([len(planners[arm].pose_calls) for arm in ARMS], [1, 1])
                if kind == "exception":
                    self.assertEqual(result["failed_arm"], "right")
                    self.assertIn("right native planner failure", result["reason"])
                else:
                    self.assertEqual(result["reason"], "cancelled" if kind == "cancel" else "planning_timeout")

    def test_slow_jaw_rebuilds_after_both_arms_arrive(self):
        adapter, _, rows = dual_adapter()
        state = adapter.state

        def moved_opening():
            value = state()
            if len(rows) >= 5:
                value["arms"]["right"]["gripper"]["position_m"] = [1., 1.]
            return value

        adapter.state = moved_opening
        action = dual_action(gripper_events=[
            {"arm": arm, "phase": "after_motion", "goal_normalized": 0.} for arm in ARMS],
            arm_options={"right": {"gripper_speed_scale": .5}})
        result = self.execute(adapter, action)
        self.assertEqual(result["status"], "completed")
        self.assert_counts(result, rows, 19)
        self.assertTrue(all(row["left_ee_joint_state"] == [1.] and row["right_ee_joint_state"] == [.3] for row in rows[:5]))
        self.assertAlmostEqual(rows[5]["right_ee_joint_state"][0], 11 / 12)
        trace = result["planned_trace"]["arms"]["right"]
        self.assertEqual(trace["gripper_compilation"]["compiled_sim_step"], 50)
        self.assertEqual(trace["planned_gripper_steps"], 14)

    def test_nonfinite_endpoint_rejects_before_jaws_and_identifies_arm(self):
        adapter, _, rows = dual_adapter()
        state = adapter.state

        def nonfinite_pose():
            value = state()
            if len(rows) >= 5:
                value["arms"]["right"]["ee_pose"]["position_m"][0] = float("nan")
            return value

        adapter.state = nonfinite_pose
        action = dual_action(gripper_events=[{"arm": "left", "phase": "after_motion", "goal_normalized": 0.}])
        with patch("auriga.robot.backends.robodojo.robot_worker.sys.stderr", new=io.StringIO()):
            result = self.execute(adapter, action)
        self.assertEqual((result["status"], result["failed_arm"]), ("failed", "right"))
        self.assertIn("nonfinite_endpoint_measurement", result["reason"])
        self.assert_counts(result, rows, 5)
        self.assertTrue(all(row["left_ee_joint_state"] == [1.] for row in rows))

    def test_terminal_cancel_timeout_share_native_row_accounting(self):
        for kind, count in (("limit", 3), ("success", 2), ("cancel", 3), ("timeout", 3), ("before", 0)):
            with self.subTest(kind=kind):
                cancellation, clock = threading.Event(), [0.]

                def after_step(index, row, measured):
                    clock[0] = float(index)
                    if kind == "cancel" and index == count:
                        cancellation.set()

                if kind == "before":
                    cancellation.set()
                adapter, _, rows = dual_adapter(after_step=after_step,
                    step_limit=3 if kind == "limit" else 1000, success_at=2 if kind == "success" else None)
                action = dual_action(timeout_s=2.5 if kind == "timeout" else 30)
                with patch("auriga.robot.backends.robodojo.robot_worker.time.monotonic", side_effect=lambda: clock[0]):
                    result = self.execute(adapter, action, cancellation)
                self.assert_counts(result, rows, count)
                expected = {"limit": "episode_ended", "success": "episode_ended", "cancel": "cancelled",
                            "timeout": "execution_timeout", "before": "cancelled"}[kind]
                self.assertEqual(result["reason"], expected)
                self.assertEqual(result["stop_confirmation"]["settling_steps"], 0)

    def test_shared_250_step_cap_counts_merged_pairs_and_shared_jaw_tail(self):
        adapter, _, rows = dual_adapter()
        adapter._resample_plan = lambda positions, arm: np.linspace(positions[0], positions[-1], 131)[1:]
        result = self.execute(adapter, dual_action(pairs=((.125, .225), (.25, .45))))
        self.assertEqual((result["reason"], result["failed_waypoint_index"]), ("motion_duration_limit", 1))
        self.assert_counts(result, rows, 130)
        # Each arm's own motion + jaw fits, but max(motion) + max(jaw) does not.
        adapter, _, rows = dual_adapter()
        adapter._resample_plan = lambda positions, arm: np.linspace(positions[0], positions[-1], 250 if arm == "left" else 2)[1:]
        action = dual_action(gripper_events=[{"arm": "right", "phase": "after_motion", "goal_normalized": 1.}])
        result = self.execute(adapter, action)
        self.assertEqual(result["reason"], "motion_duration_limit")
        self.assert_counts(result, rows, 0)

    def test_exact_250_merged_steps_succeeds(self):
        adapter, _, rows = dual_adapter()
        adapter._resample_plan = lambda positions, arm: np.linspace(positions[0], positions[-1], 251 if arm == "left" else 2)[1:]
        result = self.execute(adapter)
        self.assertEqual(result["status"], "completed")
        self.assert_counts(result, rows, 250)

    def test_feedback_and_preview_reject_before_planning_observation_or_dispatch(self):
        for mode, preview_id, reason in (("feedback", False, "feedback_dual_arm_unsupported"),
                                        ("resampled", True, "dual_arm_motion_preview_unsupported")):
            adapter, planners, rows = dual_adapter()
            adapter.motion_execution_mode = mode
            adapter.observe = Mock(side_effect=AssertionError("must reject before observation"))
            action = dual_action()
            if preview_id:
                action["preview_plan_id"] = "single-arm-plan"
            result = self.execute(adapter, action)
            self.assertEqual((result["status"], result["reason"]), ("rejected", reason))
            self.assertEqual(result["motion_execution_mode"], mode)
            self.assert_counts(result, rows, 0)
            self.assertTrue(all(not planner.pose_calls for planner in planners.values()))
            caps = adapter.capabilities()
            self.assertEqual((caps["dual_arm"], caps["max_simultaneous_arms"]), (mode == "resampled", 2 if mode == "resampled" else 1))
        with self.assertRaisesRegex(ValueError, "dual_arm_motion_preview_unsupported"):
            adapter.preview_motion(dual_action())

    def test_dual_detector_defers_malformed_waypoints_to_validation(self):
        from auriga.robot.motion.dual_arm_motion import is_dual_action
        for invalid in (None, True, 5, "left,right", {"left": {}}):
            self.assertFalse(is_dual_action({"path": {"waypoints": invalid}}))

    def test_native_fault_keeps_started_and_completed_counts(self):
        def fail(count, row, measured):
            if count == 3:
                raise RuntimeError("fault after physics advanced")

        adapter, _, rows = dual_adapter(after_step=fail)
        with patch("auriga.robot.backends.robodojo.robot_worker.sys.stderr", new=io.StringIO()):
            result = self.execute(adapter)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual((len(rows), result["executed_native_steps"], result["completed_native_steps"]), (3, 3, 2))
        self.assertEqual(result["executed_control_steps"], 30)
        self.assertEqual(result["stop_confirmation"]["settling_steps"], 0)

    def test_private_planning_diagnostics_attribute_both_native_planners(self):
        adapter, _, rows = dual_adapter()
        original = adapter._plan_segment

        def record(robot, target, diagnostics, index, **options):
            diagnostics.append({"waypoint_index": index, "stage": "fixture_native_plan"})
            return original(robot, target, diagnostics, index, **options)

        adapter._plan_segment = record
        with tempfile.TemporaryDirectory() as directory:
            adapter.directory = Path(directory)
            result = self.execute(adapter)
            private = json.loads(next((adapter.directory / "development-diagnostics").glob("*.json")).read_text())
        self.assertEqual(result["status"], "completed")
        self.assertEqual([(item["arm"], item["waypoint_index"]) for item in private["planning_diagnostics"]],
                         [("left", 0), ("right", 0)])


if __name__ == "__main__":
    unittest.main()
