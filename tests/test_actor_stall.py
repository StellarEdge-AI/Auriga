"""Repeated completed inability answers stop a benchmark for native evaluation."""
import asyncio
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from auriga.cortex.supervision.actor_stall import (
    ACTOR_STALL_LIMIT, ACTOR_STALL_REASON, ActorStallTracker, inability_reason,
)
from auriga.cortex.session.codex import CodexSession
from auriga.apps.evaluation.episode import BENCHMARK_CONTINUATION, wait_benchmark
from auriga.apps.library.scoring import classify_episode_result
from auriga.core.storage.store import Store


# Exact first and last final answers from the recorded bottle-run incident.
FIRST_RECORDED_FINAL = (
    "Both return commands completed using the recovered initial positions and orientations. "
    "Both grippers are open and empty.\n\n"
    "The environment still has not ended: three bottles are inside the bin, while the blue "
    "bottle remains outside beyond either arm’s reach. The task cannot be completed with "
    "the available tools."
)
LAST_RECORDED_FINAL = (
    "Both return commands completed using the specified initial observation.\n\n"
    "The remaining blue bottle landed outside the bin and is beyond either arm’s reach. "
    "Three bottles are inside, both grippers are empty and open, and no feasible recovery "
    "action remains."
)
# Chronological first five completed finals from the recorded-run audit. Keep
# these literal: the regression must not depend on an archived run or /tmp.
FIRST_FIVE_RECORDED_FINALS = (
    "Both return commands completed using the recovered initial positions and orientations. "
    "Both grippers are open and empty.\n\n"
    "The environment still has not ended: three bottles are inside the bin, while the blue "
    "bottle remains outside beyond either arm’s reach. The task cannot be completed with "
    "the available tools.",
    "Both arms have returned successfully to the specified observation’s initial link6 poses, "
    "with grippers open and empty.\n\n"
    "Placement remains incomplete: three bottles are inside the bin; the blue bottle is "
    "outside, beyond both arms’ reach. No feasible recovery action is available with the "
    "permitted tools.",
    "The return requirement is satisfied and verified for both arms using the specified "
    "initial observation.\n\n"
    "The task remains incomplete because the blue bottle is outside the bin, approximately "
    "1.9 m from the nearest arm base—beyond its reach. Three bottles are inside. No further "
    "feasible action is available without external repositioning.",
    "Both arms are already at the recovered initial poses, with empty, open grippers.\n\n"
    "The unresolved issue is the blue bottle outside the bin and beyond either arm’s reach. "
    "Three bottles are inside; completion cannot be achieved with the available tools.",
    "Unable to complete: the blue bottle landed outside the bin, beyond both arms’ reach. "
    "Three bottles are inside.\n\n"
    "Both arms have successfully returned to the initial link6 poses recovered from "
    "`obs-ab87c2ad4f3b45a191186c68ec233027`, with grippers open and empty.",
)
IDENTITY = dict(run_id="synthetic-actor-stall", episode_id="episode-1",
                env_instance_id="fixture-env", env_generation=1)


