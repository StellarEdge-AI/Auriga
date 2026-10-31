"""Native terminal conditions stop a prefix without hidden settling commands."""

from types import SimpleNamespace
import threading
import unittest

import numpy as np

from auriga.robot.backends.robodojo.robodojo import validate_joint_prefix
from auriga.robot.backends.robodojo.robot_worker import RoboDojoAdapter, host_articulation_properties


class NativeRuntimeCheck(unittest.TestCase):
    def test_articulation_numpy_metadata_reads_copy_to_host_and_restore(self):
        class DeviceArray:
            def __init__(self, value): self.value = np.asarray(value)
            def __array__(self, *args, **kwargs): raise TypeError("CUDA tensor requires cpu()")
            def detach(self): return self
            def cpu(self): return SimpleNamespace(numpy=lambda: self.value)

        for device_values in (False, True):
            for fail in (False, True):
                with self.subTest(device_values=device_values, fail=fail):
                    wrap = DeviceArray if device_values else np.asarray
                    gains = (wrap([[4.]]), wrap([[5.]]))
                    view = SimpleNamespace(**{name: (lambda: wrap([[3.]])) for name in (
                        "get_dof_types", "get_dof_limits", "get_drive_types", "get_max_efforts", "get_joint_max_velocities")},
                        get_gains=lambda: gains)
                    class NativeObject:
                        _articulation_view = view
                        @property
                        def dof_properties(self):
                            result = np.zeros(1, dtype=[("effort", float), ("stiffness", float), ("damping", float)])
                            result["effort"] = np.asarray(self._articulation_view.get_max_efforts())[0]
                            stiffness, damping = self._articulation_view.get_gains()
                            result["stiffness"], result["damping"] = np.asarray(stiffness)[0], np.asarray(damping)[0]
                            if fail: raise RuntimeError("native getter failed")
                            return result
                    class SceneObject(NativeObject): pass
                    original = dict(vars(view))
                    if device_values:
                        with self.assertRaisesRegex(TypeError, "CUDA"):
                            SceneObject().dof_properties
                    with host_articulation_properties(SceneObject):
                        if fail:
                            with self.assertRaisesRegex(RuntimeError, "native getter failed"):
                                SceneObject().dof_properties
                        else:
                            self.assertEqual(SceneObject().dof_properties.tolist(), [(3., 4., 5.)])
                        self.assertEqual(vars(view), original)
                    self.assertNotIn("dof_properties", vars(SceneObject))
                    self.assertEqual(vars(view), original)

    def adapter(self, *, step_limit=3, success_at=None):
        adapter = object.__new__(RoboDojoAdapter)
        adapter.np, adapter.control_hz = np, 25
        adapter.control_step = adapter.evaluator_actions = 0
        adapter.versions = {}
        adapter.observation_profile = "rgb_proprio_calibrated_v1"
        adapter.goals = adapter.last_dispatched_goals = {"left": 1., "right": 1.}
        adapter.robot = SimpleNamespace(robot_list=[], robot_key=[])
        adapter.env = SimpleNamespace(end_flag=[False], success=[True], take_action_cnt=[0], step_lim=step_limit)
        adapter.state = lambda: {"arms": {side: {"q_rad": [0.] * 6} for side in ("left", "right")}}
        adapter.observe = lambda: {"robot_state": adapter.state()}
        def take_action(action):
            self.assertFalse(adapter.env.end_flag[0], "no native call may follow terminal")
            adapter.env.take_action_cnt[0] += 1
            adapter.control_step += 10
            count = adapter.env.take_action_cnt[0]
            if count == success_at or count == step_limit:
                adapter.env.end_flag[0] = True
                adapter.env.success[0] = count == success_at
        adapter.env.take_action = take_action
        return adapter

    def test_native_terminal_mid_chunk_and_already_terminal(self):
        for success_at, count, reason in ((None, 3, "native_step_limit"), (2, 2, "native_success")):
            with self.subTest(success_at=success_at):
                adapter = self.adapter(success_at=success_at)
                action = dict(type="JointPrefixAction", actions=[[0.] * 14] * 50,
                              policy_provenance={"fixture": "terminal_semantics"})
                result = adapter.execute(action, lambda value: None, threading.Event())
                self.assertEqual(result["status"], "stopped")
                self.assertEqual(result["reason"], "episode_ended")
                self.assertEqual(result["executed_native_steps"], count)
                self.assertEqual(result["executed_control_steps"], 10 * count)
                self.assertEqual(result["episode_terminal"]["reason"], reason)
                self.assertEqual(result["stop_confirmation"]["settling_steps"], 0)
                again = adapter.execute(action, lambda value: None, threading.Event())
                self.assertEqual(again["executed_native_steps"], 0)
                self.assertEqual(again["executed_control_steps"], 0)

    def test_polyline_variation_preserves_detour_and_final_target(self):
        adapter = self.adapter()
        adapter.arm_step_limits = {"left": np.array([.05])}
        result = adapter._resample_plan(np.array([[0.], [.1], [0.]]), "left")
        np.testing.assert_allclose(result[:, 0], [.05, .1, .05, 0.])

    def test_partial_native_fault_preserves_started_action_and_ticks(self):
        adapter = self.adapter()
        def fail_after_motion(action):
            adapter.env.take_action_cnt[0] += 1
            adapter.control_step += 4
            raise RuntimeError("injected native error after four physics ticks")
        adapter.env.take_action = fail_after_motion
        result = adapter.execute(dict(type="JointPrefixAction", actions=[[0.] * 14],
                                     policy_provenance={"fixture": "partial_fault"}), lambda value: None, threading.Event())
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["completed_native_steps"], 0)
        self.assertEqual(result["executed_native_steps"], 1)
        self.assertEqual(result["executed_control_steps"], 4)
        self.assertIn("native_action_failed", adapter.evaluator_failure)

    def test_exhausted_ik_retries_without_plan_status_are_classified(self):
        for results, expected in (([[[False]]] * 5, "inverse_kinematics"),
                                  ([[[False]], [[True]], [[False]]], "path_planning"),
                                  ([[[False]], None], "path_planning"),
                                  ([[]], "path_planning"), ([], "path_planning")):
            with self.subTest(results=results):
                adapter = self.adapter()
                adapter.robot.get_robot_by_arm_name = lambda arm: object()
                adapter.capabilities = lambda: {"collision_scope": "fixture"}
                def no_plan(robot, target, diagnostics, waypoint_index):
                    # A previous waypoint must not contaminate this failure stage.
                    diagnostics.append(dict(waypoint_index=-1, stage="plan_pose", status="IK_FAIL"))
                    diagnostics.extend(dict(waypoint_index=waypoint_index, stage="ik", success=value)
                                       for value in results)
                    diagnostics.append(dict(waypoint_index=waypoint_index, stage="plan_pose",
                                            status=None, returned_none=True))
                    return None
                adapter._plan_segment = no_plan
                action = dict(mode="absolute", frame_id="env_origin", tcp_config_id="arx-x5-link6-v1",
                              limits_profile="robodojo-native-v1", timeout_s=10, gripper_events=[],
                              path=dict(kind="backend_free", waypoints=[dict(left=dict(
                                  position_m=[-.3, 0, 1], quaternion_xyzw=[0, 0, 0, 1]))]))
                receipt = adapter.execute(action, lambda value: None, threading.Event())
                self.assertEqual(receipt["status"], "failed")
                self.assertEqual(receipt["reason"], "path_infeasible")
                self.assertEqual(receipt["planner_failure_stage"], expected)
                self.assertEqual(receipt["executed_native_steps"], 0)
                self.assertEqual(receipt["executed_control_steps"], 0)
                self.assertNotIn("planning_diagnostics", receipt)

    def test_prefix_boundary_rejects_base_output_and_empty_chunks(self):
        for rows in ([], [[0.] * 32], [[0.] * 14] * 51, [[float("nan")] * 14]):
            with self.assertRaises(ValueError):
                validate_joint_prefix(dict(type="JointPrefixAction", actions=rows, policy_provenance={"source": "test"}))


if __name__ == "__main__":
    unittest.main()
