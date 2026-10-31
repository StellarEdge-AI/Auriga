"""Evidence-aware continuation keeps alternatives and bounds redundant finals."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from auriga.cortex.supervision.actor_stall import ActorProgressTracker, completed_turn_activity
from auriga.cortex.supervision.recovery_evidence import completed_turn_evidence
from auriga.core.storage.store import Store


class RecoveryEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name), dict(run_id="r", episode_id="e",
                           env_instance_id="env", env_generation=1))
        self.store.save("observation", "o", dict(observation_id="o", episode_id="e", sim_step=990))
        self.tracker = ActorProgressTracker(evidence_aware=True)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def candidate(self, turn, *, label="approach", z=.8, cost=30, remaining=11,
                  status="rejected", binding=99, valid=True, thread="t", trajectory=False,
                  old_state=False, duplicate=False, reason=None, program=None):
        correlation = dict(thread_id=thread, turn_id=turn, tool_call_id="call-" + turn)
        args = dict(observation_id="o", include_trajectory=False,
                    candidates=[dict(label=label, move=dict(arm="left", grasp_position_m=[0., 0., z]))])
        if duplicate:
            args["candidates"].append(dict(label=label + "-again",
                move=dict(arm="left", grasp_position_m=[0., 0., z])))
        params = dict(tool="robot_preview_cost", arguments=args)
        if program:
            correlation["program_id"] = program
            self.store.event("program.call_started", params, correlation)
        else:
            self.store.save("tool_call", "call-" + turn, dict(correlation=correlation, params=params))
        payload = dict(status="completed", same_measured_state_verified=valid,
                       zero_step_check_passed=valid, observation_id="o",
                       state_binding=dict(native_action_count=binding, sha256="a" * 64),
                       state_binding_after=dict(native_action_count=binding + int(old_state), sha256="b" * 64
                           if old_state else "a" * 64),
                       action_budget=dict(native_steps_used=binding, native_steps_remaining=remaining),
                       candidates=[dict(label=label, nominal_native_steps=cost, status=status,
                                        feasible=status == "planned", reason=(reason or
                                            (None if status == "planned" else "motion_safety_rejected")))])
        if status == "rejected":
            payload["candidates"][0]["motion_safety"] = dict(status="reject", arms={"left": dict(
                status="reject", reasons=["insufficient_remaining_native_budget"],
                nominal_native_steps=cost, episode_remaining_native_steps=remaining,
                exceeded_budgets={"episode": remaining})})
        if duplicate:
            payload["candidates"].append(dict(payload["candidates"][0], label=label + "-again"))
        if trajectory:
            payload["candidates"][0]["trajectory_ref"] = "artifact:" + turn
        self.store.event("motion.cost_preview", payload, correlation)

    def evidence(self, turn, *, remaining=11, count=99):
        return completed_turn_evidence(self.store, thread_id="t", turn_id=turn,
                                       episode_id="e", native_action_count=count,
                                       native_steps_remaining=remaining)

    def batch_candidate(self, turn, *, cost=30, z=.8, count=99, zero_step=True):
        correlation = dict(thread_id="t", turn_id=turn, tool_call_id="call-" + turn)
        self.store.set_state("actor_id", "obs_1", dict(alias="obs_1", original="o"))
        args = dict(observation_id="obs_1", stages=[dict(id=turn, arm="left", kind="move", position_m=[0, 0, z])])
        self.store.save("tool_call", "call-" + turn, dict(correlation=correlation,
            params=dict(tool="robot_action", arguments=args)))
        self.store.event("action.batch_preview", dict(plan_id="p-" + turn, status="planned",
            observation_id="o", physics_advanced=not zero_step, executed_native_steps=0 if zero_step else 1,
            planned_native_steps=cost, trajectory_ref="artifact:" + turn,
            action_budget=dict(native_steps_used=count, native_steps_remaining=11)), correlation)

    def test_unified_batch_costs_keep_existing_budget_recovery_limit(self):
        self.observe("baseline")
        for turn, z in (("first", .7), ("second", .9)):
            self.batch_candidate(turn, z=z)
            result = self.observe(turn)
            self.assertEqual(result["triggered"], turn == "second")
            self.assertEqual(result["new_activity_count"], 0)
        self.assertEqual(result["reason"], "budget_rejection_final_limit")
        self.assertEqual(result["idle_limit"], 2)
        self.assertEqual(result["recovery_limit"], 6)

    def test_unified_renamed_affordable_batch_is_same_evidence(self):
        self.batch_candidate("baseline", cost=7)
        self.observe("baseline")
        self.batch_candidate("renamed", cost=7)
        result = self.observe("renamed")
        self.assertEqual(result["new_activity_count"], 0)
        self.assertEqual(result["recovery_evidence"]["tested_candidate_count"], 1)
        self.assertEqual(self.evidence("renamed")["candidates"][0]["cost_scope"],
                         "complete_submitted_batch_excluding_optional_settle")

    def test_unified_stale_or_nonzero_preview_keeps_request_credit(self):
        for turn, options in (("stale", dict(count=98)), ("advanced", dict(zero_step=False))):
            self.batch_candidate(turn, **options)
            evidence = self.evidence(turn)
            self.assertEqual(evidence["candidates"], [])
            self.assertEqual(evidence["budget_rejected_count"], 0)
            self.assertTrue(evidence["activity"][0].startswith("robot_action:"))

    def test_program_cost_preview_uses_existing_budget_witness_checks(self):
        self.candidate("program-cost", program="program-1")
        evidence = self.evidence("program-cost")
        self.assertEqual(len(evidence["candidates"]), 1)
        self.assertEqual(evidence["budget_rejected_count"], 1)

    def test_program_batch_preview_requires_prior_request_in_same_context(self):
        for mismatch in (None, "program_id", "turn_id", "thread_id", "episode_id",
                         "missing_program", "late_request", "stale_budget", "observation"):
            turn = "program-" + str(mismatch)
            with self.subTest(mismatch=mismatch):
                correlation = dict(thread_id="t", turn_id=turn, program_id="program-1", tool_call_id="inner-call")
                request_correlation = dict(correlation)
                if mismatch in ("program_id", "turn_id", "thread_id"):
                    request_correlation[mismatch] = "other"
                elif mismatch == "missing_program":
                    request_correlation.pop("program_id")
                request = dict(tool="robot_action", arguments=dict(observation_id="other" if
                    mismatch == "observation" else "o", stages=[dict(id="reach", arm="left",
                        kind="move", position_m=[0, 0, .8])]))
                payload = dict(status="planned", observation_id="o", physics_advanced=False,
                    executed_native_steps=0, planned_native_steps=30,
                    action_budget=dict(native_steps_used=98 if mismatch == "stale_budget" else 99,
                        native_steps_remaining=11))
                if mismatch == "late_request":
                    self.store.event("action.batch_preview", payload, correlation)
                with patch.dict(self.store.identity, episode_id="other" if mismatch == "episode_id" else "e"):
                    self.store.event("program.call_started", request, request_correlation)
                if mismatch != "late_request":
                    self.store.event("action.batch_preview", payload, correlation)
                evidence = self.evidence(turn)
                self.assertEqual(len(evidence["candidates"]), 1 if mismatch is None else 0)
                self.assertEqual(evidence["budget_rejected_count"], 1 if mismatch is None else 0)

    def observe(self, turn, *, remaining=11, count=99):
        return self.tracker.observe(thread_id="t", turn_id=turn, episode_id="e",
            native_action_count=count, activity=completed_turn_activity(self.store,
                thread_id="t", turn_id=turn, episode_id="e"),
            evidence=self.evidence(turn, remaining=remaining, count=count))

    def test_distinct_unaffordable_endpoints_get_two_recovery_turns_not_six(self):
        self.observe("baseline")
        self.candidate("first", z=.7)
        first = self.observe("first")
        self.assertFalse(first["triggered"])  # One failed candidate never ends recovery.
        self.assertEqual(first["idle_count"], 1)
        self.candidate("second", z=.9)
        second = self.observe("second")
        self.assertTrue(second["triggered"])
        self.assertEqual(second["reason"], "budget_rejection_final_limit")
        self.assertEqual(second["recovery_evidence"]["tested_candidate_count"], 2)
        self.assertIn("not global task infeasibility", second["recovery_evidence"]["scope"])

    def test_viable_alternative_after_budget_rejection_keeps_opportunity(self):
        self.observe("baseline")
        self.candidate("first")
        self.observe("first")
        self.candidate("alternative", z=.6, cost=7, status="planned")
        result = self.observe("alternative")
        self.assertFalse(result["triggered"])
        self.assertEqual(result["idle_count"], 0)
        self.assertEqual(result["new_activity_count"], 1)

    def test_repeated_affordable_candidate_and_renamed_ids_are_not_new(self):
        self.candidate("baseline", cost=7, status="planned")
        self.observe("baseline")
        for turn in ("first", "second"):
            self.candidate(turn, label=turn, cost=7, status="planned")
            result = self.observe(turn)
            self.assertEqual(result["new_activity_count"], 0)
        self.assertTrue(result["triggered"])
        self.assertEqual(result["reason"], "no_new_evidence_final_limit")
        self.assertEqual(result["recovery_evidence"]["tested_candidate_count"], 1)

    def test_changed_result_of_same_candidate_is_new(self):
        self.candidate("baseline")
        self.observe("baseline")
        self.candidate("changed", cost=8, status="planned")
        result = self.observe("changed")
        self.assertEqual(result["idle_count"], 0)
        self.assertFalse(result["triggered"])

    def test_actually_delivered_trajectory_can_add_new_information_once(self):
        self.candidate("baseline", cost=8, status="planned")
        self.observe("baseline")
        self.candidate("with-trajectory", cost=8, status="planned", trajectory=True)
        self.assertEqual(self.observe("with-trajectory")["new_activity_count"], 1)
        self.candidate("repeated-trajectory", cost=8, status="planned", trajectory=True)
        self.assertEqual(self.observe("repeated-trajectory")["new_activity_count"], 0)

    def test_new_route_geometry_is_credit_even_when_candidate_over_budget(self):
        self.candidate("baseline")
        self.observe("baseline")
        self.candidate("geometry", trajectory=True)
        result = self.observe("geometry")
        self.assertEqual(result["new_activity_count"], 1)
        self.assertEqual(result["idle_count"], 0)

    def test_default_graphs_on_new_unaffordable_endpoints_do_not_refresh_idle(self):
        self.observe("baseline")
        for index in (1, 2):
            turn = str(index)
            self.candidate(turn, z=index / 10, trajectory=True)
            result = self.observe(turn)
            self.assertEqual(result["triggered"], index == 2)

    def test_unknown_mismatched_or_boolean_budget_never_rejects(self):
        for index, budget in enumerate((None, True, 12)):
            turn = "turn-" + str(index)
            self.candidate(turn, z=.7 + index / 10)
            evidence = self.evidence(turn, remaining=budget)
            self.assertEqual(evidence["budget_rejected_count"], 0)
            self.assertTrue(evidence["activity"])
            self.assertFalse(evidence["candidates"][0]["exceeds_episode_budget"])

    def test_stale_or_unverified_preview_preserves_request_credit(self):
        for turn, args in (("stale", dict(binding=98)), ("changed", dict(valid=False))):
            self.candidate(turn, **args)
            evidence = self.evidence(turn)
            self.assertEqual(evidence["candidates"], [])
            self.assertTrue(evidence["activity"][0].startswith("robot_preview_cost:"))

    def test_infeasible_unknown_cost_is_not_budget_proof(self):
        self.candidate("first", cost=None, status="failed")
        evidence = self.evidence("first")
        self.assertEqual(evidence["budget_rejected_count"], 0)
        self.assertTrue(evidence["activity"])

    def test_ik_rejection_is_recorded_as_infeasible_without_budget_failure(self):
        self.candidate("ik", cost=None, status="failed", reason="path_infeasible")
        evidence = self.evidence("ik")
        self.assertEqual(len(evidence["candidates"]), 1)
        self.assertFalse(evidence["candidates"][0]["feasible"])
        self.assertEqual(evidence["candidates"][0]["reason"], "path_infeasible")
        self.assertEqual(evidence["budget_rejected_count"], 0)

    def test_old_state_binding_cannot_create_budget_rejection_or_feasibility(self):
        self.candidate("old", old_state=True)
        evidence = self.evidence("old")
        self.assertEqual(evidence["candidates"], [])
        self.assertEqual(evidence["budget_rejected_count"], 0)
        self.assertTrue(evidence["activity"][0].startswith("robot_preview_cost:"))

    def test_duplicate_candidate_requests_count_once(self):
        self.candidate("duplicate", duplicate=True)
        evidence = self.evidence("duplicate")
        self.assertEqual(len(evidence["candidates"]), 1)
        self.assertEqual(evidence["budget_rejected_count"], 1)

    def test_changed_result_for_same_candidate_is_kept(self):
        self.candidate("changed-result", cost=7, status="planned")
        correlation = dict(thread_id="t", turn_id="changed-result", tool_call_id="call-changed-result-2")
        args = dict(observation_id="o", include_trajectory=False,
                    candidates=[dict(label="approach", move=dict(arm="left", grasp_position_m=[0., 0., .8]))])
        self.store.save("tool_call", correlation["tool_call_id"], dict(correlation=correlation,
            params=dict(tool="robot_preview_cost", arguments=args)))
        self.store.event("motion.cost_preview", dict(status="completed", same_measured_state_verified=True,
            zero_step_check_passed=True, observation_id="o",
            state_binding=dict(native_action_count=99, sha256="a" * 64),
            state_binding_after=dict(native_action_count=99, sha256="a" * 64),
            action_budget=dict(native_steps_used=99, native_steps_remaining=11),
            candidates=[dict(label="approach", nominal_native_steps=8, status="planned", feasible=True)]),
            correlation)
        evidence = self.evidence("changed-result")
        self.assertEqual(len(evidence["candidates"]), 2)
        self.assertEqual({value["nominal_native_steps"] for value in evidence["candidates"]}, {7, 8})
        self.assertEqual(sum(value.startswith("preview_result:") for value in evidence["activity"]), 2)

    def test_budget_witness_does_not_override_affordable_or_unknown_cost(self):
        self.candidate("affordable-witness", cost=10, remaining=11)
        evidence = self.evidence("affordable-witness")
        self.assertEqual(evidence["budget_rejected_count"], 0)
        self.assertFalse(evidence["candidates"][0]["exceeds_episode_budget"])

    def test_actual_delivered_rgb_not_observation_uuid_is_evidence(self):
        def delivery(turn, sha, *, delivered=True):
            obs_id = "obs-" + turn
            self.store.save("observation", obs_id, dict(observation_id=obs_id, episode_id="e", sim_step=990))
            self.store.save("delivery", "delivery-" + turn, dict(observation_id=obs_id,
                correlation=dict(thread_id="t", turn_id=turn), delivered_to_actor=delivered,
                images=[dict(camera_id="cam_head", source_sha256=sha * 64)]))
        delivery("baseline", "a")
        self.observe("baseline")
        delivery("same", "a")
        self.assertEqual(self.observe("same")["new_activity_count"], 0)
        delivery("new", "b")
        self.assertEqual(self.observe("new")["new_activity_count"], 1)
        delivery("undelivered", "c", delivered=False)
        self.assertEqual(self.evidence("undelivered")["delivered_rgb_count"], 0)

    def test_read_only_data_scope_and_thread_scope(self):
        self.candidate("other", thread="not-ours")
        self.store.event("observer.evaluated", {"task_success": "forbidden"})
        with patch.object(self.store, "events", side_effect=AssertionError("no unfiltered event scan")), \
             patch.object(self.store, "records", wraps=self.store.records) as records:
            evidence = self.evidence("other")
        self.assertFalse(evidence["activity"])
        self.assertFalse(evidence["candidates"])
        self.assertEqual({call.args[0] for call in records.call_args_list}, {"observation", "tool_call", "delivery"})
        self.assertNotIn("forbidden", str(evidence))

    def test_native_progress_clears_ledger_and_duplicate_turn_is_ignored(self):
        self.candidate("baseline")
        self.observe("baseline")
        self.assertIsNone(self.observe("baseline"))
        result = self.observe("progress", remaining=10, count=100)
        self.assertTrue(result["baseline"])
        self.assertEqual(result["recovery_evidence"]["tested_candidate_count"], 0)

    def test_native_callback_race_ignores_old_evidence(self):
        self.candidate("baseline")
        evidence = self.evidence("baseline")
        result = self.tracker.observe(thread_id="t", turn_id="baseline", episode_id="e",
                                     native_action_count=100, evidence=evidence)
        self.assertTrue(result["baseline"])
        self.assertEqual(result["idle_limit"], 3)
        self.assertIsNone(self.tracker.recovery_context())

    def test_unknown_native_accounting_hides_prior_plateau_ledger(self):
        self.candidate("baseline")
        self.observe("baseline")
        self.assertIsNotNone(self.tracker.recovery_context())
        result = self.observe("unknown", count=None, remaining=None)
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(result["triggered"])
        self.assertIsNone(self.tracker.recovery_context())

    def test_feature_off_preserves_v1_three_and_six_limits(self):
        self.tracker = ActorProgressTracker(evidence_aware=False)
        self.observe("baseline")
        for index in range(1, 7):
            turn = str(index)
            self.candidate(turn, z=index / 10)
            result = self.observe(turn)
            self.assertEqual(result["triggered"], index == 6)
        self.assertEqual(result["idle_limit"], 3)


if __name__ == "__main__":
    unittest.main()
