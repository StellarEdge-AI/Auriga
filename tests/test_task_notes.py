"""Exercise actual startup delivery, opt-out and historical prompt preservation."""
from auriga.cortex.context.actor_instructions import OPERATION_NOTES
from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from auriga.apps.gateway.workbench import Workbench
from auriga.apps.gateway.cli import main
from auriga.core.observation_policy import ACTOR_PROFILE
from auriga.cortex.context.task_notes import (DEFAULT_TASK_NOTES, LEGACY_DEFAULT_TASK_NOTES,
                                TASK_NOTES_VERSION, resolve_task_notes, version_for)
from scripts.benchmark_report import summarize


def fixture_workbench(root, **options):
    robot = SimpleNamespace(close=Mock(), execute=Mock(), reset=Mock(), evaluate=Mock())
    # This process-free fixture exercises prompt archival, not the Sol helper.
    options.setdefault("visual_review_model", None)
    workbench = Workbench(root, robot=robot, **options)
    done = threading.Event()
    done.set()
    harness = SimpleNamespace(
        accepting=True, active_action=None, states={}, action_count=0,
        latest={"task_instruction": "Fixture native task.", "observation_id": "initial",
                "observation_profile": ACTOR_PROFILE},
        capabilities={}, episode_done=threading.Event(), episode_terminal=None,
        native_action_count=0, started_at=time.monotonic(), last_native_progress_at=time.monotonic(),
        scene=SimpleNamespace(capabilities=lambda: {}), start=Mock(),
        action_budget=lambda: dict(native_steps_used=0, native_step_limit=700, native_steps_remaining=700),
    )
    for field in ('tool_registry', 'config', 'disabled_tools', 'observation_profile',
                  'motion_review', 'enable_wait', 'vision_compare', 'visual_reviewer',
                  'action_policy', 'programs'):
        setattr(harness, field, getattr(workbench.harness, field))
    harness.request_stop = lambda: setattr(harness, "accepting", False)
    session = SimpleNamespace(
        thread_id="fixture-thread", turn_id="first-turn", state="completed", turn_done=done,
        model="fixture", provider="fixture", effort="high", price_card=None,
        cli_executable=Path("/fixture/codex"), command_prefix=["fixture-sandbox"], actor_dir=workbench.root,
        socket_path=workbench.root / "fixture.sock", env={}, process=SimpleNamespace(pid=-1),
        start=Mock(return_value="fixture-thread"), run_turn=Mock(), close=Mock(),
        model_proxy=SimpleNamespace(limit_notified=False, request_count=1),
    )
    workbench.harness, workbench.session = harness, session
    terminal = SimpleNamespace(start=AsyncMock(), close=AsyncMock(),
                              session_id="fixture-terminal", process=SimpleNamespace(pid=-1))
    return workbench, terminal


