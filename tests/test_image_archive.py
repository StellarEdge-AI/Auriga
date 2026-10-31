"""Offline compaction with real codecs and HTTP; no model or robot execution."""
import base64
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import ClientSession, web
import numpy as np
from PIL import Image

from auriga.runtime.policy.action_policy import load_policy_observation
from auriga.apps.library.experiments import allocate_run, atomic_json, catalog_id
from auriga.runtime.harness import Harness
from auriga.core.storage.image_archive import compact_images
from auriga.core.storage.images import camera_path
from auriga.apps.library.service import Library
from auriga.perception.visual_feedback import comparison_inputs
from scripts.compact_observation_images import compact_run
from scripts.audit_run import audit
import test_images as image_fixtures


def fixture(test, directory, count=5):
    store, adapter = image_fixtures.ImagePipelineTest.fixture(test, directory)
    harness = Harness(store, adapter)
    texture = np.random.default_rng(8).integers(0, 256, (96, 128, 3), dtype=np.uint8)
    observations, captures = [], []
    for step in range(count):
        cameras = {}
        for number, name in enumerate(("cam_head", "cam_left_wrist", "cam_right_wrist")):
            rgb = texture.copy()
            rgb[:32, step * 3:step * 3 + 20] = (step * 8, 50 * number, 200)
            cameras[name] = dict(color=rgb)
        adapter.control_step = step
        adapter.obs.get_obs = lambda _, cameras=cameras: [dict(instruction="Synthetic archive fixture", vision=cameras)]
        raw = adapter.observe()
        captures.extend(camera_path(c) for c in raw["cameras"])
        observations.append(harness.publish_observation(raw))
    atomic_json(store.root / "manifest.json", store.identity)
    atomic_json(store.root / "runtime.json", dict(status="completed", finished_at="fixture", active_action=None))
    return store, harness, observations, captures


