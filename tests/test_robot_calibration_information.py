"""CPU-only calibration reads from real Harness publication and Store artifacts."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from auriga.core.contracts import canonical
from auriga.runtime.harness import Harness
from auriga.core.observation_policy import ACTOR_PROFILE, LEGACY_ACTOR_PROFILE
from auriga.robot.model.robot_calibration_information import robot_calibration_information
from auriga.core.storage.store import Store
from test_harness import FixtureRobot


def translation(x=0., y=0., z=0.):
    return [[1., 0., 0., x], [0., 1., 0., y], [0., 0., 1., z], [0., 0., 0., 1.]]


class CalibratedRobot(FixtureRobot):
    def __init__(self, root):
        super().__init__(root)
        self.observations = 0

    def observe(self):
        self.observations += 1
        raw = super().observe()
        frames, calibration, poses = [], {}, {}
        for name, arm in (("head", None), ("left_wrist", "left"), ("right_wrist", "right")):
            frame_id = f"{name}-{self.observations}"
            frames.append(dict(camera_id=name, frame_id=frame_id, png_path=str(self.path), width=1, height=1))
            calibration[name] = dict(K=[[123.45678901234567, 0., .5], [0., 124.98765432109876, .5], [0., 0., 1.]],
                distortion=[.0123456789012345, 0., 0., 0., 0.], distortion_model="fixture_distortion",
                image_size=[1, 1], optical_axes="+x right,+y down,+z forward")
            poses[name] = dict(frame_id=frame_id, T_world_camera=translation(.12345678901234567, .2, .3),
                               source_kind="robot_kinematics" if arm else "simulator_camera_transform")
            if arm:
                calibration[name].update(arm=arm, link_name="link6", T_link_camera=translation(.0123456789012345),
                                         T_world_base=translation(99.))
                poses[name].update(arm=arm, link_name="link6", T_world_link=translation(.25),
                                   T_world_base=translation(-.345678901234567 if arm == "left" else .345678901234567))
            else:
                calibration[name]["T_world_camera"] = translation(88.)
        raw.update(observation_profile=ACTOR_PROFILE, cameras=frames,
            camera_calibration=dict(reference_frame_id="env_origin", cameras=calibration),
            camera_pose_sample=dict(reference_frame_id="env_origin", sim_step=self.executions,
                                    step_domain=raw["step_domain"], cameras=poses),
            robot_model=dict(base_link="base_link", joints=[]))
        self.last_raw = deepcopy(raw)
        return raw


class RobotCalibrationInformationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        identity = dict(run_id="calibration", episode_id="episode", env_instance_id="environment", env_generation=2)
        self.store = Store(self.root / "run", identity)
        self.addCleanup(self.store.close)
        self.robot = CalibratedRobot(self.root)
        self.harness = Harness(self.store, self.robot, observation_profile=ACTOR_PROFILE)
        self.harness.start()

    def replace_artifact(self, field, kind, update, *, scope="actor"):
        reference = self.harness.latest["cameras"][0][field]
        _, content = self.store.resolve_artifact(reference)
        value = json.loads(content)
        update(value)
        changed = self.store.put_artifact(canonical(value).encode(), kind, "application/json", scope)
        for frame in self.harness.latest["cameras"]:
            frame[field] = changed

    def test_exact_latest_camera_and_base_values_without_new_capture(self):
        before = self.store.db.total_changes
        calls = self.robot.observations
        result = robot_calibration_information(self.harness)
        self.assertTrue(result["available"], result)
        self.assertEqual(result["observation_id"], self.harness.latest["observation_id"])
        self.assertEqual(result["env_generation"], 2)
        self.assertEqual(result["sim_step"], 0)
        self.assertEqual(result["reference_frame_id"], "env_origin")
        self.assertEqual(result["units"]["transform_translation"], "m")
        raw = self.robot.last_raw
        for name, camera in result["cameras"].items():
            for key in ("K", "distortion", "image_size", "optical_axes"):
                self.assertEqual(camera[key], raw["camera_calibration"]["cameras"][name][key])
            self.assertEqual(camera["T_world_camera"], raw["camera_pose_sample"]["cameras"][name]["T_world_camera"])
            self.assertNotIn("T_world_base", camera)
        for arm in ("left", "right"):
            self.assertEqual(result["bases"][arm]["T_world_base"],
                             raw["camera_pose_sample"]["cameras"][arm + "_wrist"]["T_world_base"])
            self.assertEqual(result["cameras"][arm + "_wrist"]["T_link_camera"],
                             raw["camera_calibration"]["cameras"][arm + "_wrist"]["T_link_camera"])
            self.assertNotIn("K", result["bases"][arm])
        self.assertEqual(self.robot.observations, calls)
        self.assertEqual(self.robot.executions, 0)
        self.assertEqual(self.store.db.total_changes, before)
        # Independent results cannot mutate retained numeric evidence.
        result["cameras"]["left_wrist"]["K"][0][0] = -1
        self.assertEqual(robot_calibration_information(self.harness)["cameras"]["left_wrist"]["K"],
                         raw["camera_calibration"]["cameras"]["left_wrist"]["K"])

    def test_reads_newly_published_anchor_and_never_static_base_fallback(self):
        old_id = self.harness.latest["observation_id"]
        raw = self.robot.observe()
        raw["sim_step"] = 17
        raw["camera_pose_sample"]["sim_step"] = 17
        del raw["camera_pose_sample"]["cameras"]["left_wrist"]["T_world_base"]
        self.harness.publish_observation(raw)
        result = robot_calibration_information(self.harness)
        self.assertTrue(result["available"], result)
        self.assertNotEqual(result["observation_id"], old_id)
        self.assertEqual(result["sim_step"], 17)
        self.assertNotIn("left", result["bases"])
        self.assertIn("bases.left.T_world_base", result["unavailable_fields"])

    def test_profile_rejection_happens_before_any_artifact_read(self):
        for profile in (LEGACY_ACTOR_PROFILE, None, "unknown"):
            store = Mock()
            result = robot_calibration_information(SimpleNamespace(observation_profile=profile, store=store))
            self.assertFalse(result["available"])
            self.assertEqual(result["reason"], "calibration_not_permitted_by_observation_profile")
            store.resolve_artifact.assert_not_called()
        result = robot_calibration_information(SimpleNamespace(observation_profile=ACTOR_PROFILE))
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "latest_observation_unavailable")

    def test_wrong_generation_or_profile_cannot_relabel_old_observation(self):
        self.harness.latest["env_generation"] = 1
        self.assertEqual(robot_calibration_information(self.harness)["reason"], "calibration_observation_identity_mismatch")
        self.harness.latest["env_generation"] = 2
        self.harness.latest["observation_profile"] = LEGACY_ACTOR_PROFILE
        self.assertFalse(robot_calibration_information(self.harness)["available"])

    def test_pose_step_and_frame_must_match_published_camera(self):
        for mutation, expected in ((lambda value: value.update(sim_step=99), "calibration_pose_step_mismatch"),
                                  (lambda value: value["cameras"]["head"].update(frame_id="another-frame"), "calibration_pose_frame_mismatch")):
            with self.subTest(expected=expected):
                original = deepcopy(self.harness.latest)
                self.replace_artifact("pose_sample_ref", "camera_pose_sample", mutation)
                self.assertEqual(robot_calibration_information(self.harness)["reason"], expected)
                self.harness.latest = original

    def test_scope_and_privilege_are_checked_before_projection(self):
        for scope, mutation in (("observer", lambda value: None),
                                ("actor", lambda value: value.update(evaluator_state={"answer": True}))):
            with self.subTest(scope=scope):
                original = deepcopy(self.harness.latest)
                self.replace_artifact("calibration_ref", "camera_calibration", mutation, scope=scope)
                result = robot_calibration_information(self.harness)
                self.assertFalse(result["available"])
                self.assertNotIn("cameras", result)
                self.harness.latest = original

    def test_missing_sources_and_wrong_artifact_kind_are_unavailable(self):
        original = deepcopy(self.harness.latest)
        del self.harness.latest["cameras"][0]["pose_sample_ref"]
        self.assertEqual(robot_calibration_information(self.harness)["reason"], "calibration_artifact_reference_missing")
        self.harness.latest = original
        self.replace_artifact("calibration_ref", "computed", lambda value: None)
        self.assertEqual(robot_calibration_information(self.harness)["reason"], "calibration_artifact_kind_mismatch")

    def test_absent_optical_or_hand_eye_fields_are_explicitly_unknown(self):
        def remove_fields(value):
            del value["cameras"]["left_wrist"]["T_link_camera"]
            del value["cameras"]["head"]["distortion"]
        self.replace_artifact("calibration_ref", "camera_calibration", remove_fields)
        result = robot_calibration_information(self.harness)
        self.assertTrue(result["available"], result)
        self.assertNotIn("T_link_camera", result["cameras"]["left_wrist"])
        self.assertNotIn("distortion", result["cameras"]["head"])
        self.assertIn("cameras.left_wrist.T_link_camera", result["unavailable_fields"])
        self.assertIn("cameras.head.distortion", result["unavailable_fields"])


if __name__ == "__main__":
    unittest.main()
