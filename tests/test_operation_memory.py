"""Operation lessons reuse immutable facts without merging task/shared scopes."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from auriga.core.contracts import digest
from auriga.cortex.context.operation_memory import OperationMemory
from auriga.core.storage.store import Store
from auriga.core.tool_errors import ToolInputError, error_payload


class OperationMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.identity = dict(run_id="run", episode_id="task", env_instance_id="env", env_generation=1)
        self.store = Store(Path(self.temp.name) / "task", self.identity)
        self.shared = Store(Path(self.temp.name) / "shared", self.identity)
        self.addCleanup(self.store.close)
        self.addCleanup(self.shared.close)
        self.memory = OperationMemory(self.store, self.shared)
        for number, identifier in enumerate(("before", "after", "unrelated")):
            self.store.save("observation", identifier, dict(self.identity,
                observation_id=identifier, sim_step=number * 10, step_domain="adapter_control_step"))
        self.action = dict(self.identity, action_id="move", type="EEAction", based_on_observation_id="before",
            path=dict(waypoints=[dict(left=dict(position_m=[1, 2, 3]))]),
            gripper_events=[dict(arm="left", phase="after_motion", goal_normalized=1.)], speed_scale=.5)
        self.receipt = dict(status="completed", result_observation_id="after", completed_native_steps=9)
        self.store.save("action_request", "move", self.action)
        self.store.save("action_receipt", "move", self.receipt)
        self.request = dict(kind="record", scope="task", operation="retreat",
            summary="Clear the tall rim vertically before moving sideways.",
            conditions="Object visibly released inside the container.",
            action_ids=["move"], observation_ids=["after"])

    def test_execution_facts_are_extracted_and_completion_does_not_verify_object_success(self):
        before = deepcopy(self.request)
        record = self.memory.call(self.request)["record"]
        self.assertEqual(self.request, before)
        self.assertEqual(record["memory_id"], "task_experience_1")
        self.assertEqual(record["physical_outcome"], "not_verified")
        self.assertEqual(record["facts"][0]["status"], "completed")
        self.assertEqual(record["facts"][0]["completed_native_steps"], 9)
        self.assertEqual(record["facts"][0]["gripper_commands"][0]["goal_normalized"], 1.)
        self.assertEqual(self.store.read("action_request", "move"), self.action)
        self.assertEqual(self.store.read("action_receipt", "move"), self.receipt)
        self.assertNotIn("action_id", self.store.read("action_receipt", "move"))
        self.assertNotIn("evidence", record)
        stored = self.store.read("operation_memory", record["memory_id"])
        self.assertIn("request_digest", stored["evidence"]["actions"][0])
        self.assertEqual(stored["evidence"]["actions"][0]["receipt_digest"], digest(self.receipt))
        self.assertNotIn("position_m", str(record))

    def test_shared_writers_do_not_allocate_ids_from_a_stale_record_count(self):
        request = dict(self.request, scope="shared")
        with patch.object(self.shared, "records", return_value=[]):
            first = self.memory.call(request)["record"]
            second = self.memory.call(request)["record"]
        self.assertNotEqual(first["memory_id"], second["memory_id"])
        self.assertEqual(len(self.shared.records("operation_memory")), 2)

    def test_task_and_shared_queries_remain_separate_and_shared_survives_next_task(self):
        task = self.memory.call(self.request)["record"]
        shared = self.memory.call(dict(self.request, scope="shared"))["record"]
        self.assertEqual(self.memory.call(dict(kind="query", scope="task"))["records"], [task])
        self.assertEqual(self.memory.call(dict(kind="query", scope="shared"))["records"], [shared])
        other_identity = dict(self.identity, episode_id="next", env_generation=2)
        with tempfile.TemporaryDirectory() as directory:
            other_store = Store(directory, other_identity)
            try:
                next_memory = OperationMemory(other_store, self.shared)
                self.assertEqual(next_memory.call(dict(kind="query", scope="task"))["records"], [])
                reused = next_memory.call(dict(kind="query", scope="shared"))["records"][0]
                self.assertEqual(reused["source"]["episode_id"], "task")
                self.assertIn("re-localize", reused["reuse"])
                self.assertNotIn("position_m", str(reused))
            finally:
                other_store.close()
        self.store.identity = other_identity
        self.assertEqual(self.memory.call(dict(kind="query", scope="task"))["records"], [])

    def test_all_operation_categories_use_the_same_history_contract(self):
        for operation in ("approach", "grasp", "transport", "place", "retreat", "wait", "recover"):
            self.memory.call(dict(self.request, operation=operation))
            found = self.memory.call(dict(kind="query", scope="task", operation=operation.upper()))
            self.assertEqual(found["total_matches"], 1)
            self.assertEqual(found["records"][0]["operation"], operation)
        self.assertEqual(self.memory.call(dict(kind="query", scope="task", query="tall rim", limit=2))["total_matches"], 7)
        self.assertEqual(len(self.memory.call(dict(kind="query", scope="task", limit=2))["records"]), 2)

    def test_failed_plan_without_new_observation_can_record_only_mechanical_failure(self):
        action = dict(self.action, action_id="failed")
        self.store.save("action_request", "failed", action)
        self.store.save("action_receipt", "failed", dict(action_id="failed", status="failed", reason="path_infeasible"))
        record = self.memory.call(dict(self.request, action_ids=["failed"], observation_ids=["before"]))["record"]
        self.assertEqual(record["facts"][0]["reason"], "path_infeasible")
        self.assertIsNone(record["facts"][0]["result_observation_id"])
        self.assertEqual(record["physical_outcome"], "not_verified")

    def test_wrong_identity_unrelated_and_missing_result_evidence_rejected(self):
        for observations, error in ((["before"], "requires_action_result"),
                                     (["after", "unrelated"], "unrelated_observation")):
            with self.subTest(observations=observations), self.assertRaisesRegex(ValueError, error):
                self.memory.call(dict(self.request, observation_ids=observations))
        with self.assertRaisesRegex(ToolInputError, "action_request_unavailable"):
            self.memory.call(dict(self.request, action_ids=["not-an-action"]))
        self.store.identity = dict(self.identity, env_generation=2)
        with self.assertRaisesRegex(ValueError, "identity_mismatch"):
            self.memory.call(self.request)

    def test_caller_cannot_supply_fake_facts_or_unbounded_payload(self):
        for update in (dict(facts=[dict(status="success")]), dict(summary="x" * 801),
                       dict(action_ids=["move", "move"]), dict(scope="both")):
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.memory.call(dict(self.request, **update))
        for limit in (0, 31, True):
            with self.assertRaisesRegex(ValueError, "invalid_operation_memory_limit"):
                self.memory.call(dict(kind="query", scope="task", limit=limit))

    def test_shared_conditions_and_dedicated_store_are_required(self):
        request = dict(self.request, scope="shared")
        request.pop("conditions")
        with self.assertRaisesRegex(ValueError, "shared_conditions_required"):
            self.memory.call(request)
        local_only = OperationMemory(self.store)
        self.assertFalse(local_only.call(dict(kind="help"))["shared_available"])
        with self.assertRaisesRegex(ValueError, "shared_store_unavailable"):
            local_only.call(dict(kind="query", scope="shared"))

    def test_explicit_receipt_or_request_ids_cannot_disagree_with_record_key(self):
        for identifier, request_id, receipt_id in (("explicit", "explicit", "explicit"),
                ("wrong-receipt", "wrong-receipt", "another-action"),
                ("wrong-request", "another-action", "wrong-request"),
                ("null-receipt", "null-receipt", None)):
            action = dict(self.action, action_id=request_id)
            receipt = dict(self.receipt, action_id=receipt_id)
            self.store.save("action_request", identifier, action)
            self.store.save("action_receipt", identifier, receipt)
            request = dict(self.request, action_ids=[identifier])
            if identifier == "explicit":
                self.assertEqual(self.memory.call(request)["record"]["facts"][0]["action_id"], identifier)
            else:
                with self.assertRaisesRegex(ToolInputError, "action_id_mismatch") as raised:
                    self.memory.call(request)
                self.assertEqual(raised.exception.field_path, "/request/action_ids/0")
                self.assertEqual(raised.exception.actual, identifier)
            self.assertEqual(self.store.read("action_request", identifier), action)
            self.assertEqual(self.store.read("action_receipt", identifier), receipt)

    def test_missing_and_unlinked_evidence_errors_identify_the_required_ids(self):
        with self.assertRaises(ToolInputError) as raised:
            self.memory.call(dict(self.request, observation_ids=["before"]))
        payload = error_payload(raised.exception, tool="operation_memory", arguments=dict(request=self.request))
        self.assertEqual(payload["field_path"], "/request/observation_ids")
        self.assertEqual(payload["actual"], "move")
        self.assertIn("after", payload["suggestion"])
        with self.assertRaises(ToolInputError) as raised:
            self.memory.call(dict(self.request, observation_ids=["after", "unrelated"]))
        self.assertEqual(raised.exception.field_path, "/request/observation_ids/1")
        self.assertEqual(raised.exception.actual, "unrelated")
        with self.assertRaises(ToolInputError) as raised:
            self.memory.call(dict(self.request, observation_ids=["not-an-observation"]))
        self.assertEqual(raised.exception.code, "operation_memory_observation_unavailable")
        self.assertEqual(self.store.records("operation_memory"), [])

    def test_empty_action_ids_explain_that_preview_notes_need_action_evidence(self):
        with self.assertRaises(ToolInputError) as raised:
            self.memory.call(dict(self.request, action_ids=[]))
        self.assertEqual(raised.exception.field_path, "/request/action_ids")
        self.assertEqual(raised.exception.actual, [])
        self.assertIn("history_list_actions", raised.exception.suggestion)
        self.assertIn("preview-only", raised.exception.suggestion.lower())
        self.assertEqual(self.store.records("operation_memory"), [])

    def test_request_errors_explain_accepted_fields_values_and_limits(self):
        cases = [
            (dict(kind="query", scope="task", extra=True), "invalid_operation_memory_fields", "/request", "Allowed fields:"),
            (dict(self.request, summary="x" * 801), "invalid_operation_memory_summary", "/request/summary", "800 characters"),
            (dict(kind="query", scope="both"), "invalid_operation_memory_scope", "/request/scope", "scope='task'"),
            (dict(kind="lookup"), "unknown_operation_memory_kind", "/request/kind", "kind='query'"),
            (dict(kind="query", scope="task", limit=True), "invalid_operation_memory_limit", "/request/limit", "1 through 30"),
        ]
        for request, code, field_path, advice in cases:
            with self.subTest(code=code), self.assertRaises(ToolInputError) as raised:
                self.memory.call(request)
            self.assertEqual(raised.exception.code, code)
            self.assertEqual(raised.exception.field_path, field_path)
            self.assertIn(advice, raised.exception.suggestion)

    def test_shared_experience_survives_disk_reopen_for_a_different_run(self):
        root = Path(self.temp.name) / "persistent-shared"
        first = Store(root, self.identity)
        try:
            recorded = OperationMemory(self.store, first).call(dict(self.request, scope="shared"))["record"]
        finally:
            first.close()
        next_identity = dict(self.identity, run_id="next-run", episode_id="next-task", env_generation=2)
        next_store = Store(Path(self.temp.name) / "next-task", next_identity)
        reopened = Store(root, next_identity)
        try:
            next_memory = OperationMemory(next_store, reopened)
            self.assertEqual(next_memory.call(dict(kind="query", scope="task"))["records"], [])
            found = next_memory.call(dict(kind="query", scope="shared", query="rim"))["records"]
            self.assertEqual(found, [recorded])
            self.assertEqual(found[0]["source"]["run_id"], self.identity["run_id"])
            self.assertEqual(self.store.read("action_receipt", "move"), self.receipt)
        finally:
            next_store.close()
            reopened.close()


if __name__ == "__main__":
    unittest.main()