class TaskNotesStartupTest(unittest.IsolatedAsyncioTestCase):
    async def test_delivery_and_archive_match_for_defaults_custom_opt_out_and_smoke(self):
        for benchmark, enabled, custom in (
                (True, True, None), (True, True, "Public extra rule."),
                (True, False, None), (True, False, "Public extra rule."),
                (False, True, None)):
            with self.subTest(benchmark=benchmark, enabled=enabled, custom=custom), tempfile.TemporaryDirectory() as temporary:
                workbench, terminal = fixture_workbench(Path(temporary) / "run", default_task_notes=enabled)
                try:
                    with patch("auriga.apps.gateway.workbench.Terminal", return_value=terminal):
                        await workbench.start("Smoke instruction.", benchmark=benchmark,
                                              task_notes=custom, task_notes_source="public-fixture" if custom else None)
                    workbench.session.run_turn.assert_called_once()
                    prompt = workbench.session.run_turn.call_args.args[0]
                    self.assertEqual(prompt.count(OPERATION_NOTES), int(benchmark and enabled))
                    if custom:
                        self.assertEqual(prompt.count(custom), 1)
                    if not benchmark:
                        self.assertEqual(prompt, "Smoke instruction.")
                    manifest = json.loads((workbench.root / "manifest.json").read_text())
                    saved = next(value for value in workbench.store.records("configuration") if "prompt" in value)
                    report = summarize(workbench.root)
                    self.assertEqual(saved["prompt"], prompt)
                    for key in ("task_notes", "task_notes_source", "task_notes_config"):
                        self.assertEqual(manifest[key], saved[key])
                        self.assertEqual(report["conditions"][key], manifest[key])
                    self.assertEqual(manifest["task_notes_config"]["default_enabled"], benchmark and enabled)
                    self.assertEqual(manifest["task_notes_config"]["custom_text"], custom)
                    if benchmark and enabled:
                        self.assertEqual(manifest["task_notes_config"]["default_version"], "unified_operation_notes_v7")
                        self.assertIn("unified_operation_notes_v7", manifest["task_notes_source"])
                        self.assertIn("| Situation | Visible check | Adjustment when evidence disagrees |", prompt)
                        self.assertIn("reselect", prompt)
                        for consideration in ("Task, roles or geometry are uncertain", "Grasp, transport or cooperation is uncertain",
                                              "Working arm cannot reach or object needs two arms",
                                              "Placement or release is uncertain", "A motion or expected event fails"):
                            self.assertIn("| " + consideration + " |", prompt)
                        self.assertIn("Avoid unnecessary intermediate home returns", prompt)
                        self.assertIn("deeper contact is not automatically better", prompt)
                        self.assertIn("lift clear of the support and neighbors before lateral transport or folding", prompt)
                        self.assertIn("actual_contact_position_m", prompt)
                    if manifest["task_notes"]:
                        self.assertIn(manifest["task_notes"], prompt)
                        self.assertNotIn(manifest["task_notes_source"], prompt)
                        self.assertTrue(prompt.endswith(manifest["task_notes"]))
                    self.assertIn("cortex/context/task_notes.py", manifest["source_sha256"])
                    for name in ("execute", "reset", "evaluate"):
                        getattr(workbench.robot, name).assert_not_called()
                finally:
                    await workbench.close()

    async def test_resume_preserves_saved_text_even_after_default_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "run"
            first, terminal = fixture_workbench(root)
            try:
                with patch("auriga.apps.gateway.workbench.Terminal", return_value=terminal):
                    await first.start("unused", benchmark=True)
            finally:
                await first.close()
            manifest = json.loads((root / "manifest.json").read_text())
            manifest["task_notes"] = "Archived operation text."
            manifest["task_notes_source"] = "archived-source"
            manifest["task_notes_config"]["default_version"] = "archived-v0"
            (root / "manifest.json").write_text(json.dumps(manifest))
            resumed, terminal = fixture_workbench(root, resume=True, default_task_notes=False)
            try:
                with patch("auriga.apps.gateway.workbench.Terminal", return_value=terminal), \
                        patch("auriga.cortex.context.task_notes.DEFAULT_TASK_NOTES", "Changed future text."):
                    await resumed.start("unused")
                resumed.session.run_turn.assert_not_called()
                saved = json.loads((root / "manifest.json").read_text())
                for key in ("task_notes", "task_notes_source", "task_notes_config"):
                    self.assertEqual(saved[key], manifest[key])
            finally:
                await resumed.close()

    async def test_legacy_replay_does_not_enable_defaults_or_rewrite_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "run"
            first, _ = fixture_workbench(root)
            original = json.dumps({**first.identity, "task_notes": "Old custom text.", "task_notes_source": "old-public"})
            (root / "manifest.json").write_text(original)
            first.store.set_state("runtime", "capabilities", {"data_origin": "synthetic_fixture"})
            await first.close()
            replay, _ = fixture_workbench(root, replay=True)
            try:
                await replay.start("unused")
                self.assertFalse(replay.default_task_notes)
                replay.session.run_turn.assert_not_called()
                self.assertEqual(replay.archived_task_notes["text"], "Old custom text.")
                self.assertEqual((root / "manifest.json").read_text(), original)
            finally:
                await replay.close()


class TaskNotesCLITest(unittest.TestCase):
    def test_protocol_ablation_selects_exact_v11_text_and_source(self):
        legacy=resolve_task_notes(manipulation_protocol=False)
        current=resolve_task_notes(manipulation_protocol=True)
        self.assertEqual(legacy["text"],LEGACY_DEFAULT_TASK_NOTES)
        self.assertEqual(legacy["config"]["default_version"],"operation-notes-v11")
        self.assertEqual(version_for(False),"operation-notes-v11")
        self.assertIn("operation-notes-v11",legacy["source"])
        self.assertEqual(current["text"],DEFAULT_TASK_NOTES)
        self.assertEqual(current["config"]["default_version"],TASK_NOTES_VERSION)
        self.assertNotIn("operation_context",legacy["text"])
        self.assertIn("operation_context",current["text"])

    def test_default_and_explicit_switches(self):
        for flag, enabled in ((None, True), ("--default-task-notes", True), ("--no-default-task-notes", False)):
            with self.subTest(flag=flag), patch("sys.argv", ["auriga", "--benchmark", *([flag] if flag else [])]), \
                    patch("auriga.apps.gateway.cli.serve", new_callable=AsyncMock) as serve:
                main()
                self.assertEqual(serve.call_args.args[0].default_task_notes, enabled)

    def test_incomplete_or_empty_custom_notes_rejected(self):
        for flags in (["--task-notes", "missing source"], ["--task-notes-source", "missing text"],
                      ["--task-notes", " ", "--task-notes-source", "public"],
                      ["--task-notes", "x" * 8001, "--task-notes-source", "public"]):
            with self.subTest(flags=flags[:1]), patch("sys.argv", ["auriga", "--benchmark", *flags]), \
                    patch("auriga.apps.gateway.cli.serve", new_callable=AsyncMock) as serve, redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main()
                self.assertEqual(error.exception.code, 2)
                serve.assert_not_called()

    def test_custom_limit_is_independent_of_default_text(self):
        record = resolve_task_notes(True, "x" * 8000, "public")
        self.assertEqual(record["config"]["custom_text"], "x" * 8000)
        self.assertIn(DEFAULT_TASK_NOTES, record["text"])
        self.assertIsNone(resolve_task_notes(False)["text"])


if __name__ == "__main__":
    unittest.main()
