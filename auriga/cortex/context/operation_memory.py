"""Short actor lessons backed by recorded actions, with separate reuse scopes.

Execution facts come from the immutable history. The actor supplies only the
useful lesson and its conditions; neither a receipt nor a lesson verifies an
object outcome. Shared records retain their evidence without becoming motion
commands in a later task.
"""

from copy import deepcopy
from uuid import uuid4

from ...core.contracts import IDENTITY, digest, utc_now
from ...core.tool_errors import ToolInputError


MEMORY_DESCRIPTION = (
    "Query or record useful operation experience for any action, including approach, "
    "grasp, transport, placement and retreat. Choose scope='task' for this task or "
    "'shared' for cross-task lessons; queries keep the scopes separate. "
    "query requires kind and scope; optional operation is an exact case-insensitive category "
    "(up to 80 characters), query is search words (up to 200 characters), and limit is "
    "an integer 1..30, default 5. record requires kind, scope, operation (up to 80 characters), "
    "summary (up to 800 characters), action_ids and observation_ids. Optional conditions "
    "is up to 400 characters and is required for shared records; supplied text must be nonblank. Record only "
    "a short summary, applicable conditions, operation and actual action/observation "
    "IDs. Each ID list requires 1..12 unique recorded IDs, each at most 200 characters; "
    "observation_ids must include each action's "
    "recorded result observation (input observation only when no result exists). "
    "Use only observations linked to those actions; execution facts are read from history. Completed commands do not prove "
    "object success. Before reusing shared lessons, inspect current observations and "
    "re-localize; never reuse source scene coordinates blindly. request={kind:'help'} shows the fields."
)


