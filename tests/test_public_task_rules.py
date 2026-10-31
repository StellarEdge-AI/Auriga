"""Public documentation delivery and frozen provenance, without a simulator."""

import hashlib
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from auriga.cortex.context.public_task_rules import actor_task_rules_text, public_task_rules
from scripts.extract_robodojo_task_rules import CATALOG, SNAPSHOT, extract_sections, rebuild
from test_task_notes import fixture_workbench


class PublicRulesTest(unittest.TestCase):
    def test_all_task_sections_match_frozen_official_html(self):
        catalog = json.loads(CATALOG.read_text())
        with gzip.open(SNAPSHOT, "rt", encoding="utf-8") as stream:
            pages = json.load(stream)
        self.assertEqual(rebuild(catalog, pages), catalog)
        self.assertEqual(len(pages), 42)
        self.assertEqual(len(catalog["tasks"]), 54)
        for task, entry in catalog["tasks"].items():
            with self.subTest(task=task):
                self.assertEqual(public_task_rules(task)["text"], "\n\n".join(entry["public_rules"]))
                if task.endswith("_random"):
                    self.assertEqual(entry["public_rules"], catalog["tasks"][task.removesuffix("_random")]["public_rules"])

    def test_html_extraction_preserves_complete_cells_and_rejects_missing_sections(self):
        html = '''<div class="sl-markdown-content"><table>
          <tr><td>Instruction</td><td>Use <b>all four</b> pieces &amp; return.</td></tr>
          <tr><td>Description</td><td>Compare x₁ and x₂; do not replace 4 with 5.</td></tr>
          <tr><td>Comment</td><td>Keep the original “example”.</td></tr></table>
          <h2>Scoring</h2><table><tr><th>Score</th><th>Condition</th></tr>
          <tr><td>15</td><td>Lift at least <b>5 cm</b>.</td></tr>
          <tr><td>100</td><td>All five pieces; gripper open.</td></tr></table></div>'''
        self.assertEqual(extract_sections(html), [
            "Instruction\n\nUse all four pieces & return.",
            "Description\n\nCompare x₁ and x₂; do not replace 4 with 5.",
            "Comment\n\nKeep the original “example”.",
            "Scoring\n\n| Score | Condition |\n| --- | --- |\n| 15 | Lift at least 5 cm. |\n| 100 | All five pieces; gripper open. |",
        ])
        with self.assertRaises(ValueError):
            extract_sections(html.replace("Description", "Unrecognized field"))
        with self.assertRaises(ValueError):
            extract_sections(html.replace("<h2>Scoring</h2>", ""))

    def test_documented_rule_and_unknown_task(self):
        result = public_task_rules("imitate_sorting_sequence")
        self.assertEqual(result["status"], "documented")
        self.assertIn("https://robodojo-benchmark.com/", result["source_url"])
        self.assertTrue(result["public_rules"])
        self.assertEqual(result["version"], 2)
        self.assertEqual(result["text"], "\n\n".join(result["public_rules"]))
        self.assertTrue(result["text"].startswith("Instruction\n"))
        self.assertIn("\n\nDescription\n", result["text"])
        self.assertIn("\n\nScoring\n", result["text"])
        self.assertIn("| Score | Condition |", result["text"])
        self.assertNotIn("examples are not this episode's answers", result["text"])
        unknown = public_task_rules("not_an_official_task")
        self.assertEqual(unknown["status"], "unavailable")
        self.assertIsNone(unknown["text"])

    def test_lookup_uses_only_frozen_catalog_without_runtime_evaluator(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "catalog.json"
            data = json.dumps(dict(version="test", tasks={"fixture": dict(
                source_url="https://robodojo-benchmark.com/doc/sim-tasks/fixture/",
                public_rules=["Use the order in the current instruction."])})).encode()
            path.write_bytes(data)
            with patch("auriga.cortex.context.public_task_rules.CATALOG_PATH", path):
                result = public_task_rules("fixture")
            self.assertEqual(result["catalog_sha256"], hashlib.sha256(data).hexdigest())
            self.assertEqual("Use the order in the current instruction.", result["text"])

    def test_legacy_archived_text_is_not_replaced_with_new_official_sections(self):
        text = "Official RoboDojo public task rules (old citation):\nArchived summary."
        self.assertEqual(actor_task_rules_text({"version": 1, "text": text}),
                         "RoboDojo task rules:\nArchived summary.")

    def test_invalid_catalog_rules_are_rejected(self):
        for rules in ([], [""], [None], "text"):
            with self.subTest(rules=rules), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "catalog.json"
                path.write_text(json.dumps(dict(version=2, tasks={"fixture": dict(
                    source_url="https://robodojo-benchmark.com/doc/sim-tasks/fixture/",
                    public_rules=rules)})))
                with patch("auriga.cortex.context.public_task_rules.CATALOG_PATH", path), \
                        self.assertRaisesRegex(ValueError, "invalid_public_task_rules_catalog"):
                    public_task_rules("fixture")


class PublicRulesStartupTest(unittest.IsolatedAsyncioTestCase):
    async def test_benchmark_rules_are_delivered_and_archived_independently_of_operation_notes(self):
        with tempfile.TemporaryDirectory() as temporary:
            workbench, terminal = fixture_workbench(Path(temporary) / "run", task="imitate_sorting_sequence",
                                                     default_task_notes=False)
            try:
                with patch("auriga.apps.gateway.workbench.Terminal", return_value=terminal):
                    await workbench.start("unused", benchmark=True)
                prompt = workbench.session.run_turn.call_args.args[0]
                manifest = json.loads((workbench.root / "manifest.json").read_text())
                recorded = next(r for r in workbench.store.records("configuration") if "prompt" in r)
                rules = manifest["public_task_rules"]
                self.assertEqual(recorded["public_task_rules"], rules)
                self.assertEqual(prompt.count(rules["text"]), 1)
                self.assertIn("Fixture native task.", prompt)
                self.assertIsNone(manifest["task_notes"])
                self.assertIn("cortex/context/robodojo_task_rules.json", manifest["source_sha256"])
                for name in ("execute", "reset", "evaluate"):
                    getattr(workbench.robot, name).assert_not_called()
            finally:
                await workbench.close()

    async def test_smoke_prompt_is_not_reinterpreted_as_native_task(self):
        with tempfile.TemporaryDirectory() as temporary:
            workbench, terminal = fixture_workbench(Path(temporary) / "run", task="imitate_sorting_sequence")
            try:
                with patch("auriga.apps.gateway.workbench.Terminal", return_value=terminal):
                    await workbench.start("Only observe, do not sort.", benchmark=False)
                self.assertEqual(workbench.session.run_turn.call_args.args[0], "Only observe, do not sort.")
                manifest = json.loads((workbench.root / "manifest.json").read_text())
                self.assertIsNone(manifest["public_task_rules"])
            finally:
                await workbench.close()

    async def test_resume_preserves_rule_text_without_reinterpreting_old_runs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "run"
            first, terminal = fixture_workbench(root, task="imitate_sorting_sequence")
            legacy = dict(version=1, status="documented", task="imitate_sorting_sequence",
                          text="RoboDojo task rules:\nFrozen pre-verbatim summary.",
                          public_rules=["Frozen pre-verbatim summary."])
            try:
                with patch("auriga.apps.gateway.workbench.Terminal", return_value=terminal), \
                        patch("auriga.apps.gateway.workbench.public_task_rules", return_value=legacy):
                    await first.start("unused", benchmark=True)
            finally:
                await first.close()
            old = json.loads((root / "manifest.json").read_text())["public_task_rules"]
            resumed, terminal = fixture_workbench(root, resume=True)
            try:
                with patch("auriga.apps.gateway.workbench.Terminal", return_value=terminal), \
                        patch("auriga.apps.gateway.workbench.public_task_rules", side_effect=AssertionError("must use archived rules")):
                    await resumed.start("unused", benchmark=True)
                current = json.loads((root / "manifest.json").read_text())
                self.assertEqual(current["public_task_rules"], old)
                resumed.session.run_turn.assert_not_called()
            finally:
                await resumed.close()


if __name__ == "__main__":
    unittest.main()