class ActorStallTrackerChecks(unittest.TestCase):
    def observe(self, tracker, turn, **changes):
        values = dict(thread_id="thread-1", turn_id=turn, episode_id="episode-1",
                      native_action_count=12, text="I cannot continue.")
        values.update(changes)
        return tracker.observe(**values)

    def test_recorded_and_explicit_bilingual_inability_declarations_match(self):
        for text in (FIRST_RECORDED_FINAL, LAST_RECORDED_FINAL,
                     "I cannot continue.", "We are unable to complete the task.",
                     "I can't safely proceed.", "No safe next action is available.",
                     "我无法继续完成任务。", "无法完成任务，已经没有可行的恢复动作。",
                     "没有可执行动作。", "不能继续执行。"):
            with self.subTest(text=text):
                self.assertIsInstance(inability_reason(text), str)

    def test_completion_temporary_failure_and_continuation_plans_do_not_match(self):
        for text in (None, "", "The task is complete.", "Both arms are home.",
                     "The last movement failed; I will retry with a different pose.",
                     "I cannot continue with this pose. I will try another approach.",
                     "I cannot complete this route. I'll try a different pose.",
                     'The tool returned "I cannot continue." I am checking the result.',
                     "> I cannot continue.\nThat was the previous answer.",
                     "```text\nI cannot continue.\n```\nThe example above describes an old run.",
                     "当前无法完成任务，下一步尝试移动左臂。",
                     "工具无法继续执行；我会尝试恢复。"):
            with self.subTest(text=text):
                self.assertIsNone(inability_reason(text))

    def test_fifth_unique_inability_triggers_and_records_evidence(self):
        tracker = ActorStallTracker()
        self.assertEqual(ACTOR_STALL_LIMIT, 5)
        self.assertEqual(ACTOR_STALL_REASON, "actor_repeated_inability")
        for count in range(1, 6):
            answer = FIRST_RECORDED_FINAL if count == 1 else LAST_RECORDED_FINAL
            result = self.observe(tracker, f"turn-{count}", text=answer)
            self.assertEqual(result["count"], count)
            self.assertEqual(result["limit"], 5)
            self.assertEqual(result["triggered"], count == 5)
            self.assertTrue(result["matched_reason"])
            self.assertEqual(result["answer_excerpt"], answer)
            self.assertEqual(result["turn_id"], f"turn-{count}")
            self.assertEqual(result["thread_id"], "thread-1")
            self.assertEqual(result["episode_id"], "episode-1")
            self.assertEqual(result["native_action_count"], 12)

    def test_first_five_chronological_recorded_finals_trigger_exactly_on_fifth(self):
        tracker = ActorStallTracker()
        self.assertEqual(len(FIRST_FIVE_RECORDED_FINALS), 5)
        for count, text in enumerate(FIRST_FIVE_RECORDED_FINALS, 1):
            with self.subTest(completed_final=count):
                result = self.observe(tracker, f"recorded-turn-{count}", text=text)
                self.assertTrue(result["matched_reason"])
                self.assertEqual(result["count"], count)
                self.assertEqual(result["triggered"], count == 5)

    def test_local_recovery_problem_with_new_plan_and_quoted_declarations_do_not_count(self):
        for text in (
            "No feasible recovery is available with this grasp. I will try another approach.",
            'The previous answer said "completion cannot be achieved with the available tools."',
            '> The permitted tools cannot recover the fourth bottle.\nThis quotes an earlier assessment.',
            '```text\nFurther progress requires external repositioning.\n```\nThis is archived text.',
        ):
            with self.subTest(text=text):
                self.assertIsNone(inability_reason(text))
                tracker = ActorStallTracker()
                result = self.observe(tracker, "new-turn", text=text)
                self.assertEqual(result["count"], 0)
                self.assertFalse(result["triggered"])

    def test_native_progress_or_counter_reset_starts_a_new_streak(self):
        for next_action_count in (13, 0):
            with self.subTest(next_action_count=next_action_count):
                tracker = ActorStallTracker()
                for count in range(1, 5):
                    self.observe(tracker, f"turn-{count}")
                result = self.observe(tracker, "turn-5", native_action_count=next_action_count)
                self.assertEqual(result["count"], 1)
                self.assertFalse(result["triggered"])

    def test_thread_and_episode_changes_reset_even_if_turn_id_is_reused(self):
        for changes in ({"thread_id": "thread-2"}, {"episode_id": "episode-2"}):
            with self.subTest(changes=changes):
                tracker = ActorStallTracker()
                for count in range(1, 5):
                    self.observe(tracker, f"turn-{count}")
                result = self.observe(tracker, "turn-4", **changes)
                self.assertEqual(result["count"], 1)
                self.assertFalse(result["triggered"])

    def test_non_inability_final_clears_streak(self):
        tracker = ActorStallTracker()
        for count in range(1, 5):
            self.observe(tracker, f"turn-{count}")
        result = self.observe(tracker, "turn-5", text="I will inspect and move the left arm.")
        self.assertEqual(result["count"], 0)
        self.assertIsNone(result["matched_reason"])
        self.assertFalse(result["triggered"])
        self.assertEqual(self.observe(tracker, "turn-6")["count"], 1)

    def test_missing_identity_and_duplicate_turns_never_increment(self):
        tracker = ActorStallTracker()
        self.assertEqual(self.observe(tracker, "turn-1")["count"], 1)
        for changes in ({"turn_id": None}, {"thread_id": None}, {"episode_id": None}):
            self.assertIsNone(self.observe(tracker, "ignored", **changes))
        self.assertIsNone(self.observe(tracker, "turn-1"))
        self.assertEqual(self.observe(tracker, "turn-2")["count"], 2)
        self.assertIsNone(self.observe(tracker, "turn-1"))
        self.assertEqual(self.observe(tracker, "turn-3")["count"], 3)

    def test_duplicate_after_native_progress_clears_streak_without_counting(self):
        tracker = ActorStallTracker()
        for count in range(1, 5):
            self.observe(tracker, f"turn-{count}")
        self.assertIsNone(self.observe(tracker, "turn-4", native_action_count=13))
        result = self.observe(tracker, "turn-5", native_action_count=13)
        self.assertEqual(result["count"], 1)
        self.assertFalse(result["triggered"])


