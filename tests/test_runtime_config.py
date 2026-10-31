"""Unified configuration reaches runtime boundaries without GPU or model calls."""

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from auriga.cortex.session.codex import CodexSession
from auriga.core.config import Config
from auriga.core.storage.images import image_settings, store_image_settings
from auriga.robot.model.geometry import GeometryTools
from auriga.apps.library.service import Library, main as library_main
from auriga.robot.backends.robodojo.robodojo import RobotBackend
from auriga.apps.reference.reference_api import ResponsesClient
from auriga.perception.scene.scene_service import SceneService, _compute
from auriga.core.storage.store import Store
from auriga.apps.library.task_review import TaskReviewService


class RuntimeConfigTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def store(self):
        store = Store(self.root / "run", dict(run_id="config", episode_id="episode",
                      env_instance_id="environment", env_generation=1))
        self.addCleanup(store.close)
        return store

    def test_explicit_config_reaches_all_runtime_consumers_without_environment_reload(self):
        config = Config.from_dict({
            "run": {"task": "stack_bowls", "gpu": 2, "seed": 8, "layout_id": 3},
            "features": {"tracking_guard": True, "joint_replan": True, "observation_profile": "rgb_proprio_v1"},
            "backend": {"root": str(self.root / "official"), "python": str(self.root / "python")},
            "model": {"name": "fixture-model", "effort": "high", "codex_home": str(self.root / "codex")},
            "images": {"transport_format": "jpeg", "reuse_exact_images": False},
            "library": {"root": str(self.root / "library"), "scan_interval": 3},
        })
        store = self.store()
        with patch.object(Config, "load", side_effect=AssertionError("unexpected environment reload")):
            robot = RobotBackend(config=config)
            session = CodexSession(store.root, store, lambda _: {}, config=config)
            library = Library(config=config)
            review = TaskReviewService(config=config)
        self.assertEqual((robot.gpu_id, robot.seed, robot.layout_id), (2, 8, 3))
        self.assertEqual((robot.root, robot.python), (str(self.root / "official"), str(self.root / "python")))
        self.assertTrue(robot.tracking_guard)
        self.assertTrue(robot.joint_replan)
        self.assertEqual(robot.observation_profile, "rgb_proprio_v1")
        self.assertEqual(robot.image_settings, session.image_settings)
        self.assertFalse(session.image_settings["reuse_exact_images"])
        self.assertEqual((session.model, session.effort), ("fixture-model", "high"))
        self.assertEqual(library.scan_interval, 3)
        self.assertEqual(library.catalog.root, self.root / "library")
        self.assertEqual(library.task_reviews.root, review.root)
        self.assertEqual(review.root, self.root / "official")

    def test_explicit_constructor_arguments_override_config_even_when_false_or_zero(self):
        config = Config.from_dict({"run": {"gpu": 2, "seed": 8},
                                   "features": {"tracking_guard": True, "joint_replan": True},
                                   "model": {"name": "configured-model"}})
        robot = RobotBackend(config=config, gpu_id=0, seed=0, tracking_guard=False, joint_replan=False)
        self.assertEqual((robot.gpu_id, robot.seed), (0, 0))
        self.assertFalse(robot.tracking_guard)
        self.assertFalse(robot.joint_replan)
        store = self.store()
        session = CodexSession(store.root, store, lambda _: {}, model="explicit-model", config=config)
        self.assertEqual(session.model, "explicit-model")

    def test_saved_image_policy_wins_over_new_config_and_legacy_missing_reuse_stays_disabled(self):
        store = self.store()
        old = image_settings({"transport_format": "jpeg", "reuse_exact_images": False})
        old.pop("reuse_exact_images")
        store.set_state("runtime", "image_settings", old)
        config = Config.from_dict({"images": {"transport_format": "webp", "reuse_exact_images": True}})
        resolved = store_image_settings(store, config.images.to_dict())
        self.assertEqual(resolved["transport_format"], "jpeg")
        self.assertFalse(resolved["reuse_exact_images"])
        session = CodexSession(store.root, store, lambda _: {}, config=config)
        self.assertEqual(session.image_settings, resolved)

    def test_backend_explicit_none_flags_remain_invalid(self):
        config = Config.from_dict({})
        for flag in ("tracking_guard", "joint_replan"):
            with self.subTest(flag=flag), self.assertRaisesRegex(ValueError, "invalid_" + flag):
                RobotBackend(config=config, **{flag: None})

    def test_visual_helper_codex_home_overrides_ambient_home(self):
        configured = self.root / "configured"
        configured.mkdir()
        (configured / "config.toml").write_text('model_provider="fixture"\n[model_providers.fixture]\nbase_url="https://configured.invalid/v1"\nenv_key="CONFIG_FIXTURE_TOKEN"\n')
        (configured / "auth.json").write_text('{"OPENAI_API_KEY":"synthetic-fixture"}')
        with patch.dict(os.environ, {"CODEX_HOME": str(self.root / "absent")}), \
                patch.object(ResponsesClient, "__init__", return_value=None) as constructor:
            ResponsesClient.from_codex_config(self.root / "helper", codex_home=configured, model="gpt-6.1-sol")
        constructor.assert_called_once_with("https://configured.invalid/v1", "synthetic-fixture",
                                             self.root / "helper", model="gpt-6.1-sol", effort="high")

    def test_geometry_service_and_child_use_frozen_images_without_environment_reload(self):
        settings = Config.from_dict({"images": {"transport_format": "jpeg", "jpeg_quality": 61}}).images.to_dict()
        request = dict(run_id="geometry", episode_id="episode", env_instance_id="environment", env_generation=1)
        connection = MagicMock()
        guard = MagicMock()
        guard.prctl.return_value = 0
        with patch.object(Config, "load", side_effect=AssertionError("unexpected environment reload")):
            service = SceneService(self.root / "scene", image_settings=settings)
            self.addCleanup(service.store.close)
            geometry = GeometryTools(service.scene, settings=service.image_settings)
            self.assertEqual(geometry.image_settings, settings)
            with patch("auriga.perception.scene.scene_service.ctypes.CDLL", return_value=guard), \
                    patch.object(GeometryTools, "execute", return_value={"status": "fixture"}):
                _compute(str(service.root), request, connection, os.getppid(), service.image_settings)
        connection.send.assert_called_once_with({"result": {"status": "fixture"}})

    def test_image_settings_reads_unified_file_and_legacy_environment(self):
        path = self.root / "config.json"
        path.write_text(json.dumps({"images": {"webp_quality": 71}}))
        with patch.dict(os.environ, {"AURIGA_CONFIG": str(path),
                                     "AURIGA_IMAGE_SETTINGS": '{"webp_quality":30,"reuse_exact_images":false}'}):
            settings = image_settings()
        self.assertEqual(settings["webp_quality"], 71)
        self.assertFalse(settings["reuse_exact_images"])

    def test_library_print_config_applies_cli_overrides_without_starting_server(self):
        path = self.root / "config.json"
        path.write_text(json.dumps({"library": {"port": 8790, "scan_interval": 2,
                                                "root": str(self.root / "catalog")}}))
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch("auriga.apps.library.service.serve", side_effect=AssertionError("server started")):
            library_main(["--config", str(path), "--port", "8791", "--print-config"])
        resolved = json.loads(output.getvalue())
        self.assertEqual(resolved["library"]["port"], 8791)
        self.assertEqual(resolved["library"]["scan_interval"], 2)
        self.assertEqual(resolved["library"]["root"], str(self.root / "catalog"))


if __name__ == "__main__":
    unittest.main()
