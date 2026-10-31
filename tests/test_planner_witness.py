from contextlib import contextmanager, nullcontext
import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import numpy as np

from auriga.robot.motion.planner_witness import capture_candidate_witness, diagnose_candidate_geometry


@contextmanager
def native_fk_fixture(joint_names=("joint1", "joint2")):
    """Run the real native adapter on CPU without importing torch or cuRobo."""
    names = list(joint_names)
    calls = dict(scene_reads=0, fk=[], tensors=[])
    limits = SimpleNamespace(joint_names=names, position=np.array([[-1.] * len(names), [1.] * len(names)]),
        velocity=np.array([[-2.] * len(names), [2.] * len(names)]),
        acceleration=np.array([[-5.] * len(names), [5.] * len(names)]),
        jerk=np.array([[-10.] * len(names), [10.] * len(names)]))
    cfg = SimpleNamespace(kinematics_config=SimpleNamespace(
        link_sphere_idx_map=np.array([0, 1]), link_name_to_idx_map={"link1": 0, "link2": 1}),
        self_collision_config=SimpleNamespace(collision_pairs=np.array([[0, 1]]),
                                              sphere_padding=np.array([.01, .02])))

    def compute_kinematics(state):
        calls["fk"].append(state)
        samples = len(state.position)
        return SimpleNamespace(robot_spheres=np.tile([[[0., 0., .04, .1], [.15, 0., .04, .1]]], (samples, 1, 1)),
            tool_poses=SimpleNamespace(tool_frames=["link6"],
                position=np.tile([[[1., 2., 3.]]], (samples, 1, 1)),
                quaternion=np.tile([[[1., 0., 0., 0.]]], (samples, 1, 1))))

    motion = SimpleNamespace(joint_names=names, compute_kinematics=compute_kinematics,
        kinematics=SimpleNamespace(config=cfg, get_joint_limits=lambda: limits))

    class Planner:
        motion_planner = motion
        device_cfg = SimpleNamespace(device="cpu", dtype="float64")

        @property
        def scene_model(self):
            calls["scene_reads"] += 1
            raise AssertionError("native environment geometry is not deployment evidence")

    class JointState:
        @staticmethod
        def from_position(position, joint_names):
            return SimpleNamespace(position=position, joint_names=list(joint_names))

    def as_tensor(q, **options):
        calls["tensors"].append(options)
        return np.array(q, copy=True)

    torch = ModuleType("torch")
    torch.as_tensor, torch.no_grad = as_tensor, nullcontext
    curobo, types = ModuleType("curobo"), ModuleType("curobo.types")
    types.JointState = JointState
    curobo.types = types
    with patch.dict(sys.modules, {"torch": torch, "curobo": curobo, "curobo.types": types}):
        yield Planner(), calls


