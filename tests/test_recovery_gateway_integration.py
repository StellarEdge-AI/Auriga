"""Continuation cannot race public terminal or unresolved-execution callbacks."""

import asyncio
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from auriga.apps.evaluation.episode import wait_benchmark
from auriga.cortex.supervision.recovery_evidence import completed_turn_evidence
from auriga.core.storage.store import Store


class RecoveryGatewayIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def make_workbench(self, root, *, bounded=True, evidence=True):
        store = Store(root, dict(run_id="r", episode_id="e", env_instance_id="env", env_generation=1))
        done = threading.Event()
        done.set()
        session = SimpleNamespace(state="completed", turn_done=done, turn_id="turn-0", thread_id="thread",
            model_limit_reached=threading.Event(), model_proxy=SimpleNamespace(limit_notified=False, request_count=1),
            completed_final_answer=Mock(return_value="I cannot continue."))
        harness = SimpleNamespace(episode_done=threading.Event(), episode_terminal=None, accepting=True,
            native_action_count=99, started_at=time.monotonic(), last_native_progress_at=time.monotonic(),
            active_action=None, wait_stop_reason=None)
        workbench = SimpleNamespace(session=session, harness=harness, completion_guidance="none", store=store,
            config=SimpleNamespace(features=SimpleNamespace(bounded_continuation=bounded, recovery_evidence=evidence)),
            identity=dict(episode_id="e"), current_unknown_actions=lambda: False, has_active_programs=lambda: False,
            control_lock=asyncio.Lock())
        def advance(prompt):
            session.turn_id = "turn-" + str(session.run_turn.call_count)
            session.model_proxy.request_count += 1
            self.assertLess(session.run_turn.call_count, 15)
        session.run_turn = Mock(side_effect=advance)
        return workbench

    async def test_callback_during_baseline_evidence_read_precedes_any_continuation(self):
        for condition in ("terminal", "model_limit", "unknown", "action", "program", "not_accepting", "shutdown"):
            with self.subTest(condition=condition), tempfile.TemporaryDirectory() as root:
                workbench = self.make_workbench(Path(root))
                shutdown = asyncio.Event()
                def evidence(*args, **kwargs):
                    result = completed_turn_evidence(*args, **kwargs)
                    if condition == "terminal": workbench.harness.episode_done.set()
                    elif condition == "model_limit": workbench.session.model_proxy.limit_notified = True
                    elif condition == "unknown": workbench.current_unknown_actions = lambda: True
                    elif condition == "action": workbench.harness.active_action = "inflight"
                    elif condition == "program": workbench.has_active_programs = lambda: True
                    elif condition == "not_accepting": workbench.harness.accepting = False
                    else: shutdown.set()
                    return result
                try:
                    with patch("auriga.cortex.supervision.recovery_evidence.completed_turn_evidence", side_effect=evidence):
                        result = await asyncio.wait_for(wait_benchmark(workbench, shutdown), 2)
                    self.assertFalse(result)
                    workbench.session.run_turn.assert_not_called()
                    self.assertNotEqual(workbench.harness.wait_stop_reason, "actor_no_progress")
                finally:
                    workbench.store.close()

    async def test_evidence_off_never_reads_new_evidence_and_retains_three_opportunities(self):
        with tempfile.TemporaryDirectory() as root:
            workbench = self.make_workbench(Path(root), evidence=False)
            try:
                with patch("auriga.cortex.supervision.recovery_evidence.completed_turn_evidence", side_effect=AssertionError("disabled")):
                    self.assertFalse(await asyncio.wait_for(wait_benchmark(workbench, asyncio.Event()), 2))
                self.assertEqual(workbench.session.run_turn.call_count, 3)
                self.assertEqual(workbench.harness.actor_stall["version"], "bounded_completed_final_progress_v1")
            finally:
                workbench.store.close()

    async def test_terminal_callback_while_recording_stall_retains_native_priority(self):
        with tempfile.TemporaryDirectory() as root:
            workbench = self.make_workbench(Path(root))
            event = workbench.store.event
            def record(kind, payload, *args, **kwargs):
                result = event(kind, payload, *args, **kwargs)
                if kind == "benchmark.actor_progress" and payload.get("triggered"):
                    workbench.harness.episode_done.set()
                return result
            try:
                with patch.object(workbench.store, "event", side_effect=record):
                    self.assertFalse(await asyncio.wait_for(wait_benchmark(workbench, asyncio.Event()), 2))
                self.assertEqual(workbench.harness.wait_stop_reason, "native_episode_ended")
                self.assertEqual(workbench.session.run_turn.call_count, 2)
            finally:
                workbench.store.close()

    async def test_unknown_callback_while_formatting_guidance_prevents_new_turn(self):
        from auriga.cortex.supervision.recovery_guidance import recovery_guidance
        with tempfile.TemporaryDirectory() as root:
            workbench = self.make_workbench(Path(root))
            def guidance(*args, **kwargs):
                result = recovery_guidance(*args, **kwargs)
                workbench.current_unknown_actions = lambda: True
                return result
            try:
                with patch("auriga.cortex.supervision.recovery_guidance.recovery_guidance", side_effect=guidance):
                    self.assertFalse(await asyncio.wait_for(wait_benchmark(workbench, asyncio.Event()), 2))
                workbench.session.run_turn.assert_not_called()
                self.assertEqual(workbench.harness.wait_stop_reason, "harness_stop_before_continuation")
            finally:
                workbench.store.close()

    async def test_bounded_off_overrides_evidence_on_and_preserves_legacy_inability_policy(self):
        with tempfile.TemporaryDirectory() as root:
            workbench = self.make_workbench(Path(root), bounded=False)
            try:
                with patch("auriga.cortex.supervision.recovery_evidence.completed_turn_evidence", side_effect=AssertionError("legacy")):
                    self.assertFalse(await asyncio.wait_for(wait_benchmark(workbench, asyncio.Event()), 2))
                self.assertEqual(workbench.session.run_turn.call_count, 4)
                self.assertEqual(workbench.harness.wait_stop_reason, "actor_repeated_inability")
            finally:
                workbench.store.close()


if __name__ == "__main__":
    unittest.main()
