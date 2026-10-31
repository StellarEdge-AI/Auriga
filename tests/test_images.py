"""Real codecs and FFmpeg, synthetic RGB; no simulator/model/robot execution."""

import base64
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import cv2
import numpy as np
from PIL import Image

from auriga.runtime.harness import Harness
from auriga.runtime.policy.action_policy import load_policy_observation
from auriga.core.storage.images import _cached_image_url, camera_path, encode_image, image_settings, image_url, store_image_settings
from auriga.robot.backends.robodojo.robot_worker import RoboDojoAdapter
from auriga.core.storage.store import Store
from test_harness import FixtureRobot
from scripts.audit_run import audit


class ImagePipelineTest(unittest.TestCase):
    def test_transport_cache_reuses_bytes_and_respects_settings(self):
        _cached_image_url.cache_clear()
        self.addCleanup(_cached_image_url.cache_clear)
        settings = image_settings({})
        data, _ = encode_image(Image.new("RGB", (64, 48), "red"), settings, storage=True)
        with patch("auriga.core.storage.images.encode_image", wraps=encode_image) as encode:
            first, metadata = image_url(data, settings)
            metadata["quality"] = -1
            again, metadata = image_url(data, settings)
            self.assertEqual(first, again)
            self.assertEqual(metadata["quality"], 85)
            self.assertEqual(encode.call_count, 1)
            image_url(data, {**settings, "webp_quality": 60})
            self.assertEqual(encode.call_count, 2)
            large, _ = encode_image(Image.new("RGB", (641, 480), "red"), settings, storage=True)
            image_url(large, settings)
            image_url(large, settings)
            self.assertEqual(encode.call_count, 4)
        _cached_image_url.cache_clear()
        with patch("auriga.core.storage.images.features.check", return_value=False):
            self.assertIn("fallback_reason", image_url(data, settings)[1])
        self.assertNotIn("fallback_reason", image_url(data, settings)[1])

    def fixture(self, directory):
        root = Path(directory) / "run"
        store = Store(root, dict(run_id="images", episode_id="ep", env_instance_id="env", env_generation=1))
        self.addCleanup(store.close)
        robot = FixtureRobot(directory)
        adapter = object.__new__(RoboDojoAdapter)
        adapter.directory = root / "private/robot"
        adapter.np, adapter.control_step = np, 0
        adapter.observation_profile = "rgb_proprio_v1"
        adapter.recording, adapter.recorded_frames = False, []
        adapter.image_settings = image_settings({})
        adapter.camera_calibration, adapter.versions = None, {}
        def state():
            value = robot.observe()["robot_state"]
            arm = value["arms"]["left"]
            arm.update(q_rad=[0.0] * 6, source_kind="measurement", gripper=dict(
                command_source_kind="command", command_normalized=1.0))
            value["arms"]["right"] = deepcopy(arm)
            value.update(sim_step=adapter.control_step, step_domain="adapter_control_step")
            return value
        adapter.state = state
        adapter.env = SimpleNamespace(dt=.004, sim=SimpleNamespace(sim=SimpleNamespace(current_time=0)))
        rgb = np.random.default_rng(24).integers(0, 256, (48, 64, 3), dtype=np.uint8)
        adapter.obs = SimpleNamespace(render_for_capture=lambda: None, get_obs=lambda _: [dict(
            instruction="Synthetic codec fixture; do not execute a robot task.",
            vision={name: dict(color=rgb) for name in ("cam_head", "cam_left_wrist", "cam_right_wrist")})])
        return store, adapter

    def test_capture_archive_delivery_crop_and_video(self):
        with tempfile.TemporaryDirectory() as directory:
            store, adapter = self.fixture(directory)
            harness = Harness(store, adapter)
            adapter.record()
            raw = adapter.observe()
            bundle = harness.publish_observation(raw)
            policy, _ = load_policy_observation(store.root, bundle["observation_id"])
            self.assertEqual(policy["images"]["cam_high"].shape, (3, 48, 64))
            self.assertEqual(policy["images"]["cam_high"].dtype, np.uint8)
            self.assertEqual([c["camera_id"] for c in bundle["cameras"]], ["cam_head", "cam_left_wrist", "cam_right_wrist"])
            for source, camera in zip(raw["cameras"], bundle["cameras"]):
                info, data = store.resolve_artifact(camera["rgb_ref"])
                self.assertEqual(info["mime_type"], "image/jpeg")
                self.assertEqual(data[:3], b"\xff\xd8\xff")
                self.assertEqual(camera["image_encoding"]["quality"], 95)
                self.assertEqual(data, camera_path(source).read_bytes())  # No second JPEG generation.
                self.assertTrue(os.path.samefile(camera_path(source), store.artifact(camera["rgb_ref"])[1]))
                self.assertEqual(cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR).shape, (48, 64, 3))
            contents = harness.observation_content(bundle, {})
            delivery = store.records("delivery")[-1]
            for content, delivered in zip(contents[1:], delivery["images"]):
                self.assertTrue(content["imageUrl"].startswith("data:image/webp;base64,"))
                data = base64.b64decode(content["imageUrl"].split(",", 1)[1])
                self.assertEqual(Image.open(io.BytesIO(data)).size, (64, 48))
                self.assertEqual(hashlib.sha256(data).hexdigest(), delivered["sha256"])
                self.assertEqual(store.artifact(delivered["rgb_ref"])[0]["sha256"], delivered["source_sha256"])
            crop = harness.observation_content(bundle, {}, "cam_head", [3, 4, 10, 12], 2)
            view = json.loads(crop[0]["text"])["image_view"]
            self.assertEqual(view["pixel_transform"], [[2, 0, -5.5], [0, 2, -7.5], [0, 0, 1]])
            self.assertTrue(view["rgb_available"])
            self.assertTrue(view["original_rgb_available"])
            crop_delivery = store.records("delivery")[-1]["images"][0]
            self.assertEqual(store.artifact(crop_delivery["rgb_ref"])[0]["mime_type"], "image/jpeg")
            self.assertEqual(Image.open(io.BytesIO(base64.b64decode(crop[1]["imageUrl"].split(",", 1)[1]))).size, (20, 24))
            adapter.control_step = 50
            adapter.observe(video_only=True)
            adapter.control_step = 100
            end = adapter.observe()
            harness.publish_observation(end)
            temporary = [camera_path(c) for f in adapter.recorded_frames if f["video_only"] for c in f["cameras"]]
            with patch("auriga.robot.backends.robodojo.robot_worker.subprocess.Popen", side_effect=OSError("fixture_encoder_failure")):
                with self.assertRaisesRegex(RuntimeError, "video encoding failed"):
                    adapter.save_video()
            self.assertTrue(all(p.exists() for p in temporary))  # Failed export loses no evidence.
            result = adapter.save_video()
            self.assertEqual(len(result["videos"]), 3)
            self.assertEqual(result["temporary_frame_cleanup_errors"], [])
            self.assertTrue(all(not p.exists() for p in temporary))
            self.assertTrue(all(camera_path(c).exists() for c in raw["cameras"] + end["cameras"]))
            for video in result["videos"]:
                self.assertEqual(video["frames"], 3)
                self.assertEqual([f["sim_step"] for f in video["frame_mapping"]], [0, 50, 100])
                self.assertEqual([f["source_retained"] for f in video["frame_mapping"]], [False, False, True])
                reader = cv2.VideoCapture(video["video_path"])
                self.assertTrue(reader.read()[0])
                reader.release()
            self.assertEqual(adapter.save_video(), result)  # Safe repeated export after cleanup.
            self.assertTrue(harness.observation_content(bundle, {}))
            # Both legacy PNG evidence and new encoded delivery hashes remain auditable.
            legacy = (Path(directory) / "synthetic-rgb.png").read_bytes()
            reference = store.put_artifact(legacy, "rgb", "image/png")
            store.save("delivery", "legacy", dict(images=[dict(rgb_ref=reference, sha256=hashlib.sha256(legacy).hexdigest())]))
            store.raw_codex("send", dict(result=dict(contentItems=contents + crop + [dict(type="inputImage",
                imageUrl="data:image/png;base64," + base64.b64encode(legacy).decode())])))
            (store.root / "manifest.json").write_text(json.dumps(dict(run_id="images", thread_id="fixture",
                terminal_session_id="fixture", data_origin="synthetic_fixture", model="gpt-6-astra")))
            self.assertEqual(audit(store.root, profile="native")["failures"], [
                "not a real RoboDojo run", "no EE action", "no completed or validly native-terminal-interrupted turn"])
            # A worker spawned by an already-running pre-upgrade parent preserves
            # its PNG-only wire contract; a new Harness also accepts this format.
            adapter.legacy_png_ipc = True
            old_parent = adapter.observe()
            self.assertIn("png_path", old_parent["cameras"][0])
            with Image.open(camera_path(old_parent["cameras"][0])) as legacy_image:
                self.assertEqual(legacy_image.format, "PNG")
            migrated = harness.publish_observation(old_parent)
            self.assertEqual(store.artifact(migrated["cameras"][0]["rgb_ref"])[0]["mime_type"], "image/jpeg")

    def test_image_reuse_default_ablation_and_legacy_settings_are_frozen(self):
        self.assertTrue(image_settings({})["reuse_exact_images"])
        with tempfile.TemporaryDirectory() as directory:
            store, _ = self.fixture(directory)
            with patch.dict(os.environ, {"AURIGA_IMAGE_SETTINGS": '{"reuse_exact_images":false}'}):
                settings = store_image_settings(store)
            self.assertFalse(settings["reuse_exact_images"])
            with patch.dict(os.environ, {"AURIGA_IMAGE_SETTINGS": '{"reuse_exact_images":true}'}):
                self.assertEqual(store_image_settings(store), settings)
            self.assertEqual(store.read_state("runtime", "image_settings"), settings)
            legacy = {key: value for key, value in settings.items() if key != "reuse_exact_images"}
            store.set_state("runtime", "image_settings", legacy)
            self.assertFalse(store_image_settings(store)["reuse_exact_images"])
            self.assertFalse(store.read_state("runtime", "image_settings")["reuse_exact_images"])
            # A run frozen before the image-count trim keeps byte-only trimming.
            self.assertEqual(settings["trim_max_inline_images"], 60)
            store.set_state("runtime", "image_settings",
                            {key: value for key, value in settings.items() if key != "trim_max_inline_images"})
            self.assertEqual(store_image_settings(store)["trim_max_inline_images"], 0)
            self.assertEqual(store.read_state("runtime", "image_settings")["trim_max_inline_images"], 0)

    def test_settings_freeze_fallback_and_invalid_inputs(self):
        rgb = Image.new("RGB", (64, 48), (250, 30, 15))
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "images.json"
            config.write_text(json.dumps(dict(transport_format="jpeg", jpeg_quality=95, storage_jpeg_quality=95)))
            with patch.dict(os.environ, {"AURIGA_IMAGE_CONFIG": str(config)}):
                store, adapter = self.fixture(directory)
                harness = Harness(store, adapter)
                config.write_text(json.dumps(dict(transport_format="webp", webp_quality=30)))
                self.assertEqual(Harness(store, adapter).image_settings, harness.image_settings)
                self.assertEqual(adapter.image_settings["transport_format"], "jpeg")
                data, encoding = encode_image(rgb, harness.image_settings)
                self.assertEqual((encoding["format"], encoding["quality"]), ("jpeg", 95))
                self.assertTrue(data.startswith(b"\xff\xd8\xff"))
        for value in ({"jpeg_quality": True}, {"storage_jpeg_quality": 101}, {"webp_method": -1},
                      {"transport_format": "gif"}, {"unknown": 1}, {"reuse_exact_images": 1},
                      {"reuse_exact_images": "false"}, {"reuse_exact_images": None}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                image_settings(value)
        settings = image_settings({})
        with patch("auriga.core.storage.images.features.check", return_value=False):
            data, metadata = encode_image(rgb, settings)
            self.assertEqual((metadata["format"], metadata["quality"]), ("jpeg", 95))
            self.assertIn("fallback_reason", metadata)
        original_save = Image.Image.save
        def save(image, target, **kwargs):
            if kwargs.get("format") == "WEBP":
                raise OSError("fixture_encoder_error")
            return original_save(image, target, **kwargs)
        with patch.object(Image.Image, "save", save):
            url, metadata = image_url(data, settings)
            self.assertTrue(url.startswith("data:image/jpeg;base64,"))
            self.assertEqual(metadata["quality"], 95)
        with self.assertRaises(OSError):
            encode_image(b"invalid-image", settings)