class PlannerWitnessTests(unittest.TestCase):
    def fixture(self, **overrides):
        values = dict(q_rad=[[0., 0.], [1.2, -.98]], joint_names=["joint1", "joint2"],
            joint_limits=[[-1., 1.], [-1., 1.]],
            spheres=[[[0., 0., 1., .1], [.5, 0., 1., .1]],
                     [[0., 0., .04, .1], [.15, 0., .04, .1]]],
            sphere_link_names=["link1", "link2"], self_collision_pairs=[[0, 1]],
            self_collision_padding=[.01, .02])
        values.update(overrides)
        return values

    def test_sample_self_collision_pair_and_joint_evidence(self):
        result = diagnose_candidate_geometry(**self.fixture())
        pair, = result["collision_model_witnesses"]
        self.assertEqual(pair["kind"], "self_collision")
        self.assertEqual(pair["trajectory_sample_index"], 1)
        self.assertEqual(pair["link_names"], ["link1", "link2"])
        self.assertAlmostEqual(pair["penetration_m"], .08)
        violation = result["joint_limit_violations"][0]
        self.assertEqual(violation["joint_name"], "joint1")
        self.assertEqual(violation["nearest_bound"], "upper")
        self.assertAlmostEqual(violation["margin_rad"], -.2)
        self.assertEqual(result["joints_near_limits"][0]["joint_name"], "joint2")
        self.assertTrue(result["candidate_only"])
        self.assertEqual(result["frame_id"], "planner_base")
        self.assertNotIn("_full_collision_model_witnesses", result)

    def test_ignored_pairs_and_disabled_spheres_have_no_witness(self):
        result = diagnose_candidate_geometry(**self.fixture(self_collision_pairs=np.empty((0, 2)),
            spheres=[[[0, 0, 1, -.1], [0, 0, 1, -.1]], [[0, 0, 0, -.1], [0, 0, 0, -.1]]]))
        self.assertEqual(result["collision_model_witnesses"], [])

    def test_bounds_per_kind_and_reports_omissions_without_mutating_inputs(self):
        args = self.fixture()
        args["q_rad"] = np.repeat(args["q_rad"], 4, axis=0)
        args["spheres"] = np.repeat(args["spheres"], 4, axis=0)
        original = args["spheres"].copy()
        result = diagnose_candidate_geometry(**args, max_witnesses=1)
        self.assertEqual(len(result["collision_model_witnesses"]), 1)
        complete = result["_full_collision_model_witnesses"]
        self.assertEqual([hit["trajectory_sample_index"] for hit in complete], [4, 5, 6, 7])
        self.assertEqual(complete[:1], result["collision_model_witnesses"])
        self.assertEqual(result["omitted_witness_count"], 3)
        np.testing.assert_array_equal(args["spheres"], original)

    def test_all_joint_limits_survive_a_small_collision_preview_limit(self):
        result = diagnose_candidate_geometry([[2., 3., 4., 5.]], ['a', 'b', 'c', 'd'],
            joint_limits=[[-1., 1.]] * 4, max_witnesses=1)
        self.assertEqual({item['joint_name'] for item in result['joint_limit_violations']},
                         {'a', 'b', 'c', 'd'})
        self.assertTrue(result['collision_geometry_unavailable'])

    def test_complete_witnesses_keep_all_pairs_and_summary_keeps_deepest(self):
        result = diagnose_candidate_geometry([[0.], [.1]], ["joint1"],
            spheres=[[[0, 0, 0, .2], [.1, 0, 0, .2], [.2, 0, 0, .2]],
                     [[0, 0, 0, .2], [.05, 0, 0, .2], [.1, 0, 0, .2]]],
            sphere_link_names=["a", "b", "c"], self_collision_pairs=[[0, 1], [0, 2], [1, 2]],
            self_collision_padding=[0., 0., 0.])
        complete = result["_full_collision_model_witnesses"]
        self.assertEqual(len(complete), 6)
        self.assertEqual(result["collision_model_witnesses"], complete[:3])
        self.assertEqual([hit["penetration_m"] for hit in complete],
                         sorted([hit["penetration_m"] for hit in complete], reverse=True))
        self.assertEqual(result["omitted_witness_count"], 3)

    def test_rejects_wrong_shapes_nonfinite_and_invalid_pair_mapping(self):
        for changed in (dict(q_rad=[float("nan"), 0.]), dict(sphere_link_names=["link1"]),
                        dict(self_collision_pairs=[[0, 9]]), dict(self_collision_pairs=[[0, .5]]),
                        dict(joint_limits=[[1., -1.], [-1., 1.]])):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                diagnose_candidate_geometry(**self.fixture(**changed))

    def test_unavailable_geometry_is_not_negative_collision_evidence(self):
        result = diagnose_candidate_geometry([0], ["joint1"], joint_limits=[[-1, 1]])
        self.assertTrue(result["collision_geometry_unavailable"])
        self.assertEqual(result["collision_model_witnesses"], [])

    def test_native_adapter_name_mismatch_is_fail_open_and_does_not_run_fk(self):
        class Motion:
            joint_names = ["joint1"]
            def compute_kinematics(self, state):
                raise AssertionError("must not compute FK")
        result = capture_candidate_witness(SimpleNamespace(motion_planner=Motion()), [0], ["other_joint"])
        self.assertFalse(result["available"])
        self.assertEqual(result["diagnostic_error"], "ValueError")

    def test_native_adapter_preserves_limits_when_fk_api_is_unavailable(self):
        limits = SimpleNamespace(joint_names=["joint1"], position=np.array([[-1.], [1.]]))
        kinematics = SimpleNamespace(config=SimpleNamespace(), get_joint_limits=lambda: limits)
        motion = SimpleNamespace(joint_names=["joint1"], kinematics=kinematics)
        result = capture_candidate_witness(SimpleNamespace(motion_planner=motion), [1.2], ["joint1"])
        self.assertTrue(result["available"])
        self.assertEqual(result["joint_limit_violations"][0]["joint_name"], "joint1")
        self.assertIn("collision_diagnostic_error", result)

    def test_native_fk_self_witness_and_limits_succeed_without_reading_scene(self):
        with native_fk_fixture() as (planner, calls):
            result = capture_candidate_witness(planner, [[0., 0.], [1.2, -.98]], ["joint1", "joint2"],
                joint_derivatives={"velocity": [[0., 0.], [3., 0.]]})
        self.assertEqual(calls["scene_reads"], 0)
        self.assertEqual(len(calls["fk"]), 1)
        self.assertEqual(calls["fk"][0].joint_names, ["joint1", "joint2"])
        self.assertEqual(calls["tensors"], [dict(device="cpu", dtype="float64")])
        self.assertTrue(result["available"])
        self.assertNotIn("collision_diagnostic_error", result)
        self.assertEqual({hit["kind"] for hit in result["collision_model_witnesses"]}, {"self_collision"})
        self.assertAlmostEqual(result["collision_model_witnesses"][0]["penetration_m"], .08)
        self.assertEqual(result["joint_limit_violations"][0]["joint_name"], "joint1")
        self.assertEqual(result["dynamic_joint_limit_violations"][0]["exceed"], 1.)
        self.assertEqual(result["frame_id"], "planner_base")
        self.assertEqual(result["last_sample_tool_poses"][0]["position_m"], [1., 2., 3.])

    def test_native_derivatives_identify_joint_sample_bounds_and_units(self):
        result = diagnose_candidate_geometry(**self.fixture(),
            joint_derivatives={"velocity": [[0, 0], [2., -3.]],
                               "acceleration": [[4., 0], [0, 0]],
                               "jerk": [[0, 0], [0, 15.]]},
            derivative_limits={"velocity": [[-1, 1], [-2, 2]],
                               "acceleration": [[-3, 3], [-3, 3]],
                               "jerk": [[-10, 10], [-10, 10]]})
        findings = result["dynamic_joint_limit_violations"]
        self.assertEqual(len(findings), 4)
        velocity = [item for item in findings if item["derivative"] == "velocity"]
        self.assertEqual(velocity[0]["trajectory_sample_index"], 1)
        self.assertEqual((velocity[0]["joint_name"], velocity[0]["units"], velocity[0]["exceed"]),
                         ("joint1", "rad/s", 1.))
        self.assertEqual(velocity[1]["violated_bound"], "lower")
        self.assertEqual(findings[-1]["units"], "rad/s^3")
        self.assertEqual(result["dynamic_diagnostic_unavailable"], {})

    def test_bad_derivative_does_not_discard_other_dynamics_or_geometry(self):
        result = diagnose_candidate_geometry(**self.fixture(),
            joint_derivatives={"velocity": [[float("nan"), 0], [0, 0]],
                               "acceleration": [[5, 0], [0, 0]], "jerk": [[0, 0]]},
            derivative_limits={kind: [[-1, 1], [-1, 1]] for kind in ("velocity", "acceleration", "jerk")})
        self.assertTrue(result["collision_model_witnesses"])
        self.assertTrue(result["joint_limit_violations"])
        self.assertEqual(result["dynamic_joint_limit_violations"][0]["derivative"], "acceleration")
        self.assertEqual(set(result["dynamic_diagnostic_unavailable"]), {"velocity", "jerk"})

    def test_all_joint_derivative_violations_and_explicit_missing_information(self):
        values = dict(q_rad=[[0]*5], joint_names=[f"joint{i}" for i in range(5)],
            joint_derivatives={kind: [[i+2 for i in range(5)]] for kind in ("velocity", "acceleration", "jerk")},
            derivative_limits={kind: [[-1, 1]]*5 for kind in ("velocity", "acceleration", "jerk")},
            max_witnesses=2)
        result = diagnose_candidate_geometry(**values)
        self.assertEqual(len(result["dynamic_joint_limit_violations"]), 15)
        self.assertEqual(result["omitted_dynamic_witness_count"], 0)
        self.assertEqual(result["dynamic_joint_limit_violations"][0]["joint_name"], "joint4")
        values["derivative_limits"].pop("velocity")
        values["joint_derivatives"].pop("acceleration")
        result = diagnose_candidate_geometry(**values)
        self.assertEqual(result["dynamic_diagnostic_unavailable"], {
            "velocity": "native_derivative_limits_unavailable", "acceleration": "native_derivative_not_returned"})

    def test_adapter_preserves_dynamic_evidence_when_fk_fails(self):
        limits = SimpleNamespace(joint_names=["joint1"], position=np.array([[-1.], [1.]]),
            velocity=np.array([[-2.], [2.]]), acceleration=np.array([[-5.], [5.]]), jerk=np.array([[-10.], [10.]]))
        kinematics = SimpleNamespace(config=SimpleNamespace(), get_joint_limits=lambda: limits)
        motion = SimpleNamespace(joint_names=["joint1"], kinematics=kinematics)
        result = capture_candidate_witness(SimpleNamespace(motion_planner=motion), [[0.], [.1]], ["joint1"],
            joint_derivatives={"velocity": [[0.], [3.]]})
        finding, = result["dynamic_joint_limit_violations"]
        self.assertEqual((finding["trajectory_sample_index"], finding["exceed"]), (1, 1.))
        self.assertIn("collision_diagnostic_error", result)


if __name__ == "__main__":
    unittest.main()
