"""Per-run RGB comparison ablation boundaries; no model or simulator requests."""
from program_fixture import program_services
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch

from auriga.apps.gateway.workbench import Workbench
from auriga.apps.gateway.cli import main
from auriga.runtime.harness import Harness
from auriga.core.observation_policy import ACTOR_POLICY, ACTOR_PROFILE
from auriga.runtime.programs.runner import ProgramRunner
from auriga.core.storage.store import Store
from scripts import benchmark_queue
from tests.test_harness import FixtureRobot


class VisionCompareFlagTests(unittest.TestCase):
    def test_cli_defaults_and_explicit_flags(self):
        for flags, enabled in (([], True), (["--vision-compare"], True), (["--no-vision-compare"], False)):
            with self.subTest(flags=flags), patch("sys.argv", ["auriga", *flags]), \
                    patch("auriga.apps.gateway.cli.serve", new_callable=AsyncMock) as serve:
                main()
                self.assertEqual(serve.call_args.args[0].vision_compare, enabled)

    def test_queue_preserves_legacy_commands_and_emits_explicit_ablation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            case = dict(cwd=str(Path(__file__).resolve().parents[1]), run_dir=str(root / "run"),
                        task="general_pickup", seed=0, layout_id=0, title="RGB test", tag=["fixture"])
            self.assertNotIn("--vision-compare", benchmark_queue.command(case, 0))
            for enabled, flag in ((True, "--vision-compare"), (False, "--no-vision-compare")):
                self.assertIn(flag, benchmark_queue.command({**case, "vision_compare": enabled}, 0))
            for invalid in (0, "false", None):
                with self.assertRaisesRegex(ValueError, "Invalid vision_compare"):
                    benchmark_queue.command({**case, "vision_compare": invalid}, 0)

    def test_disabled_dispatch_blocks_direct_and_program_calls_before_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary) / "store", dict(run_id="run", episode_id="ep", env_instance_id="env", env_generation=1))
            self.addCleanup(store.close)
            robot = FixtureRobot(temporary)
            harness = Harness(store, robot, vision_compare=False)
            harness.start()
            self.assertFalse(harness.capabilities["vision_compare_enabled"])
            with patch("auriga.perception.visual_feedback.compare_observations") as compare:
                with self.assertRaisesRegex(ValueError, "vision_compare_not_enabled_for_run"):
                    harness.dispatch("vision_compare", {}, {"tool_call_id": "direct"})
                runner = ProgramRunner(program_services(harness))
                try:
                    entry = dict(cancel=threading.Event(), deadline=time.monotonic() + 10,
                                 call_lock=threading.RLock(), job_id="program-test", calls=0,
                                 max_calls=1, correlation={"tool_call_id": "parent"})
                    result = runner.dispatch(entry, dict(operation_id="comparison", tool="vision_compare", arguments={}))
                    self.assertFalse(result["success"])
                    self.assertEqual(json.loads(result["contentItems"][0]["text"])["error"], "vision_compare_not_enabled_for_run")
                finally:
                    runner.close()
                compare.assert_not_called()
            self.assertEqual(robot.executions, 0)
            harness.vision_compare = True
            with patch("auriga.perception.visual_feedback.compare_observations", return_value={"fixture": True}) as compare:
                self.assertTrue(harness.dispatch("vision_compare", dict(before_observation_id="before",
                    after_observation_id="after", camera_id="camera", roi=[0, 0, 1, 1]), {"tool_call_id": "enabled"})["success"])
                compare.assert_called_once()


class VisionCompareArchiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_schema_and_harness_follow_new_run_setting(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled), tempfile.TemporaryDirectory() as temporary:
                workbench = Workbench(Path(temporary) / "run", motion_review=False, vision_compare=enabled)
                try:
                    self.assertEqual(workbench.harness.vision_compare, enabled)
                    names = {tool["name"] for tool in workbench.actor_tools()}
                    self.assertEqual("vision_compare" in names, enabled)
                    self.assertNotIn("skill_run", names)
                    self.assertNotIn("manipulation_run", names)
                    self.assertIn("robot_action", names)
                    self.assertIn("operation_memory", names)
                    self.assertNotIn("program_run", names)
                    workbench.harness.programs = ProgramRunner(program_services(workbench.harness))
                    self.assertIn("program_run", {tool["name"] for tool in workbench.actor_tools()})
                    self.assertIsNone(workbench.robot.process)
                finally:
                    await workbench.close()

    async def test_resume_and_replay_use_saved_setting_and_legacy_default(self):
        for mode in ("resume", "replay"):
            for saved in (None, False, True):
                with self.subTest(mode=mode, saved=saved), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary) / "run"
                    root.mkdir()
                    manifest = dict(schema_version="0.1", run_id="run", episode_id="ep", env_instance_id="env", env_generation=1,
                                    actor_information_policy=ACTOR_POLICY, observation_profile=ACTOR_PROFILE)
                    if saved is not None:
                        manifest["vision_compare"] = saved
                    (root / "manifest.json").write_text(json.dumps(manifest))
                    workbench = Workbench(root, **{mode: True}, vision_compare=not saved)
                    try:
                        expected = True if saved is None else saved
                        self.assertEqual(workbench.vision_compare, expected)
                        self.assertEqual(workbench.harness.vision_compare, expected)
                        self.assertEqual("vision_compare" in {tool["name"] for tool in workbench.actor_tools()}, expected)
                        self.assertEqual(json.loads((root / "manifest.json").read_text()), manifest)
                        self.assertIsNone(workbench.robot.process)
                    finally:
                        await workbench.close()


if __name__ == "__main__":
    unittest.main()
