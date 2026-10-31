"""CPU coverage of optional replanning through the real planning/dispatch boundary."""

from copy import deepcopy
import io
import json
import math
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import numpy as np

from test_native_wait import fake_adapter, wait_action


def periodic_pose(q):
    angle = float(q[0])
    return dict(position_m=[math.cos(angle), math.sin(angle), 1.],
                quaternion_xyzw=[0., 0., math.sin(angle / 2), math.cos(angle / 2)])


def motion_action(arm="left", angles=(-.125,), *, gripper=False, timeout_s=30):
    action = wait_action(timeout_s=timeout_s)
    del action["hold_native_steps"]
    action["path"] = dict(kind="backend_free", waypoints=[
        {arm: periodic_pose([angle])} for angle in angles])
    if gripper:
        action["gripper_events"] = [dict(arm=arm, phase="after_motion", goal_normalized=0.)]
    return action


class Limits:
    def __getitem__(self, key):
        return self

    def cpu(self):
        return self

    def numpy(self):
        return np.array([[-10., 10.]] * 5 + [[-3.14, 3.14]])


class Planner:
    """A periodic geometric fixture; native collision solving is not simulated."""

    def __init__(self, *, wound=True, candidate="success", after_candidate=None, fail_pose_at=None):
        self.wound, self.candidate = wound, candidate
        self.after_candidate, self.fail_pose_at = after_candidate, fail_pose_at
        self.motion_planner = SimpleNamespace()
        self.pose_calls, self.joint_calls, self.originals = [], [], []

    def plan_path(self, current, target, origin):
        self.pose_calls.append((np.array(current, copy=True), deepcopy(target), origin))
        if len(self.pose_calls) == self.fail_pose_at:
            return {"status": "Fail"}
        goal = np.array(current, copy=True)
        goal[0] = math.atan2(target[1], target[0]) + (math.tau if self.wound else 0.)
        original = np.stack([current, goal])
        self.originals.append(original.copy())
        return {"status": "Success", "position": original}

    def plan_joint(self, current, goal):
        self.joint_calls.append((current.copy(), goal.copy()))
        if self.after_candidate is not None:
            self.after_candidate()
        if self.candidate == "exception":
            raise RuntimeError("synthetic native planner failure")
        if self.candidate == "fail":
            return {"status": "Fail"}
        positions = np.stack([current, goal])
        if self.candidate == "bad_start":
            positions[0, 1] += .2
        return {"status": "Success", "position": positions}


def replan_adapter(arm="left", *, enabled=True, wound=True, candidate="success",
                   after_candidate=None, fail_pose_at=None, **native_options):
    measured = {"left": [0.] * 6, "right": [0.] * 6}

    def follow_dispatched(count):
        for side in measured:
            measured[side] = list(dispatched[-1][f"{side}_arm_joint_state"])

    adapter, dispatched = fake_adapter(after_step=follow_dispatched, **native_options)
    adapter.joint_replan_enabled = enabled
    adapter.state = lambda: {"arms": {side: dict(q_rad=list(q), ee_pose=periodic_pose(q))
                                     for side, q in measured.items()}}
    robot = SimpleNamespace(robot_name="fixture", arm_name=arm + "_arm", type="target",
                            arm_joints_name=[f"joint{i}" for i in range(1, 7)],
                            arm_joint_indices=list(range(6)), entity_origin_pose=object())
    planner = Planner(wound=wound, candidate=candidate, after_candidate=after_candidate,
                      fail_pose_at=fail_pose_at)
    adapter.robot = SimpleNamespace(
        planner={robot.robot_name: planner}, robot_list=[robot],
        robot_key=[SimpleNamespace(data=SimpleNamespace(soft_joint_pos_limits=Limits()))],
        get_robot_by_arm_name=lambda name: robot,
        get_joint=lambda selected, env_ids: [np.array(measured[arm], dtype=float)])
    adapter.geometry_calls = []

    def geometry(selected_robot, selected_planner, target):
        adapter.geometry_calls.append((selected_robot, selected_planner, deepcopy(target)))

        def transforms(q):
            angle = float(q[0])
            c, s = math.cos(angle), math.sin(angle)
            tool = np.array([[c, -s, 0., c], [s, c, 0., s],
                             [0., 0., 1., 1.], [0., 0., 0., 1.]])
            return {"base": np.eye(4), "tool": tool}

        def pose_error(q):
            actual = periodic_pose(q)
            distance = np.linalg.norm(np.array(actual["position_m"]) - target["position_m"])
            dot = abs(np.dot(actual["quaternion_xyzw"], target["quaternion_xyzw"]))
            return float(distance), 2 * math.acos(float(np.clip(dot, 0., 1.)))

        return ["revolute"] * 6, transforms, pose_error

    # Only simulator geometry and native planner are fake. Candidate validation,
    # optional admission, resampling and execute/receipt accounting are real.
    adapter._joint_replan_geometry = geometry
    return adapter, robot, planner, dispatched