class ImageArchiveTest(unittest.TestCase):
    def test_mapping_history_policy_and_idempotence(self):
        with tempfile.TemporaryDirectory() as directory:
            store, harness, observations, captures = fixture(self, directory, count=18)
            originals = {c["rgb_ref"]: store.resolve_artifact(c["rgb_ref"])[1]
                         for obs in observations for c in obs["cameras"]}
            # One content-addressed blob may have multiple RGB metadata aliases.
            first = observations[0]["cameras"][0]["rgb_ref"]
            alias = store.put_artifact(originals[first], "rgb", "image/x-fixture-jpeg")
            delivered = harness.observation_content(observations[0], {})
            store.raw_codex("send", dict(result=dict(contentItems=delivered)))
            atomic_json(store.root / "manifest.json", {**store.identity, "thread_id": "fixture",
                "terminal_session_id": "fixture", "model": "gpt-6-astra", "data_origin": "synthetic_fixture"})
            result = compact_run(store.root)
            self.assertEqual(result["originals_removed"], len(originals))
            self.assertLess(result["archive_bytes"], result["source_bytes"])
            self.assertEqual(result["cleanup_errors"], [])
            self.assertTrue(all(s["frames"] <= 16 for s in result["segments"]))
            self.assertTrue(all(not p.exists() for p in captures))
            self.assertEqual(store.records("observation"), observations)
            for obs in observations:
                for camera in obs["cameras"]:
                    ref = camera["rgb_ref"]
                    with self.assertRaises(FileNotFoundError):
                        store.resolve_artifact(ref)
                    info, data = store.resolve_image(ref)
                    self.assertEqual(info["sha256"], hashlib.sha256(data).hexdigest())
                    self.assertEqual(info["reconstruction"]["source_sha256"], hashlib.sha256(originals[ref]).hexdigest())
                    with Image.open(io.BytesIO(data)) as decoded:
                        self.assertEqual(decoded.size, (128, 96))
                        before = np.array(Image.open(io.BytesIO(originals[ref]))).astype(float)
                        self.assertLess(np.abs(np.array(decoded).astype(float) - before).mean(), 15)
            self.assertEqual(store.resolve_image(first)[1], store.resolve_image(alias)[1])
            crop = harness.observation_content(observations[0], {}, "cam_head", [3, 4, 10, 12], 2)
            meta = json.loads(crop[0]["text"])
            self.assertTrue(meta["bundle"]["cameras"][0]["image_reconstruction"]["lossy"])
            self.assertEqual(meta["image_view"]["pixel_transform"], [[2, 0, -5.5], [0, 2, -7.5], [0, 0, 1]])
            self.assertEqual(Image.open(io.BytesIO(base64.b64decode(crop[1]["imageUrl"].split(",")[1]))).size, (20, 24))
            _, sources = comparison_inputs(store, store.identity,
                before_observation_id=observations[0]["observation_id"], after_observation_id=observations[1]["observation_id"],
                camera_id="cam_head", roi=[10, 10, 20, 20])
            self.assertTrue(all(s["reconstruction"]["lossy"] for s in sources))
            policy, evidence = load_policy_observation(store.root, observations[0]["observation_id"])
            self.assertEqual(policy["images"]["cam_high"].shape, (3, 96, 128))
            self.assertTrue(all(s["reconstruction"]["lossy"] for s in evidence["images"]))
            self.assertEqual(compact_run(store.root), result)
            self.assertTrue(any("exact source-byte audit incomplete" in failure for failure in audit(store.root, profile="native")["failures"]))

    def test_failures_and_scope_do_not_silently_lose_originals(self):
        with tempfile.TemporaryDirectory() as directory:
            store, _, observations, captures = fixture(self, directory)
            ref = observations[0]["cameras"][0]["rgb_ref"]
            original = store.resolve_artifact(ref)[1]
            protected_ref = observations[0]["cameras"][1]["rgb_ref"]
            protected_bytes = store.resolve_artifact(protected_ref)[1]
            private = store.put_artifact(protected_bytes, "private_diagnostic", "image/jpeg", "observer")
            with patch("auriga.core.storage.image_archive.frame_png", side_effect=ValueError("fixture_decode_failure")):
                with self.assertRaisesRegex(ValueError, "fixture_decode_failure"):
                    compact_images(store)
            self.assertEqual(store.resolve_artifact(ref)[1], original)
            self.assertTrue(all(p.exists() for p in captures))
            # Simulate a cleanup failure after verified mappings were persisted.
            unlink = Path.unlink
            def interrupt(path, *args, **kwargs):
                if path.parent == store.blobs:
                    raise OSError("fixture_cleanup_failure")
                return unlink(path, *args, **kwargs)
            with patch.object(Path, "unlink", interrupt):
                self.assertTrue(compact_images(store)["cleanup_errors"])
            self.assertEqual(store.resolve_artifact(ref)[1], original)
            self.assertFalse(compact_images(store)["cleanup_errors"])
            self.assertEqual(store.resolve_artifact(protected_ref)[1], protected_bytes)
            self.assertEqual(store.resolve_artifact(private, ("observer",))[1], protected_bytes)
            with self.assertRaisesRegex(ValueError, "scope_denied"):
                store.resolve_image(private)
            mapping = store.read_state("image_archive", ref.removeprefix("artifact:"))
            for changed, error in ((dict(frame_index=16), "invalid_archive_frame_index"),
                    (dict(reconstructed_sha256="bad"), "decode_digest_mismatch"),
                    (dict(video_ref=private), "video_kind_mismatch")):
                store.set_state("image_archive", ref.removeprefix("artifact:"), {**mapping, **changed})
                with self.assertRaisesRegex(ValueError, error):
                    store.resolve_image(ref)
            store.set_state("image_archive", ref.removeprefix("artifact:"), mapping)
            video_path = store.artifact(mapping["video_ref"])[1]
            video_path.write_bytes(b"corrupt")
            with self.assertRaisesRegex(ValueError, "artifact_digest_mismatch"):
                store.resolve_image(ref)

    def test_cli_requires_finished_process_and_exclusive_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "requires_existing_store"):
                compact_run(Path(directory) / "absent")
            self.assertFalse((Path(directory) / "absent").exists())
            store, _, observations, _ = fixture(self, directory, count=1)
            ref = observations[0]["cameras"][0]["rgb_ref"]
            for runtime in (dict(status="running"), dict(status="completed", finished_at="x", active_action="busy"),
                            dict(status="completed", finished_at="x", pid=os.getpid())):
                atomic_json(store.root / "runtime.json", runtime)
                with self.assertRaisesRegex(ValueError, "image_compaction_requires"):
                    compact_run(store.root)
            atomic_json(store.root / "runtime.json", dict(status="completed", finished_at="x"))
            with (store.root / "gateway.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(BlockingIOError):
                    compact_run(store.root)
            self.assertTrue(store.resolve_artifact(ref)[1])


class ImageArchiveHTTPTest(unittest.IsolatedAsyncioTestCase):
    async def test_replay_image_is_explicit_and_video_range_survives(self):
        with tempfile.TemporaryDirectory() as directory:
            run = allocate_run(directory, category="synthetic", task="image-archive", run_id="run")
            store, _, observations, _ = fixture(self, run.parent)
            result = compact_run(store.root)
            runner = web.AppRunner(Library(directory).app(), shutdown_timeout=.2)
            await runner.setup()
            try:
                site = web.TCPSite(runner, "127.0.0.1", 0)
                await site.start()
                base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/runs/{catalog_id(run.relative_to(directory))}/v1"
                ref = observations[0]["cameras"][0]["rgb_ref"].removeprefix("artifact:")
                async with ClientSession() as client:
                    async with client.get(base + "/artifacts/" + ref) as response:
                        self.assertEqual(response.status, 404)
                    async with client.get(base + "/artifacts/" + ref + "?view=image") as response:
                        self.assertEqual(response.status, 200)
                        data = await response.read()
                        self.assertEqual(response.headers["Content-Type"], "image/png")
                        self.assertEqual(response.headers["X-Image-Reconstructed"], "video")
                        self.assertEqual(response.headers["X-Artifact-SHA256"], hashlib.sha256(data).hexdigest())
                        self.assertEqual(response.headers["X-Source-SHA256"], store.artifact(ref)[0]["sha256"])
                    async with client.get(base + "/observations") as response:
                        self.assertTrue((await response.json())["observations"][0]["cameras"][0]["image_reconstruction"]["lossy"])
                    video = result["segments"][0]["video_ref"].removeprefix("artifact:")
                    async with client.get(base + "/artifacts/" + video, headers={"Range": "bytes=0-15"}) as response:
                        self.assertEqual(response.status, 206)
                        self.assertEqual(len(await response.read()), 16)
            finally:
                await runner.cleanup()
