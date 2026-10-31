"""Actor-authored, revisable task plans; no hidden task solver or tool schedule."""

from copy import deepcopy

from jsonschema import Draft202012Validator

from ...core.contracts import IDENTITY, canonical, new_id
from .task_notes import (GRASP_APPROACH_GUIDANCE, ROUTE_PLANNING_GUIDANCE,
                         TASK_SCHEDULING_GUIDANCE, OPERATION_CANDIDATE_GUIDANCE)
from .operation_candidates import OPERATION_CANDIDATE_SCHEMA, summarize_candidate, validate_candidate


LEGACY_VERSION = "actor_task_plan_v4"
PREVIOUS_VERSION = "actor_task_plan_v7"
VERSION = "actor_task_plan_v9"
LEGACY_INSTRUCTIONS = """Before your first robot action, inspect the permitted initial observations and
submit a concise task-level plan with task_plan(operation='submit'). This is required
before movement, gripper commands, policy delegation, or robot_wait. State the
objective, stages and observable evidence for advancing them, known constraints,
uncertainties, and completion criteria. Use only the public task instructions and
permitted evidence; do not invent evaluator rules or claim unobserved facts.
For a demonstration or other unknown information, first plan how to observe it;
fill in the sequence or targets only after observing them. In each stage, consider
which available tools may help and optionally list suggested_tools. Tool suggestions
are advisory: choose other available tools, skip unnecessary calls, change stage
order, or revise the plan as feedback warrants. Existing action/tool checks still
apply. A plan is not a fixed script or proof of task success. After acting, inspect
feedback and adapt; use task_plan to record material revisions when useful, not
after every action. task_plan(operation='read') retrieves the current episode's plan.
Submit the plan against the current observation_id. No separate approval is needed.

Optional stage id, arms, depends_on and observation_gate describe dependencies.
Use unique ids when referencing stages; depends_on lists prerequisites, and an
empty or omitted list adds no dependency. Array order is presentation order,
not an implicit execution barrier. arms identifies involved arms, not ownership
or a single-arm preference. observation_gate states the fresh evidence needed
before dependent work. These are advisory planning claims, not executable gates:
revise them as feedback changes the scene. Distinguish independent work suitable
for overlap from coupled movement and observation-dependent support. Follow the
run handoff switch: when airborne handoff is disabled, transfer the same object
via a stable visible support, then release/inspect, clear the giver, and only
then regrasp with the other arm. Continue simultaneous work on different objects
when clearance permits; do not turn this into serial arms or a single-arm rule.

Plan manipulation before committing to motion, not only after a failure. Record
the important decisions concisely in the relevant stage and constraints; refine
them from fresh observations before that stage when earlier actions change the scene:
- Nearby interference: identify which visible neighboring objects, supports,
  container walls/rims/flaps or the other arm could intersect the approaching arm,
  closing fingers, held object's swept volume or withdrawal route. A reachable
  endpoint alone does not establish a clear route. Consider whether changing the
  order, approach side, arm or a justified temporary placement creates room;
  account for the consequences of moving an obstruction. Treat visually estimated
  clearances as uncertain, not known object geometry.
- Grasp choice: for unfamiliar or irregular objects, make a distinct grasp
  assessment before choosing a pose. Compare plausible local regions, jaw direction
  and opening, finger engagement, likely slip/rotation and balance, approach access,
  and whether that grasp permits the intended placement and release. Do not default
  to the image center, the longest dimension or a visually convenient protrusion;
  unknown center of mass and hidden surfaces remain uncertain. Use grasp_plan or
  relevant grasp_memory when available and useful; the tools remain optional.
- Placement: choose the object's intended resting location AND orientation, not
  just an EE endpoint or 'over the container'. For a tight target or container,
  consider the held object's offset from the fingers, the opening and interior,
  nearby contents, support, release space, and a withdrawal route that does not
  drag the object, tip the container or disturb neighbors. If the current grasp
  cannot reach that final relation, consider a supported regrasp or another approach.
  Keep a concise per-object distinction between held, released-unverified and
  visibly retained. A release hidden by a rim remains unverified. Arrange useful
  retention observations before final home, while recovery budget remains.
- Route: design suitable intermediate positions AND orientations for approach,
  lift, clearance, insertion/lowering, release and retreat as the scene requires.
  For difficult placements or crowded scenes, prefer a reasoned multi-segment route
  over a single endpoint guess when it improves access. Use robot_move_grasp with
  via_grasp_poses or robot_execute_ee with action.path.waypoints for a known route;
  use separate actions with fresh feedback where the next segment depends on the
  outcome. Merely splitting the same obstructed line does not avoid an obstacle.
  Before an expensive posture change or repeated difficult approach, compare
  available cost previews using the intended speeds; small EE displacement can
  still require a long joint path. Optional previews do not prove clearance.
Choose only the detail needed for the task. Preserve native action budget for
correction, final orientation, release and required arm return. Revise material
assumptions when objects move or a grasp slips, rather than executing an obsolete
plan. These are model planning considerations, not fixed object-specific rules,
a mandatory maneuver sequence or additional execution gates."""

