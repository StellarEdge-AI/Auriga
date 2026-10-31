import math
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import numpy as np

from auriga.robot.motion.joint_replan import nearest_equivalent, select_joint_replan
from auriga.robot.backends.robodojo.robot_worker import RoboDojoAdapter


class JointReplanTests(unittest.TestCase):
    def setUp(self):
        self.current = np.zeros(6)
        self.goal = np.array([math.tau + .2, 0, 0, 0, 0, 0])
        self.original = np.linspace(self.current, self.goal, 20)
        self.limits = np.tile([-10., 10.], (6, 1))
        self.limits[5] = [-3.14, 3.14]
        self.diagnostic = {}
        self.planner = Mock(side_effect=lambda start, goal: {"status": "Success", "position": np.linspace(start, goal, 20)})

    @staticmethod
    def transforms(q):
        matrix = np.eye(4)
        matrix[:2, :2] = [[math.cos(q[0]), -math.sin(q[0])], [math.sin(q[0]), math.cos(q[0])]]
        return {"base": np.eye(4), "tool": matrix}

    @staticmethod
    def resample(q):
        count = max(1, int(np.ceil(np.abs(np.diff(q, axis=0)).sum(axis=0) / .05).max()))
        return np.stack([np.interp(np.linspace(0, 1, count + 1)[1:], np.linspace(0, 1, len(q)), q[:, i]) for i in range(6)], axis=1)

    def select(self, **overrides):
        arguments = dict(original=self.original, current=self.current, limits=self.limits,
                         joint_types=["revolute"] * 6, transforms=self.transforms, pose_error=lambda q: (0., 0.),
                         resample=self.resample, plan_joint=self.planner, deadline=30., diagnostic=self.diagnostic,
                         clock=lambda: 0.)
        arguments.update(overrides)
        return select_joint_replan(**arguments)

    def test_selects_once_without_changing_original(self):
        before = self.original.copy()
        result = self.select()
        self.planner.assert_called_once()
        np.testing.assert_allclose(self.planner.call_args.args[1], [.2, 0, 0, 0, 0, 0], atol=1e-12)
        np.testing.assert_array_equal(self.original, before)
        self.assertTrue(self.diagnostic["selected"])
        self.assertLess(len(self.resample(result)), len(self.resample(self.original)))
        self.assertLess(self.diagnostic["candidate"]["total_joint_travel_rad"], self.diagnostic["baseline"]["total_joint_travel_rad"])

    def test_joint_six_illegal_shortcut_stays_bounded(self):
        start, goal = np.zeros(6), np.zeros(6)
        start[5], goal[5] = -1., 2.8
        equivalent, turns = nearest_equivalent(start, goal, self.limits, ["revolute"] * 6)
        np.testing.assert_array_equal(equivalent, goal)
        self.assertEqual(turns, [0] * 6)

    def test_principal_branch_unwinding_prevents_second_revolution_ratchet(self):
        start = np.array([2.353, 0., 0., 0., 0., 0.])
        wound_goal = np.array([-0.795 + math.tau, 0., 0., 0., 0., 0.])
        equivalent, turns = nearest_equivalent(start, wound_goal, self.limits, ["revolute"] * 6)
        np.testing.assert_allclose(equivalent[0], -0.795, atol=1e-9)
        self.assertEqual(turns, [-1, 0, 0, 0, 0, 0])
        original = np.linspace(start, wound_goal, 20)
        result = self.select(original=original, current=start)
        self.assertTrue(self.diagnostic["selected"])
        self.assertTrue(self.diagnostic["unwinds_principal"])
        np.testing.assert_allclose(result[-1, 0], -0.795, atol=1e-9)

    def test_admission_and_no_winding(self):
        self.assertIs(self.select(deadline=14.99), self.original)
        self.assertEqual(self.diagnostic["reason"], "insufficient_action_deadline")
        self.planner.assert_not_called()
        no_winding = np.linspace(self.current, [.2, 0, 0, 0, 0, 0], 3)
        self.assertIs(self.select(original=no_winding), no_winding)
        self.planner.assert_not_called()
        self.assertEqual(self.diagnostic["reason"], "no_strictly_closer_legal_endpoint")

    def test_native_failure_falls_back_but_exceptions_propagate(self):
        self.planner.side_effect = None
        self.planner.return_value = {"status": "Fail"}
        self.assertIs(self.select(), self.original)
        self.assertEqual(self.diagnostic["reason"], "native_replan_failed")
        for error in (RuntimeError("CUDA illegal memory access"), ValueError("native invalid state")):
            self.planner.side_effect = error
            with self.assertRaises(type(error)):
                self.select()
            self.assertEqual(self.diagnostic["reason"], "native_exception")

    def test_invalid_candidate_rows_start_endpoint_bounds_and_pose_fall_back(self):
        good = np.linspace(self.current, [.2, 0, 0, 0, 0, 0], 20)
        candidates = [np.zeros(6), np.full((2, 6), np.nan), good + .1, self.original]
        outside = good.copy()
        outside[3, 2] = 11.
        candidates.append(outside)
        self.planner.side_effect = None
        for candidate in candidates:
            with self.subTest(shape=candidate.shape):
                self.planner.return_value = {"status": "Success", "position": candidate}
                self.assertIs(self.select(), self.original)
                self.assertEqual(self.diagnostic["reason"], "candidate_validation_failed")
        self.planner.return_value = {"status": "Success", "position": good}
        count = 0
        def pose_error(q):
            nonlocal count
            count += 1
            return (0.006 if count == 3 else 0., 0.)
        self.assertIs(self.select(pose_error=pose_error), self.original)

    def test_fk_mismatch_and_nonrevolute_skip(self):
        def nonperiodic(q):
            matrix = np.eye(4)
            matrix[0, 3] = q[0]
            return {"tool": matrix}
        self.assertIs(self.select(transforms=nonperiodic), self.original)
        self.assertIs(self.select(joint_types=["prismatic"] + ["revolute"] * 5), self.original)
        self.planner.assert_not_called()

    def test_shorter_endpoint_with_bad_path_is_rejected(self):
        # Native may find a winding path even to a nearby endpoint.
        path = np.vstack([self.current, [9., 0, 0, 0, 0, 0], [.2, 0, 0, 0, 0, 0]])
        self.planner.side_effect = None
        self.planner.return_value = {"status": "Success", "position": path}
        self.assertIs(self.select(), self.original)
        self.assertEqual(self.diagnostic["reason"], "no_strict_path_improvement")

    def test_deadline_expiration_retains_original_for_worker_interruption(self):
        now = [0.]
        def plan(start, goal):
            now[0] = 31.
            return {"status": "Success", "position": np.linspace(start, goal, 3)}
        self.assertIs(self.select(plan_joint=plan, clock=lambda: now[0]), self.original)
        self.assertEqual(self.diagnostic["reason"], "action_deadline_exceeded")

    def test_production_geometry_world_pose_and_native_mapping_guards(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "Assets/Robots/x5/X5A.urdf"
            path.parent.mkdir(parents=True)
            names = [f"joint{i}" for i in range(1, 7)]
            links = ["base_link"] + [f"link{i}" for i in range(1, 7)]
            elements = ['<robot name="test">'] + [f'<link name="{link}"/>' for link in links]
            for index, name in enumerate(names):
                elements.append(f'<joint name="{name}" type="revolute"><parent link="{links[index]}"/>'
                                f'<child link="{links[index+1]}"/><origin xyz=".1 0 0"/><axis xyz="0 0 1"/></joint>')
            path.write_text("".join(elements) + "</robot>")
            adapter = RoboDojoAdapter.__new__(RoboDojoAdapter)
            adapter.np, adapter.root = np, root
            robot = SimpleNamespace(arm_joints_name=names,
                entity_origin_pose=[.3, -.2, .7, math.sqrt(.5), 0., 0., math.sqrt(.5)])
            config = dict(urdf_path=str(path), base_link="base_link")
            planner = SimpleNamespace(active_joints_name=names.copy(), cspace_joint_names=names.copy(),
                tool_frames=["link6"], frame_bias=[0., 0., 0.], robot_cfg={"robot_cfg": {"kinematics": config}})
            angle = .3
            target = dict(position_m=[.3 - .5 * math.sin(angle), -.1 + .5 * math.cos(angle), .7],
                quaternion_xyzw=[0., 0., math.sin((math.pi / 2 + angle) / 2), math.cos((math.pi / 2 + angle) / 2)])
            kinds, transforms, error = adapter._joint_replan_geometry(robot, planner, target)
            self.assertEqual(kinds, ["revolute"] * 6)
            q = np.array([angle, 0., 0., 0., 0., 0.])
            np.testing.assert_allclose(error(q), [0., 0.], atol=3e-8)
            wrapped = q.copy()
            wrapped[0] += math.tau
            for name, matrix in transforms(q).items():
                np.testing.assert_allclose(matrix, transforms(wrapped)[name], atol=1e-12)
            for field, invalid in (("active_joints_name", list(reversed(names))),
                    ("cspace_joint_names", list(reversed(names))), ("tool_frames", ["link5"]),
                    ("frame_bias", [.001, 0., 0.])):
                original = getattr(planner, field)
                setattr(planner, field, invalid)
                with self.subTest(field=field), self.assertRaises(ValueError):
                    adapter._joint_replan_geometry(robot, planner, target)
                setattr(planner, field, original)
            for field, invalid in (("base_link", "wrong_base"), ("urdf_path", str(root / "other.urdf")),
                                    ("extra_links", {"unexpected": {}})):
                original = config.get(field)
                config[field] = invalid
                with self.subTest(field=field), self.assertRaises(ValueError):
                    adapter._joint_replan_geometry(robot, planner, target)
                if original is None:
                    del config[field]
                else:
                    config[field] = original


if __name__ == "__main__":
    unittest.main()
