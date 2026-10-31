"""RGB-D storage and opt-in trust boundary, without launching a simulator."""
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
import zipfile

import numpy as np

from auriga.core.contracts import validate_observation
from auriga.perception.depth.depth_capture import validate_depth_archive
from auriga.core.observation_policy import (ACTOR_PROFILE, LEGACY_ACTOR_PROFILE, DEPTH_ACTOR_PROFILE,
    DEPTH_FIELDS, DEPTH_ACTOR_POLICY, actor_policy, allows_depth, has_calibration,
    reject_privileged, require_actor_observation)
from auriga.robot.backends.robodojo.robot_worker import RoboDojoAdapter, save_depth_capture


class DepthCaptureTests(unittest.TestCase):
    def test_native_mapping_covers_wrist_cameras_added_after_sensor_configuration(self):
        root = Path(os.environ.get("AURIGA_ROBODOJO_ROOT", Path.home() / "code/RoboDojo")).expanduser()
        python = Path(os.environ.get("AURIGA_ROBODOJO_PYTHON",
            Path.home() / "miniforge3/envs/RoboDojo/bin/python")).expanduser()
        if not python.is_file() or not (root / "env/observation_manager/obs_manager.py").is_file():
            self.skipTest("installed RoboDojo configuration and Python required; no simulator is launched")
        code = r'''
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import numpy as np
root, project = map(Path, sys.argv[1:])
sys.path[:0] = [str(project), str(root)]
from auriga.robot.backends.robodojo.robot_worker import native_eval_config, configure_depth_capture
from auriga.core.observation_policy import DEPTH_ACTOR_PROFILE
from env.observation_manager.obs_manager import ObsManager
config = native_eval_config(root, "general_pickup", 0, 0, DEPTH_ACTOR_PROFILE)
names = ["cam_head", "cam_left_wrist", "cam_right_wrist", "late_camera"]
assert "cam_left_wrist" not in config.camera and "cam_right_wrist" not in config.camera
manager = ObsManager(config.eval_cfg.observation, 1, .004, "general_pickup", {})
manager.instruction, manager.robot_manager = ["probe"], None
manager.camera_manager = SimpleNamespace(num_cams=len(names), camera_names=[names])
data = []
for name in names:
    capture = config.camera.annotator.get(name, config.camera.annotator.common)
    kinds = [value.type for key, value in capture.items() if key != "enabled"]
    assert "distance_to_image_plane" in kinds, (name, kinds)
    data.append({kind: [{"data": np.ones((2, 3, 4 if kind == "rgb" else 1),
        dtype=np.uint8 if kind == "rgb" else np.float32), "info": {}}] for kind in kinds})
manager.capture_manager = SimpleNamespace(step=lambda **kwargs: data)
observed = manager.get_obs([0])[0]["vision"]
assert all(set(value) == {"color", "shape", "depth"} for value in observed.values())
assert all(value["depth"].shape == (2, 3) and value["depth"].dtype == np.float32 for value in observed.values())
configure_depth_capture(config)
assert not config.eval_cfg.observation.vision.depth
assert all(not any(key != "enabled" and value.type in ("depth", "distance_to_image_plane", "distance_to_camera")
    for key, value in capture.items()) for capture in config.camera.annotator.values())
print(json.dumps({"cameras": list(observed), "isaac_imported": any(
    name == "isaacsim" or name.startswith(("isaacsim.", "isaaclab.")) for name in sys.modules)}))
'''
        project = Path(__file__).resolve().parents[1]
        result = subprocess.run([str(python), "-c", code, str(root), str(project)],
            cwd=project, text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        evidence = json.loads(result.stdout)
        self.assertEqual(evidence["cameras"], ["cam_head", "cam_left_wrist", "cam_right_wrist", "late_camera"])
        self.assertFalse(evidence["isaac_imported"])

    def metadata(self):
        return dict(camera_id="head", frame_id="frame", rgb_ref="rgb", width=3, height=2,
            calibration_ref="calibration", pose_sample_ref="pose", depth_ref="npz", depth_kind="z",
            depth_unit="m", depth_clip_m=[0.005, 10.0], depth_shape=[2, 3])

    def observation(self):
        return dict(schema_version="0.1", run_id="run", episode_id="ep", env_instance_id="env",
            env_generation=1, world_kind="observed", observation_id="obs", robot_state_ref="state",
            observation_profile=DEPTH_ACTOR_PROFILE, calibration_id="calibration", cameras=[self.metadata()])

    def test_default_profiles_reject_every_depth_field_at_any_nesting(self):
        self.assertTrue(has_calibration(ACTOR_PROFILE))
        self.assertTrue(has_calibration(DEPTH_ACTOR_PROFILE))
        self.assertFalse(has_calibration(LEGACY_ACTOR_PROFILE))
        self.assertEqual(actor_policy(DEPTH_ACTOR_PROFILE), DEPTH_ACTOR_POLICY)
        for profile in (ACTOR_PROFILE, LEGACY_ACTOR_PROFILE):
            self.assertFalse(allows_depth(profile))
            for field in DEPTH_FIELDS:
                with self.subTest(profile=profile, field=field):
                    with self.assertRaises(ValueError):
                        reject_privileged({"nested": [{field: None}]}, profile)
                    observation = self.observation()
                    observation.update(observation_profile=profile, cameras=[])
                    observation["nested"] = [{field: None}]
                    with self.assertRaises(ValueError):
                        validate_observation(observation)

    def test_depth_profile_still_rejects_arrays_task_contact_and_evaluator_truth(self):
        self.assertTrue(allows_depth(DEPTH_ACTOR_PROFILE))
        validate_observation(self.observation())
        require_actor_observation(self.observation(), DEPTH_ACTOR_PROFILE)
        for field in ("depth", "approximate_depth", "object_poses", "task_state", "reward", "score",
                      "contact_forces", "contact_points", "object_contacts", "evaluator_state", "native_usd_pose_diagnostic"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                reject_privileged({"nested": [{field: {}}]}, DEPTH_ACTOR_PROFILE)

    def test_depth_metadata_checks_units_shape_clip_and_missing_references(self):
        for change in ({"depth_kind": "range"}, {"depth_unit": "mm"}, {"depth_shape": [3, 2]},
                       {"depth_shape": [True, 3]}, {"width": True}, {"depth_clip_m": [10, 1]},
                       {"depth_clip_m": [0, float("inf")]}, {"depth_ref": ""}, {"depth_path": "/tmp/raw"}):
            observation = self.observation()
            observation["cameras"][0].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_observation(observation)
        for key in ("depth_ref", "depth_kind", "depth_unit", "depth_clip_m", "depth_shape"):
            observation = self.observation()
            observation["cameras"][0].pop(key)
            with self.subTest(missing=key), self.assertRaises(ValueError):
                require_actor_observation(observation, DEPTH_ACTOR_PROFILE)

    def test_npz_preserves_metric_float_depth_and_explicit_validity(self):
        depth = np.array([[np.nan, np.inf, -1, 0], [.004, .005, 10, 11]], dtype=np.float32)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "capture.npz"
            metadata = save_depth_capture(path, depth[..., None], (2, 4, 3), [.005, 10])
            self.assertEqual(metadata["depth_shape"], [2, 4])
            self.assertEqual(metadata["depth_kind"], "z")
            with np.load(path, allow_pickle=False) as saved:
                self.assertEqual(set(saved.files), {"depth", "valid"})
                self.assertEqual(saved["depth"].dtype, np.float32)
                self.assertEqual(saved["valid"].dtype, np.bool_)
                np.testing.assert_array_equal(saved["depth"], depth)
                np.testing.assert_array_equal(saved["valid"], [[False] * 4, [False, True, True, False]])
            checked_depth, checked_valid = validate_depth_archive(path.read_bytes(), dict(metadata, width=4, height=2))
            np.testing.assert_array_equal(checked_depth, depth)
            np.testing.assert_array_equal(checked_valid, [[False] * 4, [False, True, True, False]])
            wrong = Path(temporary) / "wrong.npz"
            with self.assertRaisesRegex(ValueError, "shape_mismatch"):
                save_depth_capture(wrong, depth, (4, 2, 3), [.005, 10])
            self.assertFalse(wrong.exists())

    def test_archive_rejects_bad_dtype_shape_mask_or_extra_members(self):
        depth = np.ones((2, 3), np.float32)
        valid = np.ones((2, 3), np.bool_)
        for arrays in (dict(depth=depth.astype(np.float64), valid=valid),
                       dict(depth=depth, valid=valid.astype(np.uint8)),
                       dict(depth=depth.T, valid=valid),
                       dict(depth=depth, valid=np.zeros_like(valid)),
                       dict(depth=depth.astype(object), valid=valid),
                       dict(depth=depth, valid=valid, hidden=np.zeros(1))):
            stream = io.BytesIO()
            np.savez(stream, **arrays)
            with self.subTest(keys=list(arrays), dtype=arrays["depth"].dtype), self.assertRaises(ValueError):
                validate_depth_archive(stream.getvalue(), self.metadata())

    def test_archive_bounds_uncompressed_size_and_rejects_malformed_zip(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("depth.npy", b"0" * 20000)
            archive.writestr("valid.npy", b"0" * 100)
        with self.assertRaisesRegex(ValueError, "member_size"):
            validate_depth_archive(stream.getvalue(), self.metadata())
        with self.assertRaisesRegex(ValueError, "invalid_depth_archive"):
            validate_depth_archive(b"not a zip", self.metadata())
        with self.assertRaisesRegex(ValueError, "archive_size"):
            validate_depth_archive(b"x" * 20000, self.metadata())

    def test_worker_exports_same_capture_only_for_explicit_depth_profile(self):
        for profile in (ACTOR_PROFILE, LEGACY_ACTOR_PROFILE, DEPTH_ACTOR_PROFILE):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as temporary:
                worker = RoboDojoAdapter.__new__(RoboDojoAdapter)
                worker.np, worker.directory = np, Path(temporary)
                worker.observation_profile, worker.control_step = profile, 7
                worker.recorded_frames, worker.recording, worker.legacy_png_ipc = [], False, True
                worker.camera_calibration, worker.versions = None, {}
                events = []
                raw = {"vision": {"head": {"color": np.zeros((2, 3, 3), np.uint8),
                                            "depth": np.ones((2, 3), np.float32)}}}
                worker.obs = SimpleNamespace(render_for_capture=lambda: events.append("render"),
                    get_obs=lambda ids: (events.append("read") or {0: raw}))
                worker.env = SimpleNamespace(dt=.04, sim=SimpleNamespace(sim=SimpleNamespace(current_time=.28)),
                    camera_manager=SimpleNamespace(camera_names=[["head"]],
                        cameras=[[SimpleNamespace(get_clipping_range=lambda: [.005, 10.])]]))
                worker.state = lambda: dict(arms={}, frame_id="env_origin", tcp_config_id="test",
                                           sim_step=7, step_domain="adapter_control_step")
                observation = worker.observe()
                self.assertEqual(events, ["render", "read"])
                camera = observation["cameras"][0]
                self.assertEqual(camera["frame_id"], observation["raw_observation_id"] + "-head")
                self.assertEqual(observation["sim_step"], 7)
                if allows_depth(profile):
                    self.assertEqual(camera["depth_shape"], [camera["height"], camera["width"]])
                    require_actor_observation(observation, profile)
                    self.assertTrue(Path(camera["depth_path"]).is_file())
                    raw["vision"]["head"].pop("depth")
                    with self.assertRaisesRegex(ValueError, "requires_native_depth"):
                        worker.observe()
                else:
                    self.assertFalse(DEPTH_FIELDS.intersection(camera))
                    self.assertEqual(list(Path(temporary).rglob("*.npz")), [])


if __name__ == "__main__":
    unittest.main()
