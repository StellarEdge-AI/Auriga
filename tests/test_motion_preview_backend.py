"""Preview planning and exact reuse through the real CPU native-loop fixture."""

from copy import deepcopy
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock, PropertyMock, patch

import numpy as np

from auriga.robot.backends.robodojo.robodojo import RobotBackend, validate_preview_action
from test_joint_replan_runtime import replan_adapter, motion_action


def preview_adapter(**kwargs):
    adapter, robot, planner, dispatched = replan_adapter(enabled=False, wound=False, **kwargs)
    robot.entity_origin_pose = [0., 0., 0., 1., 0., 0., 0.]
    robot.ee_link_name = "tool"
    adapter.root = Path("/nonexistent/preview-fixture")
    # Matches periodic_pose: joint1 turns a unit-radius tool at z=1.
    joints = []
    for index in range(1, 7):
        origin = np.eye(4)
        if index == 2:
            origin[:3, 3] = [1., 0., 1.]
        joints.append(dict(name=f"joint{index}", type="continuous" if index == 1 else "fixed",
            parent="base" if index == 1 else f"link{index-1}",
            child="tool" if index == 6 else f"link{index}",
            T_parent_joint=origin.tolist(), axis=[0., 0., 1.]))
    adapter.robot_model = {"base_link": "base", "joints": joints, "geometry_scope": "fixture"}
    native_state = adapter.state

    def state():
        value = native_state()
        for arm in value["arms"].values():
            arm.update(joint_names=list(robot.arm_joints_name), source_kind="measurement")
            arm["ee_pose"].update(source_kind="measurement", frame_id="env_origin",
                                  tcp_config_id="arx-x5-link6-v1")
        return value

    adapter.state = state
    planner.robot_cfg = {"robot_cfg": {"kinematics": {
        "collision_spheres": {"link1": [{"center": [0., 0., 0.], "radius": .03}]},
        "self_collision_ignore": {"base": ["link1"]}}}}
    planner.scene_model = {"cuboid": {"table": {"dims": [3., 3., .05]}}}
    return adapter, planner, dispatched


