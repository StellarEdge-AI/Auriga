"""Completed-turn stall supervision: legacy declarations or public progress.

The bounded policy reads execution counts and actor-visible tool metadata. The
legacy policy retains its final-answer regex for archived runs and ablations.
Neither policy establishes task success or reads evaluator/object ground truth.
"""
from dataclasses import dataclass, field
import re

ACTOR_STALL_LIMIT = 5
ACTOR_STALL_REASON = "actor_repeated_inability"
ACTOR_STALL_POLICY = {
    "version": "consecutive_final_inability_v1",
    "limit": ACTOR_STALL_LIMIT,
    "answer_scope": "completed_current_thread_turn_final_answer",
    "reset_on": ["native_action_count_change", "episode_change", "thread_change", "non_inability_final"],
    "scoring": "task_failure_with_current_valid_native_process_score",
}

# Conservative declarations in Chinese and English, including the recorded
# bottle-run failure. A temporary problem followed by a proposed action is not
# an admission that no further action is possible.
_NEXT_ACTION = re.compile(
    r"\b(?:(?:I|we)\s+(?:will|can|shall|plan to|intend to)|(?:I|we)['’]ll)\s+"
    r"(?:try|attempt|continue|recover|retry|move|inspect|observe|return|command|execute|measure|examine|calculate|reposition)\b"
    r"|(?:接下来(?:会)?|下一步(?:会)?|我将|我们将|我会|我们会|我打算|我们打算|计划)(?:尝试|继续|重试|检查|观察|移动|执行|恢复|测量|返回)", re.I)
_INABILITY = [
    re.compile(pattern, re.I) for pattern in (
        r"\b(?:I|we)\s+(?:cannot|can['’]t|am unable to|are unable to)\s+(?:safely\s+)?(?:continue|complete|finish|proceed)\b",
        r"\b(?:the\s+)?task\s+(?:cannot|can['’]t)\s+be\s+(?:completed|finished)\b",
        r"\bcompletion\s+(?:cannot|can['’]t)\s+be\s+(?:achieved|attained)\b",
        r"\b(?:unable|impossible)\s+to\s+(?:continue|complete|finish|proceed)\b",
        r"\bno\s+(?:further\s+)?(?:feasible|viable|safe|possible)\s+(?:recovery\s+(?:actions?\s+)?|next\s+actions?\s+|actions?\s+)(?:is\s+available|are\s+available|remains?|is\s+possible|(?:with|through)\s+the\s+(?:permitted|available)\s+tools)\b",
        r"\bno\s+(?:feasible|viable|possible|permitted)\s+(?:recovery\s+)?action\b[^.\n]{0,120}\b(?:can\s+(?:complete|recover|resolve)|is\s+feasible)\b",
        r"\b(?:permitted|available)\s+tools\s+(?:cannot|can['’]t)\s+(?:recover|complete|resolve)\b",
        r"\b(?:further\s+progress|recovery|completion)\s+requires?\s+external\s+(?:repositioning|intervention)\b[^.\n]{0,120}\b(?:unavailable|not available)\b",
        r"(?:无法|不能|没法|不可能)(?:再|安全地|安全|继续)?(?:继续(?:完成|执行|操作|行动)?|完成(?:任务)?|执行任何动作|进行下一步)",
        r"(?:没有|不存在|无)(?:任何|进一步的?|更多的?|剩余的?|可行的?|安全的?)*(?:恢复动作|恢复方案|下一步动作|可执行动作|可行动作)",
    )
]
_QUOTED = re.compile(r'```[\s\S]*?```|`[^`\n]*`|"[^"\n]*"|“[^”\n]*”|(?<!\w)\x27[^\x27\n]*\x27(?!\w)|(?m:^\s*>[^\n]*)')


def inability_reason(text):
    if not isinstance(text, str) or not text.strip():
        return None
    declaration = _QUOTED.sub("", text)
    if _NEXT_ACTION.search(declaration):
        return None
    for pattern in _INABILITY:
        match = pattern.search(declaration)
        if match:
            return match.group(0)
    return None