PROTOCOL_INSTRUCTIONS = """

For a near-term manipulation stage, record a compact operation_context with the
actor's object_label/state, current observation evidence, sources, basis and
uncertainty. Future stages may remain unspecified until observed; do not invent
geometry to complete a form. Sources are limited to RGB, proprioception,
calibration and public URDF; object shape/pose estimates come from actor RGB
interpretation, never simulator object coordinates, hidden score or contact truth.
Choose contact.region (pad_center, fingertip or finger_middle) and explain why;
object section and finger reference are separate choices. Bounded manipulation
templates require this context and apply the selected reference to their poses.
Ordinary no-contact transit and peer preparation need no contact declaration.
For placement, state the object's resting pose, bottom relative to the fingers,
rim/boundary clearance, release gap/drop distance and withdrawal route. Short
qualitative estimates or 'unknown' are valid; record the shared basis/uncertainty.
Held, released_unverified and retained are actor judgments. A completed release
template returns released_unverified, never retained. To record retained, cite a
release observation and RGB evidence of separation plus stable landing/retention;
an occluded landing remains unverified. Inspect before required final home while
recovery is affordable. If settling requires time, use only a bounded robot_wait
with explicit native-step cost; static rereads and wall-clock sleep do not settle.
For a supported transfer, choose the landing footprint, giver exit and receiver
entry together, and record independent parallel work. Deposit, inspect support,
clear giver, inspect clearance, then receive are separate observation-dependent
decisions. A receive context cites release and later giver-clear observations.
Giving clearance does not require returning home: the receiver may work once clear,
and giver home/preparation can be later or parallel where routes permit. Preserve
simultaneous dual-arm/independent/queue work and the separate run handoff policy.
Context validation checks fields, provenance and observation boundaries; it does
not recognize objects, certify support/collision clearance or prove task success."""


def instructions_for(manipulation_protocol=True, *, task_scheduling=True, operation_candidates=False):
    text = LEGACY_INSTRUCTIONS + ("\n\n" + GRASP_APPROACH_GUIDANCE + "\n\n" + ROUTE_PLANNING_GUIDANCE
                                 + PROTOCOL_INSTRUCTIONS if manipulation_protocol else "")
    if manipulation_protocol and task_scheduling:
        text += "\n\n" + TASK_SCHEDULING_GUIDANCE
    if manipulation_protocol and operation_candidates:
        text += "\n\n" + OPERATION_CANDIDATE_GUIDANCE
    return text


def version_for(manipulation_protocol=True, *, task_scheduling=True, operation_candidates=False):
    if not manipulation_protocol:
        return LEGACY_VERSION
    return VERSION if task_scheduling or operation_candidates else PREVIOUS_VERSION


INSTRUCTIONS = instructions_for()

