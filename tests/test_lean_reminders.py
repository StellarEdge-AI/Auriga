"""Decision-context reminders under the block projection (features.lean_projection)."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from auriga.core.presentation.lean_projection import place_block_reminders, tool_steps

from test_lean_projection import STEPS, body

PREFIX = "Current task decision boundary\nThe saved plan and attention"


def native(count):
    """Native Codex input after ``count`` tool steps (no proxy additions)."""
    items = [{"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "rules"}]},
             {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "task"}]}]
    for index in range(count):
        tool, arguments, output = STEPS[index % len(STEPS)]
        call_id = f"call_{index}"
        items += [{"type": "reasoning", "encrypted_content": f"enc{index}"},
                  {"type": "function_call", "name": tool, "arguments": json.dumps(arguments), "call_id": call_id},
                  {"type": "function_call_output", "call_id": call_id, "output": deepcopy(output)}]
    return items


def reminder(label):
    return {"type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": PREFIX + " " + label}]}


def reminders(items):
    return [item["content"][0]["text"] for item in items
            if isinstance(item, dict) and item.get("role") == "assistant"
            and item["content"][0]["text"].startswith(PREFIX)]


def is_prefix(previous, current):
    return body(current[:len(previous)]) == body(previous)


def ends_with_reminder(items):
    last = items[-1]
    return (isinstance(last, dict) and last.get("role") == "assistant"
            and last["content"][0]["text"].startswith(PREFIX))


class PlaceBlockRemindersTests(unittest.TestCase):
    def test_requests_extend_and_reminders_sit_at_fixed_slots(self):
        block, slots, sent = 3, {}, {}
        for count in range(0, 11):
            items = native(count)
            original = body(items)
            sent[count] = place_block_reminders(items, reminder(f"rev{count}"), block, slots)
            self.assertEqual(body(items), original)  # input list is not modified
        for count in range(0, 10):
            # Without the projection every request extends the previous one.
            self.assertTrue(is_prefix(sent[count], sent[count + 1]), count)
        for count in range(0, 11):
            # The reminder is never the last item (the request that creates a slot included).
            self.assertFalse(ends_with_reminder(sent[count]), count)
        self.assertEqual(sent[0][-1]["role"], "user")
        # Slot 0 before the task message, then one reminder per block (anchors 1, 4, 7, 10).
        self.assertEqual(reminders(sent[10]), [PREFIX + " rev0", PREFIX + " rev1", PREFIX + " rev4",
                                               PREFIX + " rev7", PREFIX + " rev10"])
        self.assertEqual(sent[10][1]["content"][0]["text"], PREFIX + " rev0")
        self.assertEqual(sent[10][2]["content"][0]["text"], "task")
        steps = tool_steps(sent[10])
        # Each block slot sits directly before the anchor's model turn (reasoning + call).
        for anchor in (1, 4, 7, 10):
            at = steps[anchor - 1][0] - 3
            self.assertEqual(sent[10][at + 1]["type"], "reasoning", anchor)
            self.assertTrue(sent[10][at]["content"][0]["text"].startswith(PREFIX), anchor)
            if anchor > 1:
                self.assertEqual(at, steps[anchor - 2][-1] + 1, anchor)
        # Inside a block the open slot keeps the snapshot of the block's first request.
        self.assertEqual(reminders(sent[6])[-1], PREFIX + " rev4")

    def test_first_plan_gets_a_new_slot_without_rewriting(self):
        slots = {}
        first = place_block_reminders(native(0), reminder("no plan"), 8, slots, planned=False)
        second = place_block_reminders(native(1), reminder("no plan"), 8, slots, planned=False)
        third = place_block_reminders(native(2), reminder("plan rev1"), 8, slots, planned=True)
        fourth = place_block_reminders(native(3), reminder("plan rev2"), 8, slots, planned=True)
        # The "no plan" variant only in slot 0; no block slot until a plan is stored.
        self.assertEqual(reminders(first), [PREFIX + " no plan"])
        self.assertEqual(reminders(second), [PREFIX + " no plan"])
        # The first planned request creates the block's slot before its newest step, then it is frozen.
        self.assertEqual(reminders(third), [PREFIX + " no plan", PREFIX + " plan rev1"])
        self.assertEqual(reminders(fourth), [PREFIX + " no plan", PREFIX + " plan rev1"])
        steps = tool_steps(third)
        self.assertEqual(third[steps[0][-1] + 1]["content"][0]["text"], PREFIX + " plan rev1")
        for previous, current in ((first, second), (second, third), (third, fourth)):
            self.assertTrue(is_prefix(previous, current))
        for request in (first, second, third, fourth):
            self.assertFalse(ends_with_reminder(request))

    def test_changed_history_drops_stale_slots(self):
        slots = {}
        for count in range(0, 6):
            place_block_reminders(native(count), reminder(f"rev{count}"), 2, slots)
        compacted = [{"type": "compaction", "encrypted_content": "summary"}] + native(1)[2:]
        result = place_block_reminders(compacted, reminder("after compaction"), 2, slots)
        self.assertEqual(reminders(result), [PREFIX + " after compaction"])
        self.assertEqual(sorted(slots), [1])


class SessionReminderTests(unittest.TestCase):
    """The decision hook followed by the request projection, in model-proxy order."""

    def session(self, features):
        try:
            from auriga.cortex.session.codex import CodexSession
        except ImportError as error:
            self.skipTest(str(error))
        from auriga.core.config import Config
        from auriga.core.storage.store import Store
        from auriga.cortex.context.task_planning import TaskPlanning
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.identity = dict(run_id="lean", episode_id="episode", env_instance_id="environment", env_generation=1)
        store = Store(root / "run", self.identity)
        self.addCleanup(store.close)
        config = Config.from_dict({"model": {"codex_home": str(root / "fixture-config")}, "features": features})
        session = CodexSession(store.root, store, lambda _: {}, config=config)
        self.addCleanup(session.close)
        session.thread_id = "thread-owned"
        session.task_planning = TaskPlanning(store)
        return session

    def forward(self, session, count):
        identity = dict(source="actor", native_thread_id=session.thread_id, endpoint="/responses")
        request = session._task_decision_context({"input": native(count)}, identity)
        return session._present_model_request(request)["input"]

    def plan(self, session, revision):
        observation = dict(self.identity, observation_id=f"obs_{revision}")
        if revision == 1:
            arguments = dict(plan="Move and verify the target.", ledger=[])
        else:
            arguments = dict(operation="update", plan=f"Plan revision {revision}.")
        session.task_planning.call(arguments, self.identity, observation, {})

    def test_block_projection_keeps_requests_extendable_with_the_hook(self):
        session = self.session({"lean_projection": True, "lean_projection_block": 3})
        sent = {0: self.forward(session, 0)}
        self.plan(session, 1)
        for count in range(1, 10):
            if count in (2, 5):
                self.plan(session, count)
            sent[count] = self.forward(session, count)
        for count in range(0, 10):
            self.assertFalse(ends_with_reminder(sent[count]), count)
        for count in range(0, 9):
            if count and count % 3 == 0:
                # Step count+1 opens a block: the closed block is rewritten once and
                # the request whose latest step was its anchor stays a prefix.
                self.assertFalse(is_prefix(sent[count], sent[count + 1]), count)
                self.assertTrue(is_prefix(sent[count - 2], sent[count + 1]), count)
            else:
                self.assertTrue(is_prefix(sent[count], sent[count + 1]), count)
        texts = reminders(sent[9])
        self.assertEqual(len(texts), 4)  # slot 0 and anchors 1, 4, 7
        self.assertIn("No plan is stored", texts[0])
        self.assertEqual(json.loads(texts[1].splitlines()[-1])["revision"], 1)
        self.assertEqual(json.loads(texts[2].splitlines()[-1])["revision"], 2)
        self.assertEqual(json.loads(texts[3].splitlines()[-1])["revision"], 3)

    def test_without_block_projection_the_hook_appends_at_the_end(self):
        for features in ({}, {"lean_projection": True, "lean_projection_block": 0}):
            session = self.session(features)
            self.plan(session, 1)
            first, second = self.forward(session, 2), self.forward(session, 3)
            self.assertEqual(reminders(second), [], features)
            self.assertEqual(session.lean_reminder_slots, {})
            if not features:
                self.assertTrue(is_prefix(first, second))
            compact_identity = dict(source="actor", native_thread_id=session.thread_id, endpoint="/responses/compact")
            compacted = session._present_model_request(
                session._task_decision_context({"input": native(3)}, compact_identity)
            )["input"]
            self.assertEqual(len(reminders(compacted)), 1, features)
            self.assertTrue(compacted[-1]["content"][0]["text"].startswith(PREFIX))


if __name__ == "__main__":
    unittest.main()