@dataclass
class ActorStallTracker:
    count: int = 0
    context: tuple | None = None
    native_action_count: int | None = None
    last_turn_id: str | None = None
    seen_turns: set = field(default_factory=set)

    def observe(self, *, thread_id, turn_id, episode_id, native_action_count, text):
        if not thread_id or not turn_id or not episode_id:
            return None
        context = (thread_id, episode_id)
        if context != self.context:
            self.context, self.count, self.last_turn_id = context, 0, None
            self.seen_turns.clear()
            self.native_action_count = native_action_count
        if native_action_count != self.native_action_count:
            self.count = 0
            self.native_action_count = native_action_count
        if turn_id in self.seen_turns:
            return None
        self.seen_turns.add(turn_id)
        self.last_turn_id = turn_id
        matched = inability_reason(text)
        self.count = self.count + 1 if matched else 0
        return dict(count=self.count, limit=ACTOR_STALL_LIMIT, triggered=self.count >= ACTOR_STALL_LIMIT,
                    matched_reason=matched, answer_excerpt=text[:2048] if isinstance(text, str) else None,
                    thread_id=thread_id, turn_id=turn_id, episode_id=episode_id,
                    native_action_count=native_action_count)


# This policy does not interpret prose or infer physical task completion. A
# completed turn establishes a baseline; subsequent completed turns without
# native execution get three empty recovery opportunities, or at most six when
# distinct public-tool investigation continues. Running turns are never inputs.
ACTOR_PROGRESS_REASON = "actor_no_progress"
ACTOR_PROGRESS_IDLE_LIMIT = 3
ACTOR_PROGRESS_RECOVERY_LIMIT = 6
ACTOR_EVIDENCE_IDLE_LIMIT = 2
ACTOR_PROGRESS_POLICY = {
    "version": "bounded_completed_final_progress_v1",
    "idle_final_limit": ACTOR_PROGRESS_IDLE_LIMIT,
    "recovery_final_limit": ACTOR_PROGRESS_RECOVERY_LIMIT,
    "count_scope": "completed_finals_after_baseline_at_unchanged_native_action_count",
    "baseline": "first_completed_final_or_native_execution_progress",
    "reset_on": ["native_action_count_increase", "episode_change", "thread_change"],
    "novel_activity": "distinct_public_tool_request_fingerprints; bounded_credit_not_task_progress",
    "excluded_activity": "final_prose, ordinary_observe, plan_rewording, repeated_tool_requests",
    "scoring": "task_failure_with_current_valid_native_process_score",
    "priority": "native_terminal_and_unresolved_execution_checks_precede_tracker",
}
ACTOR_EVIDENCE_POLICY = {
    **ACTOR_PROGRESS_POLICY,
    "version": "bounded_completed_final_evidence_v2",
    "idle_final_limit": ACTOR_EVIDENCE_IDLE_LIMIT,
    "novel_activity": "distinct public investigation, changed preview outcomes or actually delivered RGB",
    "excluded_activity": "v1 exclusions plus candidates exceeding the unchanged known episode budget",
    "budget_rejection_scope": "one nominal candidate only; no claim that the task is globally infeasible",
    "unknown_evidence": "retain v1 request credit and idle limit",
}

# A request is evidence of an attempted investigation, not its success. The hard
# recovery limit bounds even endlessly varied arguments or invalid attempts.
_RECOVERY_TOOLS = frozenset({
    "robot_action", "operation_memory",
    "geometry_query", "robot_preview_cost", "robot_inspect_trajectory", "robot_contact_overlay",
    "history_view_observation", "robot_capabilities", "scene_capabilities",
    "program_run", "manipulation_run", "robot_move_ee", "robot_move_grasp",
    "robot_move_dual_grasp", "robot_execute_ee", "robot_execute_queue",
    "robot_gripper", "grasp_plan", "grasp_memory", "robot_preview_motion", "robot_execute_plan",
    "vision_review", "operation_verify", "vision_compare",
})
_NON_SEMANTIC_KEYS = frozenset({
    "label", "action_id", "request_id", "call_id", "callId", "timeout_s",
    "max_calls", "observation_id", "observation_ids", "based_on_observation_id",
    "expected_cursor", "expected_revision",
})