_TEXT = dict(type="string", minLength=1, maxLength=1600, pattern=r"\S")
_TEXTS = dict(type="array", maxItems=16, items=_TEXT)
_STAGE_ID = dict(type="string", minLength=1, maxLength=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
LEGACY_PLAN_SCHEMA = dict(type="object", additionalProperties=False,
    properties=dict(
        objective=_TEXT,
        stages=dict(type="array", minItems=1, maxItems=12, items=dict(
            type="object", additionalProperties=False,
            properties=dict(goal=_TEXT, advance_when=_TEXT,
                            id=_STAGE_ID,
                            arms=dict(type="array", minItems=1, maxItems=2, uniqueItems=True,
                                      items=dict(type="string", enum=["left", "right"])),
                            depends_on=dict(type="array", maxItems=12, uniqueItems=True, items=_STAGE_ID),
                            observation_gate=_TEXT,
                            suggested_tools=dict(type="array", maxItems=16,
                                                 items=dict(type="string", minLength=1, maxLength=100, pattern=r"\S"))),
            required=["goal", "advance_when"])),
        constraints=_TEXTS, uncertainties=_TEXTS,
        completion_criteria={**_TEXTS, "minItems": 1}),
    required=["objective", "stages", "constraints", "uncertainties", "completion_criteria"])

# One shared basis/uncertainty keeps scene-specific context compact. Geometry is
# explicitly an actor estimate; requiring fabricated metric values would hide
# uncertainty rather than improve placement.
_SHORT = dict(type="string", minLength=1, maxLength=600, pattern=r"\S")
_OBSERVATION_ID = dict(type="string", minLength=1, maxLength=160, pattern=r"\S")
_ARM = dict(type="string", enum=["left", "right"])
BASE_OPERATION_CONTEXT_SCHEMA = dict(type="object", additionalProperties=False,
    properties=dict(
        object_label=_SHORT,
        state=dict(type="string", enum=["unknown", "held", "released_unverified", "retained"]),
        holding_arm=_ARM,
        evidence=dict(type="object", additionalProperties=False,
            properties=dict(observation_id=_OBSERVATION_ID,
                sources=dict(type="array", minItems=1, maxItems=4, uniqueItems=True,
                    items=dict(type="string", enum=["rgb", "proprioception", "calibration", "urdf"])),
                basis=_SHORT, uncertainty=_SHORT),
            required=["observation_id", "sources", "basis", "uncertainty"]),
        contact=dict(type="object", additionalProperties=False,
            properties=dict(region=dict(type="string", enum=["pad_center", "fingertip", "finger_middle"]),
                            reason=_SHORT), required=["region", "reason"]),
        placement=dict(type="object", additionalProperties=False,
            properties={name: _SHORT for name in ("resting_pose", "object_bottom", "rim_or_boundary", "release_gap", "withdrawal_route")},
            required=["resting_pose", "object_bottom", "rim_or_boundary", "release_gap", "withdrawal_route"]),
        release_observation_id=_OBSERVATION_ID,
        supported_transfer=dict(type="object", additionalProperties=False,
            properties=dict(phase=dict(type="string", enum=["deposit", "receive"]),
                support=_SHORT, giver_arm=_ARM, receiver_arm=_ARM,
                giver_exit=_SHORT, receiver_entry=_SHORT, parallel_work=_SHORT,
                clearance_observation_id=_OBSERVATION_ID),
            required=["phase", "support", "giver_arm", "receiver_arm", "giver_exit", "receiver_entry", "parallel_work"])),
    required=["object_label", "state", "evidence"])


def operation_context_schema_for(operation_candidates=False):
    schema = deepcopy(BASE_OPERATION_CONTEXT_SCHEMA)
    if operation_candidates:
        schema["properties"]["candidate"] = deepcopy(OPERATION_CANDIDATE_SCHEMA)
    return schema


OPERATION_CONTEXT_SCHEMA = operation_context_schema_for()


def plan_schema_for(manipulation_protocol=True, *, task_scheduling=True, operation_candidates=False):
    schema = deepcopy(LEGACY_PLAN_SCHEMA)
    if manipulation_protocol:
        stage = schema["properties"]["stages"]["items"]["properties"]
        stage["operation_context"] = operation_context_schema_for(operation_candidates)
        if task_scheduling:
            stage["dependency_reason"] = _SHORT
            schema["properties"]["scheduling"] = dict(type="object", additionalProperties=False,
                properties={field: _SHORT for field in
                            ("choice_reason", "independent_work", "held_or_busy_arms", "reconsider_when")},
                required=["choice_reason", "independent_work", "held_or_busy_arms", "reconsider_when"])
    return schema


PLAN_SCHEMA = plan_schema_for()


def validate_operation_context(context, *, operation_candidates=False):
    """Validate declarations only; no scene lookup or physical truth judgment."""
    if not Draft202012Validator(operation_context_schema_for(operation_candidates)).is_valid(context):
        raise ValueError("invalid_operation_context")
    if context["state"] == "held" and "holding_arm" not in context:
        raise ValueError("operation_context_held_requires_arm")
    if context["state"] != "held" and "holding_arm" in context:
        raise ValueError("operation_context_holding_arm_only_when_held")
    if context["state"] in ("released_unverified", "retained") and "release_observation_id" not in context:
        raise ValueError("operation_context_release_evidence_required")
    if context["state"] == "retained" and "rgb" not in context["evidence"]["sources"]:
        raise ValueError("operation_context_retained_requires_rgb_basis")
    transfer = context.get("supported_transfer")
    if transfer:
        if transfer["giver_arm"] == transfer["receiver_arm"]:
            raise ValueError("operation_context_transfer_requires_distinct_arms")
        if transfer["phase"] == "receive" and (context["state"] != "retained"
                or "clearance_observation_id" not in transfer):
            raise ValueError("operation_context_receive_requires_support_and_clearance_evidence")
    if "candidate" in context:
        validate_candidate(context["candidate"], context["evidence"]["observation_id"])
    return context


def validate_operation_evidence(context, store, identity, current_observation_id, *, require_current=True,
                                operation_candidates=False):
    """Check only actor-observation identity/order, never hidden object state.

    A valid citation is still an actor claim, not evidence that this module has
    recognized support, contact, retention or a clear swept volume.
    """
    validate_operation_context(context, operation_candidates=operation_candidates)
    ids = {context["evidence"]["observation_id"], current_observation_id}
    if "release_observation_id" in context:
        ids.add(context["release_observation_id"])
    transfer = context.get("supported_transfer", {})
    if "clearance_observation_id" in transfer:
        ids.add(transfer["clearance_observation_id"])
    records = {}
    for oid in ids:
        try:
            record = store.read("observation", oid)
        except KeyError as error:
            raise ValueError("operation_context_observation_not_found") from error
        if any(record.get(key) != identity[key] for key in IDENTITY):
            raise ValueError("operation_context_observation_wrong_episode")
        # Only the public observation bundle is read, not robot/object blobs,
        # artifacts, task evaluator state or contact truth.
        records[oid] = record
    evidence_id = context["evidence"]["observation_id"]
    if require_current and evidence_id != current_observation_id:
        raise ValueError("operation_context_requires_current_evidence")
    current_step = records[current_observation_id].get("sim_step")
    for record in records.values():
        step = record.get("sim_step")
        if current_step is not None and step is not None and step > current_step:
            raise ValueError("operation_context_future_observation")
    evidence_step = records[evidence_id].get("sim_step")
    if "release_observation_id" in context:
        release_step = records[context["release_observation_id"]].get("sim_step")
        if release_step is None or evidence_step is None or release_step > evidence_step:
            raise ValueError("operation_context_release_after_claim_evidence")
    if transfer.get("phase") == "receive":
        release = records[context["release_observation_id"]]
        clear = records[transfer["clearance_observation_id"]]
        release_step, clear_step = release.get("sim_step"), clear.get("sim_step")
        if (release["observation_id"] == clear["observation_id"]
                or release_step is None or clear_step is None or clear_step <= release_step):
            raise ValueError("operation_context_clearance_requires_later_observation")
        if evidence_step is None or clear_step > evidence_step:
            raise ValueError("operation_context_clearance_after_claim_evidence")
    return dict(source_kind="actor_judgment", observation_references_valid=True,
                physical_claims_verified=False)


def validate_stage_dependencies(plan):
    """Validate the actor's explicit graph without adding execution policy."""
    stages = plan["stages"]
    named = {stage["id"]: stage for stage in stages if "id" in stage}
    if len(named) != sum("id" in stage for stage in stages):
        raise ValueError("invalid_task_plan: duplicate_stage_id")
    for stage in stages:
        if any(dependency not in named for dependency in stage.get("depends_on", [])):
            raise ValueError("invalid_task_plan: unknown_stage_dependency")
    visiting, visited = set(), set()

    def visit(identifier):
        if identifier in visiting:
            raise ValueError("invalid_task_plan: stage_dependency_cycle")
        if identifier in visited:
            return
        visiting.add(identifier)
        for dependency in named[identifier].get("depends_on", []):
            visit(dependency)
        visiting.remove(identifier)
        visited.add(identifier)

    for identifier in named:
        visit(identifier)


def scheduling_failure_advisory(receipt, plan_record=None, *, task_scheduling=True):
    """A brief post-failure decision reminder using public records only.

    No stage is declared ready, no arm is inferred to hold an object, and no
    dependency is removed. The caller chooses the failed-action delivery boundary;
    this pure helper never reads state or consumes a native action step.
    """
    if type(task_scheduling) is not bool:
        raise ValueError("invalid_task_scheduling")
    if (not task_scheduling or not isinstance(receipt, dict)
            or receipt.get("status") not in ("failed", "rejected")):
        return None
    result = dict(kind="scheduling_review", source_kind="advisory",
        suggestion="Before retrying, compare a changed recovery with independent work. Recheck current RGB; if one holds an object, consider delivery while the peer recovers or observed stable support; do not release without support. Preserve dependencies, clear shared corridors, and check withdrawal/next approach against placed objects. Overlap useful phases only where clear; idle arms need not return home.")
    if isinstance(plan_record, dict):
        plan = plan_record.get("plan", {})
        stages = plan.get("stages", []) if isinstance(plan, dict) else []
        if isinstance(stages, list):
            references = [stage.get("id", str(index)) for index, stage in enumerate(stages)
                          if isinstance(stage, dict) and not stage.get("depends_on")][:3]
            if references:
                # Pointers only; they may already be complete or physically
                # blocked. The actor must reassess against current observations.
                result["dependency_free_plan_stages_to_reassess"] = references
        if plan_record.get("observation_id"):
            result["plan_observation_id"] = plan_record["observation_id"]
    return result


class TaskPlanning:
    def __init__(self, store, required=False, *, manipulation_protocol=True,
                 task_scheduling=True, operation_candidates=False):
        if type(required) is not bool:
            raise ValueError("invalid_task_plan_required")
        if type(manipulation_protocol) is not bool:
            raise ValueError("invalid_manipulation_protocol")
        if type(task_scheduling) is not bool or type(operation_candidates) is not bool:
            raise ValueError("invalid_operation_planning_flags")
        self.store, self.required = store, required
        self.manipulation_protocol = manipulation_protocol
        self.task_scheduling = task_scheduling
        self.operation_candidates = operation_candidates

    def current(self, identity):
        record = self.store.read_state("runtime", "task_plan")
        if record and record.get("identity") == {key: identity[key] for key in IDENTITY}:
            try:
                saved = self.store.read("task_plan", record["plan_id"])
            except KeyError:
                return None
            if saved == record:
                return record
        return None

    def require(self, identity):
        if self.required and self.current(identity) is None:
            raise ValueError("task_plan_required: inspect observations and submit task_plan before robot actions; suggested tools are optional; retry a rejected action with a new action_id")

    def call(self, args, identity, observation, correlation):
        identity = {key: identity[key] for key in IDENTITY}
        operation = args.get("operation")
        if operation == "read":
            if set(args) != {"operation"}:
                raise ValueError("task_plan_read_accepts_only_operation")
            return dict(required=self.required, plan=self.current(identity), tool_choices="advisory")
        if operation != "submit" or set(args) != {"operation", "observation_id", "plan"}:
            raise ValueError("task_plan_submit_requires_observation_id_and_plan")
        if (observation is None or args["observation_id"] != observation["observation_id"]
                or any(observation.get(key) != value for key, value in identity.items())):
            raise ValueError("stale_observation")
        plan = args["plan"]
        features = dict(task_scheduling=self.task_scheduling, operation_candidates=self.operation_candidates)
        if len(canonical(plan)) > 16000 or not Draft202012Validator(plan_schema_for(self.manipulation_protocol, **features)).is_valid(plan):
            raise ValueError("invalid_task_plan: provide objective, stages(goal/advance_when, optional id/arms/depends_on/observation_gate/suggested_tools), constraints, uncertainties, completion_criteria")
        validate_stage_dependencies(plan)
        if self.manipulation_protocol:
            for stage in plan["stages"]:
                if "operation_context" in stage:
                    validate_operation_evidence(stage["operation_context"], self.store,
                        identity, observation["observation_id"], require_current=False,
                        operation_candidates=self.operation_candidates)
        previous = self.current(identity)
        record = dict(plan_id=new_id("task-plan"), version=version_for(self.manipulation_protocol, **features),
                      identity={key: identity[key] for key in IDENTITY},
                      revision=previous["revision"] + 1 if previous else 1,
                      previous_plan_id=previous["plan_id"] if previous else None,
                      observation_id=observation["observation_id"], plan=plan,
                      source_kind="actor_judgment", tool_choices="advisory")
        if self.manipulation_protocol and self.task_scheduling:
            # Preserve the graph verbatim. A missing reason is a review suggestion,
            # never evidence that a physical dependency does not exist.
            missing = [stage.get("id", str(index)) for index, stage in enumerate(plan["stages"])
                       if stage.get("depends_on") and "dependency_reason" not in stage]
            if missing:
                record["scheduling_advisory"] = dict(stages_without_dependency_reason=missing,
                    suggestion="Review explicit ordering, physical or observation prerequisites; list order alone creates none. Keep justified dependencies and independent dual-arm work.")
        if self.manipulation_protocol and self.operation_candidates:
            candidates = [dict(stage=stage.get("id", str(index)),
                               requires_reassessment=stage["operation_context"]["candidate"]["from_observation_id"] != observation["observation_id"],
                               **summarize_candidate(stage["operation_context"]["candidate"]))
                          for index, stage in enumerate(plan["stages"])
                          if "candidate" in stage.get("operation_context", {})]
            if candidates:
                record["operation_candidates"] = candidates
        self.store.save("task_plan", record["plan_id"], record)
        self.store.set_state("runtime", "task_plan", record)
        self.store.event("task_plan.submitted", record, correlation)
        return record
