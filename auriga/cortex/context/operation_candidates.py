"""Compact actor-authored operation candidates, without geometry or motion inference.

This is an optional extension of operation_context, not another executor. Unknown
stages remain unknown; estimates never become collision checks or execution gates.
"""

from jsonschema import Draft202012Validator


PHASES = ("entry", "contact", "transport", "placement", "separation", "withdrawal",
          "verification", "required_return")
_SHORT = dict(type="string", minLength=1, maxLength=600, pattern=r"\S")
_STEPS = dict(anyOf=[dict(type="integer", minimum=0), dict(const="unknown")])
_PHASE = dict(type="object", additionalProperties=False,
              properties=dict(route=_SHORT, native_steps=_STEPS), required=["route", "native_steps"])
OPERATION_CANDIDATE_SCHEMA = dict(type="object", additionalProperties=False,
    description="Optional near-term whole-operation candidate. Omitted phases and unknown costs remain unassessed; no fabricated geometry is required.",
    properties=dict(
        id=dict(type="string", minLength=1, maxLength=80, pattern=r"\S"),
        from_observation_id=dict(type="string", minLength=1, maxLength=160, pattern=r"\S"),
        contact_section=_SHORT,
        object_relative_grasp=_SHORT,
        swept_clearance=_SHORT,
        phases=dict(type="object", propertyNames=dict(enum=list(PHASES)),
                    additionalProperties=_PHASE),
        reserve_native_steps=_STEPS),
    required=["from_observation_id", "contact_section", "object_relative_grasp", "swept_clearance", "phases"])


def validate_candidate(candidate, evidence_observation_id):
    if not Draft202012Validator(OPERATION_CANDIDATE_SCHEMA).is_valid(candidate):
        raise ValueError("invalid_operation_candidate")
    if candidate["from_observation_id"] != evidence_observation_id:
        raise ValueError("operation_candidate_start_must_match_context_observation")
    return candidate


def summarize_candidate(candidate, *, native_steps_remaining=None):
    """Report incomplete estimates honestly, without treating actor text as proof.

    Costs are caller estimates for sequential operation phases. Within-phase dual
    overlap must already be reflected by the caller; this helper does not add arm
    paths, solve IK, query an evaluator, or allocate motion budget.
    """
    validate_candidate(candidate, candidate.get("from_observation_id"))
    phases = candidate["phases"]
    unknown = [phase for phase in PHASES
               if phase not in phases or phases[phase]["native_steps"] == "unknown"]
    known = sum(value["native_steps"] for value in phases.values()
                if type(value["native_steps"]) is int)
    reserve = candidate.get("reserve_native_steps", "unknown")
    complete = not unknown and type(reserve) is int
    total = known + reserve if complete else None
    if native_steps_remaining is not None and (type(native_steps_remaining) is not int or native_steps_remaining < 0):
        raise ValueError("invalid_native_steps_remaining")
    exceeds = native_steps_remaining is not None and (known + (reserve if type(reserve) is int else 0)) > native_steps_remaining
    fit = ("exceeds_remaining" if exceeds else "unknown" if not complete or native_steps_remaining is None
           else "estimated_within_remaining")
    result = dict(from_observation_id=candidate["from_observation_id"],
                  known_estimate_native_steps=known, unknown_phases=unknown,
                  reserve_native_steps=reserve, complete_estimate_native_steps=total,
                  budget_fit=fit, physical_clearance="actor_judgment_unverified")
    if "id" in candidate:
        result["id"] = candidate["id"]
    if exceeds:
        result["next_check"] = "Compare a different complete route, grasp or independent subtask before committing."
    elif unknown or reserve == "unknown":
        result["next_check"] = "Estimate only decision-relevant missing phases/reserve, or retain unknown; a cheap entry does not establish an affordable completion."
    return result
