"""Per-task corrections of the actor-facing task text, without a simulator."""
import json
from pathlib import Path
import re
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from auriga.core.config import Config
from auriga.apps.gateway.workbench import Workbench
from auriga.cortex.context.public_task_rules import actor_task_rules_text, public_task_rules
from auriga.cortex.context.task_instruction_overrides import (
    CATALOG_PATH, apply_task_instruction_override, task_instruction_override,
)
from test_completion_guidance import saved_observation

ORGANIZE_NATIVE = ("Place the alarm clock on the drawer, put the figurine on the stand, "
                   "place the mouse on the mouse pad, and push the keyboard into the frame.")
LISTED = ("organize_table", "arrange_largest_number", "arrange_largest_number_random")
THRESHOLD = re.compile(r"\d+(\.\d+)? ?(cm|mm|m|deg|degrees|°)\b|\b\d+ ?x ?\d+\b", re.I)


def delivered(task, native="native"):
    return apply_task_instruction_override(native, public_task_rules(task), task_instruction_override(task))


class OverrideCatalogTest(unittest.TestCase):
    def test_every_entry_targets_existing_documented_sections(self):
        catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        tasks = set(catalog["tasks"]) | set(catalog["aliases"])
        self.assertEqual(tasks, set(LISTED))
        for task in tasks:
            with self.subTest(task=task):
                self.assertEqual(task_instruction_override(task)["status"], "applied")
                instruction, text = delivered(task)
                # The native first line is kept and the official scoring table stays verbatim.
                self.assertEqual(instruction, "native")
                self.assertTrue(text.endswith(public_task_rules(task)["public_rules"][-1]))
                # Public sources only: no numeric threshold or dimension beyond the official text.
                official = actor_task_rules_text(public_task_rules(task))
                self.assertEqual(sorted(m.group(0) for m in THRESHOLD.finditer(text)),
                                 sorted(m.group(0) for m in THRESHOLD.finditer(official)))
                self.assertNotIn("Scoring details", text)

    def test_organize_table_drops_drawer_clause(self):
        rules = public_task_rules("organize_table")
        self.assertIn("open the drawer", actor_task_rules_text(rules))
        instruction, text = apply_task_instruction_override(
            ORGANIZE_NATIVE, rules, task_instruction_override("organize_table"))
        self.assertEqual(instruction, ORGANIZE_NATIVE)
        self.assertNotIn("open the drawer", text)
        self.assertNotIn("miscellaneous items inside", text)
        self.assertNotIn("three miscellaneous items", text)
        self.assertIn("The scene contains no miscellaneous items and the drawer cannot be opened", text)
        self.assertTrue(text.startswith(
            "Instruction\n\nPlace the mouse on the mouse pad, push the keyboard into the frame, "
            "put the figurine on the stand, and place the alarm clock on the drawer.\n\nDescription\n\n"))

    def test_arrange_comment_uses_table_axes_for_both_layout_variants(self):
        for task in ("arrange_largest_number", "arrange_largest_number_random"):
            with self.subTest(task=task):
                _, text = delivered(task)
                self.assertNotIn("remain within the permitted rotational tolerance relative to its initial pose", text)
                self.assertIn("Each digit's orientation is judged against the fixed table axes, "
                              "not relative to its initial pose.", text)
                self.assertIn("place all the numbers on the pad", text)
                self.assertNotIn("four numbers", text)

    def test_unlisted_task_and_disabled_flag_are_unchanged(self):
        for task, enabled in (("stack_blocks", True), ("insert_tubes", True), ("cover_blocks", True),
                              ("organize_table", False), ("arrange_largest_number", False)):
            with self.subTest(task=task, enabled=enabled):
                rules = public_task_rules(task)
                record = task_instruction_override(task, enabled)
                self.assertEqual(record["status"], "disabled" if not enabled else "not_listed")
                self.assertNotIn("entry", record)
                self.assertEqual(apply_task_instruction_override("native text", rules, record),
                                 ("native text", actor_task_rules_text(rules)))
        rules = public_task_rules("organize_table")
        self.assertEqual(apply_task_instruction_override("native text", rules, None),
                         ("native text", actor_task_rules_text(rules)))

    def test_instruction_append_and_unknown_section_paths(self):
        record = task_instruction_override("organize_table")
        record["entry"] = {"instruction": "Replaced.", "append_sections": ["Note\n\nExtra."]}
        rules = public_task_rules("insert_tubes")
        self.assertEqual(apply_task_instruction_override("native", rules, record),
                         ("Replaced.", actor_task_rules_text(rules) + "\n\nNote\n\nExtra."))
        record["entry"] = {"replace_sections": {"Comment": "x"}}
        with self.assertRaisesRegex(ValueError, "unknown_section"):
            apply_task_instruction_override("native", rules, record)