def recovery_request(store, tool, arguments):
    """Restore actor aliases and read only the explicitly referenced public plan."""
    if not isinstance(arguments, dict):
        return arguments, {}
    if hasattr(store, "read_state"):
        from ...core.presentation.actor_output import ActorOutput
        from ...core.presentation.actor_presentation import ActorPresentation
        try:
            arguments = ActorOutput(store, ActorPresentation(store)).restore(arguments)
        except ValueError:
            pass  # Invalid/unavailable references retain ordinary bounded request credit.
    plans = {}
    identifier = arguments.get("plan_id") if tool == "robot_action" else None
    if isinstance(identifier, str) and hasattr(store, "read"):
        try:
            plans[identifier] = store.read("unified_action_plan", identifier)
        except KeyError:
            pass
    return arguments, plans


def recovery_fingerprints(tool, arguments, *, observations=None, episode_id=None, plans=None):
    """Canonical public requests; UUIDs/labels alone never buy recovery time.

    Observation references retain their episode/execution-step identity, so a
    genuinely different historical viewpoint is distinct while another capture
    ID at the same paused state is not. Unknown references share one marker.
    Preview candidates are individual unordered hypotheses, not labelled batches.
    """
    import ast
    import hashlib
    import json

    if tool not in _RECOVERY_TOOLS or not isinstance(arguments, dict):
        return set()
    observations = observations or {}
    plans = plans or {}
    if (tool == "operation_memory" and isinstance(arguments.get("request"), dict)
            and arguments["request"].get("kind") == "record"):
        return set()  # Writing another lesson is not new investigation.

    def observation_key(identifier):
        if not isinstance(identifier, str):
            return "unverified_observation"
        value = observations.get(identifier)
        if not value or value.get("episode_id") != episode_id:
            return "unverified_observation"
        return [value.get("episode_id"), value.get("sim_step")]

    def normalize(value):
        if isinstance(value, dict):
            result = {}
            for key, child in value.items():
                if key == "observation_id" or key.endswith("_observation_id"):
                    result[key] = observation_key(child)
                elif key == "observation_ids" and isinstance(child, list):
                    result[key] = [observation_key(identifier) for identifier in child]
                elif key not in _NON_SEMANTIC_KEYS:
                    if key == "source" and tool == "program_run" and isinstance(child, str):
                        # UUID churn inside a Python client call is no more
                        # novel than UUID churn in its structured arguments.
                        child = re.sub(r"\b(?:obs-[A-Za-z0-9_-]+|obs_[0-9]+)\b", lambda match:
                            "observation_state_" + hashlib.sha256(json.dumps(
                                observation_key(match.group(0)), sort_keys=True).encode()).hexdigest(), child)
                        try:
                            child = ast.dump(ast.parse(child), include_attributes=False)
                        except (SyntaxError, ValueError):
                            child = "invalid_python_source"
                    result[key] = normalize(child)
            return result
        if isinstance(value, list):
            return [normalize(child) for child in value]
        return value

    def fingerprint(value):
        payload = json.dumps([tool, value], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return tool + ":" + hashlib.sha256(payload.encode()).hexdigest()

    def stage_graph(stages):
        if not isinstance(stages, list):
            return normalize(stages)
        names = {stage.get("id"): str(index) for index, stage in enumerate(stages)
                 if isinstance(stage, dict) and isinstance(stage.get("id"), str)}
        result = []
        for stage in stages:
            if not isinstance(stage, dict):
                result.append(normalize(stage))
                continue
            dependencies = stage.get("depends_on", [])
            dependencies = ([names.get(dep, "unverified_stage") if isinstance(dep, str) else "unverified_stage"
                             for dep in dependencies] if isinstance(dependencies, list) else ["invalid_dependencies"])
            result.append(normalize({**{key: value for key, value in stage.items()
                if key not in ("id", "reason", "depends_on")}, "depends_on": sorted(dependencies)}))
        return result

    if tool == "robot_action":
        arguments = dict(arguments)
        mode = arguments.setdefault("mode", "preview")
        if "stages" in arguments:
            arguments["stages"] = stage_graph(arguments["stages"])
        if "plan_id" in arguments:
            record = plans.get(arguments["plan_id"], {}) if isinstance(arguments["plan_id"], str) else {}
            action = record.get("action", {})
            arguments["plan_id"] = (dict(stages=stage_graph(action.get("stages", [])),
                observation_id=action.get("based_on_observation_id"),
                settle=record.get("settle"), review_proxies=action.get("review_proxies"))
                if record.get("identity", {}).get("episode_id") == episode_id else "unverified_plan")
        normalized = normalize(arguments)
        steps = normalized.get("native_steps")
        if mode == "inspect" and isinstance(steps, list) and all(type(step) is int for step in steps):
            return {fingerprint({**normalized, "native_steps": [step]}) for step in steps}
        return {fingerprint(normalized)}

    if tool == "robot_preview_cost":
        candidates = arguments.get("candidates", [])
        if not isinstance(candidates, list):
            return set()
        # The current physical state already defines the progress epoch. Batch
        # order, labels and fresh observation UUIDs do not change a candidate.
        view = {key: arguments[key] for key in ("camera_id", "include_trajectory") if key in arguments}
        return {fingerprint({"candidate": normalize(candidate), "view": view})
                for candidate in candidates if isinstance(candidate, dict)}
    if tool == "robot_inspect_trajectory":
        normalized = normalize(arguments)
        steps = normalized.get("native_steps")
        if isinstance(steps, list) and all(type(step) is int for step in steps):
            # Regrouping/reordering the same samples is not new inspection;
            # another synchronized step or camera overlay is bounded novelty.
            return {fingerprint({**normalized, "native_steps": [step]}) for step in steps}
    return {fingerprint(normalize(arguments))}


def completed_turn_activity(store, *, thread_id, turn_id, episode_id):
    """Read only actor-visible request/observation metadata, never evaluator GT."""
    observations = {value["observation_id"]: value for value in store.records("observation")
                    if value.get("episode_id") == episode_id and value.get("observation_id")}
    fingerprints = set()
    for record in store.records("tool_call"):
        correlation = record.get("correlation", {})
        if correlation.get("thread_id") != thread_id or correlation.get("turn_id") != turn_id:
            continue
        params = record.get("params", {})
        arguments, plans = recovery_request(store, params.get("tool"), params.get("arguments"))
        fingerprints.update(recovery_fingerprints(params.get("tool"), arguments,
            observations=observations, episode_id=episode_id, plans=plans))
    return fingerprints


@dataclass
class ActorProgressTracker:
    """Bound completed-final recovery by public execution and tool activity.

    Native counts must be known nonnegative integers. Unknown counts cannot
    establish a plateau and never trigger this policy. Execution count increases
    reset the baseline; regressions are unknown until the prior count is restored
    (a real episode reset must change episode_id). Neither text nor scores enter.
    """
    context: tuple | None = None
    native_action_count: int | None = None
    no_native_finals: int = 0
    idle_finals: int = 0
    seen_turns: set = field(default_factory=set)
    seen_activity: set = field(default_factory=set)
    evidence_aware: bool = False
    tested_candidates: dict = field(default_factory=dict)
    budget_rejected_turns: int = 0
    last_evidence: dict | None = None

    def observe(self, *, thread_id, turn_id, episode_id, native_action_count, activity=(), evidence=None):
        if not thread_id or not turn_id or not episode_id:
            return None
        context = (thread_id, episode_id)
        context_changed = context != self.context
        if context_changed:
            self.context = context
            self.native_action_count = None
            self.no_native_finals = self.idle_finals = 0
            self.seen_turns.clear()
            self.seen_activity.clear()
            self.tested_candidates.clear()
            self.budget_rejected_turns = 0
            self.last_evidence = None
        if turn_id in self.seen_turns:
            return None
        self.seen_turns.add(turn_id)
        activity = set(activity)
        evidence_valid = (self.evidence_aware and isinstance(evidence, dict)
                          and evidence.get("version") == "recovery-evidence-v1"
                          and evidence.get("status") == "available"
                          and evidence.get("native_action_count") == native_action_count)
        policy = ACTOR_EVIDENCE_POLICY if evidence_valid else ACTOR_PROGRESS_POLICY
        idle_limit = policy["idle_final_limit"]
        if evidence_valid:
            activity = set(evidence.get("activity", ()))
            for candidate in evidence.get("candidates", []):
                key = (candidate.get("candidate_id"), candidate.get("measured_state_sha256"),
                       candidate.get("native_steps_remaining"))
                previous = self.tested_candidates.get(key)
                # Explicitly obtaining missing route geometry for an already
                # tested candidate is new evidence. An endless stream of new
                # unaffordable endpoints with default plots is still not credit.
                if (previous and previous.get("trajectory_available") is False
                        and candidate.get("trajectory_available") is True
                        and candidate.get("exceeds_episode_budget") is True):
                    activity.add("preview_trajectory:" + repr(key))
        known = type(native_action_count) is int and native_action_count >= 0
        if not known or (self.native_action_count is not None and native_action_count < self.native_action_count):
            self.last_evidence = None
            return dict(version=policy["version"], triggered=False,
                status="unknown", reason="native_action_count_unknown_or_regressed",
                thread_id=thread_id, turn_id=turn_id, episode_id=episode_id,
                native_action_count=native_action_count)
        baseline = self.native_action_count is None or native_action_count > self.native_action_count
        if baseline:
            self.native_action_count = native_action_count
            self.no_native_finals = self.idle_finals = 0
            self.seen_activity = activity
            self.tested_candidates.clear()
            self.budget_rejected_turns = 0
            novel = set()
        else:
            novel = activity - self.seen_activity
            self.seen_activity.update(activity)
            self.no_native_finals += 1
            self.idle_finals = 0 if novel else self.idle_finals + 1
        if evidence_valid:
            self.last_evidence = evidence
            self.budget_rejected_turns = (self.budget_rejected_turns + 1
                if not baseline and not novel and evidence.get("budget_rejected_count", 0) else 0)
            for candidate in evidence.get("candidates", []):
                key = (candidate.get("candidate_id"), candidate.get("measured_state_sha256"),
                       candidate.get("native_steps_remaining"))
                self.tested_candidates.pop(key, None)
                self.tested_candidates[key] = candidate
        else:
            self.last_evidence = None
            self.budget_rejected_turns = 0
        triggered = (self.idle_finals >= idle_limit
                     or self.no_native_finals >= ACTOR_PROGRESS_RECOVERY_LIMIT)
        idle_reason = ("budget_rejection_final_limit" if self.budget_rejected_turns >= idle_limit
                       else "no_new_evidence_final_limit" if evidence_valid else "idle_final_limit")
        return dict(version=policy["version"], status="observed",
            triggered=triggered, baseline=baseline,
            reason=("recovery_final_limit" if self.no_native_finals >= ACTOR_PROGRESS_RECOVERY_LIMIT
                    else idle_reason if triggered else None),
            count=self.no_native_finals, idle_count=self.idle_finals,
            idle_limit=idle_limit, recovery_limit=ACTOR_PROGRESS_RECOVERY_LIMIT,
            new_activity_count=len(novel), new_activity=sorted(novel),
            thread_id=thread_id, turn_id=turn_id, episode_id=episode_id,
            native_action_count=native_action_count,
            **({"recovery_evidence": self.recovery_context()} if evidence_valid else {}))

    def recovery_context(self):
        """Compact current plateau ledger; the full turn evidence stays in events."""
        if not self.evidence_aware or self.last_evidence is None:
            return None
        candidates = list(self.tested_candidates.values())
        fields = ("label", "nominal_native_steps", "status", "reason", "exceeds_episode_budget",
                  "native_steps_remaining", "observation_id")
        return dict(version="recovery-evidence-v1", native_action_count=self.native_action_count,
            native_steps_remaining=self.last_evidence.get("native_steps_remaining"),
            no_native_finals=self.no_native_finals, no_new_evidence_finals=self.idle_finals,
            no_new_evidence_limit=ACTOR_EVIDENCE_IDLE_LIMIT,
            tested_candidate_count=len(candidates),
            tested_candidates=[{key: candidate.get(key) for key in fields} for candidate in candidates[-6:]],
            earlier_candidate_count=max(0, len(candidates) - 6),
            scope="nominal candidates at recorded measured state and budget; not global task infeasibility")
