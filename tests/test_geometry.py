"""Meaningful component/failure checks: python -m unittest discover -s tests -p test_geometry.py."""
from copy import deepcopy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from auriga.core.contracts import canonical,digest
from auriga.robot.model.geometry import GeometryTools,fit_geometry,triangulate,fk,project
from auriga.perception.scene.store import SceneStore
from auriga.core.storage.store import Store
from geometry_fixture import IDENTITY,recorded_plane


class GeometryCheck(unittest.TestCase):
    def plane_request(self, **changes):
        return dict(IDENTITY, kind="pixel_plane", observation_id="obs-1", camera_id="head",
                    pixel=[345, 265], plane_origin_m=[0, 0, 2], plane_normal=[0, 0, 1]) | changes

    def test_pixel_plane_uses_saved_camera_and_explicit_plane_only(self):
        from auriga.perception.scene.scene_contracts import validate_response
        with patch.object(self.scene, "query", side_effect=AssertionError("no reconstructed scene")):
            result = self.geometry.execute(self.plane_request())
        self.assertEqual(result["status"], "conditional", result)
        np.testing.assert_allclose(result["value"]["point_m"], [.2, .1, 2], atol=1e-10)
        self.assertEqual(result["value"]["scope"], "caller_assumed_infinite_plane")
        self.assertIn("not measured depth", " ".join(result["assumptions"]))
        self.assertGreater(result["value"]["ray_plane_condition_factor"], 1)
        validate_response("geometry/query", result, method="POST")
        # Scaling or reversing the normal does not change the geometric plane.
        for normal in ([0, 0, -4], [0, 0, 1e300], [0, 0, 1e-300]):
            other = self.geometry.execute(self.plane_request(plane_normal=normal))
            np.testing.assert_allclose(other["value"]["point_m"], [.2, .1, 2], atol=1e-10)
        changed = self.geometry.execute(self.plane_request(plane_origin_m=[0, 0, 3]))
        np.testing.assert_allclose(changed["value"]["point_m"], [.25, .15, 3], atol=1e-10)

    def test_pixel_plane_optional_alt_plane_reports_assumption_delta(self):
        result = self.geometry.execute(self.plane_request(plane_origin_alt_m=[0, 0, 3]))
        self.assertEqual(result["status"], "conditional", result)
        np.testing.assert_allclose(result["value"]["point_m"], [.2, .1, 2], atol=1e-10)
        np.testing.assert_allclose(result["value"]["alternate_point_m"], [.25, .15, 3], atol=1e-10)
        np.testing.assert_allclose(result["value"]["d_point_d_normal_offset_m"], [.05, .05, 1.], atol=1e-10)
        self.assertEqual(result["value"]["cross_camera_projections"], {})
        self.assertEqual(result["value"]["plane_origin_alt_m"], [0., 0., 3.])
        self.assertAlmostEqual(result["value"]["alternate_ray_distance_m"], result["value"]["ray_distance_m"] * 1.5, places=10)
        self.assertAlmostEqual(result["value"]["assumption_delta_m"], np.linalg.norm([.05, .05, 1]), places=10)
        self.assertIn("not a depth error bound", " ".join(result["assumptions"]))
        from auriga.perception.scene.scene_contracts import validate_response
        validate_response("geometry/query", result, method="POST")
        behind = self.geometry.execute(self.plane_request(plane_origin_alt_m=[0, 0, -1]))
        self.assertEqual(behind["status"], "unknown", behind)
        self.assertIsNone(behind["value"])
        self.assertTrue(behind["reason"].startswith("ray_plane_not_in_front_of_camera"), behind)

    def test_pixel_plane_cross_camera_projections_and_target_overlay(self):
        observation = deepcopy(self.obs[1])
        observation["observation_id"] = "multi-cam"
        calibration = json.loads(self.artifacts.resolve_artifact(observation["cameras"][0]["calibration_ref"])[1])
        calibration["cameras"]["wrist"] = deepcopy(calibration["cameras"]["head"])
        cal_ref = self.artifacts.put_artifact(canonical(calibration).encode(), "calibration", "application/json")
        observation["cameras"][0]["calibration_ref"] = cal_ref
        wrist_cam = deepcopy(observation["cameras"][0])
        wrist_cam.update(camera_id="wrist", frame_id="frame-wrist", calibration_ref=cal_ref)
        observation["cameras"].append(wrist_cam)
        state = json.loads(self.artifacts.resolve_artifact(observation["robot_state_ref"])[1])
        t_wrist = np.eye(4); t_wrist[0, 3] = 0.20
        state["camera_poses"]["wrist"] = dict(T_world_camera=t_wrist.tolist(), frame_id="frame-wrist",
                                              sim_step=state["sim_step"], step_domain=state["step_domain"],
                                              source_kind="robot_kinematics")
        state["arms"] = {
            "left": {
                "source_kind": "measurement",
                "ee_pose": {
                    "position_m": [0.05, 0.10, 1.85],
                    "quaternion_xyzw": [0.0, 0.0, 0.0, 1.0],
                    "frame_id": "env_origin",
                    "tcp_config_id": "arx-x5-link6-v1",
                    "source_kind": "measurement",
                },
            }
        }
        observation["robot_state_ref"] = self.artifacts.put_artifact(canonical(state).encode(), "robot_state", "application/json")
        self.scene.import_observation(observation)
        result = self.geometry.execute(self.plane_request(
            observation_id="multi-cam", plane_origin_alt_m=[0, 0, 3], target_camera_id="wrist"))
        self.assertEqual(result["status"], "conditional", result)
        self.assertIn("gripper_relative", result["value"])
        np.testing.assert_allclose(result["value"]["gripper_relative"]["left"]["grasp_position_m"], [0.20007, 0.10, 1.85], atol=1e-4)
        np.testing.assert_allclose(result["value"]["gripper_relative"]["left"]["point_minus_grasp_world_m"], [-0.00007, 0.0, 0.15], atol=1e-4)
        np.testing.assert_allclose(result["value"]["gripper_relative"]["left"]["jaw_midplane_normal"], [0.0, 1.0, 0.0], atol=1e-4)
        cross = result["value"]["cross_camera_projections"]
        self.assertIn("wrist", cross)
        np.testing.assert_allclose(cross["wrist"]["pixel"], [320., 265.], atol=1e-8)
        np.testing.assert_allclose(cross["wrist"]["alternate_pixel"], [328.33333333, 265.], atol=1e-6)
        self.assertEqual(len(cross["wrist"]["d_pixel_d_normal_offset_px"]), 2)
        self.assertGreater(abs(cross["wrist"]["d_pixel_d_normal_offset_px"][0]), 1.0)
        self.assertEqual(result["value"]["target_camera_id"], "wrist")
        self.assertEqual(len(result["value"]["pixels"]), 2)
        self.assertTrue(result["value"]["overlay_ref"].startswith("artifact:"))
        from auriga.perception.scene.scene_contracts import validate_response
        validate_response("geometry/query", result, method="POST")
        single = self.geometry.execute(self.plane_request(observation_id="multi-cam", target_camera_id="wrist"))
        self.assertEqual(len(single["value"]["pixels"]), 1)
        validate_response("geometry/query", single, method="POST")

    def test_pixel_plane_respects_rotated_camera_distortion_and_sloped_plane(self):
        observation = deepcopy(self.obs[0])
        observation["observation_id"] = "rotated"
        calibration = json.loads(self.artifacts.resolve_artifact(observation["cameras"][0]["calibration_ref"])[1])
        calibration["cameras"]["head"]["distortion"]["coefficients"] = [.05, -.02, .001, -.001, 0]
        observation["cameras"][0]["calibration_ref"] = self.artifacts.put_artifact(
            canonical(calibration).encode(), "calibration", "application/json")
        state = json.loads(self.artifacts.resolve_artifact(observation["robot_state_ref"])[1])
        state["camera_poses"]["head"]["T_world_camera"] = [
            [1, 0, 0, 1], [0, 0, -1, 2], [0, 1, 0, 3], [0, 0, 0, 1]]
        observation["robot_state_ref"] = self.artifacts.put_artifact(
            canonical(state).encode(), "robot_state", "application/json")
        self.scene.import_observation(observation)
        camera = self.geometry.camera(IDENTITY, "rotated", "head")
        expected = [1.2, 0, 3.1]
        pixel = project(camera, [expected])[0][0].tolist()
        result = self.geometry.execute(self.plane_request(observation_id="rotated", pixel=pixel,
            plane_origin_m=expected, plane_normal=[1, 2, 3]))
        self.assertEqual(result["status"], "conditional", result)
        np.testing.assert_allclose(result["value"]["point_m"], expected, atol=1e-7)

    def test_pixel_plane_degeneracies_do_not_publish_a_point(self):
        cases = [
            (dict(plane_normal=[0, 0, 0]), "invalid_input", "invalid_plane_normal"),
            (dict(pixel=[320, 240], plane_normal=[1, 0, 0]), "unknown", "ray_plane_near_parallel"),
            (dict(pixel=[320, 240], plane_normal=[1, 0, .005]), "unknown", "ray_plane_near_parallel"),
            (dict(plane_origin_m=[0, 0, -1]), "unknown", "ray_plane_not_in_front_of_camera"),
            (dict(plane_origin_m=[0, 0, 0]), "unknown", "ray_plane_not_in_front_of_camera"),
            (dict(pixel=[640, 240]), "invalid_input", "pixel_out_of_bounds"),
            (dict(frame_id="other"), "invalid_input", "unsupported_frame"),
            (dict(observation_id="missing"), "unknown", None),
        ]
        for changes, status, reason in cases:
            with self.subTest(changes=changes):
                result = self.geometry.execute(self.plane_request(**changes))
                self.assertEqual(result["status"], status, result)
                self.assertIsNone(result["value"])
                if reason:
                    self.assertTrue(result["reason"].startswith(reason), result)

    def test_pixel_plane_actor_and_isolated_program_reuse_projection_without_motion(self):
        from auriga.runtime.harness import Harness
        from auriga.runtime.programs.runner import ProgramRunner
        from test_harness import FixtureRobot
        robot = FixtureRobot(Path(self.tmp.name))
        harness = Harness(self.artifacts, robot, observation_profile="rgb_proprio_calibrated_v1")
        harness.scene = self.scene
        harness.thread_id = "thread"
        request = {key: value for key, value in self.plane_request().items() if key not in IDENTITY}
        shown = harness.handle_tool(dict(threadId="thread", turnId="turn", callId="plane",
            tool="geometry_query", arguments=dict(request=request)))
        result = json.loads(shown["contentItems"][0]["text"])
        self.assertEqual(result["status"], "conditional")
        self.assertIn("not measured depth", " ".join(result["assumptions"]))
        self.assertNotIn("implementation_digest", result)
        overlay = harness.handle_tool(dict(threadId="thread", turnId="turn", callId="projection",
            tool="geometry_query", arguments=dict(request=dict(kind="project_points", observation_id="obs-1",
                camera_id="head", points_m=[result["value"]["point_m"]]))))
        self.assertTrue(any(item["type"] == "inputImage" for item in overlay["contentItems"]))
        runner = ProgramRunner(harness.program_services())
        self.addCleanup(runner.close)
        source = ("from auriga_client import AurigaClient\n"
                  "r = AurigaClient().call('geometry_query', " + repr(dict(request=request)) + ")\n"
                  "assert r['kind'] == 'data' and r['data']['status'] == 'conditional'\n"
                  "assert abs(r['data']['value']['point_m'][0] - .2) < 1e-9\n")
        job = runner.run(dict(source=source), dict(thread_id="thread", turn_id="turn", tool_call_id="program-plane"))
        self.assertEqual(job["status"], "succeeded", job)
        self.assertEqual(job["call_summary"]["failed_calls"], 0)
        self.assertEqual(robot.executions, 0)
        self.assertEqual(self.artifacts.records("action_request"), [])

    def test_pixel_plane_invalid_schema_never_reaches_geometry_at_either_port(self):
        from auriga.runtime.harness import Harness
        from auriga.runtime.programs.runner import ProgramRunner
        from test_harness import FixtureRobot
        robot = FixtureRobot(Path(self.tmp.name))
        harness = Harness(self.artifacts, robot, observation_profile="rgb_proprio_calibrated_v1")
        harness.thread_id = "thread"
        harness.scene = self.scene
        request = {key: value for key, value in self.plane_request().items() if key not in IDENTITY}
        invalid = [request | change for change in (
            dict(plane_origin_m=[0, True, 2]), dict(plane_normal=[0, 1]), dict(pixel=[False, 240]),
            dict(plane_normal=[0, 0, float("inf")]), dict(plane_origin_alt_m=[0, float("inf"), 3]),
            dict(plane_origin_alt_m=[0, 3]), dict(typo=True), dict(observation_ids=["obs-1"]))]
        invalid.append({key: value for key, value in request.items() if key != "plane_origin_m"})
        runner = ProgramRunner(harness.program_services())
        self.addCleanup(runner.close)
        with patch.object(GeometryTools, "execute", side_effect=AssertionError("invalid request reached geometry")) as execute:
            for i, bad in enumerate(invalid):
                with self.subTest(request=bad), self.assertRaises(ValueError):
                    harness.handle_tool(dict(threadId="thread", turnId="turn", callId=f"invalid-{i}",
                        tool="geometry_query", arguments=dict(request=bad)))
            bad = request | dict(typo=True)
            source = ("from auriga_client import AurigaClient\n"
                      "r = AurigaClient().call('geometry_query', " + repr(dict(request=bad)) + ")\n"
                      "assert r['success'] is False\n")
            job = runner.run(dict(source=source), dict(thread_id="thread", turn_id="turn", tool_call_id="program-invalid-plane"))
            self.assertEqual(job["status"], "succeeded", job)
            self.assertEqual(job["call_summary"]["failed_calls"], 1)
            execute.assert_not_called()
        self.assertEqual(robot.executions, 0)

    def test_public_geometry_metadata_preserves_measurements_and_complete_raw_result(self):
        from auriga.core.presentation.actor_output import ActorOutput
        from auriga.core.presentation.actor_presentation import ActorPresentation
        from auriga.runtime.tools.handlers import geometry_query
        runtime = SimpleNamespace(scene=self.scene, identity=IDENTITY, image_settings={})
        response = geometry_query(runtime, dict(request=dict(kind="pixel_ray", camera_id="head",
            observation_id="obs-0", pixel=[320, 240])), {})
        original = deepcopy(response.data)
        self.assertEqual(original["status"], "supported")
        self.assertIn("library_versions", original)
        actor = ActorOutput(self.artifacts, ActorPresentation(self.artifacts))
        shown = json.loads(actor.present(response)["contentItems"][0]["text"])
        self.assertTrue({"library_versions", "algorithm", "implementation_digest", "duration_s",
                         "schema_version", "run_id", "episode_id", "env_instance_id", "env_generation"}.isdisjoint(shown))
        for key in ("value", "status", "camera_pose_source", "camera_pose_diagnostics", "observation_states"):
            self.assertEqual(shown[key], original[key])
        self.assertEqual(response.data, original)

    def test_geometry_error_echo_excludes_injected_identity_before_bounding(self):
        result = self.geometry.execute({**self.request, "kind": "pixel_ray", "pixel": [-1, 2]})
        actual = result["diagnostics"]["input_guidance"]["actual"]
        injected = {"schema_version", "run_id", "episode_id", "env_instance_id", "env_generation"}
        self.assertTrue(injected.isdisjoint(actual))
        self.assertTrue(injected.isdisjoint(actual.get("keys", [])))
        self.assertEqual(result["reason"], "pixel_out_of_bounds")
        self.assertEqual(result["run_id"], IDENTITY["run_id"])

    def test_geometry_display_retains_executable_scene_patch_identity_and_revision(self):
        from auriga.core.presentation.actor_output import project_metadata
        result = self.geometry.execute({**self.request, "kind": "build_scene", "base_scene_version": 0,
                                        "roi": [100, 80, 400, 320]})
        shown = project_metadata(result, "geometry_result")
        self.assertNotIn("library_versions", shown)
        self.assertEqual(shown["value"]["proposal"], result["value"]["proposal"])
        scene = self.scene.apply_patch(shown["value"]["proposal"])
        self.assertEqual(scene["scene_version"], 1)

    def test_public_tracking_keeps_numeric_roi_accepted_by_direct_geometry_owner(self):
        from auriga.runtime.tools.builtins import create_registry
        request = dict(kind="sparse_track", observation_ids=["obs-0", "obs-1"], camera_id="head",
                       static_scene_assumption=True, roi=[100.5, 80.5, 400., 320.])
        registry = create_registry()
        for role in ("actor", "program"):
            registry.validate("geometry_query", dict(request=request), role=role)
        result = self.geometry.execute({**IDENTITY, **request})
        self.assertEqual(result["status"], "supported")
        self.assertGreater(result["value"]["accepted"], 0)

    def test_invalid_geometry_result_keeps_reason_and_supplies_help(self):
        result = self.geometry.execute({**self.request, 'kind': 'pixel_ray', 'pixel': [-1, 2]})
        self.assertEqual(result['status'], 'invalid_input')
        self.assertEqual(result['reason'], 'pixel_out_of_bounds')
        guidance = result['diagnostics']['input_guidance']
        self.assertEqual(guidance['minimal_example'], {'request': {'kind': 'help'}})
        self.assertIn('required fields', guidance['suggestion'])
        self.assertIsNone(result['value'])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.artifacts = Store(self.tmp.name,IDENTITY); self.addCleanup(self.artifacts.close)
        self.scene = SceneStore(Path(self.tmp.name)/"scene",self.artifacts.resolve_artifact,self.artifacts.put_artifact)
        self.obs = recorded_plane(self.artifacts)
        for obs in self.obs: self.scene.import_observation(obs)
        self.geometry = GeometryTools(self.scene)
        self.request = {**IDENTITY,"camera_id":"head","observation_ids":["obs-0","obs-1"],"static_scene_assumption":True}

    def test_rgb_build_heldout_alignment_and_snapshot(self):
        result = self.geometry.execute({**self.request,"kind":"build_scene","base_scene_version":0,"roi":[100,80,400,320]})
        self.assertEqual(result["status"],"conditional",result)
        value = result["value"]
        self.assertGreater(value["geometry_result"]["accepted"],100)
        scene = self.scene.apply_patch(value["proposal"])
        entity = next(iter(scene["entities"].values()))
        self.assertLess(abs(entity["geometry"]["origin_m"][2]-2),0.005)
        self.assertLess(entity["uncertainty"]["fit_rms_m"],0.005)
        projected=self.geometry.execute({**self.request,"kind":"project_geometry","scene_version":1})
        self.assertEqual(projected["status"],"supported",projected)
        self.assertEqual(len(projected["value"]["pixels"]),4)
        hit=self.geometry.execute({**self.request,"kind":"ray_surface","scene_version":1,"entity_id":next(iter(scene["entities"])),"pixel":[320,240]})
        self.assertEqual(hit["status"],"conditional",hit)
        self.assertAlmostEqual(hit["value"]["point_m"][2],2,places=2)
        points = [[0,0,2],[.2,.1,2],[-.2,-.1,2]]
        result = self.geometry.execute({**self.request,"kind":"alignment","observation_ids":["obs-2"],"scene_version":1,"points_m":points,"pixels":[[307.5,240],[357.5,265],[257.5,215]]})
        self.assertEqual(result["value"]["rms_px"],0)
        rejected = self.geometry.execute({**self.request,"kind":"alignment","observation_ids":["obs-0"],"scene_version":1,"points_m":points,"pixels":[[0,0]]*3})
        self.assertIn("heldout",rejected["reason"])
        before = self.scene.snapshot(IDENTITY,1,"obs-1")
        self.assertEqual(before["quality"]["artifact_resolution"],"verified_digest_closed")
        self.assertGreaterEqual(len(before["resolved_artifacts"]),6)
        self.assertEqual(self.scene.read_snapshot(IDENTITY,before["snapshot_id"]),before)
        patch = deepcopy(value["proposal"]); patch.update(base_scene_version=1,observation_ids=["obs-2"])
        patch["operations"] = [{"op":"mark_lost","entity_id":next(iter(scene["entities"])),"source_kind":"observation_inference","reason":"occluded"}]
        self.scene.apply_patch(patch)
        self.assertEqual(digest(before),digest(self.scene.snapshot(IDENTITY,1,"obs-1")))
        patch["base_scene_version"] = 2; patch["observation_ids"] = ["obs-0"]
        with self.assertRaisesRegex(ValueError,"late_observation"):
            self.scene.apply_patch(patch)

    def test_rays_projection_and_degenerate_matrix(self):
        help_result = self.geometry.execute({**IDENTITY, "kind": "help"})
        self.assertIn("pixels_a", help_result["requests"]["triangulate"])
        self.assertIn("pixel", help_result["requests"]["pixel_ray"])
        ray_result = self.geometry.execute({**self.request,"kind":"pixel_ray","pixel":[320,240]})
        self.assertEqual(ray_result["value"]["direction"],[0.,0.,1.])
        camera = self.geometry.camera(IDENTITY,"obs-0","head")
        with self.assertRaisesRegex(ValueError,"pure_rotation"):
            triangulate(camera,camera,[[320,240]],[[310,240]])
        second = self.geometry.camera(IDENTITY,"obs-1","head")
        low = triangulate(camera,second,[[320,240]],[[319.9,240]])
        self.assertEqual(low["point_status"],["insufficient_parallax"])
        wrong = triangulate(camera,second,[[320,240]],[[295,270]])
        self.assertEqual(wrong["point_status"],["inconsistent_correspondence"])
        behind = triangulate(camera,second,[[320,240]],[[345,240]])
        self.assertEqual(behind["point_status"],["negative_depth"])
        unsync = deepcopy(self.obs[0]); unsync["observation_id"]="unsync"; unsync["cameras"][0]["sim_step"]=999
        self.scene.import_observation(unsync)
        result = self.geometry.execute({**self.request,"kind":"pixel_ray","pixel":[1,1],"observation_ids":["unsync"]})
        self.assertEqual(result["reason"],"unsynchronized_observation")
        missing = deepcopy(self.obs[0]); missing["observation_id"]="uncalibrated"; missing["cameras"][0]["calibration_ref"]=None; missing["observation_profile"]="rgb_proprio_v1"; missing["calibration_id"]=None
        self.scene.import_observation(missing)
        result = self.geometry.execute({**self.request,"kind":"pixel_ray","pixel":[1,1],"observation_ids":["uncalibrated"]})
        self.assertEqual(result["reason"],"missing_calibration")
        overlay = self.geometry.execute({**self.request,"kind":"project_points","points_m":[[0,0,2]]})
        self.assertEqual(overlay["value"]["pixels"],[[320.,240.]])
        self.artifacts.resolve_artifact(overlay["value"]["overlay_ref"])

    def test_projection_keeps_robot_measurements_at_the_selected_image_time(self):
        observation = deepcopy(self.obs[0])
        observation['observation_id'] = 'measured-frame'
        observation['observation_profile'] = 'rgb_proprio_calibrated_v1'
        state = dict(sim_step=0, step_domain='control_step', arms=dict(left=dict(
            source_kind='measurement', joint_names=['joint1'], q_rad=[.125], qdot_rad_s=[.25],
            ee_pose=dict(position_m=[.1, .2, .3], quaternion_xyzw=[0., 0., 0., 1.],
                         source_kind='measurement', frame_id='env_origin', tcp_config_id='link6'),
            gripper=dict(position_m=[.02, .02], position_source_kind='measurement',
                         joint_names=['jaw1', 'jaw2'], goal_normalized=1., goal_source_kind='command'))))
        observation['robot_state_ref'] = self.artifacts.put_artifact(canonical(state).encode(), 'robot_state', 'application/json')
        calibration = dict(cameras=dict(head=dict(K=[[500., 0, 320.], [0, 500., 240.], [0, 0, 1.]],
            distortion=[], image_size=[640, 480], T_world_camera=np.eye(4).tolist())))
        observation['cameras'][0]['calibration_ref'] = self.artifacts.put_artifact(canonical(calibration).encode(), 'calibration', 'application/json')
        self.scene.import_observation(observation)
        result = self.geometry.execute(dict(IDENTITY, kind='project_points', observation_id='measured-frame',
                                            camera_id='head', points_m=[[0, 0, 2]]))
        self.assertEqual(result['status'], 'supported', result)
        evidence = result['observation_states'][0]
        self.assertEqual(evidence['observation_id'], 'measured-frame')
        self.assertEqual(evidence['robot_state'], state)
        self.assertEqual(evidence['sim_step'], 0)

    def test_dynamic_updates_dirty_occlusion_and_model_rebuild(self):
        initial=self.geometry.execute({**self.request,"kind":"build_scene","entity_id":"surface","base_scene_version":0})
        self.scene.apply_patch(initial["value"]["proposal"])
        old=self.scene.version(IDENTITY,1)["entities"]["surface"]["geometry"]["origin_m"]
        patch={"schema_version":"0.1",**IDENTITY,"world_kind":"internal","base_scene_version":1,"observation_ids":["obs-2"],"builder_digest":digest("move"),"operations":[{"op":"update_pose","entity_id":"surface","translation_m":[0.1,0,0],"source_kind":"explicit_prior","assumptions":["Synthetic explicit rigid translation fixture."]}]}
        moved=self.scene.apply_patch(patch)
        self.assertAlmostEqual(moved["entities"]["surface"]["geometry"]["origin_m"][0],old[0]+0.1)
        self.assertAlmostEqual(self.scene.version(IDENTITY,1)["entities"]["surface"]["geometry"]["origin_m"][0],old[0])
        self.assertTrue(any("previous_entity_geometry" in item.get("region",{}) for item in moved["unknown_regions"] if isinstance(item.get("region"),dict)))
        for index,operation in enumerate(("mark_dirty","mark_occluded","mark_lost"),start=2):
            patch["base_scene_version"]=index
            patch["operations"]=[{"op":operation,"entity_id":"surface","source_kind":"observation_inference","reason":"synthetic visibility/state fixture"}]
            self.scene.apply_patch(patch)
            result=self.scene.query(IDENTITY,index+1,{"kind":"entity_pose","entity_id":"surface"})
            self.assertEqual(result["status"],"unknown")
        obs=deepcopy(self.obs[2]); obs.update(observation_id="new-calibration",calibration_id="calibration-v2")
        self.scene.import_observation(obs)
        patch.update(base_scene_version=5,observation_ids=["new-calibration"])
        with self.assertRaisesRegex(ValueError,"rebuild required"):
            self.scene.apply_patch(patch)
        patch["rebuild"]=True; patch["operations"]=[{"op":"mark_unknown","source_kind":"observation_inference","region":"all","reason":"calibration changed"}]
        rebuilt=self.scene.apply_patch(patch)
        self.assertEqual(rebuilt["entities"],{})
        self.assertEqual(rebuilt["calibration_id"],"calibration-v2")

    def test_fk_is_authoritative_when_usd_camera_pose_is_stale(self):
        model={"base_link":"base","joints":[{"name":"slide","parent":"base","child":"camera_link","type":"prismatic","axis":[1,0,0],"T_parent_joint":np.eye(4).tolist()}]}
        calibration={"cameras":{"head":{"K":[[500,0,320],[0,500,240],[0,0,1]],"distortion":[],"image_size":[640,480],"link_name":"camera_link","T_link_camera":np.eye(4).tolist(),"T_world_base":np.eye(4).tolist()}}}
        moved=np.eye(4); moved[0,3]=0.1
        sample={"T_world_camera":np.eye(4).tolist(),"T_world_link":moved.tolist(),"T_world_base":np.eye(4).tolist(),"source_kind":"simulator_camera_transform","sim_step":0,"step_domain":"control_step","frame_id":"frame-0"}
        state={"source_kind":"measurement","joint_names":["slide"],"joint_positions_rad":[0.1],"sim_step":0,"step_domain":"control_step","camera_poses":{"head":sample}}
        observation=deepcopy(self.obs[0]); observation.update(observation_id="stale-usd",calibration_id="known-slide")
        for field,value,kind in (("robot_model_ref",model,"robot_model"),("robot_state_ref",state,"robot_state")):
            observation[field]=self.artifacts.put_artifact(canonical(value).encode(),kind,"application/json")
        observation["cameras"][0]["calibration_ref"]=self.artifacts.put_artifact(canonical(calibration).encode(),"calibration","application/json")
        self.scene.import_observation(observation)
        result=self.geometry.execute({**self.request,"kind":"pixel_ray","observation_ids":["stale-usd"],"pixel":[320,240]})
        self.assertEqual(result["value"]["origin_m"],[0.1,0,0])
        self.assertEqual(result["camera_pose_source"],"robot_kinematics")
        self.assertAlmostEqual(result["camera_pose_diagnostics"]["reported_camera_translation_residual_m"],0.1)
        self.assertEqual(result["camera_pose_diagnostics"]["fk_vs_measured_link_translation_m"],0)

    def test_seeded_rgb_association_rejects_implausible_motion(self):
        first=self.geometry.execute({**self.request,"kind":"build_scene","entity_id":"seeded-surface","base_scene_version":0})
        self.scene.apply_patch(first["value"]["proposal"])
        updated=self.geometry.execute({**self.request,"kind":"build_scene","entity_id":"seeded-surface","base_scene_version":1,"observation_ids":["obs-0","obs-2"]})
        operation=updated["value"]["proposal"]["operations"][0]
        self.assertEqual(operation["entity_id"],"seeded-surface")
        self.assertEqual(operation["op"],"upsert_geometry")
        self.assertEqual(operation["association"]["kind"],"appearance_and_geometry_gate")
        impossible=self.geometry.execute({**self.request,"kind":"build_scene","entity_id":"seeded-surface","base_scene_version":1,"observation_ids":["obs-0","obs-2"],"max_displacement_m":0.000001})
        self.assertEqual(impossible["value"]["proposal"]["operations"][0]["op"],"mark_lost")
        marked=self.scene.apply_patch(impossible["value"]["proposal"])
        self.assertEqual(marked["entities"]["seeded-surface"]["state"],"lost")

    def test_privileged_json_cannot_hide_in_actor_artifact(self):
        observation=deepcopy(self.obs[0]); observation["observation_id"]="forbidden-payload"
        observation["robot_state_ref"]=self.artifacts.put_artifact(canonical({"object_poses":[{"hidden":True}]}).encode(),"robot_state","application/json")
        with self.assertRaisesRegex(ValueError,"forbidden_observation_field"):
            self.scene.import_observation(observation)
        observation=deepcopy(self.obs[0]); observation["observation_id"]="internal-rgb"
        observation["cameras"][0]["rgb_ref"]=self.artifacts.put_artifact(b"internal","geometry_overlay","image/png","internal")
        with self.assertRaisesRegex(ValueError,"permitted_actor"):
            self.scene.import_observation(observation)

    def test_digest_fk_and_unconstrained_box(self):
        metadata,path = self.artifacts.artifact(self.obs[0]["cameras"][0]["rgb_ref"])
        path.write_bytes(b"corrupt")
        with self.assertRaises(ValueError): self.scene.closure(self.obs[0])
        model = {"base_link":"base","joints":[{"name":"j","parent":"base","child":"tip","type":"revolute","axis":[0,0,1],"T_parent_joint":np.eye(4).tolist()}]}
        computed = fk(model,{"joint_names":["j"],"joint_positions_rad":[np.pi/2],"source_kind":"measurement"})
        np.testing.assert_allclose(computed["tip"][:3,:3]@[1,0,0],[0,1,0],atol=1e-12)
        with self.assertRaisesRegex(ValueError,"thickness_unobserved"):
            fit_geometry([[i%3,i//3,1] for i in range(6)],"box")

if __name__=="__main__": unittest.main()