def _text(value, field, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ToolInputError("invalid_operation_memory_" + field,
            field_path="/request/" + field, actual=value,
            suggestion="Provide a nonblank string of at most " + str(limit) + " characters for " + field + ".")
    return value.strip()


def _fields(value, required, optional=()):
    if (not isinstance(value, dict) or set(required) - value.keys()
            or value.keys() - set(required) - set(optional)):
        missing = sorted(set(required) - value.keys()) if isinstance(value, dict) else sorted(required)
        unexpected = sorted(value.keys() - set(required) - set(optional)) if isinstance(value, dict) else []
        raise ToolInputError("invalid_operation_memory_fields", field_path="/request",
            actual=dict(missing=missing, unexpected=unexpected),
            suggestion="Required fields: " + ", ".join(sorted(required)) + ". Allowed fields: "
            + ", ".join(sorted(set(required) | set(optional))) + ". Supply the required fields and remove unsupported fields.")


def _ids(value, field):
    if (not isinstance(value, list) or not 1 <= len(value) <= 12
            or any(not isinstance(item, str) or not item or len(item) > 200 for item in value)
            or len(set(value)) != len(value)):
        raise ToolInputError("invalid_operation_memory_" + field,
            field_path="/request/" + field, actual=value,
            suggestion="Provide 1..12 unique, nonempty recorded IDs, each at most 200 characters. " + (
                "action_ids cannot be empty: use history_list_actions to select real action IDs, not preview plan IDs. A preview-only note cannot be recorded as action experience."
                if field == "action_ids" else
                "Include each selected action's result observation; use its input only when no result exists."))
    return value


class OperationMemory:
    def __init__(self, store, shared_store=None):
        self.store = store
        self.shared_store = shared_store if shared_store is not store else None

    def _identity(self, value, kind, field_path):
        if any(value.get(key) != self.store.identity.get(key) for key in IDENTITY):
            raise ToolInputError("operation_memory_" + kind + "_identity_mismatch",
                field_path=field_path, actual=value.get("observation_id" if kind == "observation" else "action_id"),
                suggestion="This recorded evidence belongs to a different run, episode or environment generation. Select evidence from the current episode with history_list_actions; never relabel stored evidence to reuse its IDs.")

    def _owner(self, scope):
        if scope == "task":
            return self.store
        if scope == "shared":
            if self.shared_store is None:
                raise ToolInputError("operation_memory_shared_store_unavailable",
                    field_path="/request/scope", actual=scope,
                    suggestion="Shared experience storage is unavailable for this run. Use scope='task' for current-task notes; shared reuse requires a configured shared store.")
            return self.shared_store
        raise ToolInputError("invalid_operation_memory_scope", field_path="/request/scope", actual=scope,
            suggestion="Choose scope='task' for this task or scope='shared' for cross-task experience; scopes are queried separately.")

    def _history(self, kind, identifier, field_path):
        try:
            return self.store.read(kind, identifier)
        except KeyError:
            raise ToolInputError("operation_memory_" + kind + "_unavailable",
                field_path=field_path, actual=identifier,
                suggestion="No " + kind + " is recorded for this ID. Use history_list_actions and its existing observation IDs; preview plan IDs are not executed action IDs.") from None

    @staticmethod
    def _public(record):
        # Integrity and full source identities remain in the stored record. The
        # actor needs functional source IDs and facts, not a provenance dump.
        result = {key: deepcopy(record[key]) for key in
                  ("memory_id", "scope", "operation", "summary", "conditions", "facts")}
        result.update(source_kind="actor_lesson_with_recorded_execution_facts",
                      physical_outcome="not_verified")
        result["source"] = {key: record["identity"][key] for key in ("run_id", "episode_id")}
        result["observation_ids"] = [item["observation_id"] for item in record["evidence"]["observations"]]
        if record["scope"] == "shared":
            result["reuse"] = "Check conditions in current observations and re-localize; source IDs belong to the original task and are not current motion targets."
        return result

    def _evidence(self, action_ids, observation_ids):
        observations, actions, facts = [], [], []
        for index, identifier in enumerate(observation_ids):
            field_path = "/request/observation_ids/" + str(index)
            observation = self._history("observation", identifier, field_path)
            self._identity(observation, "observation", field_path)
            if observation.get("observation_id") != identifier:
                raise ToolInputError("operation_memory_observation_id_mismatch",
                    field_path=field_path, actual=identifier,
                    suggestion="The stored observation_id disagrees with its history key. Select a consistent recorded observation linked to the selected action; do not edit or relabel its evidence.")
            observations.append(dict(observation_id=identifier, digest=digest(observation)))
        linked_observations = set()
        for index, identifier in enumerate(action_ids):
            field_path = "/request/action_ids/" + str(index)
            action = self._history("action_request", identifier, field_path)
            self._identity(action, "action", field_path)
            receipt = self._history("action_receipt", identifier, field_path)
            # The immutable receipt is keyed by action ID; backend receipts may omit that field.
            if action.get("action_id") != identifier or ("action_id" in receipt and receipt["action_id"] != identifier):
                raise ToolInputError("operation_memory_action_id_mismatch",
                    field_path=field_path, actual=identifier,
                    suggestion="The stored action request or explicit receipt action_id disagrees with this ID. Select a consistent recorded action with history_list_actions; do not edit or relabel its evidence.")
            before = action.get("based_on_observation_id")
            after = receipt.get("result_observation_id")
            related = {value for value in (before, after, receipt.get("execution_start_observation_id")) if value}
            linked_observations.update(related)
            # A failed plan may have no terminal capture; its input observation
            # remains valid evidence of a mechanical failure, not of an outcome.
            if (after or before) not in observation_ids:
                raise ToolInputError("operation_memory_requires_action_result_or_failure_input_observation",
                    field_path="/request/observation_ids", actual=identifier,
                    suggestion="Include recorded observation " + str(after or before) + " for action " + identifier
                    + ". Each selected action requires its result observation, or its input only when no result exists. Later unrelated captures cannot replace it; inspect history_list_actions.")
            for source_id in related:
                source = self._history("observation", source_id, "/request/observation_ids")
                self._identity(source, "observation", "/request/observation_ids")
            actions.append(dict(action_id=identifier, request_digest=digest(action), receipt_digest=digest(receipt)))
            fact = {key: deepcopy(receipt[key]) for key in (
                "status", "reason", "executed_native_steps", "completed_native_steps", "executed_control_steps",
                "position_error_m", "orientation_error_rad", "execution_uncertain", "stop_confirmed") if key in receipt}
            fact.update(action_id=identifier, action_type=action.get("type", "EEAction"),
                        before_observation_id=before, result_observation_id=after)
            if action.get("hold_native_steps") is not None:
                fact["requested_wait_native_steps"] = action["hold_native_steps"]
            for key in ("speed_scale", "gripper_speed_scale"):
                if key in action:
                    fact[key] = action[key]
            jaw_events = action.get("gripper_events")
            if jaw_events:
                fact["gripper_commands"] = [{key: deepcopy(event[key]) for key in
                    ("arm", "phase", "goal_normalized") if key in event} for event in jaw_events]
            facts.append(fact)
        if not set(observation_ids) <= linked_observations:
            identifier = next(item for item in observation_ids if item not in linked_observations)
            raise ToolInputError("operation_memory_unrelated_observation",
                field_path="/request/observation_ids/" + str(observation_ids.index(identifier)), actual=identifier,
                suggestion="This observation is not an input, execution-start or result observation of the selected actions. Remove it, or include the actual recorded action that links it; do not invent an association.")
        return dict(observations=observations, actions=actions), facts

    def call(self, request, correlation=None):
        if request == {"kind": "help"}:
            return dict(scopes=dict(task="current environment task only", shared="cross-task lessons; revalidate before reuse"),
                shared_available=self.shared_store is not None,
                query=dict(kind="query", scope="task", operation="optional action category", query="optional search words", limit=5),
                record=dict(kind="record", scope="task", operation="retreat",
                    summary="Withdraw vertically until the fingers clear the rim before moving sideways.",
                    conditions="Tall receiving rim; object visibly released.",
                    action_ids=["actual action ID"], observation_ids=["its result observation ID"]),
                limits=dict(max_actions=12, max_observations=12, id_characters=200, operation_characters=80,
                    query_characters=200, query_limit_min=1, query_limit_max=30, query_limit_default=5,
                    summary_characters=800, conditions_characters=400),
                query_rule="operation is an exact case-insensitive category; query words match operation/summary/conditions. Omit filters to read this scope. Supplied text must be nonblank; shared records require conditions.",
                evidence_rule="Select 1..12 recorded action IDs and include each result observation; use input only when no result exists. Every supplied observation must link to a selected action. Use history_list_actions for IDs; preview-only notes require action evidence before record.",
                semantics="History supplies mechanical facts. Lessons remain actor judgments; completed commands do not verify object success. Shared records require conditions and object-relative descriptions, not scene poses.")
        if not isinstance(request, dict):
            raise ToolInputError("invalid_operation_memory_request", field_path="/request", actual=request,
                suggestion="Provide a request object with kind='help', 'query' or 'record'. Use request={kind:'help'} to read the fields.")
        kind = request.get("kind")
        if kind == "query":
            _fields(request, {"kind", "scope"}, {"operation", "query", "limit"})
            owner = self._owner(request["scope"])
            operation = _text(request["operation"], "operation", 80).casefold() if "operation" in request else None
            words = _text(request["query"], "query", 200).casefold().split() if "query" in request else []
            limit = request.get("limit", 5)
            if type(limit) is not int or not 1 <= limit <= 30:
                raise ToolInputError("invalid_operation_memory_limit", field_path="/request/limit", actual=limit,
                    suggestion="Provide an integer limit from 1 through 30, or omit it for the default 5.")
            matches = []
            for index, record in enumerate(owner.records("operation_memory")):
                if record.get("scope") != request["scope"]:
                    continue
                if request["scope"] == "task" and record.get("identity") != self.store.identity:
                    continue
                if operation is not None and record["operation"].casefold() != operation:
                    continue
                content = " ".join(record[key] for key in ("operation", "summary", "conditions")).casefold()
                score = sum(word in content for word in words)
                if words and not score:
                    continue
                matches.append((score, index, record))
            matches.sort(key=lambda item: item[:2], reverse=True)
            return dict(scope=request["scope"], records=[self._public(item[2]) for item in matches[:limit]],
                        total_matches=len(matches), semantics="Lessons are attributed judgments, not verified object outcomes.")
        if kind != "record":
            raise ToolInputError("unknown_operation_memory_kind", field_path="/request/kind", actual=kind,
                suggestion="Choose kind='query' or 'record' with their documented fields, or use exactly request={kind:'help'} for help.")
        _fields(request, {"kind", "scope", "operation", "summary", "action_ids", "observation_ids"}, {"conditions"})
        owner = self._owner(request["scope"])
        operation = _text(request["operation"], "operation", 80)
        summary = _text(request["summary"], "summary", 800)
        conditions = _text(request["conditions"], "conditions", 400) if "conditions" in request else ""
        if request["scope"] == "shared" and not conditions:
            raise ToolInputError("operation_memory_shared_conditions_required", field_path="/request/conditions",
                actual=dict(present=False), suggestion="Shared records require nonblank conditions of at most 400 characters explaining when the lesson applies. Supply conditions or choose scope='task'.")
        evidence, facts = self._evidence(_ids(request["action_ids"], "action_ids"),
                                         _ids(request["observation_ids"], "observation_ids"))
        with owner.lock:
            number = len(owner.records("operation_memory")) + 1
            # Shared stores have writers in multiple benchmark processes; their
            # Python locks do not serialize a read-count/allocate sequence.
            suffix = uuid4().hex if request["scope"] == "shared" else str(number)
            identifier = request["scope"] + "_experience_" + suffix
            record = dict(memory_id=identifier, scope=request["scope"], operation=operation, summary=summary,
                conditions=conditions, facts=facts, identity=deepcopy(self.store.identity), evidence=evidence,
                created_at=utc_now(), correlation=deepcopy(correlation or {}))
            owner.save("operation_memory", identifier, record)
        if owner is not self.store:
            self.store.save("operation_memory_shared", identifier, record)
        self.store.event("operation.memory_recorded", dict(memory_id=identifier, scope=request["scope"]), correlation)
        return dict(scope=request["scope"], record=self._public(record))
