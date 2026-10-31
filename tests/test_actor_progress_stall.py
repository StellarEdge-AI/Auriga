"""Completed-final progress budgets use public execution, never wording or GT."""
import asyncio
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from auriga.cortex.supervision.actor_stall import (
    ACTOR_PROGRESS_IDLE_LIMIT, ACTOR_PROGRESS_RECOVERY_LIMIT, ACTOR_PROGRESS_REASON,
    ActorProgressTracker, completed_turn_activity, recovery_fingerprints,
)
from auriga.apps.evaluation.episode import wait_benchmark
from auriga.core.storage.store import Store


class ProgressTrackerTests(unittest.TestCase):
    def observe(self, tracker, turn, **changes):
        values = dict(thread_id="thread", turn_id=f"turn-{turn}", episode_id="episode",
                      native_action_count=606, activity=())
        values.update(changes)
        return tracker.observe(**values)

    def test_baseline_then_three_empty_recovery_turns(self):
        tracker = ActorProgressTracker()
        first = self.observe(tracker, 1)
        self.assertTrue(first["baseline"])
        self.assertEqual(first["count"], 0)
        for index in range(1, ACTOR_PROGRESS_IDLE_LIMIT + 1):
            result = self.observe(tracker, index + 1)
            self.assertEqual(result["count"], index)
            self.assertEqual(result["triggered"], index == ACTOR_PROGRESS_IDLE_LIMIT)
        self.assertEqual(result["reason"], "idle_final_limit")

    def test_distinct_investigation_is_bounded_at_six_continuations(self):
        tracker = ActorProgressTracker()
        self.observe(tracker, 0)
        for index in range(1, ACTOR_PROGRESS_RECOVERY_LIMIT + 1):
            result = self.observe(tracker, index, activity={f"geometry_query:hypothesis-{index}"})
            self.assertEqual(result["idle_count"], 0)
            self.assertEqual(result["triggered"], index == ACTOR_PROGRESS_RECOVERY_LIMIT)
        self.assertEqual(result["reason"], "recovery_final_limit")

    def test_baseline_activity_and_repeated_tools_never_get_new_credit(self):
        tracker = ActorProgressTracker()
        activity = {"robot_capabilities:same", "geometry_query:same"}
        self.observe(tracker, 0, activity=activity)
        for index in range(1, 4):
            result = self.observe(tracker, index, activity=activity)
            self.assertEqual(result["new_activity_count"], 0)
        self.assertTrue(result["triggered"])

    def test_native_execution_resets_both_limits_even_after_partial_failure(self):
        tracker = ActorProgressTracker()
        for index in range(3):
            self.observe(tracker, index)
        # Success/receipt status is deliberately absent. Executed steps count,
        # including partial motion; an infeasible zero-step attempt does not.
        progressed = self.observe(tracker, 3, native_action_count=607)
        self.assertTrue(progressed["baseline"])
        self.assertEqual(progressed["count"], 0)
        for index in range(4, 7):
            result = self.observe(tracker, index, native_action_count=607)
            self.assertEqual(result["triggered"], index == 6)

    def test_context_change_resets_but_duplicate_turn_is_not_another_final(self):
        tracker = ActorProgressTracker()
        self.observe(tracker, 0)
        self.observe(tracker, 1)
        self.assertIsNone(self.observe(tracker, 1, activity={"made-up"}))
        self.assertEqual(tracker.no_native_finals, 1)
        for key, value in [("episode_id", "new-episode"), ("thread_id", "new-thread")]:
            result = self.observe(tracker, 1, **{key: value})
            self.assertTrue(result["baseline"])
            self.assertFalse(result["triggered"])

    def test_unknown_or_regressed_counts_do_not_establish_a_stall(self):
        tracker = ActorProgressTracker()
        self.observe(tracker, 0)
        for index, count in enumerate([None, True, -1, 605, 606.0], 1):
            result = self.observe(tracker, index, native_action_count=count)
            self.assertEqual(result["status"], "unknown")
            self.assertFalse(result["triggered"])
        self.assertEqual(tracker.native_action_count, 606)
        self.assertEqual(tracker.no_native_finals, 0)

    def test_recorded_airborne_93_terminal_events_replay(self):
        # Public-event reduction of a recorded airborne-handoff run:
        # 8 finals at606, 22 at611, 62 at612, then one failed turn. New activity on
        # finals2(9),3(3),4(2); remaining turns are prose or repeated activity.
        # Keep literals self-contained; no archived run, model or robot required.
        counts = [606] * 8 + [611] * 22 + [612] * 63
        novelty = {
            2: ["geometry_query"] * 2 + ["history_view_observation"] * 2 + ["program_run"] * 2 + ["robot_preview_cost"] * 3,
            3: ["history_view_observation"] * 2 + ["scene_capabilities"],
            4: ["geometry_query", "program_run"],
        }
        tracker, first_stop = ActorProgressTracker(), None
        self.assertEqual(len(counts), 93)
        completed = 0
        for ordinal, count in enumerate(counts, 1):
            if ordinal == 93:  # Budget-ended failed turn is not another final.
                continue
            completed += 1
            activity = {f"{name}:recorded-{ordinal}-{i}" for i, name in enumerate(novelty.get(ordinal, []))}
            result = self.observe(tracker, ordinal, native_action_count=count, activity=activity)
            if result["triggered"] and first_stop is None:
                first_stop = ordinal
        self.assertEqual(completed, 92)
        self.assertEqual(first_stop, 7)  # Six continued turns, not 92.


