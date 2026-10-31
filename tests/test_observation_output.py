"""Compact observations retain current camera geometry and distinguish budgets."""
from copy import deepcopy
import json
import unittest

from auriga.core.presentation.observation_output import observation_metadata, same_frame_robot_state, saved_observation_state
import test_dual_arm_interface as harness_fixture


class ObservationProjectionTests(unittest.TestCase):
    def test_only_reconstructive_matrices_are_omitted_and_full_refs_survive(self):
        matrix = [[1, 0, 0, .123456789], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]
        value = dict(bundle=dict(observation_id="old", action_budget=dict(native_steps_remaining=700),
            cameras=[dict(camera_id="wrist", calibration_ref="artifact:cal", pose_sample_ref="artifact:pose")]),
            camera_calibration=dict(cameras=dict(wrist=dict(K=matrix[:3], T_world_base=matrix,
                T_link_camera=matrix, distortion=[0, 0, 0, 0, 0], source_kind="calibration", future_warning="keep"))),
            camera_pose_sample=dict(cameras=dict(wrist=dict(T_world_camera=matrix, T_world_base=matrix,
                T_world_link=matrix, source_kind="robot_kinematics", frame_id="wrist-frame")), sim_step=30))
        original = deepcopy(value)
        result = observation_metadata(value, current_observation_id="now",
            current_action_budget=dict(native_steps_remaining=12), compact_geometry=True)
        self.assertEqual(value, original)
        self.assertFalse(result["observation_is_current"])
        self.assertEqual(result["bundle"]["action_budget"]["native_steps_remaining"], 700)
        self.assertEqual(result["current_action_budget"]["native_steps_remaining"], 12)
        camera = result["camera_pose_sample"]["cameras"]["wrist"]
        self.assertEqual(camera["T_world_camera"], matrix)
        self.assertEqual(camera["source_kind"], "robot_kinematics")
        self.assertNotIn("T_world_link", camera)
        cal = result["camera_calibration"]["cameras"]["wrist"]
        self.assertEqual(cal["K"], matrix[:3])
        self.assertEqual(cal["future_warning"], "keep")
        self.assertNotIn("T_link_camera", cal)
        self.assertEqual(result["camera_calibration"]["full_ref"], "artifact:cal")
        self.assertEqual(result["geometry_delivery"]["read_full"]["detail"], "full")
        full = observation_metadata(value, current_observation_id="old", current_action_budget={}, compact_geometry=False)
        self.assertTrue(full["observation_is_current"])
        self.assertEqual(full["camera_calibration"], original["camera_calibration"])
        self.assertEqual(full["camera_pose_sample"], original["camera_pose_sample"])

    def test_missing_current_camera_transform_does_not_remove_calibration(self):
        value = dict(bundle=dict(observation_id="obs"), camera_calibration=dict(cameras={
            "head": dict(T_world_camera=[[1]], T_link_camera=[[2]], unknown=True)}),
            camera_pose_sample=dict(cameras={"head": {"unavailable_reason": "no pose"}}))
        result = observation_metadata(value, current_observation_id=None, current_action_budget={}, compact_geometry=True)
        self.assertEqual(result["camera_calibration"], value["camera_calibration"])
        self.assertFalse(result["observation_is_current"])


class ObservationDeliveryTests(unittest.TestCase):
    setUp = harness_fixture.DualArmHarnessTests.setUp

    def test_historical_snapshot_and_current_budget_are_separate_in_actual_delivery(self):
        self.h.native_budget_known, self.h.native_budget_limit, self.h.native_action_count = True, 700, 0
        old = self.h.observe()
        self.h.native_action_count = 688
        latest = self.h.observe()
        count = self.robot.executions
        response = self.h.dispatch("history_view_observation", dict(observation_id=old["observation_id"], detail="summary"), {})
        value = json.loads(response["contentItems"][0]["text"])
        self.assertFalse(value["observation_is_current"])
        self.assertEqual(value["current_observation_id"], latest["observation_id"])
        self.assertEqual(value["current_action_budget"]["native_steps_remaining"], 12)
        self.assertEqual(value["bundle"]["action_budget"]["native_steps_remaining"], 700)
        self.assertEqual(self.robot.executions, count)
        stored = self.store.read("observation", old["observation_id"])
        self.assertNotIn("current_action_budget", stored)

    def test_full_and_program_paths_keep_original_geometry_and_image_count(self):
        observation = self.h.latest
        full = self.h.observation_content(observation, {}, detail="full")
        internal = self.h.observation_content(observation, {"program_id": "test"})
        for field in ("camera_calibration", "camera_pose_sample", "robot_state"):
            self.assertEqual(json.loads(full[0]["text"]).get(field), json.loads(internal[0]["text"]).get(field))
        self.assertEqual(sum(c["type"] == "inputImage" for c in full), sum(c["type"] == "inputImage" for c in internal))
        with self.assertRaisesRegex(ValueError, "invalid_output_detail"):
            self.h.observation_content(observation, {}, detail="invalid")

    def test_saved_frame_state_is_bound_to_capture_and_rejects_mismatched_timestamps(self):
        raw = self.robot.observe()
        raw['robot_state'].update(sim_step=raw['sim_step'], step_domain=raw['step_domain'])
        old = self.h.publish_observation(raw)
        current = self.h.observe()
        self.assertNotEqual(old['observation_id'], current['observation_id'])
        evidence = saved_observation_state(self.store, old['observation_id'])
        state = json.loads(self.store.resolve_artifact(old['robot_state_ref'], ('actor',))[1])
        self.assertEqual(evidence['status'], 'available', evidence)
        self.assertEqual(evidence['observation_id'], old['observation_id'])
        self.assertEqual(evidence['robot_state'], state)
        wrong = deepcopy(state)
        wrong['sim_step'] += 1
        rejected = same_frame_robot_state(old, wrong)
        self.assertEqual(rejected['status'], 'unavailable')
        self.assertNotIn('robot_state', rejected)
        self.assertEqual(saved_observation_state(self.store, 'missing')['status'], 'unavailable')


if __name__ == "__main__":
    unittest.main()