class JointReplanRuntimeTests(unittest.TestCase):
    def segment(self, adapter, robot, target=None):
        diagnostics = []
        plan = adapter._plan_segment(
            robot, target or periodic_pose([-.125]), diagnostics, waypoint_index=2,
            joint_replan_context={"deadline": time.monotonic() + 60.})
        return plan, diagnostics

    def execute(self, adapter, action=None):
        return adapter.execute(action or motion_action(), lambda event: None, threading.Event())

    def assert_counts(self, result, count):
        self.assertEqual(result["executed_native_steps"], count)
        self.assertEqual(result["completed_native_steps"], count)
        self.assertEqual(result["executed_steps"], count)
        self.assertEqual(result["executed_control_steps"], count * 10)

    def test_enabled_segment_selects_validated_shorter_native_plan_for_either_arm(self):
        for arm in ("left", "right"):
            with self.subTest(arm=arm):
                adapter, robot, planner, dispatched = replan_adapter(arm)
                plan, diagnostics = self.segment(adapter, robot)
                self.assertEqual(plan[1], 1)
                np.testing.assert_allclose(plan[0], [[0.] * 6, [-.125] + [0.] * 5])
                self.assertEqual(len(adapter._resample_plan(plan[0], arm)), 3)
                self.assertGreater(len(adapter._resample_plan(planner.originals[0], arm)), 100)
                self.assertEqual(len(planner.pose_calls), 1)
                self.assertEqual(len(planner.joint_calls), 1)
                np.testing.assert_array_equal(planner.joint_calls[0][0], [0.] * 6)
                np.testing.assert_allclose(planner.joint_calls[0][1], [-.125] + [0.] * 5)
                self.assertIs(planner.pose_calls[0][2], robot.entity_origin_pose)
                target = periodic_pose([-.125])
                self.assertEqual(planner.pose_calls[0][1], target["position_m"] +
                                 target["quaternion_xyzw"][3:] + target["quaternion_xyzw"][:3])
                self.assertEqual(dispatched, [])
                self.assertEqual(adapter.env.take_action_cnt[0], 0)
                selected = [d for d in diagnostics if d.get("stage") == "joint_replan"]
                self.assertEqual(len(selected), 1)
                self.assertTrue(selected[0]["selected"])

    def test_disabled_or_absent_flag_preserves_original_path_without_geometry_or_replan(self):
        for absent in (False, True):
            with self.subTest(absent=absent):
                adapter, robot, planner, _ = replan_adapter(enabled=False)
                if absent:
                    del adapter.joint_replan_enabled
                plan, diagnostics = self.segment(adapter, robot)
                np.testing.assert_array_equal(plan[0], planner.originals[0])
                self.assertEqual(len(planner.pose_calls), 1)
                self.assertEqual(planner.joint_calls, [])
                self.assertEqual(adapter.geometry_calls, [])
                self.assertFalse(any(d.get("stage") == "joint_replan" for d in diagnostics))

    def test_no_equivalent_improvement_and_native_pose_failure_do_not_replan(self):
        for options in ({"wound": False}, {"fail_pose_at": 1}):
            with self.subTest(options=options):
                adapter, robot, planner, _ = replan_adapter(**options)
                plan, _ = self.segment(adapter, robot)
                if options.get("fail_pose_at"):
                    self.assertIsNone(plan)
                else:
                    np.testing.assert_array_equal(plan[0], planner.originals[0])
                self.assertEqual(planner.joint_calls, [])

    def test_replan_from_ik_candidate_rescues_wound_ik_when_plan_path_fails(self):
        adapter, robot, planner, _ = replan_adapter(fail_pose_at=1)
        wound_goal = [-0.125 + 2.0 * np.pi, 0., 0., 0., 0., 0.]
        diagnostics = [{
            "stage": "ik",
            "waypoint_index": 0,
            "candidate": {
                "q_rad": [wound_goal],
                "solver_feasible": True,
                "position_error_m": 0.0002,
                "orientation_error_rad": 0.0002,
            },
        }]
        plan = adapter._plan_segment(
            robot, periodic_pose([-0.125]), diagnostics, 0,
            joint_replan_context={"deadline": time.monotonic() + 60.},
        )
        self.assertIsNotNone(plan)
        self.assertEqual(len(planner.joint_calls), 1)
        np.testing.assert_allclose(plan[0][-1], [-0.125, 0., 0., 0., 0., 0.], atol=1e-6)
        ik_replan_diag = next(d for d in diagnostics if d.get("stage") == "joint_replan_from_ik")
        self.assertTrue(ik_replan_diag["selected"])
        self.assertEqual(ik_replan_diag["reason"], "rescued_winding_ik_via_joint_plan")

    def test_native_failure_or_invalid_candidate_keeps_original_without_wrapping_it(self):
        for candidate in ("fail", "bad_start"):
            with self.subTest(candidate=candidate):
                adapter, robot, planner, dispatched = replan_adapter(candidate=candidate)
                plan, diagnostics = self.segment(adapter, robot)
                np.testing.assert_array_equal(plan[0], planner.originals[0])
                self.assertGreater(plan[0][-1, 0], 6.)
                self.assertEqual(len(planner.joint_calls), 1)
                self.assertEqual(dispatched, [])
                self.assertFalse(next(d for d in diagnostics if d.get("stage") == "joint_replan")["selected"])

    def test_original_requested_pose_is_passed_to_geometry_and_mismatch_rejects(self):
        adapter, robot, planner, _ = replan_adapter()
        requested = periodic_pose([-.125])
        requested["position_m"][2] = 1.02
        plan, _ = self.segment(adapter, robot, requested)
        self.assertEqual(adapter.geometry_calls[0][2], requested)
        np.testing.assert_array_equal(plan[0], planner.originals[0])
        self.assertEqual(planner.joint_calls, [])

    def test_multiwaypoint_replans_from_current_and_gripper_runs_only_after_final_motion(self):
        for arm in ("left", "right"):
            with self.subTest(arm=arm):
                adapter, _, planner, dispatched = replan_adapter(arm)
                result = self.execute(adapter, motion_action(arm, (-.125, -.25), gripper=True))
                self.assertEqual((result["status"], result["reason"]), ("completed", None))
                dwell = 6 if arm == "left" else 2
                self.assert_counts(result, 6 + dwell)
                self.assertEqual(len(dispatched), 6 + dwell)
                self.assertEqual(len(planner.joint_calls), 2)
                np.testing.assert_allclose(planner.joint_calls[1][0], [-.125] + [0.] * 5)
                self.assertEqual([r["waypoint_index"] for r in result["waypoint_results"]], [0, 1])
                initial_gripper = 1. if arm == "left" else .3
                for row in dispatched[:6]:
                    self.assertEqual(row[f"{arm}_ee_joint_state"], [initial_gripper])
                for row in dispatched[6:]:
                    self.assertEqual(row[f"{arm}_ee_joint_state"], [0.])
                    np.testing.assert_allclose(row[f"{arm}_arm_joint_state"], [-.25] + [0.] * 5)
                other = "right" if arm == "left" else "left"
                for row in dispatched:
                    self.assertEqual(row[f"{other}_arm_joint_state"], [0.] * 6)
                self.assertNotIn("joint_replan", result)

    def test_later_original_failure_preserves_partial_counts_and_skips_final_gripper(self):
        adapter, _, planner, dispatched = replan_adapter(fail_pose_at=2)
        result = self.execute(adapter, motion_action(angles=(-.125, -.25), gripper=True))
        self.assertEqual((result["status"], result["reason"]), ("failed", "path_infeasible"))
        self.assertEqual(result["failed_waypoint_index"], 1)
        self.assert_counts(result, 3)
        self.assertEqual(len(planner.joint_calls), 1)
        self.assertEqual([r["waypoint_index"] for r in result["waypoint_results"]], [0])
        self.assertTrue(all(row["left_ee_joint_state"] == [1.] for row in dispatched))

    def test_native_terminal_stops_selected_plan_without_gripper_or_hidden_steps(self):
        for success_at, reason in ((None, "native_step_limit"), (2, "native_success")):
            with self.subTest(success_at=success_at):
                adapter, _, planner, dispatched = replan_adapter(step_limit=2, success_at=success_at)
                result = self.execute(adapter, motion_action(gripper=True))
                self.assertEqual((result["status"], result["reason"]), ("stopped", "episode_ended"))
                self.assertEqual(result["episode_terminal"]["reason"], reason)
                self.assert_counts(result, 2)
                self.assertEqual(len(dispatched), 2)
                self.assertEqual(result["stop_confirmation"]["settling_steps"], 0)
                again = self.execute(adapter)
                self.assert_counts(again, 0)
                self.assertEqual(len(planner.joint_calls), 1)
                self.assertTrue(all(row["left_ee_joint_state"] == [1.] for row in dispatched))

    def test_candidate_exhausting_action_deadline_cannot_dispatch_fallback(self):
        clock = [time.monotonic()]
        adapter, _, planner, dispatched = replan_adapter(
            after_candidate=lambda: clock.__setitem__(0, clock[0] + 21.))
        with patch("auriga.robot.backends.robodojo.robot_worker.time.monotonic", side_effect=lambda: clock[0]):
            result = self.execute(adapter, motion_action(timeout_s=20))
        self.assertEqual(len(planner.joint_calls), 1)
        self.assertEqual((result["status"], result["reason"]), ("failed", "planning_timeout"))
        self.assert_counts(result, 0)
        self.assertEqual(dispatched, [])
        self.assertEqual(result["stop_confirmation"]["settling_steps"], 0)

    def test_short_remaining_deadline_skips_candidate_and_executes_original(self):
        adapter, _, planner, dispatched = replan_adapter(step_limit=500)
        result = self.execute(adapter, motion_action(timeout_s=10))
        self.assertEqual((result["status"], result["reason"]), ("completed", None))
        self.assertEqual(planner.joint_calls, [])
        expected = adapter._resample_plan(planner.originals[0], "left")
        self.assert_counts(result, len(expected))
        np.testing.assert_allclose([r["left_arm_joint_state"] for r in dispatched], expected)

    def test_cancellation_or_native_terminal_after_planning_prevents_dispatch(self):
        for stage in ("original", "candidate"):
            for terminal in (False, True):
                with self.subTest(stage=stage, terminal=terminal):
                    cancellation = threading.Event()
                    adapter, _, planner, dispatched = replan_adapter()

                    def interrupt():
                        if terminal:
                            adapter.env.end_flag[0] = True
                        else:
                            cancellation.set()

                    if stage == "original":
                        original_plan = planner.plan_path

                        def stop_after_original(*args):
                            result = original_plan(*args)
                            interrupt()
                            return result

                        planner.plan_path = stop_after_original
                    else:
                        planner.after_candidate = interrupt
                    result = adapter.execute(motion_action(), lambda event: None, cancellation)
                    self.assertEqual((result["status"], result["reason"]),
                                     ("stopped", "episode_ended" if terminal else "cancelled"))
                    self.assertEqual(len(planner.joint_calls), 0 if stage == "original" else 1)
                    if stage == "original":
                        self.assertEqual(adapter.geometry_calls, [])
                    self.assert_counts(result, 0)
                    self.assertEqual(dispatched, [])

    def test_replan_diagnostics_are_private_and_do_not_enter_actor_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter, _, _, _ = replan_adapter()
            adapter.directory = Path(directory)
            adapter.observation_profile = "rgb_proprio_v1"
            state = adapter.state

            def state_with_velocity():
                value = state()
                value["arms"]["left"]["qdot_rad_s"] = [0.] * 6
                return value

            adapter.state = state_with_velocity
            progress = []
            result = adapter.execute(motion_action(), progress.append, threading.Event())
            self.assertEqual(result["status"], "completed")
            diagnostic_path, = (Path(directory) / "development-diagnostics").glob("action-*.json")
            private = json.loads(diagnostic_path.read_text())
            diagnostic, = [d for d in private["planning_diagnostics"] if d["stage"] == "joint_replan"]
            self.assertTrue(diagnostic["selected"])
            self.assertIn("receipt", private)
            self.assertIn("qdot_rad_s", private["receipt"]["before_state"]["arms"]["left"])
            self.assertNotIn("qdot_rad_s", result["before_state"]["arms"]["left"])
            published = json.dumps({"receipt": result, "progress": progress, "observation": adapter.observe()})
            self.assertNotIn("joint_replan", published)
            self.assertNotIn("turn_shifts", published)

    def test_calibrated_profiles_archive_selected_fallback_and_no_improvement_privately(self):
        cases = [({}, True, True, "validated_shorter_native_plan", 3),
                 ({"candidate": "fail"}, True, False, "native_replan_failed", 124),
                 ({"wound": False}, False, False, "no_strictly_closer_legal_endpoint", 3)]
        for profile in ("rgb_proprio_calibrated_v1", "rgbd_proprio_calibrated_v1"):
            for options, attempted, selected, reason, count in cases:
                with self.subTest(profile=profile, options=options), tempfile.TemporaryDirectory() as directory:
                    adapter, _, planner, dispatched = replan_adapter(step_limit=500, **options)
                    adapter.directory = Path(directory)
                    adapter.observation_profile = profile
                    action = motion_action()
                    action["action_id"] = "calibrated-replan"
                    progress = []
                    result = adapter.execute(action, progress.append, threading.Event())
                    self.assertEqual((result["status"], result["reason"]), ("completed", None))
                    self.assert_counts(result, count)
                    self.assertEqual(len(dispatched), count)
                    self.assertEqual(len(planner.joint_calls), int(attempted))
                    diagnostic_path, = (Path(directory) / "development-diagnostics").glob("action-*.json")
                    private = json.loads(diagnostic_path.read_text())
                    self.assertEqual(private["audience"], "observer_only")
                    self.assertEqual(private["action_id"], action["action_id"])
                    self.assertEqual(private["observation_profile"], profile)
                    self.assertTrue(private["joint_replan_enabled"])
                    self.assertEqual(private["executed_native_steps"], count)
                    self.assertNotIn("receipt", private)
                    self.assertNotIn("terminal_observation", private)
                    diagnostic, = [d for d in private["planning_diagnostics"] if d["stage"] == "joint_replan"]
                    self.assertEqual((diagnostic["attempted"], diagnostic["selected"], diagnostic["reason"]),
                                     (attempted, selected, reason))
                    if attempted:
                        self.assertGreaterEqual(diagnostic["elapsed_wall_s"], 0.)
                        self.assertEqual(diagnostic["baseline"]["resampled_rows"], 124)
                    if selected:
                        self.assertEqual(diagnostic["candidate"]["resampled_rows"], count)
                    # Calibrated actor state keeps its pose, but planner internals stay private.
                    self.assertIn("ee_pose", result["before_state"]["arms"]["left"])
                    published = json.dumps({"receipt": result, "progress": progress,
                                            "observation": adapter.observe()})
                    for field in ("planning_diagnostics", "joint_replan", "turn_shifts", "equivalent_goal_q_rad"):
                        self.assertNotIn(field, published)

    def test_private_diagnostics_allow_fixtures_without_episode_directory(self):
        for profile in ("rgb_proprio_v1", "rgb_proprio_calibrated_v1", "rgbd_proprio_calibrated_v1"):
            for none_directory in (False, True):
                with self.subTest(profile=profile, none_directory=none_directory):
                    adapter, _, _, dispatched = replan_adapter()
                    adapter.observation_profile = profile
                    if none_directory:
                        adapter.directory = None
                    with patch("auriga.robot.backends.robodojo.robot_worker.sys.stderr", new=io.StringIO()) as stderr:
                        result = self.execute(adapter)
                    self.assertEqual(result["status"], "completed")
                    self.assert_counts(result, 3)
                    self.assertEqual(len(dispatched), 3)
                    self.assertEqual(stderr.getvalue(), "")

    def test_private_archive_failures_cannot_change_execution(self):
        baseline, _, _, baseline_rows = replan_adapter()
        expected = self.execute(baseline)
        for failure in ("path", "mkdir", "serialization", "write", "logging"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                adapter, _, planner, dispatched = replan_adapter()
                adapter.directory = object() if failure == "path" else Path(directory)
                if failure == "serialization":
                    plan_segment = adapter._plan_segment

                    def unserializable_diagnostic(robot, target, diagnostics, *args, **kwargs):
                        result = plan_segment(robot, target, diagnostics, *args, **kwargs)
                        diagnostics.append({"stage": "fixture", "private": object()})
                        return result

                    adapter._plan_segment = unserializable_diagnostic
                target = "pathlib.Path.mkdir" if failure == "mkdir" else "pathlib.Path.write_text"
                error = OSError("synthetic archive failure") if failure in ("mkdir", "write", "logging") else None
                with patch(target, side_effect=error), patch("auriga.robot.backends.robodojo.robot_worker.sys.stderr", new=io.StringIO()) as stderr:
                    if failure == "logging":
                        with patch.object(stderr, "write", side_effect=OSError("synthetic logging failure")):
                            result = self.execute(adapter)
                    else:
                        result = self.execute(adapter)
                    if failure != "logging":
                        self.assertIn("Private development diagnostic write failed:", stderr.getvalue())
                self.assertEqual((result["status"], result["reason"]),
                                 (expected["status"], expected["reason"]))
                self.assert_counts(result, 3)
                self.assertEqual(dispatched, baseline_rows)
                self.assertEqual(len(planner.joint_calls), 1)

    def test_native_exception_does_not_dispatch_original_after_possible_runtime_damage(self):
        adapter, _, planner, dispatched = replan_adapter(candidate="exception")
        with patch("auriga.robot.backends.robodojo.robot_worker.sys.stderr", new=io.StringIO()):
            result = self.execute(adapter)
        self.assertEqual(result["status"], "failed")
        self.assertIn("backend_error: RuntimeError", result["reason"])
        self.assertEqual(len(planner.joint_calls), 1)
        self.assert_counts(result, 0)
        self.assertEqual(dispatched, [])


if __name__ == "__main__":
    unittest.main()