class RecoveryFingerprintTests(unittest.TestCase):
    def test_unified_stage_names_do_not_buy_credit_but_routes_and_dependencies_do(self):
        first = dict(stages=[dict(id="reach", arm="left", kind="move", position_m=[0, 0, 1]),
            dict(id="jaw", arm="right", kind="gripper", goal_normalized=0, depends_on=["reach"])])
        renamed = dict(mode="preview", stages=[dict(id="a", arm="left", kind="move", position_m=[0, 0, 1]),
            dict(id="b", arm="right", kind="gripper", goal_normalized=0, depends_on=["a"])])
        original = recovery_fingerprints("robot_action", first)
        self.assertTrue(original)
        self.assertEqual(original, recovery_fingerprints("robot_action", renamed))
        renamed["stages"][1]["depends_on"] = []
        self.assertNotEqual(original, recovery_fingerprints("robot_action", renamed))

    def test_unified_inspect_uses_plan_commands_and_individual_samples(self):
        plan = dict(identity=dict(episode_id="e"), action=dict(stages=[
            dict(id="reach", arm="left", kind="move", position_m=[0, 0, 1])]))
        plans = {"p1": plan, "p2": plan}
        def inspect(identifier, steps):
            return recovery_fingerprints("robot_action", dict(mode="inspect", plan_id=identifier,
                native_steps=steps), episode_id="e", plans=plans)
        self.assertEqual(inspect("p1", [0, 5]), inspect("p2", [5, 0]))
        self.assertEqual(len(inspect("p2", [5, 10]) - inspect("p1", [0, 5])), 1)

    def test_operation_memory_queries_are_investigation_but_records_are_not(self):
        self.assertTrue(recovery_fingerprints("operation_memory", dict(request=dict(kind="query", query="grasp"))))
        self.assertFalse(recovery_fingerprints("operation_memory", dict(request=dict(kind="record", summary="new words"))))

    def test_stored_short_observation_aliases_restore_before_fingerprinting(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary), dict(run_id="r", episode_id="e", env_instance_id="env", env_generation=1))
            try:
                store.save("observation", "original", dict(observation_id="original", episode_id="e", sim_step=12))
                store.set_state("actor_id", "obs_1", dict(alias="obs_1", original="original"))
                for turn, observation in (("raw", "original"), ("short", "obs_1")):
                    store.save("tool_call", turn, dict(correlation=dict(thread_id="t", turn_id=turn),
                        params=dict(tool="history_view_observation", arguments=dict(observation_id=observation))))
                self.assertEqual(completed_turn_activity(store, thread_id="t", turn_id="raw", episode_id="e"),
                                 completed_turn_activity(store, thread_id="t", turn_id="short", episode_id="e"))
            finally:
                store.close()

    def test_preview_labels_order_ids_and_timeout_do_not_refresh_same_candidates(self):
        a = dict(observation_id="obs-first", timeout_s=45, candidates=[
            dict(label="left", move=dict(arm="left", grasp_position_m=[1, 2, 3])),
            dict(label="right", move=dict(arm="right", grasp_position_m=[1, 2, 3]))])
        b = dict(observation_id="obs-recaptured", timeout_s=120, candidates=[
            dict(label="renamed right", move=dict(arm="right", grasp_position_m=[1, 2, 3])),
            dict(label="renamed left", move=dict(arm="left", grasp_position_m=[1, 2, 3]))])
        self.assertEqual(recovery_fingerprints("robot_preview_cost", a),
                         recovery_fingerprints("robot_preview_cost", b))
        b["candidates"][0]["move"]["grasp_position_m"] = [1, 2, 4]
        self.assertEqual(len(recovery_fingerprints("robot_preview_cost", b)
                             - recovery_fingerprints("robot_preview_cost", a)), 1)

    def test_same_paused_observation_id_is_not_a_new_view_but_historical_step_is(self):
        observations = {"obs-1": dict(episode_id="e", sim_step=600),
                        "obs-2": dict(episode_id="e", sim_step=600),
                        "obs-3": dict(episode_id="e", sim_step=500)}
        def fingerprint(identifier):
            return recovery_fingerprints("history_view_observation",
                dict(observation_id=identifier, camera_id="cam_left_wrist", roi=[1, 2, 3, 4]),
                observations=observations, episode_id="e")
        self.assertEqual(fingerprint("obs-1"), fingerprint("obs-2"))
        self.assertNotEqual(fingerprint("obs-1"), fingerprint("obs-3"))
        self.assertEqual(fingerprint("missing-1"), fingerprint("missing-2"))

    def test_observe_and_plan_prose_do_not_refresh_recovery(self):
        for tool in ("robot_observe", "task_plan", "not_a_supported_tool"):
            self.assertEqual(recovery_fingerprints(tool, {"text": "A new claim", "revision": 500}), set())

    def test_program_formatting_and_comments_do_not_count_as_new_computation(self):
        a = recovery_fingerprints("program_run", dict(source="x = 1\nprint(x)\n", timeout_s=30))
        b = recovery_fingerprints("program_run", dict(source="# another label\nx=1\nprint( x )", timeout_s=60))
        self.assertEqual(a, b)

    def test_program_observation_uuid_churn_is_not_new_recovery(self):
        observations = {"obs-first": dict(episode_id="e", sim_step=100),
                        "obs-again": dict(episode_id="e", sim_step=100)}
        def fingerprint(observation_id):
            return recovery_fingerprints("program_run", {"source": f"client.observe('{observation_id}')"},
                                         observations=observations, episode_id="e")
        self.assertEqual(fingerprint("obs-first"), fingerprint("obs-again"))

    def test_trajectory_inspection_steps_are_individual_bounded_evidence(self):
        base = dict(trajectory_ref="artifact:trajectory", camera_id="cam_head")
        original = recovery_fingerprints("robot_inspect_trajectory", {**base, "native_steps": [0, 5]})
        reordered = recovery_fingerprints("robot_inspect_trajectory", {**base, "native_steps": [5, 0]})
        extended = recovery_fingerprints("robot_inspect_trajectory", {**base, "native_steps": [5, 10]})
        self.assertEqual(original, reordered)
        self.assertEqual(len(extended - original), 1)
        tracker = ActorProgressTracker()
        tracker.observe(thread_id="t", turn_id="0", episode_id="e", native_action_count=0, activity=original)
        for index in range(1, 7):
            activity = recovery_fingerprints("robot_inspect_trajectory", {**base, "native_steps": [10 + index]})
            result = tracker.observe(thread_id="t", turn_id=str(index), episode_id="e", native_action_count=0, activity=activity)
        self.assertTrue(result["triggered"])
        self.assertEqual(result["reason"], "recovery_final_limit")

    def test_actual_vision_review_name_and_recaptured_ids(self):
        observations = {"obs-a": dict(episode_id="e", sim_step=10),
                        "obs-b": dict(episode_id="e", sim_step=20),
                        "obs-c": dict(episode_id="e", sim_step=20)}
        base = dict(before_observation_id="obs-a", camera_id="cam_head", roi=[1, 2, 3, 4], target="bottle")
        first = recovery_fingerprints("vision_review", {**base, "after_observation_id": "obs-b"},
                                      observations=observations, episode_id="e")
        again = recovery_fingerprints("vision_review", {**base, "after_observation_id": "obs-c"},
                                      observations=observations, episode_id="e")
        self.assertEqual(len(first), 1)
        self.assertEqual(first, again)
        self.assertTrue(recovery_fingerprints("operation_verify", {"request": {"kind": "help"}}))
        self.assertFalse(recovery_fingerprints("visual_review", base))

    def test_store_reader_scopes_thread_turn_and_public_record_types(self):
        records = {
            "observation": [dict(observation_id="o", episode_id="e", sim_step=0)],
            "tool_call": [
                dict(correlation=dict(thread_id="t", turn_id="current"), params=dict(tool="geometry_query", arguments={"request": {"kind": "pixel_ray", "pixel": [1, 2]}})),
                dict(correlation=dict(thread_id="other", turn_id="current"), params=dict(tool="program_run", arguments={"source": "print(1)"})),
                dict(correlation=dict(thread_id="t", turn_id="old"), params=dict(tool="robot_capabilities", arguments={})),
            ],
        }
        store = SimpleNamespace(records=Mock(side_effect=lambda kind: records[kind]))
        result = completed_turn_activity(store, thread_id="t", turn_id="current", episode_id="e")
        self.assertEqual(len(result), 1)
        self.assertTrue(next(iter(result)).startswith("geometry_query:"))
        self.assertEqual({call.args[0] for call in store.records.call_args_list}, {"observation", "tool_call"})


class ProgressBenchmarkTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name), dict(run_id="progress", episode_id="e", env_instance_id="env", env_generation=1))
        done = threading.Event()
        done.set()
        self.session = SimpleNamespace(state="completed", turn_done=done, turn_id="t1", thread_id="thread",
            model_limit_reached=threading.Event(), model_proxy=SimpleNamespace(limit_notified=False, request_count=1),
            completed_final_answer=Mock(side_effect=AssertionError("bounded policy must not parse prose")))
        self.harness = SimpleNamespace(episode_done=threading.Event(), episode_terminal=None, accepting=True,
            native_action_count=606, started_at=time.monotonic(), last_native_progress_at=time.monotonic(), active_action=None)
        self.workbench = SimpleNamespace(session=self.session, harness=self.harness, completion_guidance="none",
            store=self.store, config=SimpleNamespace(features=SimpleNamespace(bounded_continuation=True)),
            identity=dict(episode_id="e"), current_unknown_actions=lambda: False, has_active_programs=lambda: False,
            control_lock=asyncio.Lock())

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def next_turn(self, prompt):
        self.session.turn_id = f"t{self.session.run_turn.call_count + 1}"
        self.session.model_proxy.request_count += 1
        self.assertLess(self.session.run_turn.call_count, 20)

    async def run_benchmark(self):
        return await asyncio.wait_for(wait_benchmark(self.workbench, asyncio.Event()), 3)

    async def test_wording_independent_stop_and_auditable_events(self):
        self.session.run_turn = Mock(side_effect=self.next_turn)
        self.assertFalse(await self.run_benchmark())
        self.assertEqual(self.session.run_turn.call_count, 3)
        self.session.completed_final_answer.assert_not_called()
        self.assertEqual(self.harness.wait_stop_reason, ACTOR_PROGRESS_REASON)
        self.assertFalse(self.harness.episode_done.is_set())
        events = [x["payload"] for x in self.store.events() if x["type"] == "benchmark.actor_progress"]
        self.assertEqual([x["count"] for x in events], [0, 1, 2, 3])
        self.assertTrue(events[-1]["triggered"])
        continuations = [x["payload"] for x in self.store.events()
                         if x["type"] == "benchmark.actor_turn_continuation"]
        self.assertEqual(len(continuations), 3)
        for item in continuations:
            self.assertEqual(item["recovery_context"]["version"], "recovery-guidance-v1")
            self.assertIn("one concrete recovery objective", item["prompt"])
            self.assertIn("complete attempt", item["prompt"])

    async def test_evidence_switch_stops_after_two_unchanged_recovery_finals(self):
        self.workbench.config.features.recovery_evidence = True
        self.session.run_turn = Mock(side_effect=self.next_turn)
        self.assertFalse(await self.run_benchmark())
        self.assertEqual(self.session.run_turn.call_count, 2)
        self.assertEqual(self.harness.wait_stop_reason, ACTOR_PROGRESS_REASON)
        self.assertEqual(self.harness.actor_stall["reason"], "no_new_evidence_final_limit")
        continuations = [x["payload"] for x in self.store.events()
                         if x["type"] == "benchmark.actor_turn_continuation"]
        self.assertTrue(all(item["recovery_context"]["version"] == "recovery-guidance-v4"
                            for item in continuations))
        self.assertTrue(all("never all routes" in item["prompt"] for item in continuations))

    async def test_native_terminal_callback_during_new_evidence_read_still_wins(self):
        from auriga.cortex.supervision.recovery_evidence import completed_turn_evidence
        self.workbench.config.features.recovery_evidence = True
        calls = 0
        def evidence(*args, **kwargs):
            nonlocal calls
            result = completed_turn_evidence(*args, **kwargs)
            calls += 1
            if calls == 3:
                self.harness.episode_done.set()
            return result
        self.session.run_turn = Mock(side_effect=self.next_turn)
        with patch("auriga.cortex.supervision.recovery_evidence.completed_turn_evidence", side_effect=evidence):
            self.assertFalse(await self.run_benchmark())
        self.assertEqual(self.harness.wait_stop_reason, "native_episode_ended")
        self.assertEqual(self.session.run_turn.call_count, 2)

    async def test_native_execution_callback_during_new_evidence_read_resets_plateau(self):
        from auriga.cortex.supervision.recovery_evidence import completed_turn_evidence
        self.workbench.config.features.recovery_evidence = True
        calls = 0
        def evidence(*args, **kwargs):
            nonlocal calls
            result = completed_turn_evidence(*args, **kwargs)
            calls += 1
            if calls == 3:
                self.harness.native_action_count += 1
            return result
        self.session.run_turn = Mock(side_effect=self.next_turn)
        with patch("auriga.cortex.supervision.recovery_evidence.completed_turn_evidence", side_effect=evidence):
            self.assertFalse(await self.run_benchmark())
        self.assertEqual(self.session.run_turn.call_count, 4)
        self.assertEqual(self.harness.actor_stall["native_action_count"], 607)

    async def test_actual_motion_gets_a_fresh_recovery_budget(self):
        def advance(prompt):
            self.next_turn(prompt)
            if self.session.run_turn.call_count == 2:
                self.harness.native_action_count += 1
        self.session.run_turn = Mock(side_effect=advance)
        self.assertFalse(await self.run_benchmark())
        self.assertEqual(self.session.run_turn.call_count, 5)
        self.assertEqual(self.harness.native_action_count, 607)

    async def test_legacy_false_keeps_non_inability_finals_until_existing_budget(self):
        self.workbench.config.features.bounded_continuation = False
        self.session.completed_final_answer = Mock(return_value="Same physical state. No new result.")
        def advance(prompt):
            self.next_turn(prompt)
            if self.session.run_turn.call_count == 9:
                self.session.model_proxy.limit_notified = True
        self.session.run_turn = Mock(side_effect=advance)
        self.assertFalse(await self.run_benchmark())
        self.assertEqual(self.session.run_turn.call_count, 9)
        self.assertEqual(self.harness.wait_stop_reason, "model_request_limit")
        self.assertFalse(any(e["type"] == "benchmark.actor_progress" for e in self.store.events()))
        from auriga.apps.evaluation.episode import BENCHMARK_CONTINUATION
        self.assertTrue(all(call.args[0] == BENCHMARK_CONTINUATION for call in self.session.run_turn.call_args_list))

    async def test_native_terminal_and_unresolved_work_take_priority(self):
        for condition in ("native", "unknown", "active_action", "active_program", "not_accepting"):
            with self.subTest(condition=condition):
                self.harness.episode_done.clear()
                self.harness.accepting = True
                self.harness.active_action = None
                self.workbench.current_unknown_actions = lambda: False
                self.workbench.has_active_programs = lambda: False
                self.session.run_turn = Mock()
                if condition == "native": self.harness.episode_done.set()
                elif condition == "unknown": self.workbench.current_unknown_actions = lambda: True
                elif condition == "active_action": self.harness.active_action = "action-inflight"
                elif condition == "active_program": self.workbench.has_active_programs = lambda: True
                else: self.harness.accepting = False
                self.assertFalse(await self.run_benchmark())
                self.session.run_turn.assert_not_called()
                self.assertNotEqual(self.harness.wait_stop_reason, ACTOR_PROGRESS_REASON)

    async def test_terminal_callback_during_evidence_read_wins_stall_race(self):
        calls = 0
        def activity(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 4:
                self.harness.episode_done.set()
            return set()
        self.session.run_turn = Mock(side_effect=self.next_turn)
        with patch("auriga.apps.evaluation.episode.completed_turn_activity", side_effect=activity):
            self.assertFalse(await self.run_benchmark())
        self.assertEqual(self.harness.wait_stop_reason, "native_episode_ended")
        self.assertEqual(self.session.run_turn.call_count, 3)

    async def test_native_accounting_callback_during_evidence_read_gets_new_baseline(self):
        calls = 0
        def activity(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 4:
                self.harness.native_action_count += 1
            return set()
        self.session.run_turn = Mock(side_effect=self.next_turn)
        with patch("auriga.apps.evaluation.episode.completed_turn_activity", side_effect=activity):
            self.assertFalse(await self.run_benchmark())
        self.assertEqual(self.session.run_turn.call_count, 6)
        self.assertEqual(self.harness.actor_stall["native_action_count"], 607)

    async def test_normal_long_running_turn_is_not_a_completed_final(self):
        self.session.state = "running"
        self.session.turn_done.clear()
        task = asyncio.create_task(wait_benchmark(self.workbench, asyncio.Event()))
        await asyncio.sleep(.07)
        self.assertFalse(task.done())
        self.assertFalse(any(e["type"] == "benchmark.actor_progress" for e in self.store.events()))
        self.harness.episode_done.set()
        self.assertFalse(await asyncio.wait_for(task, 1))


if __name__ == "__main__":
    unittest.main()
