"""CPU-only task-plan admission and persistence checks; no model or robot process."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from auriga.core.config import Config
from auriga.apps.gateway.workbench import Workbench
from auriga.runtime.harness import Harness
from auriga.core.observation_policy import LEGACY_ACTOR_PROFILE
from auriga.runtime.programs.runner import ProgramRunner
from auriga.core.storage.store import Store
from auriga.cortex.context.task_planning import (VERSION, TaskPlanning, instructions_for, plan_schema_for,
                                    validate_operation_evidence, version_for)
from test_harness import FixtureRobot


def plan(**updates):
    value = dict(objective="Observe the demonstration, then reproduce the visible result.",
                 stages=[dict(goal="Observe the unknown sequence.",
                              advance_when="The relevant ordering is visible in chronological frames.")],
                 constraints=[], uncertainties=["The demonstrated ordering is not yet known."],
                 completion_criteria=["The visible arrangement matches the observed demonstration."])
    value.update(updates)
    return value


def payload(result):
    return json.loads(result["contentItems"][0]["text"])


def attention(**updates):
    return dict(dict(concern="The held object may have shifted.",
        check="Compare its visible alignment with the fingers; adjust the placement approach if shifted.",
        when="After the normal transfer action, before placement."), **updates)


def ledger_entry(**updates):
    return dict(dict(id="retention", expected="The object remains aligned with the fingers after the lift.",
                     status="pending", basis="unknown",
                     next_check="Use the next task-progress frame to compare object and fingers."), **updates)


class PlanningRobot(FixtureRobot):
    def start(self):
        return dict(super().start(), path_kinds=["backend_free", "hold"],
                    bounded_wait=dict(max_native_steps=50), reset=True)

    def reset(self, seed=0, layout_id=0):
        return dict(observation=self.observe())


class TaskPlanningCheck(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.identity = dict(run_id="task-planning", episode_id="initial", env_instance_id="fixture", env_generation=1)
        self.store = Store(self.root / "store", self.identity)
        self.addCleanup(self.store.close)
        self.robot = PlanningRobot(self.root)
        self.config = Config.from_dict(dict(features=dict(completion_guidance="none",
            observation_profile=LEGACY_ACTOR_PROFILE, actor_payload_profile="full_v1")))
        self.harness = Harness(self.store, self.robot, config=self.config, enable_wait=True, task_plan_required=True)
        self.harness.start()
        self.calls = 0

    def dispatch(self, name, arguments=None):
        self.calls += 1
        return self.harness.dispatch(name, arguments or {}, dict(tool_call_id=f"call-{self.calls}"))

    def submit(self, value=None, observation_id=None):
        value = plan() if value is None else value
        args = dict(operation="submit", plan=value,
                    observation_id=observation_id or self.harness.latest["observation_id"])
        if isinstance(value, str):
            args["ledger"] = []
        return payload(self.dispatch("task_plan", args))

    def action_args(self):
        return dict(observation_id=self.harness.latest["observation_id"], native_steps=1)

    def test_robot_action_requires_plan_without_consuming_budget(self):
        with patch.object(self.harness, "invalidate_scene") as invalidate:
            result = payload(self.dispatch("robot_wait", self.action_args()))
        self.assertEqual(result["receipt"]["status"], "rejected")
        self.assertIn("task_plan_required", result["receipt"]["reason"])
        invalidate.assert_not_called()
        self.assertEqual(self.robot.executions, 0)
        self.assertEqual(self.harness.action_count, 0)
        self.submit()
        result = payload(self.dispatch("robot_wait", self.action_args()))
        self.assertEqual(result["receipt"]["status"], "completed")
        self.assertEqual(self.robot.executions, 1)

    def test_queries_allowed_and_do_not_unlock_actions(self):
        read = payload(self.dispatch("task_plan", dict(operation="read")))
        self.assertTrue(read["required"])
        self.assertIsNone(read["plan"])
        self.assertEqual(read["tool_choices"], "advisory")
        self.assertTrue(self.dispatch("robot_observe")["success"])
        self.assertTrue(self.dispatch("robot_capabilities")["success"])
        self.assertTrue(self.dispatch("history_view_observation", dict(
            observation_id=self.harness.latest["observation_id"], camera_id="fixture_camera", roi=[0, 0, 1, 1]))["success"])
        self.assertIsNone(self.harness.task_planning.current(self.identity))
        self.assertEqual(self.robot.executions, 0)

    def test_revision_and_tool_suggestions_are_advisory(self):
        first = self.submit()
        original = deepcopy(first)
        stages = [dict(goal="Move after reviewing evidence.", advance_when="The arm reaches the selected pose.",
                       suggested_tools=["robot_wait", "an_unavailable_optional_tool"])]
        second = self.submit(plan(stages=stages, uncertainties=[]))
        self.assertEqual(second["revision"], 2)
        self.assertEqual(second["previous_plan_id"], first["plan_id"])
        self.assertEqual(self.store.read("task_plan", first["plan_id"]), original)
        self.assertEqual(payload(self.dispatch("task_plan", dict(operation="read")))["plan"], second)
        moved = payload(self.dispatch("robot_wait", self.action_args()))
        self.assertEqual(moved["receipt"]["status"], "completed")
        self.assertEqual(self.harness.task_planning.current(self.identity)["plan_id"], second["plan_id"])

    def test_freeform_experience_choices_can_change_without_action_binding(self):
        initial = "Check uncertain geometry with a side view before choosing a grasp."
        first = payload(self.dispatch("task_plan", dict(plan=initial, ledger=[])))
        self.assertEqual(first, dict(plan_id="P1", status="saved"))
        # A provisional consideration does not restrict subsequent tool choices.
        moved = payload(self.dispatch("robot_wait", self.action_args()))
        self.assertEqual(moved["receipt"]["status"], "completed")
        self.assertEqual(self.harness.task_planning.current(self.identity)["plan_id"], "P1")

        revised = "The target stayed at its source; reconsider the moving object and test retention."
        second = payload(self.dispatch("task_plan", dict(operation="update", plan=revised)))
        self.assertEqual(second, dict(plan_id="P2", status="saved"))
        self.assertEqual(self.store.read("task_plan", "P1")["plan"], initial)
        self.assertEqual(payload(self.dispatch("task_plan", dict(operation="read")))["plan"], revised)
        record = self.harness.task_planning.current(self.identity)
        self.assertEqual((record["revision"], record["previous_plan_id"]), (2, "P1"))
        self.assertEqual(record["observation_id"], self.harness.latest["observation_id"])
        self.assertEqual(payload(self.dispatch("robot_wait", self.action_args()))["receipt"]["status"],
                         "completed")

    def test_attention_partial_update_preserves_plan_and_replaces_or_clears_list(self):
        def call(role, arguments):
            return self.harness.invoke("task_plan", arguments, {}, role=role).data
        first = call("actor", dict(plan="Understand the target; transfer it and verify support.", ledger=[]))
        self.assertEqual(first, dict(plan_id="P1", status="saved"))
        self.assertNotIn("attention", call("program", dict(operation="read")))
        self.assertEqual(self.harness.task_planning.decision_context(self.identity)["attention"], [])
        self.harness.observe()
        selected = [attention()]
        second = call("program", dict(operation="update", attention=selected))
        self.assertEqual(second, dict(plan_id="P2", status="saved"))
        saved = self.store.read("task_plan", "P2")
        selected[0]["concern"] = "Caller mutation must not change saved intent."
        self.assertEqual(saved["attention"], [attention()])
        self.assertEqual(saved["plan"], self.store.read("task_plan", "P1")["plan"])
        self.assertEqual((saved["revision"], saved["previous_plan_id"]), (2, "P1"))
        self.assertEqual(saved["observation_id"], self.harness.latest["observation_id"])
        third = call("actor", dict(operation="update", plan="Use the changed approach, then verify support."))
        self.assertEqual(self.store.read("task_plan", third["plan_id"])["attention"], [attention()])
        replacement = [attention(concern="Release might be hidden by the support rim.")]
        call("program", dict(operation="update", attention=replacement))
        self.assertEqual(call("actor", dict(operation="read"))["attention"], replacement)
        cleared = call("actor", dict(operation="update", attention=[]))
        self.assertEqual(cleared, dict(plan_id="P5", status="saved"))
        self.assertEqual(call("program", dict(operation="read"))["attention"], [])
        self.assertEqual(self.store.read("task_plan", "P2"), saved)
        self.assertEqual(self.robot.executions, 0)

    def test_ledger_is_independent_replaceable_and_advisory(self):
        selected = [ledger_entry()]
        first = self.dispatch("task_plan", dict(plan="Transfer and verify support.", attention=[attention()], ledger=selected))
        self.assertEqual(payload(first)["status"], "saved")
        selected[0]["basis"] = "caller mutation"
        saved = self.harness.task_planning.current(self.identity)
        self.assertEqual(saved["ledger"], [ledger_entry()])
        self.assertEqual(self.harness.task_planning.decision_context(self.identity)["ledger"], [ledger_entry()])
        self.assertEqual(payload(self.dispatch("task_plan", dict(operation="read")))["ledger"], [ledger_entry()])

        # A pending row remains advisory: normal task progress is still admitted.
        moved = payload(self.dispatch("robot_wait", self.action_args()))
        self.assertEqual(moved["receipt"]["status"], "completed")

        self.harness.observe()
        supported = ledger_entry(status="supported", basis="RGB shows the object moving with the fingers.")
        updated = payload(self.dispatch("task_plan", dict(operation="update", ledger=[supported])))
        self.assertEqual(updated["status"], "saved")
        record = self.harness.task_planning.current(self.identity)
        self.assertEqual(record["attention"], [attention()])
        self.assertEqual(record["ledger"], [supported])
        self.assertEqual(record["ledger_observation_id"], self.harness.latest["observation_id"])

        self.harness.observe()
        self.dispatch("task_plan", dict(operation="update", ledger=[]))
        self.assertEqual(self.harness.task_planning.decision_context(self.identity)["ledger"], [])

    def test_repeated_update_same_observation_is_a_noop_but_new_observation_is_saved(self):
        self.dispatch("task_plan", dict(plan="Inspect and verify the transfer.", ledger=[]))
        before_records = self.store.records("task_plan")
        before_events = self.store.events_by_type(["task_plan.submitted"])
        repeated = self.dispatch("task_plan", dict(operation="update",
            plan="Inspect and verify the transfer."))
        self.assertEqual(payload(repeated), dict(plan_id="P1", status="unchanged"))
        self.assertEqual(self.store.records("task_plan"), before_records)
        self.assertEqual(self.store.events_by_type(["task_plan.submitted"]), before_events)

        self.harness.observe()
        reconsidered = self.dispatch("task_plan", dict(operation="update",
            plan="Inspect and verify the transfer."))
        self.assertEqual(payload(reconsidered), dict(plan_id="P2", status="saved"))
        self.assertEqual(self.harness.task_planning.current(self.identity)["plan_observation_id"],
                         self.harness.latest["observation_id"])

    def test_initial_freeform_submit_requires_explicit_ledger(self):
        with self.assertRaisesRegex(ValueError, "task_plan_submit_requires_ledger"):
            self.dispatch("task_plan", dict(plan="Observe and verify the transfer."))
        self.assertIsNone(self.harness.task_planning.current(self.identity))
        self.dispatch("task_plan", dict(plan="Observe and verify the transfer.", ledger=[]))
        self.assertEqual(self.harness.task_planning.current(self.identity)["ledger"], [])

    def test_invalid_ledger_updates_are_atomic_and_old_records_project_empty_ledger(self):
        self.dispatch("task_plan", dict(plan="Inspect and transfer.", ledger=[ledger_entry()]))
        before = self.harness.task_planning.current(self.identity)
        records = self.store.records("task_plan")
        events = self.store.events_by_type(["task_plan.submitted"])
        invalid = [None, [{}], [ledger_entry(status="bad")],
                   [ledger_entry(), ledger_entry(id="retention")],
                   [ledger_entry(extra="unexpected")], [ledger_entry(expected=" ")],
                   [ledger_entry()] * 9]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.dispatch("task_plan", dict(operation="update", ledger=value))
            self.assertEqual(self.store.records("task_plan"), records)
            self.assertEqual(self.harness.task_planning.current(self.identity), before)
            self.assertEqual(self.store.events_by_type(["task_plan.submitted"]), events)

        old = dict(before)
        old.pop("ledger")
        old.pop("ledger_observation_id")
        old["plan_id"] = "P-old"
        old["revision"] = 99
        self.store.save("task_plan", old["plan_id"], old)
        self.store.set_state("runtime", "task_plan", old)
        self.assertEqual(self.harness.task_planning.decision_context(self.identity)["ledger"], [])
        self.assertEqual(payload(self.dispatch("task_plan", dict(operation="read")))["ledger"], [])

    def test_invalid_attention_updates_leave_all_saved_planning_state_unchanged(self):
        long_valid = attention(check="证" * 1600)
        self.dispatch("task_plan", dict(plan="Inspect the first transfer.", attention=[long_valid], ledger=[]))
        before = self.harness.task_planning.current(self.identity)
        records = self.store.records("task_plan")
        events = self.store.events_by_type(["task_plan.submitted"])
        invalid = [None, {}, "concern", [None], [{}], [dict(concern="x", check="y")],
                   [dict(attention(), extra="unexpected")]]
        invalid += [[attention(**{field: value})] for field in ("concern", "check", "when")
                    for value in (" ", "x" * 1601, False)]
        requests = [dict(operation="update", plan="Must not be partially saved.", attention=value)
                    for value in invalid]
        requests += [dict(operation="update"), dict(attention=[]),
                     dict(operation="update", plan=" ", attention=[])]
        for arguments in requests:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                self.dispatch("task_plan", arguments)
            self.assertEqual(self.store.records("task_plan"), records)
            self.assertEqual(self.harness.task_planning.current(self.identity), before)
            self.assertEqual(self.store.events_by_type(["task_plan.submitted"]), events)
        self.assertEqual(before["attention"], [long_valid])

    def test_partial_updates_preserve_each_fields_observation_source(self):
        owner = self.harness.task_planning

        def check_sources(plan_observation, attention_observation):
            saved = owner.current(self.identity)
            context = owner.decision_context(self.identity)
            read = self.harness.invoke("task_plan", dict(operation="read"), {}, role="program").data
            for value in (saved, context, read):
                self.assertEqual(value["plan_observation_id"], plan_observation)
                self.assertEqual(value["attention_observation_id"], attention_observation)
                if value is not saved or "ledger_observation_id" in value:
                    self.assertIn("ledger_observation_id", value)
                self.assertNotIn("based_on_observation_id", value)
            self.assertEqual(saved["observation_id"], self.harness.latest["observation_id"])

        initial = self.harness.latest["observation_id"]
        self.dispatch("task_plan", dict(plan="Transfer then verify support.", attention=[attention()], ledger=[]))
        check_sources(initial, initial)
        self.harness.observe()
        attention_observation = self.harness.latest["observation_id"]
        self.dispatch("task_plan", dict(operation="update", attention=[attention(concern="Check the new alignment.")]))
        check_sources(initial, attention_observation)
        self.assertEqual(owner.current(self.identity)["plan"], "Transfer then verify support.")

        self.harness.observe()
        self.dispatch("task_plan", dict(operation="update", plan="Inspect the changed support first."))
        check_sources(self.harness.latest["observation_id"], attention_observation)
        # Explicitly resubmitting the same text records reconsideration at the new observation.
        self.harness.observe()
        plan_observation = self.harness.latest["observation_id"]
        self.dispatch("task_plan", dict(operation="update", plan=owner.current(self.identity)["plan"]))
        check_sources(plan_observation, attention_observation)

        self.harness.observe()
        self.dispatch("task_plan", dict(operation="update", attention=[]))
        check_sources(plan_observation, self.harness.latest["observation_id"])
        self.assertEqual(owner.decision_context(self.identity)["attention"], [])
        self.harness.observe()
        self.dispatch("task_plan", dict(plan="A new full submission without concerns.", ledger=[]))
        check_sources(self.harness.latest["observation_id"], None)
        self.assertNotIn("attention", owner.current(self.identity))
        self.assertEqual(owner.decision_context(self.identity)["attention"], [])

    def test_old_records_keep_unknown_field_sources_until_explicitly_updated(self):
        owner = self.harness.task_planning
        # Old records have only the observation at save time; it cannot identify
        # which retained field was reconsidered, even when its text is unchanged.
        for revision in (1, 2):
            self.harness.observe()
            record = dict(plan_id=f"P{revision}", version="freeform_task_plan", identity=self.identity,
                revision=revision, previous_plan_id="P1" if revision == 2 else None,
                observation_id=self.harness.latest["observation_id"], plan="Retained old plan.",
                attention=[attention()], source_kind="actor_judgment", tool_choices="advisory")
            self.store.save("task_plan", record["plan_id"], record)
            self.store.set_state("runtime", "task_plan", record)
            for value in (owner.decision_context(self.identity),
                          payload(self.dispatch("task_plan", dict(operation="read")))):
                self.assertEqual(value["plan"], record["plan"])
                self.assertEqual(value["attention"], record["attention"])
                self.assertIsNone(value["plan_observation_id"])
                self.assertIsNone(value["attention_observation_id"])
            self.assertEqual(owner.current(self.identity), record)

        self.harness.observe()
        updated_attention = self.harness.latest["observation_id"]
        self.dispatch("task_plan", dict(operation="update", attention=[attention()]))
        self.assertIsNone(owner.current(self.identity)["plan_observation_id"])
        self.assertEqual(owner.current(self.identity)["attention_observation_id"], updated_attention)
        self.harness.observe()
        self.dispatch("task_plan", dict(operation="update", plan="Reconsidered plan."))
        self.assertEqual(owner.current(self.identity)["plan_observation_id"], self.harness.latest["observation_id"])
        self.assertEqual(owner.current(self.identity)["attention_observation_id"], updated_attention)
        self.assertEqual(self.store.read("task_plan", "P2"), record)

    def test_attention_context_uses_persisted_owner_and_isolates_episode(self):
        self.dispatch("task_plan", dict(plan="Verify support before withdrawing.", attention=[attention()], ledger=[]))
        owner = TaskPlanning(self.store)
        original = owner.current(self.identity)
        expected = dict(plan_id="P1", revision=1, plan=original["plan"], attention=[attention()],
                        ledger=[],
                        plan_observation_id=self.harness.latest["observation_id"],
                        attention_observation_id=self.harness.latest["observation_id"],
                        ledger_observation_id=self.harness.latest["observation_id"])
        self.assertEqual(owner.decision_context(self.identity), expected)
        projected = owner.decision_context(self.identity)
        projected["attention"][0]["check"] = "Do not mutate the owner."
        self.assertEqual(owner.decision_context(self.identity), expected)
        self.store.set_state("runtime", "task_plan", dict(original, attention=[]))
        self.assertIsNone(owner.decision_context(self.identity))
        with self.assertRaisesRegex(ValueError, "task_plan_required"):
            owner.require(self.identity)
        self.store.set_state("runtime", "task_plan", original)
        old_observation = self.harness.latest["observation_id"]
        self.harness.observe()
        with self.assertRaisesRegex(ValueError, "stale_observation"):
            self.dispatch("task_plan", dict(operation="update", attention=[], observation_id=old_observation))
        with self.assertRaisesRegex(ValueError, "task_plan_requires_current_episode_observation"):
            owner.call(dict(operation="update", attention=[]), self.identity,
                       dict(self.harness.latest, episode_id="other-episode"), {})
        self.assertEqual(owner.current(self.identity), original)
        for field in self.identity:
            other = dict(self.identity, **{field: 2 if field == "env_generation" else "other"})
            self.assertIsNone(owner.decision_context(other))
        self.harness.reset()
        self.assertIsNone(owner.decision_context(self.identity))
        with self.assertRaisesRegex(ValueError, "task_plan_not_submitted"):
            self.dispatch("task_plan", dict(operation="update", attention=[]))
        self.assertEqual(self.store.read("task_plan", "P1"), original)
        self.dispatch("task_plan", dict(plan="Inspect the new episode.", ledger=[]))
        context = owner.decision_context(self.identity)
        self.assertEqual((context["revision"], context["attention"]), (1, []))

    def test_legacy_dictionary_plan_never_silently_discards_attention(self):
        legacy = self.submit()
        owner = self.harness.task_planning
        self.assertEqual(owner.decision_context(self.identity)["plan"], legacy["plan"])
        self.assertEqual(owner.decision_context(self.identity)["attention"], [])
        for arguments in (dict(operation="update", attention=[]),
                          dict(operation="update", plan="New text", attention=[]),
                          dict(operation="submit", observation_id=self.harness.latest["observation_id"],
                               plan=plan(), attention=[])):
            with self.subTest(arguments=arguments), self.assertRaisesRegex(ValueError, "attention_requires"):
                self.dispatch("task_plan", arguments)
            self.assertEqual(owner.current(self.identity), legacy)
        self.assertEqual(payload(self.dispatch("task_plan", dict(operation="read")))["plan"], legacy)
        # Existing freeform replacement of a legacy plan remains supported.
        self.dispatch("task_plan", dict(operation="update", plan="A revised ordinary-language plan."))
        self.assertEqual(owner.decision_context(self.identity)["plan"], "A revised ordinary-language plan.")

    def test_stage_dependency_graph_is_validated_but_advisory(self):
        stages = [dict(id="release", goal="Release after verified support.",
                       advance_when="Visible separation.", arms=["right"], depends_on=["receive"],
                       observation_gate="Inspect fresh receiver support before release."),
                  dict(id="receive", goal="Receive the object.", advance_when="Fresh support evidence.",
                       arms=["left", "right"], depends_on=[]),
                  dict(goal="Unrelated preparation.", advance_when="A clear approach.", arms=["left"])]
        saved = self.submit(plan(stages=stages))
        self.assertEqual(saved["plan"]["stages"], stages)
        # Graph dependencies are actor intent, not newly enforced action gates.
        moved = payload(self.dispatch("robot_wait", self.action_args()))
        self.assertEqual(moved["receipt"]["status"], "completed")
        self.assertEqual(self.harness.task_planning.current(self.identity)["plan_id"], saved["plan_id"])

    def test_invalid_stage_dependency_graph_never_persists(self):
        base = dict(goal="Observe.", advance_when="Evidence is visible.")
        cases = [
            [dict(base, id="a"), dict(base, id="a")],
            [dict(base, id="a", depends_on=["missing"])],
            [dict(base, id="a", depends_on=["a"])],
            [dict(base, id="a", depends_on=["b"]), dict(base, id="b", depends_on=["a"])],
            [dict(base, id="a"), dict(base, depends_on=["a", "a"])],
            [dict(base, id="a", arms=["left", "left"])],
            [dict(base, id="a", observation_gate=" ")],
        ]
        for stages in cases:
            with self.subTest(stages=stages), self.assertRaisesRegex(ValueError, "invalid_task_plan"):
                self.submit(plan(stages=stages))
        self.assertEqual(self.store.records("task_plan"), [])

    def test_stale_and_invalid_submissions_do_not_unlock(self):
        old_observation = self.harness.latest["observation_id"]
        self.harness.observe()
        with self.assertRaisesRegex(ValueError, "stale_observation"):
            self.submit(observation_id=old_observation)
        for value in ({}, plan(objective="  "), plan(stages=[]), plan(completion_criteria=[]),
                      plan(stages=[dict(goal="Observe")]), plan(unexpected="field")):
            with self.subTest(plan=value), self.assertRaisesRegex(ValueError, "invalid_task_plan"):
                self.submit(value)
        self.assertIsNone(self.harness.task_planning.current(self.identity))
        self.assertEqual(self.store.records("task_plan"), [])

    def test_projection_requires_matching_immutable_record(self):
        original = self.submit()
        for changed in (dict(original, plan_id="missing-record"), dict(original, revision=99)):
            with self.subTest(projection=changed):
                self.store.set_state("runtime", "task_plan", changed)
                self.assertIsNone(self.harness.task_planning.current(self.identity))
                with self.assertRaisesRegex(ValueError, "task_plan_required"):
                    self.harness.task_planning.require(self.identity)
        self.store.set_state("runtime", "task_plan", original)
        self.assertEqual(self.harness.task_planning.current(self.identity), original)

    def test_observation_from_other_episode_cannot_anchor_submission(self):
        self.harness.latest = dict(self.harness.latest, episode_id="other-episode")
        with self.assertRaisesRegex(ValueError, "stale_observation"):
            self.submit()
        self.assertIsNone(self.harness.task_planning.current(self.identity))

    def test_operation_context_is_actor_judgment_and_does_not_gate_unrelated_transit(self):
        observation_id=self.harness.latest["observation_id"]
        context=dict(object_label="current visible object",state="unknown",
            evidence=dict(observation_id=observation_id,sources=["rgb"],basis="Only the front surface is visible.",
                          uncertainty="Depth and support are unknown."))
        value=plan(stages=[dict(goal="Inspect and choose a grasp.",advance_when="A reachable exposed section is visible.",operation_context=context)])
        saved=self.submit(value)
        self.assertEqual(saved["source_kind"],"actor_judgment")
        self.assertEqual(saved["plan"]["stages"][0]["operation_context"],context)
        # Ordinary motion is not retrofitted with a contact/geometry form.
        result=payload(self.dispatch("robot_wait",self.action_args()))
        self.assertEqual(result["receipt"]["status"],"completed")

    def test_protocol_disabled_keeps_v4_schema_and_instructions(self):
        legacy=TaskPlanning(self.store,required=True,manipulation_protocol=False)
        saved=legacy.call(dict(operation="submit",observation_id=self.harness.latest["observation_id"],plan=plan()),
                          self.identity,self.harness.latest,{})
        self.assertEqual(saved["version"],"actor_task_plan_v4")
        self.assertEqual(version_for(False),"actor_task_plan_v4")
        self.assertNotIn("operation_context",instructions_for(False))
        self.assertNotIn("operation_context",plan_schema_for(False)["properties"]["stages"]["items"]["properties"])
        self.assertIn("operation_context",instructions_for(True))
        self.assertIn("operation_context",plan_schema_for(True)["properties"]["stages"]["items"]["properties"])

    def test_reset_invalidates_plan_and_same_identity_reconstruction_restores_it(self):
        first = self.submit()
        rebuilt = Harness(self.store, self.robot, config=self.config, task_plan_required=True)
        self.assertEqual(rebuilt.task_planning.current(self.identity), first)
        for key, alternate in (("run_id", "other"), ("episode_id", "other"),
                               ("env_instance_id", "other"), ("env_generation", 2)):
            with self.subTest(identity_field=key):
                self.assertIsNone(rebuilt.task_planning.current(dict(self.identity, **{key: alternate})))
        previous_identity = deepcopy(self.identity)
        self.harness.reset()
        self.assertIsNone(self.harness.task_planning.current(self.identity))
        self.assertEqual(self.store.read("task_plan", first["plan_id"])["identity"], previous_identity)
        rejected = payload(self.dispatch("robot_wait", self.action_args()))
        self.assertIn("task_plan_required", rejected["receipt"]["reason"])
        second = self.submit()
        self.assertEqual(second["revision"], 1)
        self.assertIsNone(second["previous_plan_id"])

    def test_rejected_action_id_is_immutable_after_plan_submission(self):
        args = dict(self.action_args(), action_id="before-plan")
        rejected = payload(self.dispatch("robot_wait", args))
        self.submit()
        self.assertEqual(payload(self.dispatch("robot_wait", args)), rejected)
        admitted = payload(self.dispatch("robot_wait", dict(args, action_id="after-plan")))
        self.assertEqual(admitted["receipt"]["status"], "completed")
        self.harness.reset()
        self.assertEqual(self.harness.execute(admitted["request"]),
                         {key: admitted[key] for key in ("request", "receipt")})
        self.assertEqual(self.robot.executions, 1)

    def test_program_dispatch_obeys_gate_and_can_submit_plan(self):
        from program_fixture import LegacyProgramRunner
        runner = LegacyProgramRunner(self.harness)
        self.harness.programs = runner
        entry = dict(job_id="fixture-program", cancel=threading.Event(), deadline=time.monotonic() + 600,
                     call_lock=threading.Lock(), calls=0, max_calls=10, correlation={},
                     condition=threading.Condition(), workspace=runner.root,
                     job=dict(job_id='fixture-program', status='running'),
                     action_evidence=[], model_wait_s=0., review_wait_s=0.)
        runner.entries[entry["job_id"]] = entry
        def call(operation, tool, arguments):
            return payload(runner.dispatch(entry, dict(operation_id=operation, tool=tool, arguments=arguments)))
        result = call("1", "robot_wait", self.action_args())
        self.assertIn("task_plan_required", result["receipt"]["reason"])
        call("2", "task_plan", dict(operation="submit", plan=plan(), observation_id=self.harness.latest["observation_id"]))
        result = call("3", "robot_wait", self.action_args())
        self.assertEqual(result["receipt"]["status"], "completed")
        self.assertEqual(self.robot.executions, 1)

    def test_policy_gate_prevents_inference_and_still_calls_execute_after_submission(self):
        policy = SimpleNamespace(infer=Mock(return_value=dict(actions=[[0.] * 14])))
        self.harness.action_policy = policy
        args = dict(action_id="policy-action", observation_id=self.harness.latest["observation_id"],
                    prompt="Raise the left arm.", prefix=1)
        with self.assertRaisesRegex(ValueError, "task_plan_required"):
            self.harness.policy_step(args, {})
        policy.infer.assert_not_called()
        self.submit()
        with patch.object(self.harness, "execute", return_value={"sentinel": True}) as execute:
            self.assertEqual(self.harness.policy_step(args, {}), {"sentinel": True})
        policy.infer.assert_called_once()
        self.assertEqual(execute.call_args.args[0]["type"], "JointPrefixAction")

    def test_required_tool_cannot_be_disabled_and_default_harness_remains_optional(self):
        config = self.config.with_overrides(dict(features=dict(disabled_tools=["task_plan"])))
        with self.assertRaisesRegex(ValueError, "required_task_plan_tool_cannot_be_disabled"):
            Harness(self.store, self.robot, config=config, task_plan_required=True)
        optional = Harness(self.store, self.robot, config=config)
        optional.start()
        result = payload(optional.dispatch("robot_wait", dict(self.action_args(),
                         observation_id=optional.latest["observation_id"]), dict(tool_call_id="optional")))
        self.assertEqual(result["receipt"]["status"], "completed")

    def test_policy_inference_cannot_carry_old_plan_across_reset(self):
        self.submit()
        def infer(*args, **kwargs):
            self.harness.reset()
            return dict(actions=[[0.] * 14])
        self.harness.action_policy = SimpleNamespace(infer=Mock(side_effect=infer))
        args = dict(action_id="reset-during-policy", observation_id=self.harness.latest["observation_id"],
                    prompt="Raise the arm.", prefix=1)
        result = self.harness.policy_step(args, {})
        self.assertEqual(result["receipt"]["status"], "rejected")
        self.assertIn("task_plan_required", result["receipt"]["reason"])
        self.assertEqual(self.robot.executions, 0)
        self.assertEqual(self.harness.policy_step(args, {}), result)
        self.harness.action_policy.infer.assert_called_once()


class TaskPlanningGatewayCheck(unittest.IsolatedAsyncioTestCase):
    async def test_production_cannot_disable_required_tool(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Config.from_dict(dict(features=dict(disabled_tools=["task_plan"])))
            with self.assertRaisesRegex(ValueError, "required_task_plan_tool_cannot_be_disabled"):
                Workbench(Path(temporary) / "disabled-plan", robot=SimpleNamespace(close=lambda: None), config=config)

    async def test_production_contract_is_fixed_and_exported(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Config.from_dict(dict(features=dict(observation_profile=LEGACY_ACTOR_PROFILE,
                motion_review=False, completion_guidance="none", terminal=False, visual_review_model=None)))
            workbench = Workbench(Path(temporary) / "gateway-plan", robot=SimpleNamespace(close=lambda: None), config=config)
            self.assertTrue(workbench.harness.task_planning.required)
            self.assertIn("task_plan", {tool["name"] for tool in workbench.actor_tools()})
            # Follow gateway lifecycle fixtures: substitute process-free startup dependencies.
            harness = SimpleNamespace(accepting=True, active_action=None, states={}, action_count=0,
                capabilities=dict(data_origin="synthetic_fixture"), thread_id=None,
                scene=SimpleNamespace(capabilities=lambda: dict(physics="unsupported")))
            for field in ('tool_registry', 'config', 'disabled_tools', 'observation_profile',
                          'motion_review', 'enable_wait', 'vision_compare', 'visual_reviewer',
                          'action_policy', 'programs'):
                setattr(harness, field, getattr(workbench.harness, field))
            harness.start = lambda: None
            harness.request_stop = lambda: setattr(harness, "accepting", False)
            session = SimpleNamespace(thread_id="fixture-thread", turn_id=None, state="idle", actor_dir=workbench.root,
                model="fixture-model", provider="fixture-provider", effort="high", price_card=None,
                cli_executable=Path("/fixture/codex"), process=SimpleNamespace(pid=-1), close=lambda: None)
            session.start = Mock(return_value="fixture-thread")
            session.run_turn = lambda prompt: None
            workbench.harness, workbench.session = harness, session
            try:
                await workbench.start("Observe the task, then act.")
                instructions = session.start.call_args.args[1]
                self.assertIn("Before your first physical action", instructions)
                self.assertIn("provisional understanding", instructions)
                manifest = json.loads((workbench.root / "manifest.json").read_text())
                self.assertEqual(manifest["task_planning"], dict(version="freeform_task_plan", required_before_robot_actions=True,
                                 tool_choices="advisory", revisions="actor_discretion"))
                self.assertIn("cortex/context/task_planning.py", manifest["source_sha256"])
                _, source = workbench.store.resolve_artifact(manifest["source_ref"], allowed_scopes=("observer",))
                self.assertIn("cortex/context/task_planning.py", json.loads(source))
            finally:
                await workbench.close()