class GatewayPromptTest(unittest.IsolatedAsyncioTestCase):
    async def initial_prompt(self, task, native, enabled=None):
        features = {} if enabled is None else {"task_instruction_overrides": enabled}
        config = Config().with_overrides({"run": {"task": task}, "features": features})
        with tempfile.TemporaryDirectory() as temporary:
            robot = SimpleNamespace(close=Mock(), execute=Mock(), reset=Mock(), evaluate=Mock())
            workbench = Workbench(Path(temporary) / "run", robot=robot, visual_review_model=None, config=config)
            initial = {**saved_observation(workbench.store, "initial-observation"), "task_instruction": native}
            done = threading.Event()
            done.set()
            harness = SimpleNamespace(
                accepting=True, active_action=None, states={}, action_count=0,
                latest=initial, home_check=None, capabilities={},
                action_budget=lambda: dict(native_steps_used=0, native_steps_limit=None, native_steps_remaining=None),
                episode_done=threading.Event(), episode_terminal=None,
                native_action_count=0, started_at=time.monotonic(), last_native_progress_at=time.monotonic(),
                scene=SimpleNamespace(capabilities=lambda: {}), start=Mock())
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
                model_proxy=SimpleNamespace(limit_notified=False, request_count=1))
            workbench.harness, workbench.session = harness, session
            terminal = SimpleNamespace(start=AsyncMock(), close=AsyncMock(),
                                       session_id="fixture-terminal", process=SimpleNamespace(pid=-1))
            try:
                with patch("auriga.apps.gateway.workbench.Terminal", return_value=terminal):
                    await workbench.start("unused", benchmark=True)
                prompt = session.run_turn.call_args.args[0]
                manifest = json.loads((workbench.root / "manifest.json").read_text())
                records = [value for value in workbench.store.records("configuration") if "prompt" in value]
            finally:
                await workbench.close()
        return prompt, manifest, records[0]

    @staticmethod
    def historic_head(native, rules):
        return (f"Complete the following native RoboDojo task: {native}\n"
                "Use permitted RGB, measured state and tools; inspect actual feedback. "
                "The environment ends execution automatically on its terminal condition. "
                "Do not read simulator files or evaluator state.\n" + actor_task_rules_text(rules)
                + "\nCurrent native execution budget: ")

    async def test_default_on_override_reaches_prompt_manifest_and_record(self):
        prompt, manifest, record = await self.initial_prompt("organize_table", ORGANIZE_NATIVE)
        override = task_instruction_override("organize_table")
        expected_instruction, expected_rules = apply_task_instruction_override(
            ORGANIZE_NATIVE, public_task_rules("organize_table"), override)
        self.assertEqual(expected_instruction, ORGANIZE_NATIVE)
        self.assertTrue(prompt.startswith(
            f"Complete the following native RoboDojo task: {expected_instruction}\n"))
        self.assertIn("\n" + expected_rules + "\nCurrent native execution budget: ", prompt)
        self.assertNotIn("open the drawer", prompt)
        self.assertTrue(manifest["effective_config"]["features"]["task_instruction_overrides"])
        self.assertEqual(manifest["task_instruction_override"], override)
        self.assertIn("cortex/context/task_instruction_overrides.json", manifest["source_sha256"])
        self.assertEqual(record["native_instruction"], ORGANIZE_NATIVE)
        self.assertEqual(record["task_instruction_override"]["status"], "applied")

    async def test_disabled_and_unlisted_tasks_keep_historic_prompt(self):
        cases = (("organize_table", ORGANIZE_NATIVE, False, "disabled"),
                 ("insert_tubes", "Insert the three tubes into the rack one by one.", None, "not_listed"),
                 ("stack_blocks", "Stack the blocks.", None, "not_listed"))
        for task, native, enabled, status in cases:
            with self.subTest(task=task, enabled=enabled):
                prompt, manifest, _ = await self.initial_prompt(task, native, enabled)
                self.assertTrue(prompt.startswith(self.historic_head(native, public_task_rules(task))))
                self.assertEqual(manifest["task_instruction_override"]["status"], status)
                self.assertEqual(manifest["effective_config"]["features"]["task_instruction_overrides"],
                                 enabled is not False)


if __name__ == "__main__":
    unittest.main()