class CompletedActorFinalChecks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(Path(self.temporary.name) / "store", IDENTITY)
        self.addCleanup(self.store.close)
        self.session = CodexSession(self.temporary.name, self.store, lambda params: None)
        self.session.thread_id = "owned-thread"
        self.session.turn_id = "current-turn"
        self.session.state = "completed"
        self.session.turn_done.set()

    def item(self, identifier, **changes):
        item = dict(id=identifier, thread_id="owned-thread", turn_id="current-turn",
                    type="agentMessage", phase="final_answer", status="completed",
                    text="I cannot continue.")
        item.update(changes)
        self.store.set_state("codex_item", identifier, item)

    def test_only_completed_current_turn_final_agent_messages_are_read(self):
        self.item("foreign-thread", thread_id="other-thread")
        self.item("earlier-turn", turn_id="previous-turn")
        self.item("commentary", phase="commentary")
        self.item("partial-final", status="running")
        self.item("tool-output", type="dynamicToolCall")
        self.item("unphased", phase=None)
        self.item("empty", text=" ")
        self.assertIsNone(self.session.completed_final_answer())
        self.item("owned-final", text=LAST_RECORDED_FINAL)
        self.assertEqual(self.session.completed_final_answer(), LAST_RECORDED_FINAL)

    def test_completed_items_are_not_read_before_completed_turn(self):
        self.item("final")
        for state in ("running", "failed", "interrupted", "disconnected"):
            with self.subTest(state=state):
                self.session.state = state
                self.assertIsNone(self.session.completed_final_answer())
        self.session.state = "completed"
        self.session.turn_done.clear()
        self.assertIsNone(self.session.completed_final_answer())

    def test_real_notification_projection_preserves_final_phase_and_scope(self):
        self.session.state = "running"
        self.session.turn_done.clear()
        for identifier, phase in (("commentary", "commentary"), ("final", "final_answer")):
            self.session._notification(dict(method="item/completed", params=dict(
                threadId="owned-thread", turnId="current-turn",
                item=dict(id=identifier, type="agentMessage", phase=phase,
                          text=FIRST_RECORDED_FINAL))))
        self.assertIsNone(self.session.completed_final_answer())
        self.session._notification(dict(method="turn/completed", params=dict(
            threadId="owned-thread", turn=dict(id="current-turn", status="completed"))))
        self.assertEqual(self.session.completed_final_answer(), FIRST_RECORDED_FINAL)


