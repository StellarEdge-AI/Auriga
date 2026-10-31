"""Official actor inputs and development evidence must never share a publication path."""
from copy import deepcopy
import asyncio
import sqlite3
from unittest.mock import patch
import json
from pathlib import Path
import tempfile
import unittest

from auriga.apps.gateway.workbench import Workbench
from auriga.runtime.harness import Harness
from auriga.core.config import Config
from auriga.core.observation_policy import (ACTOR_PROFILE, LEGACY_ACTOR_PROFILE, ACTOR_POLICY,
    DEVELOPMENT_TOOLS, actor_robot_state, actor_policy, require_actor_observation, reject_privileged)
from auriga.core.storage.store import Store
from auriga.runtime.tools.definitions import TOOLS
from test_harness import FixtureRobot


class ObservationPolicyTest(unittest.TestCase):
    def test_fail_closed_publication_history_and_derived_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "run", dict(run_id="policy", episode_id="ep", env_instance_id="env", env_generation=1))
            self.addCleanup(store.close)
            robot = FixtureRobot(directory)
            harness = Harness(store, robot, observation_profile=LEGACY_ACTOR_PROFILE, motion_review=False)
            initial = harness.start()
            before = len(store.records("observation"))
            for changes in ({"observation_profile": "rgb_proprio_calibrated_v1"}, {"camera_calibration": {}},
                            {"camera_pose_sample": {}}, {"robot_model": {}}, {"extra": {"native_usd_pose_diagnostic": {}}},
                            {"extra": [{"planning_diagnostics": [{"status": "native_failure"}]}]}):
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    harness.publish_observation({**robot.observe(), **changes})
            raw = robot.observe()
            raw["robot_state"]["arms"]["left"]["camera_poses"] = {}
            with self.assertRaises(ValueError):
                harness.publish_observation(raw)
            self.assertEqual(len(store.records("observation")), before)
            self.assertTrue(DEVELOPMENT_TOOLS.issubset(item["name"] for item in TOOLS))
            for name in DEVELOPMENT_TOOLS:
                with self.subTest(tool=name), self.assertRaisesRegex(ValueError, "tool_unavailable"):
                    harness.dispatch(name, {}, {})
            # A historical calibrated record must not be re-published, even if it exists on disk.
            historical = {**initial, "observation_id": "old-calibrated", "observation_profile": "rgb_proprio_calibrated_v1"}
            store.save("observation", "old-calibrated", historical)
            with self.assertRaisesRegex(ValueError, "actor_requires"):
                harness.dispatch("history_view_observation", {"observation_id": "old-calibrated"}, {})
            # Developer evidence can still be saved and explicitly read by the observer.
            ref = store.put_artifact(b'{"K":"development-only"}', "calibration", "application/json", "observer")
            self.assertIn(b"development-only", store.resolve_artifact(ref, allowed_scopes=("observer",))[1])
            with self.assertRaisesRegex(ValueError, "access_scope_denied"):
                store.resolve_artifact(ref)

    def test_native_proprioception_projection_and_clean_session_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            robot = FixtureRobot(directory)
            state = robot.observe()["robot_state"]
            arm = state["arms"]["left"]
            arm.update(joint_names=["joint1"], qdot_rad_s=[17.0], gripper=dict(
                joint_names=["finger"], position_m=[0.123456], position_source_kind="measurement",
                command_normalized=.25, command_source_kind="command", goal_normalized=.25,
                goal_source_kind="command", contact_feedback=None, contact_feedback_reason="unavailable"))
            original = deepcopy(state)
            projected = actor_robot_state(state, LEGACY_ACTOR_PROFILE)["arms"]["left"]
            self.assertEqual(projected["q_rad"], arm["q_rad"])
            self.assertEqual(projected["ee_pose"], arm["ee_pose"])
            self.assertNotIn("qdot_rad_s", projected)
            self.assertNotIn("joint_names", projected)
            self.assertNotIn("position_m", projected["gripper"])
            self.assertEqual(projected["gripper"]["command_normalized"], .25)
            self.assertEqual(state, original)
            arm["unexpected_scene_measurement"] = 1
            with self.assertRaisesRegex(ValueError, "non_actor"):
                actor_robot_state(state)
            path = Path(directory) / "forbidden-run"
            with self.assertRaisesRegex(ValueError, "actor_requires"):
                Workbench(path, observation_profile="unknown_profile")
            self.assertFalse(path.exists())
            path.mkdir()
            (path / "manifest.json").write_text(json.dumps({"observation_profile": ACTOR_PROFILE}))
            with self.assertRaisesRegex(ValueError, "clean_actor_history"):
                Workbench(path, resume=True)

    def test_calibration_is_permitted_without_object_or_contact_truth(self):
        public = dict(observation_profile=ACTOR_PROFILE,
                      camera_calibration={"K": [[1, 0, 0], [0, 1, 0], [0, 0, 1]]},
                      camera_pose_sample={"cameras": {"wrist": {"T_world_camera": []}}},
                      robot_model={"joints": []})
        require_actor_observation(public, ACTOR_PROFILE)
        self.assertEqual(actor_policy(), ACTOR_POLICY)
        with self.assertRaisesRegex(ValueError, "matching_observation_profile"):
            require_actor_observation(public, LEGACY_ACTOR_PROFILE)
        for field in ("object_poses", "depth", "contact_forces", "evaluator_state", "native_usd_pose_diagnostic"):
            for profile in (ACTOR_PROFILE, LEGACY_ACTOR_PROFILE):
                with self.subTest(field=field, profile=profile), self.assertRaises(ValueError):
                    reject_privileged({"nested": [{field: {}}]}, profile)
        with self.assertRaises(ValueError):
            require_actor_observation({**public, "observation_profile": LEGACY_ACTOR_PROFILE})

    def test_calibrated_proprioception_keeps_fk_inputs_but_not_contacts(self):
        with tempfile.TemporaryDirectory() as directory:
            state = FixtureRobot(directory).observe()["robot_state"]
            state["arms"]["left"].update(joint_names=["joint1"], qdot_rad_s=[0.2],
                gripper=dict(joint_names=["finger"], position_m=[0.02],
                             position_source_kind="measurement", contact_feedback=None))
            projected = actor_robot_state(state, ACTOR_PROFILE)["arms"]["left"]
            self.assertEqual(projected["joint_names"], ["joint1"])
            self.assertEqual(projected["gripper"]["position_m"], [0.02])
            self.assertNotIn("contact_feedback", projected["gripper"])
            state["arms"]["left"]["gripper"]["contact_feedback"] = {"object": "hidden"}
            with self.assertRaisesRegex(ValueError, "contact_truth"):
                actor_robot_state(state, ACTOR_PROFILE)

    def test_gateway_filters_controls_and_owns_shared_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = Path(directory) / "memory"
            with patch("auriga.core.config.LOCAL_CONFIG_PATH", Path(directory) / "absent.local.json"):
                config = Config.load(environ={}, overrides={"features": {"grasp_memory_dir": str(memory)}})
                workbench = Workbench(Path(directory) / "calibrated", robot=FixtureRobot(directory), config=config)
                try:
                    self.assertEqual(workbench.observation_profile, ACTOR_PROFILE)
                    names = {tool["name"] for tool in workbench.actor_tools()}
                    self.assertIn("geometry_query", names)
                    self.assertIn("robot_action", names)
                    self.assertIn("operation_memory", names)
                    self.assertFalse({"robot_preview_motion", "grasp_memory", "skill_run", "manipulation_run"} & names)
                    self.assertFalse(any(name.startswith("scene_") for name in names))
                    self.assertNotIn("operation_verify", names)
                    self.assertNotIn("vision_review", names)
                    self.assertEqual(workbench.grasp_memory_store.root, memory)
                    shared = workbench.grasp_memory_store
                finally:
                    asyncio.run(workbench.close())
                with self.assertRaises(sqlite3.ProgrammingError):
                    shared.db.execute("SELECT 1")
                for profile in (ACTOR_PROFILE, LEGACY_ACTOR_PROFILE):
                    control = Workbench(Path(directory) / profile, robot=FixtureRobot(directory), config=config,
                                        observation_profile=profile, motion_review=False)
                    try:
                        names = {tool["name"] for tool in control.actor_tools()}
                        self.assertNotIn("robot_preview_motion", names)
                        self.assertEqual("robot_action" in names, profile == ACTOR_PROFILE)
                        self.assertIn("operation_memory", names)
                        self.assertFalse({"skill_run", "manipulation_run", "grasp_memory"} & names)
                        self.assertFalse(any(name.startswith("scene_") for name in names))
                        self.assertEqual("geometry_query" in names, profile == ACTOR_PROFILE)
                        self.assertEqual(control.grasp_memory_store.root, memory)
                        self.assertIs(control.harness.operation_memory.shared_store, control.grasp_memory_store)
                        shared = control.grasp_memory_store
                    finally:
                        asyncio.run(control.close())
                    with self.assertRaises(sqlite3.ProgrammingError):
                        shared.db.execute("SELECT 1")

    def test_calibrated_harness_publishes_exact_camera_and_model_closure(self):
        from auriga.robot.model.geometry import GeometryTools
        from auriga.perception.scene.store import SceneStore
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = dict(run_id="calibrated", episode_id="ep", env_instance_id="env", env_generation=1)
            store, scene_artifacts = Store(root/"run", identity), Store(root/"scene-artifacts", identity)
            self.addCleanup(store.close)
            self.addCleanup(scene_artifacts.close)
            robot = FixtureRobot(directory)
            harness = Harness(store, robot, observation_profile=ACTOR_PROFILE, motion_review=False)
            harness.scene_artifacts = scene_artifacts
            harness.scene = SceneStore(root/"separate-scene", scene_artifacts.resolve_artifact, scene_artifacts.put_artifact)
            raw = robot.observe()
            raw["cameras"][0]["frame_id"] = "frame-calibrated"
            raw["robot_state"].update(sim_step=0, step_domain=raw["step_domain"])
            matrix = [[1.,0.,0.,0.], [0.,1.,0.,0.], [0.,0.,1.,0.], [0.,0.,0.,1.]]
            camera_pose = deepcopy(matrix)
            camera_pose[0][3] = 0.2
            intrinsic = [[100.,0.,0.5], [0.,100.,0.5], [0.,0.,1.]]
            raw.update(observation_profile=ACTOR_PROFILE,
                camera_calibration=dict(reference_frame_id="env_origin", cameras={"fixture_camera": dict(
                    K=intrinsic, distortion={"model":"opencv", "coefficients":[]}, image_size=[1,1],
                    T_link_camera=matrix)}),
                camera_pose_sample=dict(reference_frame_id="env_origin", sim_step=0, step_domain=raw["step_domain"],
                    cameras={"fixture_camera": dict(T_world_camera=camera_pose, frame_id="frame-calibrated", source_kind="measurement")}),
                robot_model=dict(base_link="base", joints=[], source_kind="public_robot_model"))
            bundle = harness.publish_observation(raw)
            metadata = json.loads(harness.observation_content(bundle, {}, detail="full")[0]["text"])
            self.assertEqual(metadata["camera_calibration"], raw["camera_calibration"])
            self.assertEqual(metadata["camera_pose_sample"], raw["camera_pose_sample"])
            frame = bundle["cameras"][0]
            for ref in (frame["rgb_ref"], frame["calibration_ref"], frame["pose_sample_ref"],
                        bundle["robot_model_ref"], bundle["robot_state_ref"]):
                self.assertEqual(store.resolve_artifact(ref)[1], scene_artifacts.resolve_artifact(ref)[1])
            self.assertEqual(json.loads(store.resolve_artifact(bundle["robot_model_ref"])[1]), raw["robot_model"])
            camera = GeometryTools(harness.scene).camera(identity, bundle["observation_id"], "fixture_camera")
            self.assertEqual(camera["K"].tolist(), intrinsic)
            self.assertEqual(camera["T"].tolist(), camera_pose)
            with self.assertRaisesRegex(ValueError, "missing_geometry"):
                harness.publish_observation({**robot.observe(), "observation_profile": ACTOR_PROFILE})

    def test_native_capability_contact_unavailable_is_not_contact_truth(self):
        for profile in (ACTOR_PROFILE, LEGACY_ACTOR_PROFILE):
            capabilities = dict(observation_profile=profile, gripper_semantics=dict(
                command="normalized last command", contact_feedback="unavailable"))
            require_actor_observation(capabilities, profile)
            for feedback in ({"contacts": []}, "measured", False, 0, []):
                with self.subTest(profile=profile, feedback=feedback), self.assertRaisesRegex(ValueError, "contact_truth"):
                    require_actor_observation({**capabilities, "gripper_semantics": {"contact_feedback": feedback}}, profile)
