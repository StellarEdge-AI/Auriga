"""Motion execution policy survives configuration and process boundaries."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from auriga.core.config import Config, ConfigError
from auriga.robot.backends.robodojo.robodojo import RobotBackend
from scripts import benchmark_queue as queue


class FeedbackConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.case = dict(cwd=str(Path(__file__).resolve().parents[1]),
                         run_dir=str(self.root / "run"), task="move_bottle",
                         seed=0, layout_id=0, title="feedback config", tag=[])

    def test_default_and_archived_configs_preserve_execution_policy(self):
        self.assertEqual(Config.from_dict({}).features.motion_execution_mode, "resampled")
        for mode in ("resampled", "feedback"):
            with self.subTest(mode=mode):
                config = Config.from_dict({"features": {"motion_execution_mode": mode, "motion_review": False}})
                manifest = queue.configured_manifest({"cases": [self.case]}, config)
                with patch.object(Config, "load", side_effect=AssertionError("ambient reload")):
                    restored = queue.queue_config(json.loads(json.dumps(manifest)))
                self.assertEqual(restored.features.motion_execution_mode, mode)

    def test_invalid_modes_fail_at_config_backend_and_queue_boundaries(self):
        for value in (None, False, 0, "", "closed_loop", ["feedback"]):
            with self.subTest(value=value):
                with self.assertRaises(ConfigError):
                    Config.from_dict({"features": {"motion_execution_mode": value}})
                with self.assertRaisesRegex(ValueError, "invalid_motion_execution_mode"):
                    RobotBackend(config=Config(), motion_execution_mode=value)
                with self.assertRaisesRegex(ValueError, "Invalid motion_execution_mode"):
                    queue.command({**self.case, "motion_execution_mode": value}, 0)

    def test_queue_case_override_reaches_gateway_resolved_config_and_backend(self):
        from auriga.apps.gateway import cli as gateway
        snapshot = self.root / "config.json"
        for configured, override in (("resampled", None), ("feedback", None),
                                     ("resampled", "feedback"), ("feedback", "resampled")):
            with self.subTest(configured=configured, override=override):
                config = Config.from_dict({"features": {"motion_execution_mode": configured, "motion_review": False}})
                snapshot.write_text(json.dumps(config.to_dict()))
                case = {**self.case, **({"motion_execution_mode": override} if override else {})}
                if (override or configured) == "feedback":
                    with self.assertRaisesRegex(ConfigError, "feedback_is_diagnostic_only"):
                        queue.command(case, 0, config=config, config_path=snapshot)
                    continue
                command = queue.command(case, 0, config=config, config_path=snapshot)
                with patch.object(gateway, "serve", new_callable=AsyncMock) as serve, \
                        patch.object(sys, "argv", ["gateway", *command[4:]]):
                    gateway.main()
                resolved = serve.call_args.args[0].resolved_config
                self.assertEqual(resolved.features.motion_execution_mode, override or configured)
                with patch.object(Config, "load", side_effect=AssertionError("ambient reload")):
                    backend = RobotBackend(config=resolved)
                self.assertEqual(backend.motion_execution_mode, override or configured)

    def test_feedback_diagnostics_and_archives_remain_readable_but_benchmark_cannot_launch(self):
        from auriga.apps.gateway import cli as gateway
        config = Config.from_dict({"features": {"motion_execution_mode": "feedback", "motion_review": False}})
        self.assertIs(config.validate_launch(), config)
        archived = config.with_overrides({"run": {"replay": True, "run_dir": str(self.root / 'archive')}})
        self.assertIs(archived.validate_launch(), archived)
        with self.assertRaisesRegex(ConfigError, "feedback_is_diagnostic_only"):
            config.with_overrides({"run": {"benchmark": True}}).validate_launch()
        with patch.object(gateway, "serve", new_callable=AsyncMock) as serve, \
                patch.object(sys, "argv", ["gateway", "--benchmark", "--motion-execution-mode", "feedback", "--no-motion-review"]), \
                self.assertRaises(SystemExit) as stopped:
            gateway.main()
        self.assertEqual(stopped.exception.code, 2)
        serve.assert_not_called()

    def test_backend_constructor_override_and_worker_cli(self):
        for configured, override in (("resampled", None), ("feedback", None),
                                     ("feedback", "resampled")):
            with self.subTest(configured=configured, override=override):
                config = Config.from_dict({"features": {"motion_execution_mode": configured, "motion_review": False}})
                backend = RobotBackend(config=config, run_dir=self.root / configured,
                                       **({"motion_execution_mode": override} if override else {}))
                process = MagicMock(pid=999999999)
                with patch("auriga.robot.backends.robodojo.robot_process.subprocess.Popen", return_value=process) as popen, \
                        patch("auriga.robot.backends.robodojo.robot_process.threading.Thread"), \
                        patch.object(backend, "_request", return_value={"ready": True}):
                    try:
                        self.assertEqual(backend.start(), {"ready": True})
                    finally:
                        # The fake Popen has no live child; retain no real log.
                        backend.process = None
                        if getattr(backend, 'log', None) is not None:
                            backend.log.close()
                args = popen.call_args.args[0]
                self.assertEqual(args.count("--motion-execution-mode"), 1)
                self.assertEqual(args[args.index("--motion-execution-mode") + 1], override or configured)

    def test_feedback_conflicts_rejected_by_snapshots_files_and_overrides(self):
        for feature in ("motion_review", "tracking_guard"):
            valid = {"motion_execution_mode": "feedback", "motion_review": False}
            invalid = {**valid, feature: True}
            expected = "requires_" + feature + "_disabled"
            with self.subTest(feature=feature):
                with self.assertRaisesRegex(ConfigError, expected):
                    Config.from_dict({"features": invalid})
                config = Config.from_dict({"features": valid})
                with self.assertRaisesRegex(ConfigError, expected):
                    config.with_overrides({"features": {feature: True}})
                snapshot = self.root / "invalid.json"
                snapshot.write_text(json.dumps({"features": invalid}))
                with self.assertRaisesRegex(ConfigError, expected):
                    Config.load(snapshot, environ={})
                with self.assertRaisesRegex(ConfigError, expected):
                    queue.command({**self.case, **invalid}, 0)
        switched = Config().with_overrides({"features": {
            "motion_execution_mode": "feedback", "motion_review": False}})
        self.assertEqual(switched.features.motion_execution_mode, "feedback")
        self.assertTrue(switched.with_overrides({"features": {
            "motion_execution_mode": "resampled", "motion_review": True}}).features.motion_review)


if __name__ == "__main__":
    unittest.main()