class ActorStallBenchmarkChecks(unittest.IsolatedAsyncioTestCase):
    def workbench(self):
        done = threading.Event()
        done.set()
        session = SimpleNamespace(state="completed", turn_done=done, turn_id="turn-1",
            thread_id="same-thread", turn_result={"status": "completed"},
            model_limit_reached=threading.Event(),
            model_proxy=SimpleNamespace(limit_notified=False, request_count=3),
            completed_final_answer=Mock(return_value=LAST_RECORDED_FINAL))
        harness = SimpleNamespace(episode_done=threading.Event(), episode_terminal=None,
            accepting=True, native_action_count=12, started_at=time.monotonic(),
            last_native_progress_at=time.monotonic(), active_action=None)
        return SimpleNamespace(session=session, harness=harness, completion_guidance="none",
            store=SimpleNamespace(event=Mock()), identity=dict(IDENTITY),
            current_unknown_actions=lambda: False, has_active_programs=lambda: False,
            control_lock=asyncio.Lock())

    async def test_five_finals_stop_before_sixth_turn_and_keep_native_clocks(self):
        workbench = self.workbench()
        session, harness = workbench.session, workbench.harness
        started, progress, proxy = harness.started_at, harness.last_native_progress_at, session.model_proxy
        def continue_turn(prompt):
            self.assertEqual(prompt, BENCHMARK_CONTINUATION)
            session.turn_id = f"turn-{session.run_turn.call_count + 1}"
            proxy.request_count += 1
            self.assertLessEqual(session.run_turn.call_count, 4, "a sixth actor turn must never start")
        session.run_turn = Mock(side_effect=continue_turn)
        finished = await asyncio.wait_for(wait_benchmark(workbench, asyncio.Event()), timeout=5)
        self.assertFalse(finished)
        self.assertEqual(session.run_turn.call_count, 4)
        self.assertEqual(session.completed_final_answer.call_count, 5)
        self.assertEqual(harness.wait_stop_reason, ACTOR_STALL_REASON)
        self.assertEqual(harness.actor_stall["count"], 5)
        self.assertTrue(harness.actor_stall["triggered"])
        self.assertEqual(harness.actor_continuation_count, 4)
        self.assertFalse(harness.episode_done.is_set())
        self.assertEqual(session.turn_id, "turn-5")
        self.assertEqual(session.thread_id, "same-thread")
        self.assertIs(session.model_proxy, proxy)
        self.assertEqual(proxy.request_count, 7)
        self.assertEqual(harness.native_action_count, 12)
        self.assertEqual(harness.started_at, started)
        self.assertEqual(harness.last_native_progress_at, progress)
        evidence = [call.args[1] for call in workbench.store.event.call_args_list
                    if call.args[0] == "benchmark.actor_inability"]
        self.assertEqual([item["count"] for item in evidence], [1, 2, 3, 4, 5])
        self.assertTrue(evidence[-1]["triggered"])

    async def test_native_progress_during_continuation_requires_five_new_finals(self):
        workbench = self.workbench()
        session, harness = workbench.session, workbench.harness
        def continue_turn(prompt):
            calls = session.run_turn.call_count
            session.turn_id = f"turn-{calls + 1}"
            if calls == 4:
                harness.native_action_count += 1
            self.assertLessEqual(calls, 8)
        session.run_turn = Mock(side_effect=continue_turn)
        self.assertFalse(await asyncio.wait_for(wait_benchmark(workbench, asyncio.Event()), timeout=5))
        self.assertEqual(session.run_turn.call_count, 8)
        self.assertEqual(harness.wait_stop_reason, ACTOR_STALL_REASON)
        self.assertEqual(harness.actor_stall["count"], 5)

    async def test_safety_recheck_precedes_reading_and_counting_final(self):
        for condition in ("native", "budget", "timeout", "unknown", "active_action", "active_program"):
            with self.subTest(condition=condition):
                workbench = self.workbench()
                session, harness = workbench.session, workbench.harness
                session.run_turn = Mock()
                class ContendedLock:
                    async def __aenter__(self):
                        if condition == "native": harness.episode_done.set()
                        if condition == "budget": session.model_limit_reached.set()
                        if condition == "timeout":
                            harness.started_at -= 1801
                            harness.last_native_progress_at -= 1801
                        if condition == "unknown": workbench.current_unknown_actions = lambda: True
                        if condition == "active_action": harness.active_action = "unfinished"
                        if condition == "active_program": workbench.has_active_programs = lambda: True
                    async def __aexit__(self, *arguments):
                        pass
                workbench.control_lock = ContendedLock()
                self.assertFalse(await wait_benchmark(workbench, asyncio.Event()))
                session.run_turn.assert_not_called()
                session.completed_final_answer.assert_not_called()
                workbench.store.event.assert_not_called()


