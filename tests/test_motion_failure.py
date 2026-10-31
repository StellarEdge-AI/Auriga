"""CPU regression of native failure capture and the public action receipt.

Native solver objects are deterministic fixtures. The adapter's hooks, candidate
selection, execution accounting, privacy boundary and Harness writer are real.
"""
from program_fixture import program_services

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import numpy as np

from auriga.robot.motion import arx_actions
from auriga.runtime.harness import Harness
from auriga.core.presentation.actor_payload import compact_action_result
from auriga.robot.motion.motion_failure import failure_feedback, solver_snapshot
from auriga.core.observation_policy import ACTOR_PROFILE, LEGACY_ACTOR_PROFILE, reject_privileged
from auriga.core.storage.store import Store
from auriga.core.tool_output import wire_response
from auriga.runtime.tools.actions import _action_response
from test_harness import FixtureRobot
from test_joint_replan_runtime import motion_action, periodic_pose, replan_adapter
from test_planner_witness import native_fk_fixture


def native_result(stage="ik", *, position=(.001, .004, .003), rotation=(.4, .02, .08),
                  success=(False, False, False), feasible=(False, True, False), offset=0.):
    """Each seed has distinct joint values; endpoint errors favor different seeds."""
    joints = np.arange(18, dtype=float).reshape(1, 3, 6) / 10 + offset
    if stage == "trajopt":
        joints = np.stack([joints - .1, joints], axis=2)
    return SimpleNamespace(success=np.array([success]), feasible=np.array([feasible]),
        position_error=np.array([position]), rotation_error=np.array([rotation]),
        position_tolerance=.005, orientation_tolerance=.1,
        js_solution=SimpleNamespace(joint_names=[f"joint{i}" for i in range(1, 7)], position=joints))


def full_dof_result(stage):
    """Native reordered JointState includes two locked prismatic jaws in meters."""
    result = native_result(stage)
    names = ["jaw2", "joint3", "joint1", "joint6", "jaw1", "joint2", "joint5", "joint4"]
    q = np.array([[[.021 if name == "jaw2" else .012 if name == "jaw1" else
                   seed * 10 + int(name[-1]) / 10 for name in names] for seed in range(3)]])
    if stage == "trajopt":
        start = q.copy()
        start[..., [i for i, name in enumerate(names) if name.startswith("joint")]] -= .05
        q = np.stack([start, q], axis=2)
    result.js_solution = SimpleNamespace(joint_names=names, position=q)
    return result


def with_native_derivatives(result):
    """Give every seed, sample and named joint a distinct native derivative."""
    shape = result.js_solution.position.shape
    for field, base in (("velocity", 100.), ("acceleration", 1000.), ("jerk", 10000.)):
        values = np.empty(shape)
        for seed in range(shape[1]):
            for sample in range(shape[2] if len(shape) == 4 else 1):
                vector = [base + seed * 100 + sample * 10 + int(name[-1])
                          if name.startswith("joint") else 999999. for name in result.js_solution.joint_names]
                if len(shape) == 4:
                    values[0, seed, sample] = vector
                else:
                    values[0, seed] = vector
        setattr(result.js_solution, field, values)
    return result


def captured_segment(results, *, final_status="IK_FAIL", graph=False, fail_native=False,
                     mutate_after_capture=False, active_joint_names=None):
    """Drive real adapter hooks around a native planner call, including retries."""
    adapter, robot, planner, dispatched = replan_adapter(enabled=False)
    class Solver:
        def solve_pose(self):
            result = next(calls)
            if isinstance(result, BaseException):
                raise result
            return result

    ik, trajopt = Solver(), Solver()
    motion = SimpleNamespace(ik_solver=ik, trajopt_solver=trajopt)
    if active_joint_names is not None:
        motion.joint_names = active_joint_names
    originals = {}

    def plan_pose():
        for stage, _ in results:
            result = getattr(motion, "ik_solver" if stage == "ik" else "trajopt_solver").solve_pose()
            if mutate_after_capture:
                result.position_error[:] = 99.
                result.js_solution.position[:] = 99.
        if graph:
            motion._get_graph_seed_trajectories()
        if fail_native:
            raise RuntimeError("native planner exception")
        return SimpleNamespace(status=final_status, success=np.array([False]))

    # Keep the stage labels outside Solver; iteration returns result objects.
    calls = iter(result for _, result in results)
    motion.plan_pose = plan_pose
    motion._get_graph_seed_trajectories = lambda: SimpleNamespace(status="GRAPH_FAIL", success=np.array([False]))
    planner.motion_planner = motion

    def plan_path(*args):
        motion.plan_pose()
        return {"status": "Fail"}

    planner.plan_path = plan_path
    for owner, name in ((ik, "solve_pose"), (trajopt, "solve_pose"),
                        (motion, "plan_pose"), (motion, "_get_graph_seed_trajectories")):
        originals[(id(owner), name)] = (owner, dict(vars(owner)))
    diagnostics = []
    return adapter, robot, planner, dispatched, diagnostics, originals


