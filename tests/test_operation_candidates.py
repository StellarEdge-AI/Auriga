"""Planning declarations preserve uncertainty and never synthesize execution policy."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from auriga.cortex.context.operation_candidates import PHASES, summarize_candidate, validate_candidate
from auriga.core.storage.store import Store
from auriga.cortex.context.task_notes import (PREVIOUS_DEFAULT_TASK_NOTES, TASK_SCHEDULING_GUIDANCE,
    OPERATION_CANDIDATE_GUIDANCE, default_notes_for, resolve_task_notes)
from auriga.cortex.context.task_planning import (TaskPlanning, instructions_for, plan_schema_for,
    LEGACY_INSTRUCTIONS, GRASP_APPROACH_GUIDANCE, ROUTE_PLANNING_GUIDANCE,
    PROTOCOL_INSTRUCTIONS, validate_operation_context, scheduling_failure_advisory)


def candidate(**changes):
    return dict(from_observation_id="obs", contact_section="Exposed body; hidden back uncertain.",
                object_relative_grasp="Oblique across the body; compare release access.",
                swept_clearance="Opening fingers and held-object turn need visible rim clearance.",
                phases={"entry": dict(route="Clear overhead corridor if reachable.", native_steps=3)},
                **changes)


def context(**changes):
    return dict(object_label="actor object label", state="unknown",
        evidence=dict(observation_id="obs", sources=["rgb"], basis="Visible public RGB.",
                      uncertainty="Hidden surface and support are unknown."),
        contact=dict(region="finger_middle", reason="Visible access permits deeper contact; verify lift."),
        **changes)


class OperationCandidateTest(unittest.TestCase):
    def test_cheap_entry_cannot_be_reported_as_complete_operation_budget(self):
        result = summarize_candidate(candidate(), native_steps_remaining=10)
        self.assertEqual(result["known_estimate_native_steps"], 3)
        self.assertEqual(result["unknown_phases"], list(PHASES[1:]))
        self.assertEqual(result["reserve_native_steps"], "unknown")
        self.assertIsNone(result["complete_estimate_native_steps"])
        self.assertEqual(result["budget_fit"], "unknown")
        self.assertEqual(result["physical_clearance"], "actor_judgment_unverified")

    def test_separation_withdrawal_and_reserve_count_before_budget_fit(self):
        value = candidate(reserve_native_steps=5)
        value["phases"] = {phase: dict(route="Actor-estimated route; not certified.", native_steps=2)
                           for phase in PHASES}
        self.assertEqual(summarize_candidate(value, native_steps_remaining=20)["budget_fit"], "exceeds_remaining")
        result = summarize_candidate(value, native_steps_remaining=21)
        self.assertEqual(result["complete_estimate_native_steps"], 21)
        self.assertEqual(result["budget_fit"], "estimated_within_remaining")
        value["phases"]["withdrawal"]["native_steps"] = "unknown"
        self.assertEqual(summarize_candidate(value, native_steps_remaining=21)["budget_fit"], "unknown")

    def test_known_partial_cost_can_exceed_budget_without_inventing_unknown_cost(self):
        result = summarize_candidate(candidate(), native_steps_remaining=2)
        self.assertEqual(result["budget_fit"], "exceeds_remaining")
        self.assertIsNone(result["complete_estimate_native_steps"])
        self.assertIn("different complete route", result["next_check"])

    def test_unknown_and_explicit_unnecessary_phase_are_distinct(self):
        value = candidate()
        value["phases"]["required_return"] = dict(route="No return required by public task.", native_steps=0)
        result = summarize_candidate(value)
        self.assertNotIn("required_return", result["unknown_phases"])
        self.assertIn("verification", result["unknown_phases"])

    def test_candidate_bound_to_context_evidence_not_arbitrary_stale_start(self):
        value = candidate()
        validate_operation_context(context(candidate=value), operation_candidates=True)
        value["from_observation_id"] = "previous-scene"
        with self.assertRaisesRegex(ValueError, "start_must_match"):
            validate_operation_context(context(candidate=value), operation_candidates=True)

    def test_invalid_or_privileged_fields_and_false_precision_rejected(self):
        for steps in (-1, True, 2.5, None):
            value = candidate()
            value["phases"]["entry"]["native_steps"] = steps
            with self.subTest(steps=steps), self.assertRaisesRegex(ValueError, "invalid_operation_candidate"):
                validate_candidate(value, "obs")
        value = candidate(simulator_object_pose=[0, 0, 0])
        with self.assertRaisesRegex(ValueError, "invalid_operation_candidate"):
            validate_candidate(value, "obs")


class TaskSchedulingTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.identity = dict(run_id="r", episode_id="e", env_instance_id="env", env_generation=1)
        self.store = Store(Path(temp.name), self.identity)
        self.addCleanup(self.store.close)
        self.observation = dict(self.identity, observation_id="obs", sim_step=0)
        self.store.save("observation", "obs", self.observation)
        self.plan = dict(objective="Complete independent visible work.",
            stages=[dict(id="a", goal="One object.", advance_when="Placed."),
                    dict(id="b", goal="Another object.", advance_when="Placed.")],
            constraints=[], uncertainties=[], completion_criteria=["Visible objects are placed."])

    def submit(self, plan=None, **flags):
        return TaskPlanning(self.store, **flags).call(dict(operation="submit", observation_id="obs",
            plan=self.plan if plan is None else plan), self.identity, self.observation, {})

    def test_presentation_order_never_creates_dependency_or_arm_ownership(self):
        result = self.submit()
        self.assertEqual(result["plan"], self.plan)
        for stage in result["plan"]["stages"]:
            self.assertNotIn("depends_on", stage)
            self.assertNotIn("arms", stage)

    def test_unexplained_dependency_is_advisory_and_never_removed(self):
        self.plan["stages"][1]["depends_on"] = ["a"]
        result = self.submit()
        self.assertEqual(result["plan"]["stages"][1]["depends_on"], ["a"])
        self.assertEqual(result["scheduling_advisory"]["stages_without_dependency_reason"], ["b"])
        self.plan["stages"][1]["dependency_reason"] = "The first object physically blocks the route."
        self.assertNotIn("scheduling_advisory", self.submit())

    def test_recovery_review_preserves_independent_work_and_held_arm_choices(self):
        self.plan["scheduling"] = dict(choice_reason="Compare revised recovery with a free subtask.",
            independent_work="Other arm can deliver its object if routes remain clear.",
            held_or_busy_arms="Right holds an object; use observed support only if temporary placement helps.",
            reconsider_when="Another slip or new obstacle changes the whole-operation choice.")
        self.assertEqual(self.submit()["plan"]["scheduling"], self.plan["scheduling"])

    def test_candidate_summary_is_compact_and_does_not_claim_clearance(self):
        self.plan["stages"][0]["operation_context"] = context(candidate=candidate())
        result = self.submit(operation_candidates=True)
        self.assertEqual(result["operation_candidates"][0]["stage"], "a")
        self.assertEqual(result["operation_candidates"][0]["budget_fit"], "unknown")
        self.assertNotIn("phases", result["operation_candidates"][0])

    def test_same_source_ablations_restore_exact_old_prompt_and_schema(self):
        flags = dict(task_scheduling=False, operation_candidates=False)
        expected = LEGACY_INSTRUCTIONS + "\n\n" + GRASP_APPROACH_GUIDANCE + "\n\n" + ROUTE_PLANNING_GUIDANCE + PROTOCOL_INSTRUCTIONS
        self.assertEqual(instructions_for(**flags), expected)
        self.assertEqual(default_notes_for(**flags), PREVIOUS_DEFAULT_TASK_NOTES)
        self.assertEqual(resolve_task_notes(**flags)["config"]["default_version"], "operation-notes-v14")
        self.assertEqual(self.submit(**flags)["version"], "actor_task_plan_v7")
        self.assertNotIn("scheduling", plan_schema_for(**flags)["properties"])
        self.plan["stages"][1]["depends_on"] = ["a"]
        self.assertNotIn("scheduling_advisory", self.submit(**flags))

    def test_each_switch_changes_only_its_own_instructions_and_fields(self):
        for scheduling, candidates in ((False, True), (True, False)):
            flags = dict(task_scheduling=scheduling, operation_candidates=candidates)
            text, notes = instructions_for(**flags), default_notes_for(**flags)
            for content in (text, notes):
                self.assertEqual(TASK_SCHEDULING_GUIDANCE in content, scheduling)
                self.assertEqual(OPERATION_CANDIDATE_GUIDANCE in content, candidates)
            schema = plan_schema_for(**flags)["properties"]
            self.assertEqual("scheduling" in schema, scheduling)
            context_schema = schema["stages"]["items"]["properties"]["operation_context"]["properties"]
            self.assertEqual("candidate" in context_schema, candidates)

    def test_failed_action_advisory_preserves_dependencies_and_does_not_infer_hold(self):
        self.plan["stages"][1]["depends_on"] = ["a"]
        record = dict(plan=self.plan, observation_id="previous-plan-observation")
        before = deepcopy(record)
        result = scheduling_failure_advisory(dict(status="failed", reason="tracking_error"), record)
        self.assertEqual(result["dependency_free_plan_stages_to_reassess"], ["a"])
        self.assertEqual(result["plan_observation_id"], "previous-plan-observation")
        self.assertIn("if one holds an object", result["suggestion"])
        self.assertIn("do not release without support", result["suggestion"])
        self.assertNotIn("holding_arm", result)
        self.assertNotIn("ready_stages", result)
        self.assertEqual(record, before)

    def test_failed_action_reminder_is_optional_and_never_on_success_or_unknown(self):
        self.assertIsNone(scheduling_failure_advisory(dict(status="failed"), task_scheduling=False))
        for receipt in (None, {}, dict(status="completed"), dict(status="executing"), dict(status="cancelled")):
            with self.subTest(receipt=receipt):
                self.assertIsNone(scheduling_failure_advisory(receipt))
        self.assertEqual(scheduling_failure_advisory(dict(status="rejected", reason="path_infeasible"))["kind"], "scheduling_review")

    def test_failure_advisory_stays_bounded_without_copying_actor_plan_text(self):
        record = dict(plan=dict(stages=[dict(id=str(index), goal="long actor text" * 1000)
                                       for index in range(12)]))
        result = scheduling_failure_advisory(dict(status="failed"), record)
        self.assertEqual(result["dependency_free_plan_stages_to_reassess"], ["0", "1", "2"])
        self.assertNotIn("long actor text", str(result))


if __name__ == "__main__":
    unittest.main()
