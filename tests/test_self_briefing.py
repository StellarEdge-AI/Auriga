"""features.self_briefing: optional, task-independent pre-action operating notes."""
import io
import json
from pathlib import Path
import re
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image

from auriga.cortex.context import self_briefing
from auriga.cortex.session.codex_evidence_agent import BASE_INSTRUCTIONS as EVIDENCE_BASE_INSTRUCTIONS, CodexEvidenceAgent
from auriga.core.config import Config
from auriga.apps.gateway.workbench import Workbench
from auriga.cortex.context.public_task_rules import actor_task_rules_text, public_task_rules
from auriga.core.storage.store import Store


TASK = "pour_balls_into_vase"
NOTES = "Grasp the source first, keep it level, pour slowly, then check the target in the head view."


def saved_observation(store, identifier, cameras=()):
    state = {"frame_id": "env_origin", "sim_step": 0, "step_domain": "adapter_control_step", "arms": {}}
    return {**store.identity, "schema_version": "0.1", "world_kind": "observed",
            "observation_id": identifier, "sim_step": 0, "step_domain": "adapter_control_step",
            "robot_state_ref": store.put_artifact(json.dumps(state).encode(), "robot_state", "application/json"),
            "task_instruction": "Fixture native instruction.", "cameras": list(cameras)}


def jpeg(color):
    buffer = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(buffer, "JPEG", quality=90)
    return buffer.getvalue()


class FakeAgent:
    """Stands in for CodexEvidenceAgent; records the constructor and request."""
    instances = []
    answer = NOTES
    error = None

    def __init__(self, session, **kwargs):
        self.session, self.kwargs = session, kwargs
        self.model = kwargs.get("model")
        self.calls = []
        FakeAgent.instances.append(self)

    def review_evidence(self, *, input_items, output_schema, request_id, cancel_event=None):
        self.calls.append(dict(input_items=input_items, output_schema=output_schema, request_id=request_id))
        if FakeAgent.error is not None:
            raise FakeAgent.error
        return dict(model=self.model, status="completed", answer=dict(notes=FakeAgent.answer),
                    usage=dict(input_tokens=1200, output_tokens=90), thread_id="helper-thread", turn_id="helper-turn")


