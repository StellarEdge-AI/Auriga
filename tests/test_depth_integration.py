"""End-to-end CPU RGB-D publication, actor gates and numerical deliveries."""
from program_fixture import program_services
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from auriga.core.config import Config
from auriga.core.contracts import canonical
from auriga.perception.depth.depth_payload import compact_action_feedback
from auriga.apps.gateway.workbench import Workbench
from auriga.runtime.harness import Harness
from auriga.core.observation_policy import ACTOR_PROFILE, DEPTH_ACTOR_PROFILE
from auriga.runtime.programs.runner import ProgramRunner
from auriga.robot.backends.robodojo.robodojo import FRAME_ID, TCP_CONFIG_ID
from auriga.core.storage.store import Store


class DepthRobot:
    def __init__(self, root):
        self.root = Path(root)
        self.executions = 0
        self.position = [0., 0., .7501]
        self.goal = 1.
        self.observation_profile = DEPTH_ACTOR_PROFILE
        texture = np.random.default_rng(8).integers(0, 255, (96, 96, 3), dtype=np.uint8)
        self.image = self.root / "rgb.png"
        cv2.imwrite(str(self.image), cv2.GaussianBlur(texture, (3, 3), 0))
        self.depth = self.root / "depth.npz"
        np.savez_compressed(self.depth, depth=np.full((96, 96), .5, np.float32), valid=np.ones((96, 96), bool))

    def start(self):
        return dict(data_origin="synthetic_fixture", frame_id=FRAME_ID, tcp_config_id=TCP_CONFIG_ID,
                    path_kinds=["backend_free"], max_waypoints=8, dual_arm=False, gripper_after_motion=True)

    def observe(self):
        frame_id = "f" + str(self.executions)
        pose = np.diag([1., -1., -1., 1.]); pose[2, 3] = 1.
        robot = dict(source_kind="measurement", frame_id=FRAME_ID, sim_step=self.executions,
                     step_domain="adapter_control_step", arms=dict(left=dict(
                         q_rad=[0.] * 6, joint_names=["joint" + str(i) for i in range(1, 7)],
                         source_kind="measurement", ee_pose=dict(position_m=self.position,
                             quaternion_xyzw=[-.5, .5, .5, .5], frame_id=FRAME_ID,
                             tcp_config_id=TCP_CONFIG_ID, source_kind="measurement"),
                         gripper=dict(command_normalized=self.goal, command_source_kind="command",
                             goal_normalized=self.goal, goal_source_kind="command",
                             joint_names=["joint7", "joint8"], position_m=[.02, .02], position_source_kind="measurement"))))
        camera = dict(camera_id="head", frame_id=frame_id, width=96, height=96,
                      png_path=str(self.image), depth_path=str(self.depth), depth_kind="z", depth_unit="m",
                      depth_clip_m=[.01, 10.], depth_shape=[96, 96])
        calibration = dict(reference_frame_id=FRAME_ID, cameras=dict(head=dict(
            K=[[200., 0., 48.], [0., 200., 48.], [0., 0., 1.]], distortion=[0.] * 5,
            image_size=[96, 96], T_world_camera=pose.tolist())))
        return dict(observation_profile=DEPTH_ACTOR_PROFILE, data_origin="synthetic_fixture", world_kind="observed",
                    cameras=[camera], camera_calibration=calibration, robot_model=dict(base_link="base", joints=[]),
                    camera_pose_sample=dict(sim_step=self.executions, step_domain="adapter_control_step",
                        cameras=dict(head=dict(T_world_camera=pose.tolist(), frame_id=frame_id, source_kind="measurement"))),
                    robot_state=robot, sim_step=self.executions, step_domain="adapter_control_step")

    def execute(self, action, progress_callback=None, cancel_flag=None):
        self.executions += 1
        for waypoint in action.get("path", {}).get("waypoints", []):
            self.position = waypoint["left"]["position_m"]
        for event in action.get("gripper_events", []):
            self.goal = event["goal_normalized"]
        return dict(status="completed", terminal_observation=self.observe(), executed_native_steps=1)


class DepthIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "store", dict(run_id="depth-test", episode_id="ep", env_instance_id="env", env_generation=1))
        self.addCleanup(self.store.close)
        self.robot = DepthRobot(self.root)

    def harness(self, **features):
        config = Config.from_dict(dict(features=dict(observation_profile=DEPTH_ACTOR_PROFILE, motion_review=False, **features)))
        harness = Harness(self.store, self.robot, config=config)
        harness.start()
        harness.capabilities.update(bounded_wait=dict(max_native_steps=50), path_kinds=["backend_free", "hold"])
        return harness

    def wait(self, harness, **args):
        return harness.dispatch("robot_wait", dict(observation_id=harness.latest["observation_id"], native_steps=5, **args),
                                dict(tool_call_id=args.get("action_id", "wait")))

    def query(self, harness, **request):
        result = harness.dispatch("depth_query", dict(request=request), {})
        self.assertEqual(len(result["contentItems"]), 1)
        return json.loads(result["contentItems"][0]["text"])

    def test_geometry_depth_compatibility_keeps_owner_validation_for_both_roles(self):
        harness = self.harness()
        request = dict(kind="depth_points", observation_id=harness.latest["observation_id"],
                       camera_id="head", pixels=[[48, 48]])
        target = harness.invoke("depth_query", dict(request=dict(kind="register_target",
            observation_id=request["observation_id"], camera_id="head", roi=[36, 36, 24, 24])), {}, role="program")
        target_id = target.data["target_id"]
        for role in ("actor", "program"):
            result = harness.invoke("geometry_query", dict(request=request), {}, role=role)
            self.assertEqual(result.data["status"], "supported")
            for invalid, reason in ((dict(request, radius_px=5), "invalid_radius_px"),
                    (dict(kind="region_geometry", target_id=target_id, observation_id=request["observation_id"]),
                     "depth_target_fields_cannot_be_overridden"),
                    (dict(kind="refresh_target", target_id=target_id, observation_id=request["observation_id"],
                          camera_id="head", roi=[0, 0, 4, 4]), "invalid_depth_target_fields")):
                with self.subTest(role=role, invalid=invalid), self.assertRaisesRegex(ValueError, reason):
                    harness.invoke("geometry_query", dict(request=invalid), {}, role=role)
        self.assertEqual(self.robot.executions, 0)

    def test_default_profile_hides_tools_and_rejects_all_depth_routes(self):
        from test_harness import FixtureRobot
        config = Config.from_dict({})
        harness = Harness(self.store, FixtureRobot(self.root), config=config, motion_review=False)
        for name, args in (("depth_query", dict(request=dict(kind="help"))),
                           ("geometry_query", dict(request=dict(kind="depth_points", observation_id="obs",
                                                                camera_id="head", pixels=[[1, 2]])))):
            with self.subTest(tool=name), self.assertRaisesRegex(ValueError, "depth_not_enabled"):
                harness.dispatch(name, args, {})
        workbench = Workbench.__new__(Workbench)
        workbench.config = config
        workbench.pi05_enabled = False
        workbench.visual_review_model = None
        workbench.observation_profile = ACTOR_PROFILE
        self.assertNotIn("depth_query", {item["name"] for item in workbench.actor_tools()})
        with self.assertRaises(ValueError):
            harness.publish_observation(self.robot.observe())

    def test_binary_publication_numeric_query_and_candidate_identity(self):
        harness = self.harness()
        obs = harness.latest
        camera = obs["cameras"][0]
        info, raw = self.store.resolve_artifact(camera["depth_ref"])
        self.assertEqual(info["kind"], "depth")
        with np.load(io.BytesIO(raw), allow_pickle=False) as arrays:
            self.assertEqual(arrays["depth"].dtype, np.float32)
        points = self.query(harness, kind="depth_points", observation_id=obs["observation_id"], camera_id="head", pixels=[[48, 48]])
        self.assertEqual(points["status"], "supported")
        np.testing.assert_allclose(points["value"]["points"][0]["point_m"], [0, 0, .5], atol=1e-6)
        self.assertLess(len(canonical(points)), 2000)
        again = self.query(harness, kind="depth_points", observation_id=obs["observation_id"], camera_id="head", pixels=[[48, 48]])
        self.assertEqual(again["measurement_id"], points["measurement_id"])
        target = self.query(harness, kind="register_target", observation_id=obs["observation_id"], camera_id="head", roi=[36, 36, 24, 24], arm="left")
        measured = self.query(harness, kind="region_geometry", target_id=target["target_id"])
        self.assertIn("visible_dimensions_m", measured["value"])
        candidates = self.query(harness, kind="grasp_candidates", target_id=target["target_id"])
        self.assertTrue(candidates["value"]["candidates"], candidates)
        self.wait(harness)
        self.assertEqual(self.robot.executions, 1)
        with self.assertRaisesRegex(ValueError, "current_observation"):
            self.query(harness, kind="region_geometry", target_id=target["target_id"])

    def test_rgbd_history_and_program_artifacts_cannot_cross_rgb_boundary(self):
        harness = self.harness()
        obs = harness.latest
        harness.observation_profile = ACTOR_PROFILE
        with self.assertRaisesRegex(ValueError, "matching_observation"):
            harness.dispatch("history_view_observation", dict(observation_id=obs["observation_id"]), {})
        runner = ProgramRunner(program_services(harness))
        self.addCleanup(runner.close)
        entry = dict(cancel=threading.Event(), deadline=time.monotonic() + 30, call_lock=threading.RLock())
        with self.assertRaisesRegex(ValueError, "depth_artifact"):
            runner.dispatch(entry, dict(artifact_ref=obs["cameras"][0]["depth_ref"]))
        ref = self.store.put_artifact(b'{"depth_derived":true,"value":{"lift_m":0.02}}', "computed", "application/json")
        with self.assertRaisesRegex(ValueError, "privileged_data"):
            runner.dispatch(entry, dict(artifact_ref=ref))

    def test_persisted_measurement_does_not_bypass_source_integrity(self):
        harness = self.harness()
        request = dict(kind="depth_points", observation_id=harness.latest["observation_id"],
                       camera_id="head", pixels=[[48, 48]])
        self.query(harness, **request)
        reference = harness.latest["cameras"][0]["depth_ref"]
        _, path = self.store.artifact(reference)
        path.write_bytes(b"corrupted source")
        with self.assertRaisesRegex(ValueError, "artifact_digest_mismatch"):
            self.query(harness, **request)

    def test_depth_skills_are_separately_disabled_and_rgb_comparison_independent(self):
        harness = self.harness(vision_compare=False)
        self.assertIn("queries", self.query(harness, kind="help"))
        workbench = Workbench.__new__(Workbench)
        workbench.config = harness.config.with_overrides(dict(features=dict(depth_skills=True)))
        workbench.pi05_enabled = False
        workbench.visual_review_model = None
        workbench.vision_compare = False
        workbench.observation_profile = DEPTH_ACTOR_PROFILE
        tools = {item["name"]: item for item in workbench.actor_tools()}
        self.assertTrue(workbench.config.features.depth_skills)
        self.assertIn("depth_query", tools)
        self.assertIn("robot_action", tools)
        self.assertIn("operation_memory", tools)
        self.assertNotIn("skill_run", tools)
        self.assertNotIn("manipulation_run", tools)
        self.assertNotIn("vision_compare", tools)

    def test_numeric_delivery_keeps_exception_and_periodic_images(self):
        harness = self.harness(depth_auto_feedback=True, depth_event_payload=True)
        measurement = dict(status="supported", value=dict(evidence="rigid_surface_motion",
                           world_displacement_m=[0, 0, 0], world_rotation_deg=0))
        action = dict(action_id="a", path={}, gripper_events=[])
        result = dict(request=action, receipt=dict(status="completed", result_observation_id=harness.latest["observation_id"]))
        self.assertIsNotNone(compact_action_feedback(harness, result, [measurement], {}))
        action["action_id"] = "b"
        self.assertIsNotNone(compact_action_feedback(harness, result, [measurement], {}))
        action["action_id"] = "c"
        self.assertIsNone(compact_action_feedback(harness, result, [measurement], {}))
        self.assertIsNone(compact_action_feedback(harness, result, [{**measurement, "status": "uncertain"}], {}))
        self.assertIsNone(compact_action_feedback(harness, {**result, "receipt": {**result["receipt"], "status": "unknown"}}, [measurement], {}))
        self.assertIsNone(compact_action_feedback(harness, result, [measurement], dict(program_id="p")))
        self.assertIsNone(compact_action_feedback(harness, {**result, "request": {**action, "gripper_events": [dict(goal_normalized=1)]}}, [measurement], {}))

    def test_feedback_preserves_seed_and_old_action_does_not_mutate_new_target(self):
        harness = self.harness(depth_auto_feedback=True)
        first = harness.latest["observation_id"]
        self.wait(harness, action_id="empty-target-action")
        saved = self.store.read("depth_action_feedback", "empty-target-action")
        self.assertEqual(saved["measurements"], [])
        target = self.query(harness, kind="register_target", observation_id=harness.latest["observation_id"],
                            camera_id="head", roi=[30, 30, 35, 35], seed_pixel=[40, 40], arm="left")
        harness.dispatch("robot_wait", dict(observation_id=first, native_steps=5, action_id="empty-target-action"),
                         dict(tool_call_id="move1-repeat"))
        tracked = harness.depth._target(target["target_id"])
        self.assertEqual(tracked["status"], "tracked")
        fake = dict(status="supported", value=dict(after_roi=[31, 31, 35, 35], after_seed_pixel=[41, 41]))
        with patch.object(harness.depth, "_measure", return_value=fake) as measure:
            self.wait(harness, action_id="seed-action")
        self.assertEqual(measure.call_args.args[0]["seed_pixel"], [40, 40])
        self.assertEqual(harness.depth._target(target["target_id"])["seed_pixel"], [41, 41])

    def test_optional_perception_failure_preserves_completed_action_and_rgb(self):
        harness = self.harness(depth_auto_feedback=True, depth_event_payload=True)
        with patch.object(harness.depth, "feedback", side_effect=RuntimeError("synthetic perception failure")):
            result = self.wait(harness, action_id="perception-error")
        answer = json.loads(result["contentItems"][0]["text"])
        self.assertEqual(answer["receipt"]["status"], "completed")
        self.assertEqual(answer["depth_feedback"][0]["status"], "uncertain")
        self.assertTrue(any(item["type"] == "inputImage" for item in result["contentItems"]))
        self.assertEqual(self.robot.executions, 1)


if __name__ == "__main__":
    unittest.main()
