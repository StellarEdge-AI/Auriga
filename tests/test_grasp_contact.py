"""Selected finger contacts retain a calibrated, orientation-aware tool frame.

These CPU checks establish coordinate and diagnostic behavior, not retention,
contact safety, or the benefit of any contact region on a physical object.
"""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from auriga.robot.model import roboprobe_pose
from auriga.robot.motion.arx_actions import compile_action
from auriga.robot.model.grasp_contact import contact_target_feedback, resolve_contact_spec, values_to_contact_pose
from auriga.runtime.harness import Harness
from auriga.robot.motion.motion_failure import failure_feedback
from auriga.core.storage.store import Store
from test_harness import FixtureRobot


def values(position=(.2, -.1, .9), angles=(0., 0., 0.)):
    return dict(zip((*roboprobe_pose.POSITION_AXES, *roboprobe_pose.ANGLE_AXES),
                    (*position, *angles)))


def pose_args(position=(.2, -.1, .9), angles=(0., 0., 0.), **selection):
    return dict(grasp_position_m=list(position), angles_deg=list(angles), **selection)


def xyzw_pose(pose):
    return measured_pose(dict(position_m=pose[:3].tolist(), quaternion_xyzw=[*pose[4:].tolist(), float(pose[3])]))


def measured_pose(pose):
    return dict(deepcopy(pose), source_kind="measurement", tcp_config_id="arx-x5-link6-v1", frame_id="env_origin")


class ContactGeometryTests(unittest.TestCase):
    def test_default_is_exact_legacy_conversion_even_when_rotated(self):
        for angles in ((0, 0, 0), (0, 90, 0), (48, -34, 129), (-180, 0, 180)):
            with self.subTest(angles=angles):
                target = values(angles=angles)
                original = deepcopy(target)
                spec = resolve_contact_spec({"unrelated_action_option": 7})
                self.assertEqual(spec, dict(contact_region="pad_center", contact_depth_m=0., offset_m=.1501))
                np.testing.assert_array_equal(values_to_contact_pose(target, spec),
                                              roboprobe_pose.values_to_pose(target))
                self.assertEqual(target, original)

    def test_contact_reference_reaches_requested_point_in_arbitrary_orientation(self):
        target = np.array([.2, -.1, .9])
        for angles in ((0, 0, 0), (0, 90, 0), (48, -34, 129)):
            for selection in ({"contact_region": "finger_middle"}, {"contact_region": "fingertip"},
                              {"contact_depth_m": .04}):
                with self.subTest(angles=angles, selection=selection):
                    spec = resolve_contact_spec(selection)
                    converted = values_to_contact_pose(values(target, angles), spec)
                    axis = roboprobe_pose.tool_axis(converted[3:])
                    physical_contact = converted[:3] + spec["offset_m"] * axis
                    np.testing.assert_allclose(physical_contact, target, atol=1e-12)
                    legacy_center = np.array(list(roboprobe_pose.pose_to_values(converted).values())[:3])
                    np.testing.assert_allclose(legacy_center, target + spec["contact_depth_m"] * axis,
                                               atol=1e-12)

    def test_middle_insertion_moves_flange_down_for_top_down_and_forward_for_side_grasp(self):
        middle = resolve_contact_spec({"contact_region": "finger_middle"})
        self.assertEqual(middle["contact_depth_m"], .0275)
        self.assertAlmostEqual(middle["offset_m"], .1226)
        np.testing.assert_allclose(values_to_contact_pose(values(), middle)[:3], [.2, -.1, 1.0226], atol=1e-12)
        np.testing.assert_allclose(values_to_contact_pose(values(angles=(0, 90, 0)), middle)[:3],
                                   [.3226, -.1, .9], atol=1e-12)
        tip = resolve_contact_spec({"contact_region": "fingertip"})
        self.assertEqual(tip["contact_depth_m"], -.0075)
        np.testing.assert_allclose(values_to_contact_pose(values(), tip)[:3], [.2, -.1, 1.0576], atol=1e-12)

    def test_numeric_selection_and_inherited_selection_are_independent_copies(self):
        parent = resolve_contact_spec({"contact_region": "finger_middle"})
        original = deepcopy(parent)
        inherited = resolve_contact_spec({"grasp_position_m": [0, 0, 1]}, inherited=parent)
        self.assertEqual(inherited, parent)
        inherited["contact_depth_m"] = 0
        self.assertEqual(parent, original)
        custom = resolve_contact_spec({"contact_depth_m": .01}, inherited=parent)
        self.assertEqual(custom["contact_region"], "custom")
        self.assertAlmostEqual(custom["offset_m"], .1401)
        self.assertEqual(resolve_contact_spec({"contact_region": "pad_center"}, inherited=parent),
                         resolve_contact_spec({}))

    def test_invalid_selection_cannot_be_silently_ignored(self):
        invalid = [{"contact_region": value} for value in (None, "middle", "", True, [], {})]
        invalid += [{"contact_depth_m": value} for value in
                    (None, True, False, "0.02", float("nan"), float("inf"), -.0075001, .0625001)]
        invalid += [{"contact_region": "pad_center", "contact_depth_m": 0.}]
        for selection in invalid:
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                resolve_contact_spec(selection)
        for boundary in (-.0075, .0625):
            self.assertEqual(resolve_contact_spec({"contact_depth_m": boundary})["contact_depth_m"], boundary)


class ContactHarnessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name) / "store", dict(run_id="contact", episode_id="ep",
            env_instance_id="env", env_generation=1))
        self.addCleanup(self.store.close)
        self.robot = FixtureRobot(temporary.name)
        caps = dict(self.robot.start(), max_waypoints=8, dual_arm=True)
        self.robot.start = lambda: caps
        self.harness = Harness(self.store, self.robot, motion_review=False)
        self.harness.start()

    def single(self, **updates):
        return dict(observation_id=self.harness.latest["observation_id"], arm="left", action_id="contact",
                    **pose_args(), **updates)

    def build(self, args):
        return compile_action("grasp", args, self.harness.identity, self.harness.capabilities)

    def test_omitted_selection_preserves_legacy_action_metadata_and_coordinates(self):
        action = self.build(self.single(via_grasp_poses=[pose_args(position=(.1, -.1, 1.))]))
        meta = action["input_grasp_pose"]
        self.assertEqual(set(meta), {"position_m", "angles_deg", "convention", "offset_m", "via_grasp_poses"})
        self.assertEqual(meta["offset_m"], .1501)
        np.testing.assert_allclose(action["path"]["waypoints"][0]["left"]["position_m"],
                                   [.1, -.1, 1.1501], atol=1e-12)
        np.testing.assert_allclose(action["path"]["waypoints"][1]["left"]["position_m"],
                                   [.2, -.1, 1.0501], atol=1e-12)

    def test_request_contact_inherits_to_vias_and_explicit_via_override_wins(self):
        args = self.single(contact_region="finger_middle", via_grasp_poses=[
            pose_args(position=(.1, -.1, 1.)),
            pose_args(position=(.15, -.1, .95), angles=(0, 90, 0), contact_region="pad_center")])
        original = deepcopy(args)
        action = self.build(args)
        np.testing.assert_allclose([p["left"]["position_m"] for p in action["path"]["waypoints"]],
                                   [[.1, -.1, 1.1226], [.3001, -.1, .95], [.2, -.1, 1.0226]], atol=1e-12)
        meta = action["input_grasp_pose"]
        self.assertEqual([s["contact_region"] for s in meta["waypoint_contact_frames"]],
                         ["finger_middle", "pad_center", "finger_middle"])
        self.assertEqual(args, original)

    def test_via_only_contact_override_does_not_change_default_final_reference(self):
        action = self.build(self.single(via_grasp_poses=[pose_args(contact_depth_m=.04)]))
        meta = action["input_grasp_pose"]
        self.assertEqual(meta["contact_region"], "pad_center")
        self.assertEqual(meta["contact_depth_m"], 0.)
        np.testing.assert_allclose([p["left"]["position_m"] for p in action["path"]["waypoints"]],
                                   [[.2, -.1, 1.0101], [.2, -.1, 1.0501]], atol=1e-12)

    def test_invalid_final_or_via_selection_never_compiles(self):
        for selector in ({"contact_depth_m": True}, {"contact_region": "middle"},
                         {"contact_depth_m": .063}, {"contact_region": "pad_center", "contact_depth_m": 0.}):
            for args in (self.single(**selector), self.single(via_grasp_poses=[pose_args(**selector)])):
                with self.subTest(args=args, selector=selector), self.assertRaises(ValueError):
                    self.build(args)
        self.assertEqual(self.robot.executions, 0)
        self.assertEqual(self.harness.action_count, 0)

    def test_failed_intermediate_reports_its_contact_not_final_contact(self):
        args = self.single(contact_region="finger_middle", via_grasp_poses=[
            pose_args(position=(.1, -.1, 1.), contact_depth_m=.01)])
        action = self.build(args)
        feedback = failure_feedback(action, "left", 0, [], "configured_robot_model")
        self.assertEqual(feedback["requested_grasp_pose"], args["via_grasp_poses"][0])
        self.assertAlmostEqual(feedback["grasp_to_ee_offset_m"], .1401)
        self.assertEqual(feedback["requested_ee_pose"], action["path"]["waypoints"][0]["left"])
        final = failure_feedback(action, "left", 1, [], "configured_robot_model")
        self.assertAlmostEqual(final["grasp_to_ee_offset_m"], .1226)
        self.assertEqual(final["requested_grasp_pose"]["contact_region"], "finger_middle")

    def test_feedback_uses_actual_orientation_and_selected_reference_from_measured_ee(self):
        action = self.build(self.single(contact_region="finger_middle"))
        requested = np.array([.2, -.1, .9])
        actual = requested + np.array([.01, 0, .03])
        measured = values_to_contact_pose(values(actual, (15, 0, 0)),
                                           resolve_contact_spec({"contact_region": "finger_middle"}))
        state = dict(arms=dict(left=dict(ee_pose=xyzw_pose(measured),
            grasp_pose=dict(position_m=[888, 888, 888], angles_deg=[0, 0, 0]))))
        original = deepcopy(state)
        feedback = contact_target_feedback(action, state)
        self.assertEqual(feedback["target_scope"], "requested_final_pose_not_completion_claim")
        arm = feedback["arms"]["left"]
        self.assertTrue(arm["available"])
        self.assertEqual(arm["contact_region"], "finger_middle")
        np.testing.assert_allclose(arm["requested_contact_position_m"], requested, atol=1e-12)
        np.testing.assert_allclose(arm["actual_contact_position_m"], actual, atol=1e-12)
        np.testing.assert_allclose(arm["position_error_vector_m"], [.01, 0, .03], atol=1e-12)
        self.assertAlmostEqual(arm["position_error_m"], np.linalg.norm([.01, 0, .03]))
        self.assertAlmostEqual(arm["orientation_error_rad"], np.deg2rad(15))
        self.assertAlmostEqual(arm["approach_shortfall_m"], .03)
        self.assertEqual(state, original)

    def test_feedback_shortfall_sign_distinguishes_stop_short_from_overshoot(self):
        action = self.build(self.single(contact_depth_m=.04))
        spec = resolve_contact_spec({"contact_depth_m": .04})
        for actual_z, expected_shortfall in ((.905, .005), (.895, -.005)):
            with self.subTest(actual_z=actual_z):
                pose = values_to_contact_pose(values(position=(.2, -.1, actual_z)), spec)
                state = dict(arms=dict(left=dict(ee_pose=xyzw_pose(pose))))
                arm = contact_target_feedback(action, state)["arms"]["left"]
                self.assertAlmostEqual(arm["approach_shortfall_m"], expected_shortfall)

    def test_partial_failure_feedback_still_labels_comparison_to_unreached_final_target(self):
        args = self.single(contact_region="finger_middle", via_grasp_poses=[
            pose_args(position=(.1, -.1, 1.), contact_region="pad_center")])
        action = self.build(args)
        state = dict(arms=dict(left=dict(ee_pose=measured_pose(action["path"]["waypoints"][0]["left"]))))
        feedback = contact_target_feedback(action, state)
        self.assertEqual(feedback["target_scope"], "requested_final_pose_not_completion_claim")
        arm = feedback["arms"]["left"]
        np.testing.assert_allclose(arm["requested_contact_position_m"], args["grasp_position_m"], atol=1e-12)
        # The observed flange is at the via endpoint; evaluating the FINAL chosen
        # reference there differs from both the via's pad center and final goal.
        np.testing.assert_allclose(arm["actual_contact_position_m"], [.1, -.1, 1.0275], atol=1e-12)
        self.assertGreater(arm["position_error_m"], .1)

    def test_predicted_wrong_tcp_or_wrong_frame_pose_cannot_be_reported_as_measured_contact(self):
        action = self.build(self.single(contact_region="finger_middle"))
        pose = measured_pose(action["path"]["waypoints"][-1]["left"])
        for change in (dict(source_kind="planner_prediction"), dict(tcp_config_id="another_tool"),
                       dict(frame_id="base_link")):
            with self.subTest(change=change):
                state = dict(arms=dict(left=dict(ee_pose=dict(pose, **change))))
                result = contact_target_feedback(action, state)["arms"]["left"]
                self.assertFalse(result["available"])
                self.assertNotIn("actual_contact_position_m", result)

    def test_failed_action_archives_selected_contact_feedback_without_changing_outcome(self):
        self.robot.outcome = "failed"
        original_observe = self.robot.observe

        def observe():
            observation = original_observe()
            observation["robot_state"]["arms"]["left"]["ee_pose"] = measured_pose(
                observation["robot_state"]["arms"]["left"]["ee_pose"])
            return observation

        self.robot.observe = observe
        receipt = self.harness.execute(self.build(self.single(contact_region="finger_middle")),
                                       {"tool_call_id": "failed-contact"})["receipt"]
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["reason"], "synthetic_fixture")
        feedback = receipt["grasp_contact_feedback"]
        self.assertTrue(feedback["arms"]["left"]["available"])
        self.assertEqual(feedback["target_scope"], "requested_final_pose_not_completion_claim")
        self.assertEqual(self.store.read("action_receipt", "contact")["grasp_contact_feedback"], feedback)
        self.assertEqual(self.robot.executions, 1)

    def test_diagnostic_exception_does_not_turn_executed_action_into_unknown_outcome(self):
        with patch("auriga.runtime.harness.contact_target_feedback", side_effect=ValueError("diagnostic fixture")):
            receipt = self.harness.execute(self.build(self.single(contact_region="finger_middle")),
                                           {"tool_call_id": "diagnostic-failure"})["receipt"]
        self.assertEqual(receipt["status"], "completed")
        self.assertFalse(receipt["grasp_contact_feedback"]["available"])
        self.assertEqual(receipt["grasp_contact_feedback"]["reason"], "diagnostic_unavailable")
        self.assertTrue(self.harness.accepting)
        self.assertEqual(self.robot.executions, 1)


if __name__ == "__main__":
    unittest.main()