class ActorStallScoringChecks(unittest.TestCase):
    def snapshot(self, **changes):
        result = dict(benchmark=True, termination_reason=ACTOR_STALL_REASON,
            valid_episode=True, task_success=False, normalized_score=.4,
            native_process_score=40, native_reward=0, native_episode={"ended": False},
            authority="installed RoboDojo task.run_reward / RewardManager",
            success_authority="native_final_check")
        result.update(changes)
        return result

    def test_current_native_failure_is_scored_without_inventing_native_termination(self):
        snapshot = self.snapshot()
        result = classify_episode_result(snapshot, "stopped")
        self.assertEqual(result["primary_outcome"], "scored_failure")
        self.assertTrue(result["scored_episode"])
        self.assertFalse(result["task_success"])
        self.assertEqual(result["normalized_score"], .4)
        self.assertEqual(result["native_process_score"], 40)
        self.assertEqual(result["termination_authority"], "harness")
        self.assertFalse(result["native_episode"]["ended"])
        self.assertNotIn("partial_native_result", result)
        self.assertEqual(snapshot, self.snapshot(), "classification must not mutate input")

    def test_task_limit_is_failure_even_when_current_snapshot_reports_success(self):
        for success, score in ((True, 1.), (False, 0.)):
            with self.subTest(success=success):
                result = classify_episode_result(self.snapshot(task_success=success, normalized_score=score,
                                                               native_process_score=score * 100), "stopped")
                self.assertTrue(result["scored_episode"])
                self.assertEqual(result["primary_outcome"], "scored_failure")
                self.assertEqual(result["normalized_score"], score)
                self.assertIs(result["task_success"], False)

    def test_unknown_and_infrastructure_remain_unscored(self):
        for status, changes in (("unknown", {}), ("stopped", {"infrastructure_error": "robot disconnected"}),
                                ("failed", {})):
            with self.subTest(status=status, changes=changes):
                result = classify_episode_result(self.snapshot(**changes), status)
                self.assertEqual(result["primary_outcome"], "unscored_infrastructure")
                self.assertFalse(result["scored_episode"])
                self.assertIsNone(result["normalized_score"])
                self.assertIsNone(result["task_success"])
                self.assertEqual(result["partial_native_result"]["normalized_score"], .4)

    def test_invalid_native_evaluation_does_not_score_an_inability_stop(self):
        invalid = [{"valid_episode": False}, {"valid_episode": None}, {"valid_episode": 1},
                   {"task_success": None}, {"task_success": "false"}, {"task_success": 0}]
        invalid += [{"normalized_score": score} for score in
                    (None, "0.4", True, float("nan"), float("inf"), -.1, 1.1)]
        for changes in invalid:
            with self.subTest(changes=changes):
                result = classify_episode_result(self.snapshot(**changes), "stopped")
                self.assertFalse(result["scored_episode"])
                self.assertIsNone(result["normalized_score"])
                if "valid_episode" in changes:
                    self.assertIsNone(result["task_success"])
                else:
                    self.assertFalse(result["task_success"])
        snapshot = self.snapshot()
        del snapshot["normalized_score"]
        self.assertFalse(classify_episode_result(snapshot, "stopped")["scored_episode"])
        self.assertFalse(classify_episode_result(snapshot, "stopped")["task_success"])

    def test_task_limits_score_current_failure_and_external_interruption_remains_excluded(self):
        for reason, outcome in (("official_no_action_progress_timeout", "scored_failure"),
                                ("official_startup_no_action_timeout", "scored_failure"),
                                ("model_request_limit", "scored_failure"),
                                ("development_wall_budget", "scored_failure"),
                                ("shutdown-signal", "unscored_interrupted")):
            with self.subTest(reason=reason):
                result = classify_episode_result(self.snapshot(termination_reason=reason), "stopped")
                self.assertEqual(result["primary_outcome"], outcome)
                if outcome == "scored_failure":
                    self.assertTrue(result["scored_episode"])
                    self.assertFalse(result["task_success"])
                    self.assertEqual(result["normalized_score"], .4)
                else:
                    self.assertFalse(result["scored_episode"])
                    self.assertIsNone(result["normalized_score"])
                    self.assertEqual(result["partial_native_result"]["normalized_score"], .4)


if __name__ == "__main__":
    unittest.main()