class MotionPreviewBackendTests(unittest.TestCase):
    def test_preview_has_no_physics_or_dispatch_and_exposes_both_arm_geometry(self):
        adapter, planner, dispatched = preview_adapter()
        before = deepcopy(adapter.state())
        result = adapter.preview_motion(motion_action(gripper=True))
        self.assertEqual(result["status"], "planned")
        self.assertEqual(adapter.state(), before)
        self.assertEqual((adapter.control_step, adapter.env.take_action_cnt[0]), (0, 0))
        self.assertEqual(dispatched, [])
        self.assertEqual(len(planner.pose_calls), 1)
        self.assertEqual(set(result["before_state"]["arms"]), {"left", "right"})
        self.assertEqual(set(result["T_world_bases"]), {"left", "right"})
        self.assertTrue(result["preview_geometry"]["available"])
        self.assertEqual(result["T_world_bases_source_kind"], "derived_from_public_proprioception_and_robot_model")
        self.assertNotIn("planner_scene_model", result)
        self.assertEqual(result["robot_collision_model"]["spheres"]["link1"][0]["radius"], .03)
        self.assertEqual(result["planned_native_steps"], len(result["native_rows"]))
        self.assertGreater(result["planned_native_steps"], len(result["planned_trace"]["q_rad"]))

    def test_preview_derives_bases_from_public_pose_without_simulator_sources(self):
        from auriga.robot.model.geometry import fk
        adapter, planner, dispatched = preview_adapter()
        robot = adapter.robot.robot_list[0]

        class PoisonBase:
            def __array__(self, *args, **kwargs):
                raise AssertionError("simulator base must not be converted")

        robot.entity_origin_pose = PoisonBase()
        original_state = adapter.state
        expected = {}

        def state():
            value = original_state()
            for index, (side, measured) in enumerate(value["arms"].items()):
                measured["q_rad"][0] = .25 + index * .1
                base = np.eye(4)
                base[:3, :3] = [[0., -1., 0.], [1., 0., 0.], [0., 0., 1.]]
                base[:3, 3] = [index + .2, -.3, .4]
                tool = fk({**adapter.robot_model, "T_world_base": base}, measured)["tool"]
                angle = np.pi / 2 + measured["q_rad"][0]
                measured["ee_pose"].update(position_m=tool[:3, 3].tolist(),
                    quaternion_xyzw=[0., 0., float(np.sin(angle / 2)), float(np.cos(angle / 2))])
                expected[side] = base
            return value

        adapter.state = state
        with patch.object(type(planner), "scene_model", create=True, new_callable=PropertyMock,
                          side_effect=AssertionError("simulator scene must not be read")) as scene:
            result = adapter.preview_motion(motion_action())
        scene.assert_not_called()
        self.assertTrue(result["preview_geometry"]["available"])
        self.assertNotIn("planner_scene_model", result)
        for side, base in expected.items():
            np.testing.assert_allclose(result["T_world_bases"][side], base, atol=1e-12)
        self.assertEqual(dispatched, [])

    def test_execute_uses_exact_cached_rows_once_without_replanning_or_resampling(self):
        for arm in ("left", "right"):
            with self.subTest(arm=arm):
                adapter, planner, dispatched = preview_adapter(arm=arm)
                action = motion_action(arm=arm, gripper=True)
                preview = adapter.preview_motion(action)
                expected = deepcopy(preview["native_rows"])
                # A caller cannot mutate the native cache through preview output.
                preview["planned_trace"]["q_rad"][0][0] = 999
                preview["native_rows"][0][0] = 999
                adapter._plan_segment = Mock(side_effect=AssertionError("must not replan"))
                adapter._resample_plan = Mock(side_effect=AssertionError("must not resample"))
                executable = dict(action, preview_plan_id=preview["plan_id"], action_id="execute-preview",
                                  based_on_observation_id="refreshed-observation", timeout_s=60)
                result = adapter.execute(executable, lambda event: None, threading.Event())
                self.assertEqual(result["status"], "completed")
                actual = [row["left_arm_joint_state"] + row["left_ee_joint_state"] +
                          row["right_arm_joint_state"] + row["right_ee_joint_state"] for row in dispatched]
                np.testing.assert_array_equal(actual, expected)
                self.assertEqual(result["preview_plan_id"], preview["plan_id"])
                self.assertEqual(result["executed_native_steps"], len(expected))
                rejected = adapter.execute(executable, lambda event: None, threading.Event())
                self.assertEqual(rejected["status"], "rejected")
                self.assertIn("unknown_expired_or_consumed", rejected["reason"])
                self.assertEqual(rejected["executed_native_steps"], 0)
                self.assertEqual(len(dispatched), len(expected))

    def test_state_or_command_change_rejects_with_zero_motion_and_consumes_reference(self):
        for mutation in (lambda a: setattr(a, "control_step", 1),
                         lambda a: a.env.take_action_cnt.__setitem__(0, 1),
                         lambda a: a.last_dispatched_goals.__setitem__("right", .9),
                         lambda a: a.env.end_flag.__setitem__(0, True)):
            adapter, _, dispatched = preview_adapter()
            action = motion_action()
            preview = adapter.preview_motion(action)
            executable = dict(action, preview_plan_id=preview["plan_id"])
            mutation(adapter)
            rejected = adapter.execute(executable, lambda event: None, threading.Event())
            self.assertEqual(rejected["status"], "rejected")
            self.assertEqual(rejected["executed_native_steps"], 0)
            self.assertIn("stale_state", rejected["reason"])
            repeated = adapter.execute(executable, lambda event: None, threading.Event())
            self.assertEqual(repeated["status"], "rejected")
            self.assertIn("unknown_expired_or_consumed", repeated["reason"])
            self.assertEqual(dispatched, [])

    def test_goal_or_gripper_change_cannot_reuse_cached_plan(self):
        for changed in (motion_action(angles=(-.2,)), motion_action(gripper=True)):
            adapter, _, dispatched = preview_adapter()
            preview = adapter.preview_motion(motion_action())
            rejected = adapter.execute(dict(changed, preview_plan_id=preview["plan_id"]), lambda event: None, threading.Event())
            self.assertEqual(rejected["status"], "rejected")
            self.assertIn("action_mismatch", rejected["reason"])
            self.assertEqual(dispatched, [])

    def test_failed_planning_has_no_executable_plan_and_no_movement(self):
        adapter, _, dispatched = preview_adapter(fail_pose_at=1)
        result = adapter.preview_motion(motion_action())
        self.assertEqual((result["status"], result["reason"], result["plan_id"]), ("failed", "path_infeasible", None))
        self.assertEqual(result["planner_failure_stage"], "path_planning")
        self.assertEqual(dispatched, [])
        self.assertFalse(getattr(adapter, "_motion_previews", {}))

    def test_preview_boundaries_and_bounded_cache(self):
        adapter, _, dispatched = preview_adapter()
        with self.assertRaisesRegex(ValueError, "one_backend_free_waypoint"):
            adapter.preview_motion(motion_action(angles=(-.1, -.2)))
        adapter.observation_profile = "rgb_proprio_v1"
        with self.assertRaisesRegex(ValueError, "requires_calibrated_profile"):
            adapter.preview_motion(motion_action())
        adapter.observation_profile = "rgb_proprio_calibrated_v1"
        first = adapter.preview_motion(motion_action())
        for _ in range(16):
            adapter.preview_motion(motion_action())
        self.assertEqual(len(adapter._motion_previews), 16)
        rejected = adapter.execute(dict(motion_action(), preview_plan_id=first["plan_id"]), lambda event: None, threading.Event())
        self.assertEqual(rejected["status"], "rejected")
        self.assertIn("unknown_expired_or_consumed", rejected["reason"])
        self.assertEqual(dispatched, [])

    def test_backend_routes_validated_read_only_rpc(self):
        backend = object.__new__(RobotBackend)
        backend.operation_lock = threading.Lock()
        backend._request = Mock(return_value={"status": "planned"})
        action = motion_action()
        self.assertEqual(backend.preview_motion(action), {"status": "planned"})
        backend._request.assert_called_once_with("preview_motion", action, timeout=90)
        invalid = motion_action(angles=(-.1, -.2))
        with self.assertRaises(ValueError):
            validate_preview_action(invalid)
        with self.assertRaises(ValueError):
            backend.preview_motion(invalid)
        self.assertEqual(backend._request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