class SelfBriefingStartupCheck(unittest.IsolatedAsyncioTestCase):
    async def start(self, temporary, *, enabled, error=None):
        FakeAgent.instances, FakeAgent.error = [], error
        robot = SimpleNamespace(close=Mock(), execute=Mock(), reset=Mock(), evaluate=Mock())
        features = {"self_briefing": bool(enabled)}
        config = Config().with_overrides({"run": {"task": TASK}, "features": features})
        workbench = Workbench(Path(temporary) / "run", robot=robot, visual_review_model=None, config=config)
        initial = saved_observation(workbench.store, "initial-observation")
        done = threading.Event()
        done.set()
        harness = SimpleNamespace(
            accepting=True, active_action=None, states={}, action_count=0, latest=initial, home_check=None,
            capabilities={}, episode_done=threading.Event(), episode_terminal=None,
            action_budget=lambda: dict(native_steps_used=0, native_steps_limit=None, native_steps_remaining=None),
            native_action_count=0, started_at=time.monotonic(), last_native_progress_at=time.monotonic(),
            scene=SimpleNamespace(capabilities=lambda: {}), start=Mock())
        for field in ('tool_registry', 'config', 'disabled_tools', 'observation_profile', 'motion_review',
                      'enable_wait', 'vision_compare', 'visual_reviewer', 'action_policy', 'programs'):
            setattr(harness, field, getattr(workbench.harness, field))
        harness.request_stop = lambda: setattr(harness, "accepting", False)
        session = SimpleNamespace(
            thread_id="fixture-thread", turn_id="first-turn", state="completed", turn_done=done,
            model="gpt-6-astra", provider="fixture", effort="high", price_card=None,
            cli_executable=Path("/fixture/codex"), command_prefix=["fixture-sandbox"], actor_dir=workbench.root,
            socket_path=workbench.root / "fixture.sock", env={}, process=SimpleNamespace(pid=-1),
            start=Mock(return_value="fixture-thread"), run_turn=Mock(), close=Mock(),
            model_proxy=SimpleNamespace(limit_notified=False, request_count=1))
        workbench.harness, workbench.session = harness, session
        terminal = SimpleNamespace(start=AsyncMock(), close=AsyncMock(), session_id="fixture-terminal",
                                   process=SimpleNamespace(pid=-1))
        with patch("auriga.apps.gateway.workbench.Terminal", return_value=terminal), \
                patch("auriga.cortex.session.codex_evidence_agent.CodexEvidenceAgent", FakeAgent):
            await workbench.start("unused", benchmark=True)
        session.run_turn.assert_called_once()
        # The unsandboxed actor-source path names the temporary run directory.
        prompt = session.run_turn.call_args.args[0].replace(str(workbench.root), "<run>")
        instructions = session.start.call_args.args[1].replace(str(workbench.root), "<run>")
        manifest = json.loads((workbench.root / "manifest.json").read_text())
        self.saved = [value for value in workbench.store.records("configuration")
                      if value.get("version") == self_briefing.VERSION]
        self.events = [event["type"] for event in workbench.store.events()]
        await workbench.close()
        return workbench, prompt, instructions, manifest

    async def test_default_off_and_disabled_or_failed_leaves_prompt_unchanged(self):
        self.assertFalse(Config().features.self_briefing)
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            _, off_prompt, off_instructions, off_manifest = await self.start(first, enabled=False)
            self.assertEqual(FakeAgent.instances, [])
            self.assertNotIn("self_briefing", off_manifest)
            self.assertNotIn("Operating notes", off_prompt)
            # A failed call is skipped: the actor receives exactly the flag-off text.
            workbench, failed_prompt, failed_instructions, failed_manifest = await self.start(
                second, enabled=True, error=RuntimeError("upstream unavailable"))
            self.assertEqual(len(FakeAgent.instances), 1)
            self.assertEqual(failed_prompt, off_prompt)
            self.assertEqual(failed_instructions, off_instructions)
            record = failed_manifest["self_briefing"]
            self.assertEqual((record["status"], record["notes"], record["error_type"]), ("failed", None, "RuntimeError"))
            self.assertEqual(record["actor_prompt"].replace(str(workbench.root), "<run>"), off_prompt)
            self.assertIsNotNone(record["elapsed_s"])

    async def test_enabled_appends_notes_after_task_text_and_public_rules(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            _, off_prompt, off_instructions, _ = await self.start(first, enabled=False)
            workbench, prompt, instructions, manifest = await self.start(second, enabled=True)
            saved, events = self.saved, self.events
        agent, = FakeAgent.instances
        self.assertEqual(agent.kwargs, dict(model="gpt-6-astra", base_instructions=self_briefing.BASE_INSTRUCTIONS,
                                            developer_instructions=self_briefing.DEVELOPER_INSTRUCTIONS,
                                            source="self_briefing"))
        # Developer instructions are untouched; only the initial task text gains one section.
        self.assertEqual(instructions, off_instructions)
        added = self_briefing.section(NOTES)
        cut = off_prompt.index("\nCurrent native execution budget: ")
        self.assertEqual(prompt, off_prompt[:cut] + added + off_prompt[cut:])
        self.assertTrue(prompt.startswith("Complete the following native RoboDojo task: Fixture native instruction.\n"))
        rules_text = actor_task_rules_text(public_task_rules(TASK))
        self.assertTrue(rules_text)
        self.assertTrue(prompt[:prompt.index(self_briefing.SECTION_HEADING)].endswith(rules_text + "\n"))
        self.assertEqual(prompt.count(NOTES), 1)
        # The helper saw the delivered task text and public rules, not the harness prompt.
        request_text = "\n".join(item["text"] for item in agent.calls[0]["input_items"] if item["type"] == "text")
        self.assertIn("Fixture native instruction.", request_text)
        self.assertIn("Task text:\nFixture native instruction.\n" + rules_text, request_text)
        self.assertNotIn("Current native execution budget", request_text)
        record = manifest["self_briefing"]
        self.assertEqual((record["status"], record["notes"], record["version"]), ("applied", NOTES, self_briefing.VERSION))
        self.assertEqual(record["usage"], dict(input_tokens=1200, output_tokens=90))
        self.assertEqual(record["actor_prompt"].replace(str(workbench.root), "<run>"), prompt)
        self.assertEqual(len(saved), 1)
        self.assertIn("cortex/context/self_briefing.py", manifest["source_sha256"])
        self.assertIn("self_briefing.started", events)
        self.assertIn("self_briefing.applied", events)


class SelfBriefingGenerateCheck(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        identity = dict(run_id="run", episode_id="ep", env_instance_id="env", env_generation=1)
        self.store = Store(Path(self.temporary.name), identity)

    def tearDown(self):
        self.temporary.cleanup()

    def observation(self):
        cameras = [dict(camera_id=name, rgb_ref=self.store.put_artifact(jpeg(color), "rgb", "image/jpeg"))
                   for name, color in (("head", "red"), ("left_wrist", "green"), ("right_wrist", "blue"))]
        return saved_observation(self.store, "initial", cameras)

    def test_request_carries_task_text_and_every_initial_view(self):
        agent = FakeAgent(None, model="gpt-6-astra")
        FakeAgent.error, FakeAgent.answer = None, "  " + NOTES + "  \n"
        record = self_briefing.generate(agent, self.store, self.observation(), "Native task.\nRoboDojo task rules:\nRule.")
        FakeAgent.answer = NOTES
        self.assertEqual((record["status"], record["notes"], record["truncated"]), ("applied", NOTES, False))
        items = agent.calls[0]["input_items"]
        self.assertEqual(items[0], dict(type="text", text=self_briefing.PROMPT))
        self.assertEqual(items[1]["text"], "Task text:\nNative task.\nRoboDojo task rules:\nRule.")
        self.assertEqual([item["text"] for item in items[2::2]],
                         ["Initial view: head", "Initial view: left_wrist", "Initial view: right_wrist"])
        self.assertTrue(all(item["type"] == "image" and item["url"].startswith("data:image/") for item in items[3::2]))
        self.assertEqual([image["camera_id"] for image in record["images"]], ["head", "left_wrist", "right_wrist"])
        self.assertEqual(agent.calls[0]["output_schema"], self_briefing.ANSWER_SCHEMA)

    def test_unusable_answers_fail_without_raising(self):
        for answer, error in (("   ", "ValueError"), (None, "ValueError")):
            with self.subTest(answer=answer):
                agent = FakeAgent(None, model="gpt-6-astra")
                FakeAgent.error, FakeAgent.answer = None, answer
                record = self_briefing.generate(agent, self.store, self.observation(), "Task.")
                self.assertEqual((record["status"], record["notes"], record["error_type"]), ("failed", None, error))
        FakeAgent.answer = "word " * 1000
        record = self_briefing.generate(FakeAgent(None, model="gpt-6-astra"), self.store, self.observation(), "Task.")
        self.assertEqual(record["status"], "applied")
        self.assertTrue(record["truncated"])
        self.assertLessEqual(len(record["notes"]), self_briefing.MAX_CHARS)
        FakeAgent.answer = NOTES

    def test_model_mismatch_is_not_applied(self):
        class Other(FakeAgent):
            def review_evidence(self, **kwargs):
                return dict(super().review_evidence(**kwargs), model="other-model")
        FakeAgent.error = None
        record = self_briefing.generate(Other(None, model="gpt-6-astra"), self.store, self.observation(), "Task.")
        self.assertEqual(record["status"], "failed")


class SelfBriefingContentBoundaryCheck(unittest.TestCase):
    def test_new_model_facing_text_names_no_task(self):
        catalog = json.loads(Path(self_briefing.__file__).with_name("robodojo_task_rules.json").read_text(encoding="utf-8"))
        overrides = json.loads(Path(self_briefing.__file__).with_name("task_instruction_overrides.json").read_text(encoding="utf-8"))
        names = set(catalog["tasks"]) | set(overrides["tasks"]) | set(overrides.get("aliases", {}))
        self.assertGreater(len(names), 40)
        text = "\n".join((self_briefing.BASE_INSTRUCTIONS, self_briefing.DEVELOPER_INSTRUCTIONS, self_briefing.PROMPT,
                          self_briefing.SECTION_HEADING, json.dumps(self_briefing.ANSWER_SCHEMA))).lower()
        for name in sorted(names):
            for form in (name.lower(), name.lower().replace("_", " ")):
                self.assertNotIn(form, text, name)
        # No evaluator values: the only number is the public word limit.
        self.assertEqual(sorted(set(re.findall(r"\d+(?:\.\d+)?", text))), ["1", "150", "2", "3"])

    def test_evidence_agent_defaults_are_unchanged(self):
        agent = CodexEvidenceAgent(SimpleNamespace(store=None))
        self.assertEqual((agent.base_instructions, agent.developer_instructions, agent.source),
                         (EVIDENCE_BASE_INSTRUCTIONS, "Use only the supplied evidence packet.", "sol_visual_review"))


if __name__ == "__main__":
    unittest.main()