class SolverFailureCaptureTests(unittest.TestCase):
    def test_native_full_dof_jointstate_maps_reordered_arm_subset_and_excludes_locked_jaws(self):
        active = [f"joint{i}" for i in range(1, 7)]
        for stage in ("ik", "trajopt"):
            with self.subTest(stage=stage):
                result = full_dof_result(stage)
                snapshot = solver_snapshot(result, stage, active_joint_names=active)
                candidate = snapshot["candidate"]
                self.assertEqual(candidate["seed_index"], 2)
                self.assertEqual(candidate["joint_names"], active)
                endpoint = np.arange(1, 7) / 10 + 20.
                expected = endpoint[None, :] if stage == "ik" else np.stack([endpoint - .05, endpoint])
                np.testing.assert_allclose(candidate["q_rad"], expected)
                self.assertEqual(np.asarray(candidate["q_rad"]).shape[-1], 6)
                adapter, robot, _, _, diagnostics, _ = captured_segment(
                    [(stage, full_dof_result(stage))], active_joint_names=active)
                self.assertIsNone(adapter._plan_segment(robot, periodic_pose([-.1]), diagnostics, 0))
                captured = next(item["candidate"] for item in diagnostics if item["stage"] == stage)
                self.assertEqual(captured["joint_names"], active)
                np.testing.assert_allclose(captured["q_rad"], expected)
                feedback = adapter._failure_feedback(motion_action(), "left", 0, diagnostics)
                public, = feedback["candidates"]
                self.assertEqual(public["joint_names"], active)
                np.testing.assert_allclose(public["endpoint_q_rad"], endpoint)
                self.assertNotIn("jaw1", public["joint_names"])
                self.assertNotIn("jaw2", public["joint_names"])

    def test_missing_or_ambiguous_active_joint_mapping_cannot_invent_q(self):
        for stage in ("ik", "trajopt"):
            for active in (["joint1", "missing_joint"], ["joint1", "joint1"], []):
                with self.subTest(stage=stage, active=active):
                    snapshot = solver_snapshot(full_dof_result(stage), stage, active_joint_names=active)
                    self.assertNotIn("q_rad", snapshot.get("candidate", {}))
                    self.assertEqual(snapshot["candidate"]["position_error_m"], .003)
                    self.assertIn("joint_mapping_unavailable", snapshot["candidate"])
                    adapter, robot, _, _, diagnostics, _ = captured_segment(
                        [(stage, full_dof_result(stage))], active_joint_names=active)
                    self.assertIsNone(adapter._plan_segment(robot, periodic_pose([-.1]), diagnostics, 0))
                    feedback = adapter._failure_feedback(motion_action(), "left", 0, diagnostics)
                    candidate, = feedback["candidates"]
                    self.assertEqual(candidate["position_error_m"], .003)
                    self.assertEqual(candidate["orientation_error_rad"], .08)
                    self.assertIn("joint_mapping_unavailable", candidate)
                    self.assertNotIn("endpoint_q_rad", json.dumps(feedback))

    def test_hook_without_motion_joint_names_uses_known_arm_names_for_full_dof_subset(self):
        for stage in ("ik", "trajopt"):
            with self.subTest(stage=stage):
                adapter, robot, planner, _, diagnostics, _ = captured_segment([(stage, full_dof_result(stage))])
                self.assertFalse(hasattr(planner.motion_planner, "joint_names"))
                self.assertIsNone(adapter._plan_segment(robot, periodic_pose([-.1]), diagnostics, 0))
                candidate = next(item["candidate"] for item in diagnostics if item["stage"] == stage)
                self.assertEqual(candidate["joint_names"], robot.arm_joints_name)
                np.testing.assert_allclose(candidate["q_rad"][-1], np.arange(1, 7) / 10 + 20.)

    def test_native_derivatives_share_selected_seed_samples_and_active_joint_mapping(self):
        active = [f"joint{i}" for i in range(1, 7)]
        for stage in ("ik", "trajopt"):
            with self.subTest(stage=stage):
                result = with_native_derivatives(full_dof_result(stage))
                snapshot = solver_snapshot(result, stage, active_joint_names=active)
                candidate = snapshot["candidate"]
                for field, base in (("velocity", 100.), ("acceleration", 1000.), ("jerk", 10000.)):
                    expected = np.arange(1, 7) + base + 200.
                    expected = expected[None, :] if stage == "ik" else np.stack([expected, expected + 10.])
                    np.testing.assert_array_equal(candidate["joint_derivatives"][field], expected)
                    getattr(result.js_solution, field)[:] = -999.
                    np.testing.assert_array_equal(candidate["joint_derivatives"][field], expected)
                adapter, robot, _, _, diagnostics, _ = captured_segment(
                    [(stage, with_native_derivatives(full_dof_result(stage)))], active_joint_names=active)
                self.assertIsNone(adapter._plan_segment(robot, periodic_pose([-.1]), diagnostics, 0))
                captured = next(item["candidate"] for item in diagnostics if item["stage"] == stage)
                self.assertEqual(captured["joint_derivatives"], candidate["joint_derivatives"])

    def test_bad_native_derivatives_preserve_q_and_other_derivative_fields(self):
        active = [f"joint{i}" for i in range(1, 7)]
        for stage in ("ik", "trajopt"):
            for malformed in ("shape", "nonfinite", "nonnumeric"):
                with self.subTest(stage=stage, malformed=malformed):
                    result = with_native_derivatives(full_dof_result(stage))
                    if malformed == "shape":
                        result.js_solution.velocity = result.js_solution.velocity[..., :-1]
                    elif malformed == "nonfinite":
                        result.js_solution.velocity.flat[0] = np.nan
                    else:
                        result.js_solution.velocity = "not native numerical data"
                    snapshot = solver_snapshot(result, stage, active_joint_names=active)
                    candidate = snapshot["candidate"]
                    np.testing.assert_allclose(candidate["q_rad"][-1], np.arange(1, 7) / 10 + 20.)
                    self.assertEqual(candidate["derivatives_unavailable"], ["velocity"])
                    self.assertEqual(set(candidate["joint_derivatives"]), {"acceleration", "jerk"})
                    adapter, robot, _, _, diagnostics, _ = captured_segment([(stage, result)], active_joint_names=active)
                    self.assertIsNone(adapter._plan_segment(robot, periodic_pose([-.1]), diagnostics, 0))
                    captured = next(item for item in diagnostics if item["stage"] == stage)
                    self.assertNotIn("diagnostic_error", captured)
                    self.assertEqual(captured["candidate"], candidate)

    def test_native_derivatives_reach_witness_privately_without_public_raw_arrays(self):
        active = [f"joint{i}" for i in range(1, 7)]
        snapshot = solver_snapshot(with_native_derivatives(full_dof_result("trajopt")), "trajopt", active)
        diagnostic = dict(stage="trajopt", waypoint_index=0, success=[[False] * 3], **snapshot)
        calls = []

        def witness(*args):
            calls.append(deepcopy(args))
            return dict(available=True, candidate_only=True, sampled_only=True,
                        derivative_fields_checked=list(args[2]))

        feedback = failure_feedback(motion_action(), "left", 0, [diagnostic], "configured planner geometry", witness)
        expected = snapshot["candidate"]
        self.assertEqual(calls, [(expected["q_rad"], active, expected["joint_derivatives"])])
        candidate, = feedback["candidates"]
        self.assertEqual(candidate["constraint_witness"]["derivative_fields_checked"], ["velocity", "acceleration", "jerk"])
        self.assertNotIn("joint_derivatives", json.dumps(feedback))
        self.assertNotIn("q_rad", candidate)
        self.assertNotIn("velocity", candidate)
        self.assertNotIn("acceleration", candidate)
        self.assertNotIn("jerk", candidate)
        adapter, _, _, _ = replan_adapter(enabled=False)
        with patch("auriga.robot.motion.planner_witness.capture_candidate_witness", return_value=dict(available=True)) as capture:
            public = adapter._failure_feedback(motion_action(), "left", 0, [diagnostic])
        self.assertEqual(capture.call_args.kwargs, dict(joint_derivatives=expected["joint_derivatives"]))
        self.assertEqual(capture.call_args.args[1:], (expected["q_rad"], active))
        self.assertNotIn("joint_derivatives", json.dumps(public))

    def test_selection_normalization_identifies_native_thresholds_and_diagnostic_references(self):
        native = solver_snapshot(native_result(), "ik")["candidate"]
        self.assertEqual(native["selection_normalization"], dict(
            position_error_m=dict(value=.005, source="native_solver_tolerance"),
            orientation_error_rad=dict(value=.1, source="native_solver_tolerance")))
        for threshold in (None, 0., -.1, float("nan"), float("inf")):
            with self.subTest(threshold=threshold):
                result = native_result()
                result.position_tolerance = result.orientation_tolerance = threshold
                candidate = solver_snapshot(result, "ik")["candidate"]
                self.assertEqual(candidate["selection_normalization"], dict(
                    position_error_m=dict(value=.005, source="diagnostic_selection_reference_not_solver_tolerance"),
                    orientation_error_rad=dict(value=.1, source="diagnostic_selection_reference_not_solver_tolerance")))
                self.assertNotIn("position_tolerance_m", candidate)
                self.assertNotIn("orientation_tolerance_rad", candidate)
                self.assertEqual(candidate["seed_index"], native["seed_index"])
        result = native_result()
        del result.position_tolerance
        mixed = solver_snapshot(result, "ik")["candidate"]
        self.assertEqual(mixed["selection_normalization"]["position_error_m"]["source"],
                         "diagnostic_selection_reference_not_solver_tolerance")
        self.assertEqual(mixed["selection_normalization"]["orientation_error_rad"]["source"],
                         "native_solver_tolerance")
        self.assertEqual(mixed["orientation_tolerance_rad"], .1)
        self.assertNotIn("position_tolerance_m", mixed)
        result = native_result()
        result.position_tolerance, result.orientation_tolerance = .001, .5
        differently_scaled = solver_snapshot(result, "ik")["candidate"]
        self.assertEqual(differently_scaled["seed_index"], 0)
        self.assertEqual(differently_scaled["position_tolerance_m"], .001)
        self.assertEqual(differently_scaled["orientation_tolerance_rad"], .5)
        self.assertAlmostEqual(differently_scaled["selection_score"], 1.)

    def test_residuals_and_joints_are_one_selected_seed_and_copied(self):
        for stage in ("ik", "trajopt"):
            with self.subTest(stage=stage):
                result = native_result(stage)
                snapshot = solver_snapshot(result, stage)
                candidate = snapshot["candidate"]
                # Minima belong to seeds 0 and 1; the balanced candidate is seed 2.
                self.assertEqual(candidate["seed_index"], 2)
                self.assertEqual(snapshot["min_position_error_m"], .001)
                self.assertEqual(snapshot["min_orientation_error_rad"], .02)
                self.assertEqual(candidate["position_error_m"], .003)
                self.assertEqual(candidate["orientation_error_rad"], .08)
                self.assertAlmostEqual(candidate["selection_score"], .8)
                self.assertFalse(candidate["solver_feasible"])
                self.assertFalse(candidate["solver_success"])
                expected = result.js_solution.position[0, 2].copy()
                if stage == "ik":
                    expected = expected[None, :]
                np.testing.assert_array_equal(candidate["q_rad"], expected)
                result.js_solution.position[:] = 99.
                result.position_error[:] = 99.
                np.testing.assert_array_equal(candidate["q_rad"], expected)
                self.assertEqual(candidate["position_error_m"], .003)

    def test_hooks_capture_ik_retries_before_buffers_change_and_restore_methods(self):
        first = native_result(position=(.02, .03, .04), rotation=(.5, .6, .7))
        second = native_result(offset=2.)
        adapter, robot, planner, dispatched, diagnostics, originals = captured_segment(
            [("ik", first), ("ik", second)], mutate_after_capture=True)
        self.assertIsNone(adapter._plan_segment(robot, periodic_pose([-.1]), diagnostics, 2))
        ik = [item for item in diagnostics if item["stage"] == "ik"]
        self.assertEqual(len(ik), 2)
        self.assertEqual(ik[0]["candidate"]["position_error_m"], .02)
        self.assertEqual(ik[1]["candidate"]["position_error_m"], .003)
        feedback = adapter._failure_feedback(motion_action(angles=(.1, .2, .3)), "left", 2, diagnostics)
        self.assertEqual(feedback["failure_stage"], "inverse_kinematics")
        summary = next(item for item in feedback["solver_summary"] if item["stage"] == "ik")
        self.assertEqual((summary["calls"], summary["successful_seed_count"], summary["checked_seed_count"]), (2, 0, 6))
        self.assertEqual(feedback["candidates"][0]["stage_call_index"], 1)
        np.testing.assert_allclose(feedback["candidates"][0]["endpoint_q_rad"], np.arange(12, 18) / 10 + 2.)
        for owner, before in originals.values():
            self.assertEqual(vars(owner), before)
        self.assertEqual(dispatched, [])
        self.assertEqual(adapter.env.take_action_cnt[0], 0)

    def test_successful_ik_then_trajopt_and_graph_failure_is_path_planning(self):
        adapter, robot, _, _, diagnostics, _ = captured_segment([
            ("ik", native_result(success=(False, True, False))),
            ("trajopt", native_result("trajopt"))], final_status="TRAJOPT_FAIL", graph=True)
        self.assertIsNone(adapter._plan_segment(robot, periodic_pose([-.1]), diagnostics, 0))
        feedback = adapter._failure_feedback(motion_action(), "left", 0, diagnostics)
        self.assertEqual(feedback["failure_stage"], "path_planning")
        self.assertEqual({item["stage"] for item in feedback["solver_summary"]},
                         {"ik", "trajopt", "graph_seed", "plan_pose"})
        trajectory = next(item for item in feedback["candidates"] if item["stage"] == "trajopt")
        self.assertEqual(trajectory["trajectory_sample_count"], 2)
        self.assertTrue(trajectory["candidate_only"])
        self.assertEqual(trajectory["source_kind"], "solver_candidate_not_executed")
        self.assertNotIn("q_rad", trajectory)
        self.assertEqual(feedback["cause_status"], "undetermined")

    def test_native_exception_keeps_diagnostics_and_restores_hooks(self):
        adapter, robot, _, _, diagnostics, originals = captured_segment(
            [("ik", RuntimeError("native solver failed"))])
        with self.assertRaisesRegex(RuntimeError, "native solver failed"):
            adapter._plan_segment(robot, periodic_pose([-.1]), diagnostics, 0)
        self.assertEqual([item["stage"] for item in diagnostics], ["ik", "plan_pose"])
        self.assertTrue(all(item["exception_type"] == "RuntimeError" for item in diagnostics))
        for owner, before in originals.values():
            self.assertEqual(vars(owner), before)

    def test_misaligned_native_arrays_do_not_invent_seed_or_joint_pairing(self):
        for malformed in ("error_shape", "success_shape", "joint_shape", "duplicate_names", "negative_error"):
            with self.subTest(malformed=malformed):
                result = native_result()
                if malformed == "error_shape":
                    result.rotation_error = np.array([[.01, .02]])
                elif malformed == "success_shape":
                    result.success = result.success[0]
                elif malformed == "joint_shape":
                    result.js_solution.position = result.js_solution.position[:, :2]
                elif malformed == "duplicate_names":
                    result.js_solution.joint_names = ["same"] * 6
                else:
                    result.position_error[0, 0] = -.001
                snapshot = solver_snapshot(result, "ik")
                if malformed in ("joint_shape", "duplicate_names"):
                    self.assertEqual(snapshot["candidate"]["seed_index"], 2)
                    self.assertNotIn("q_rad", snapshot["candidate"])
                else:
                    self.assertNotIn("candidate", snapshot)

    def test_nonfinite_native_values_degrade_to_available_summary_without_replanning(self):
        for field in ("position_error", "rotation_error", "joint_position"):
            with self.subTest(field=field):
                result = native_result()
                values = result.js_solution.position if field == "joint_position" else getattr(result, field)
                values.flat[0] = np.nan
                adapter, robot, _, dispatched, diagnostics, _ = captured_segment([("ik", result)])
                self.assertIsNone(adapter._plan_segment(robot, periodic_pose([-.1]), diagnostics, 0))
                diagnostic = next(item for item in diagnostics if item["stage"] == "ik")
                self.assertEqual(diagnostic["diagnostic_error"], "ValueError")
                self.assertEqual(diagnostic["success"], [[False, False, False]])
                feedback = adapter._failure_feedback(motion_action(), "left", 0, diagnostics)
                self.assertEqual(feedback["failure_stage"], "inverse_kinematics")
                self.assertEqual(feedback["candidates"], [])
                self.assertIn("detail_unavailable_reason", feedback)
                json.dumps(feedback, allow_nan=False)
                self.assertEqual(dispatched, [])

    def test_witness_receives_selected_attempt_and_seed_only_for_failed_waypoint(self):
        other_waypoint = dict(stage="ik", waypoint_index=0, success=[[False] * 3],
                              **solver_snapshot(native_result(position=(0., 0., 0.), rotation=(0., 0., 0.)), "ik"))
        rejected_retry = dict(stage="ik", waypoint_index=1, success=[[False] * 3],
            **solver_snapshot(native_result(position=(.1, .2, .3), rotation=(.5, .6, .7)), "ik"))
        selected_retry = dict(stage="ik", waypoint_index=1, success=[[False] * 3],
                              **solver_snapshot(native_result(offset=5.), "ik"))
        calls = []

        def witness(q, names):
            calls.append((q, names))
            return dict(available=True, candidate_only=True, sampled_only=True,
                        joint_limit_violations=[dict(joint_name=names[0], position_rad=q[-1][0])])

        action = motion_action(angles=(.1, .2, .3))
        feedback = failure_feedback(action, "left", 1, [other_waypoint, rejected_retry, selected_retry],
                                    "configured planner geometry", witness)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], (selected_retry["candidate"]["q_rad"], selected_retry["candidate"]["joint_names"]))
        candidate, = feedback["candidates"]
        self.assertEqual((candidate["stage_call_index"], candidate["seed_index"]), (1, 2))
        self.assertEqual(candidate["constraint_witness"]["joint_limit_violations"][0]["position_rad"],
                         candidate["endpoint_q_rad"][0])
        self.assertEqual(feedback["solver_summary"][0]["calls"], 2)
        self.assertEqual(feedback["cause_status"], "undetermined")

    def test_witness_exception_preserves_the_failed_candidate_without_executing_it(self):
        diagnostics = [dict(stage="ik", waypoint_index=0, success=[[False] * 3],
                            **solver_snapshot(native_result(), "ik"))]

        def unavailable(q, names):
            raise ValueError("private native geometry details")

        feedback = failure_feedback(motion_action(), "left", 0, diagnostics,
                                    "configured planner geometry", unavailable)
        candidate, = feedback["candidates"]
        self.assertEqual(candidate["constraint_witness"],
                         dict(available=False, reason="diagnostic_unavailable", error_type="ValueError"))
        self.assertTrue(candidate["candidate_only"])
        self.assertEqual(candidate["position_error_m"], .003)
        self.assertNotIn("private native geometry details", json.dumps(feedback))


class FailureReceiptTests(unittest.TestCase):
    def test_public_solver_statuses_keep_all_distinct_causes(self):
        statuses = ['solver_cause_' + str(i) for i in range(8)]
        diagnostics = [dict(stage='plan_pose', waypoint_index=0, status=status)
                       for status in statuses + statuses[:2]]
        feedback = failure_feedback(motion_action(), 'left', 0, diagnostics, 'robot model')
        self.assertEqual(feedback['solver_summary'][0]['native_statuses'], statuses)

    def assert_no_environment_truth(self, value):
        encoded = json.dumps(value, allow_nan=False)
        for forbidden in ("scene_model", "static_obstacle", "obstacle_id", "obstacle_surface_point_m",
                          "T_world_planner_base", "candidate_ee_pose_world", "target_minus_candidate_position_m",
                          "world_frame_id", "entity_origin_pose", "frame_bias", "unsupported_scene_geometry",
                          '"world_frame_transform_unavailable"', "private_optimizer_state", "private_debug", "never-public"):
            self.assertNotIn(forbidden, encoded)

    def test_feedback_does_not_read_simulator_origin_or_native_frame_bias(self):
        adapter, robot, planner, dispatched = replan_adapter(enabled=False)
        reads = []

        def unavailable_capabilities():
            reads.append("capabilities")
            raise AssertionError("failure scope must not inherit a simulator collision scope")

        adapter.capabilities = unavailable_capabilities

        class Guard:
            def __init__(self, source, forbidden):
                self.source, self.forbidden = source, forbidden

            def __getattr__(self, name):
                if name == self.forbidden:
                    reads.append(name)
                    raise AssertionError("simulator pose must not feed model diagnostics")
                return getattr(self.source, name)

        guarded_robot = Guard(robot, "entity_origin_pose")
        guarded_planner = Guard(planner, "frame_bias")
        adapter.robot.get_robot_by_arm_name = lambda name: guarded_robot
        adapter.robot.planner[robot.robot_name] = guarded_planner
        original = dict(available=True, candidate_only=True, sampled_only=True, frame_id="planner_base",
            collision_model_witnesses=[dict(kind="self_collision", penetration_m=.02)],
            joint_limit_violations=[dict(joint_name="joint1", margin_rad=-.01)],
            last_sample_tool_poses=[dict(tool_frame="link6", position_m=[1., 2., 3.],
                                        quaternion_wxyz=[1., 0., 0., 0.])])
        diagnostics = [dict(stage="ik", waypoint_index=0, success=[[False] * 3],
                            **solver_snapshot(native_result(), "ik"))]
        with patch("auriga.robot.motion.planner_witness.capture_candidate_witness", return_value=deepcopy(original)) as capture:
            feedback = adapter._failure_feedback(motion_action(), "left", 0, diagnostics)
        self.assertEqual(reads, [])
        candidate, = feedback["candidates"]
        self.assertEqual(candidate["constraint_witness"], original)
        self.assertEqual(candidate["position_error_m"], .003)
        self.assertEqual(candidate["orientation_error_rad"], .08)
        self.assertEqual(capture.call_args.args[0], guarded_planner)
        self.assertEqual(capture.call_args.args[1:],
                         (diagnostics[0]["candidate"]["q_rad"], diagnostics[0]["candidate"]["joint_names"]))
        self.assert_no_environment_truth(feedback)
        self.assertEqual(dispatched, [])

    def test_feedback_native_self_geometry_and_residuals_need_no_scene_access(self):
        adapter, robot, _, dispatched = replan_adapter(enabled=False)
        names = robot.arm_joints_name
        diagnostics = [dict(stage="ik", waypoint_index=0, success=[[False] * 3],
                            **solver_snapshot(native_result(), "ik", names))]
        with native_fk_fixture(names) as (planner, calls):
            adapter.robot.planner[robot.robot_name] = planner
            feedback = adapter._failure_feedback(motion_action(), "left", 0, diagnostics)
        self.assertEqual(calls["scene_reads"], 0)
        self.assertEqual(len(calls["fk"]), 1)
        candidate, = feedback["candidates"]
        self.assertEqual(candidate["position_error_m"], .003)
        self.assertEqual(candidate["orientation_error_rad"], .08)
        self.assertEqual(candidate["endpoint_q_rad"], diagnostics[0]["candidate"]["q_rad"][-1])
        witness = candidate["constraint_witness"]
        self.assertTrue(witness["available"])
        self.assertNotIn("collision_diagnostic_error", witness)
        self.assertEqual(witness["collision_model_witnesses"][0]["kind"], "self_collision")
        self.assertTrue(witness["joint_limit_violations"])
        self.assertEqual(witness["frame_id"], "planner_base")
        self.assertEqual(witness["last_sample_tool_poses"][0]["position_m"], [1., 2., 3.])
        self.assertEqual(witness["environment_geometry_unavailable_reason"],
                         "no_real_sensor_or_calibrated_environment_source")
        self.assertEqual(witness["world_frame_transform_unavailable_reason"], "no_real_calibration_source")
        self.assertEqual(feedback["information_sources"]["environment_collision"],
                         "unavailable_without_real_sensor_or_calibrated_source")
        self.assertEqual(feedback["information_sources"]["world_transform"],
                         "unavailable_without_real_calibration")
        self.assertEqual(feedback["cause_status"], "undetermined")
        self.assert_no_environment_truth(feedback)
        self.assertEqual(dispatched, [])

    def test_real_execute_failed_second_waypoint_retains_first_execution_evidence(self):
        adapter, _, planner, dispatched = replan_adapter(enabled=False, wound=False, fail_pose_at=2)
        action = motion_action(angles=(-.125, -.25, -.5), gripper=True)
        progress = []
        receipt = adapter.execute(action, progress.append, threading.Event())
        self.assertEqual((receipt["status"], receipt["reason"], receipt["failed_waypoint_index"]),
                         ("failed", "path_infeasible", 1))
        self.assertEqual(len(planner.pose_calls), 2)
        self.assertEqual(receipt["executed_steps"], len(dispatched))
        self.assertGreater(receipt["executed_steps"], 0)
        self.assertEqual(receipt["executed_control_steps"], len(dispatched) * 10)
        self.assertEqual([item["waypoint_index"] for item in receipt["waypoint_results"]], [0])
        self.assertEqual(receipt["planned_trace"]["waypoint_index"], 0)
        self.assertTrue(receipt["actual_trace"])
        self.assertTrue(receipt["terminal_observation"])
        feedback = receipt["failure_feedback"]
        self.assertEqual((feedback["waypoint_index"], feedback["waypoint_role"]), (1, "intermediate"))
        self.assertEqual(feedback["requested_ee_pose"], action["path"]["waypoints"][1]["left"])
        self.assertIn("final target was not tested", feedback["suggested_adjustments"][0])
        self.assertTrue(all(row["left_ee_joint_state"] == [1.] for row in dispatched))

    def test_all_dispatch_profiles_and_program_replies_exclude_environment_truth(self):
        class FailureRobot(FixtureRobot):
            def execute(self, action, **kwargs):
                receipt = super().execute(action, **kwargs)
                diagnostics = [dict(stage="ik", waypoint_index=0, success=[[False] * 3],
                    **solver_snapshot(with_native_derivatives(native_result()), "ik")),
                    dict(stage="plan_pose", waypoint_index=0, status="IK_FAIL", success=[False],
                         private_optimizer_state={"secret": "never-public"},
                         scene_model={"cuboid": {"never-public": {"dims": [1., 2., 3.]}}},
                         T_world_planner_base=np.eye(4).tolist(),
                         candidate_ee_pose_world={"position_m": [90., 80., 70.]})]
                diagnostics[0]["candidate"].update(
                    planner_scene_model={"cuboid": {"never-public": {"dims": [1., 2., 3.]}}},
                    candidate_ee_pose_world={"position_m": [90., 80., 70.]},
                    private_debug={"secret": "never-public"})
                # The model-facing result uses real failure capture and FK adapters.
                # The native scene is deliberately unreadable even though robot
                # geometry, limit evidence and solver residuals remain available.
                adapter, native_robot, _, _ = replan_adapter(enabled=False)
                with native_fk_fixture(native_robot.arm_joints_name) as (planner, calls):
                    adapter.robot.planner[native_robot.robot_name] = planner
                    public = adapter._failure_feedback(action, "left", 0, diagnostics)
                self.native_calls = calls
                self.raw_failure_feedback = deepcopy(public)
                receipt.update(reason="path_infeasible", failed_waypoint_index=0, failure_feedback=public)
                return receipt

        for profile in ("full_v1", "lean_v1", "compact_v1"):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as directory:
                store = Store(Path(directory) / "store", dict(run_id="failure", episode_id="ep", env_instance_id="env", env_generation=1))
                self.addCleanup(store.close)
                robot = FailureRobot(directory, outcome="failed")
                harness = Harness(store, robot, actor_payload_profile=profile)
                observation = harness.start()
                args = dict(observation_id=observation["observation_id"], arm="left", action_id="failed",
                            grasp_position_m=[.2, -.1, 1.08], angles_deg=[0, 0, 0], timeout_s=10)
                action = arx_actions.compile_action("grasp", args, harness.identity, harness.capabilities)
                def dispatch(correlation):
                    return wire_response(_action_response(harness, harness.execute(action, correlation), correlation))
                result = dispatch(dict(tool_call_id="direct"))
                public = json.loads(result["contentItems"][0]["text"])["receipt"]["failure_feedback"]
                persisted = store.read("action_receipt", "failed")["failure_feedback"]
                self.assert_no_environment_truth(robot.raw_failure_feedback)
                expected = (compact_action_result(dict(receipt=dict(failure_feedback=persisted)))["receipt"]["failure_feedback"]
                            if profile == "compact_v1" else persisted)
                self.assertEqual(public, expected)
                self.assertEqual(public["requested_grasp_pose"]["grasp_position_m"], [.2, -.1, 1.08])
                self.assertAlmostEqual(public["requested_ee_pose"]["position_m"][2], 1.2301)
                self.assertEqual(public["candidates"][0]["position_error_m"], .003)
                self.assertEqual(public["candidates"][0]["orientation_error_rad"], .08)
                witness = persisted["candidates"][0]["constraint_witness"]
                self.assertTrue(witness["available"])
                self.assertEqual(witness["collision_model_witnesses"][0]["kind"], "self_collision")
                self.assertTrue(witness["joint_limit_violations"])
                self.assertTrue(witness["dynamic_joint_limit_violations"])
                self.assertEqual(witness["environment_geometry_unavailable_reason"],
                                 "no_real_sensor_or_calibrated_environment_source")
                self.assertEqual(witness["world_frame_transform_unavailable_reason"], "no_real_calibration_source")
                self.assertEqual(public["information_sources"]["solver_candidates"],
                                 "motion_planner_internal_result_not_measurement")
                self.assertEqual(public["information_sources"]["self_collision"], "configured_robot_model_fk")
                self.assertEqual(public["information_sources"]["environment_collision"],
                                 "unavailable_without_real_sensor_or_calibrated_source")
                self.assertEqual(robot.native_calls["scene_reads"], 0)
                self.assertEqual(len(robot.native_calls["fk"]), 1)
                from auriga.runtime.programs.runner import ProgramRunner
                harness.programs = ProgramRunner(program_services(harness))
                harness.programs.entries['program'] = dict(cancel=threading.Event(), deadline=time.monotonic() + 60)
                program = dispatch(dict(tool_call_id="program", program_id="program"))
                program_value = json.loads(program["contentItems"][0]["text"])
                self.assertEqual(program_value["receipt"], store.read("action_receipt", "failed"))
                self.assertEqual(program_value["request"], store.read("action_request", "failed"))
                self.assertEqual(robot.executions, 1)
                for reply in (result, program):
                    text = json.dumps(reply)
                    self.assertNotIn("planning_diagnostics", text)
                    self.assertNotIn("joint_derivatives", text)
                    self.assertNotIn("private_optimizer_state", text)
                    self.assertNotIn("never-public", text)
                    self.assert_no_environment_truth(reply)
                reopened = Store(Path(directory) / "store", store.identity)
                try:
                    self.assertEqual(reopened.read("action_receipt", "failed")["failure_feedback"], persisted)
                finally:
                    reopened.close()
                self.assertNotIn("joint_derivatives", json.dumps(store.read("action_receipt", "failed")))
                self.assert_no_environment_truth(persisted)

    def test_public_feedback_is_allowed_but_raw_diagnostics_fail_closed(self):
        action = motion_action()
        diagnostics = [dict(stage="ik", waypoint_index=0, success=[[False] * 3],
                            **solver_snapshot(native_result(), "ik"))]
        public = failure_feedback(action, "left", 0, diagnostics, "configured planner geometry")
        for profile in (ACTOR_PROFILE, LEGACY_ACTOR_PROFILE):
            with self.subTest(profile=profile):
                reject_privileged(dict(failure_feedback=public), profile)
                with self.assertRaises(ValueError):
                    reject_privileged(dict(extra=[dict(planning_diagnostics=diagnostics)]), profile)


if __name__ == "__main__":
    unittest.main()
