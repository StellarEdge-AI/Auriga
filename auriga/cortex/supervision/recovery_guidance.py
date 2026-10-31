"""Shared next-step advice and continuation context from actor-visible state."""

from ...core.contracts import canonical

VERSION = "recovery-guidance-v1"
EVIDENCE_VERSION = "recovery-guidance-v4"
NEXT_STEP_INSTRUCTIONS = (
    "Reassess the plan and concerns against actual feedback. If an expected event is absent, "
    "keep that prerequisite unresolved; choose a changed hypothesis or useful work that neither "
    "depends on nor interferes with it. Choose a useful bounded next step and the RGB or measured "
    "evidence that will verify it. Budget the step, necessary release, withdrawal or required "
    "return, and an uncertainty reserve. Consider remaining task cost without requiring a "
    "complete route to success before useful action. Keep unknown later outcomes unknown; "
    "update the plan and affected attention when your interpretation changes."
)
INSTRUCTIONS = (
    "Before spending more native steps, choose one concrete recovery objective and the fresh "
    "RGB/measured evidence that would verify it. Briefly identify what failed or remains uncertain "
    "and what changes in this attempt (arm, grasp, orientation, clearance route or support). "
    "Budget the complete attempt: approach, manipulation/correction, release or verification, "
    "withdrawal and any required final return, with an explicit uncertainty reserve. A cheap "
    "approach endpoint alone is not a viable recovery budget. Use cost previews when they can "
    "change this choice. robot_action preview costs cover the complete submitted batch, not "
    "omitted later work; its optional settling has a separate additional step limit. "
    "If evidence or remaining budget does not support a complete attempt, inspect the specific "
    "missing evidence or state the concrete limitation instead of repeating the failed motion. "
    "Native execution is not proof of useful task progress. This advice adds no action, approval "
    "gate or completion rule; existing native termination and continuation limits still apply."
)
EVIDENCE_INSTRUCTIONS = (
    "Use the tested-candidate ledger; do not repeat an unchanged candidate or recite the same "
    "budget limitation. A rejected preview rules out only its nominal candidate at its recorded "
    "state and budget, never all routes. Spend a new recovery turn only on a concrete changed "
    "hypothesis or missing observation that could change the decision. Fresh IDs, labels or a "
    "different unaffordable endpoint are not such evidence. A cheap approach "
    "alone does not establish the feasibility of later work. Do not add idle native motion to "
    "reset recovery accounting. If neither a useful next step nor a decision-changing observation "
    "is supported, state the concrete limitation once; do not claim native success or global "
    "infeasibility. This guidance changes no action admission, native termination or continuation "
    "limit and does not require consuming all remaining steps or more preview calls."
)


def recovery_guidance(harness, home_check=None, *, evidence=None):
    """Return compact context and prose, without robot/observer calls or scene truth."""
    latest = getattr(harness, "latest", None) or {}
    budget_fn = getattr(harness, "action_budget", None)
    try:
        budget = budget_fn() if callable(budget_fn) else {}
    except Exception:
        budget = {}
    if not isinstance(budget, dict):
        budget = {}
    budget = {key: value if type(value) is int and value >= 0 else None
              for key in ("native_steps_used", "native_steps_limit", "native_steps_remaining")
              for value in (budget.get(key),)}
    home_status = (home_check or {}).get("status")
    context = dict(version=VERSION, observation_id=latest.get("observation_id"),
                   action_budget=budget,
                   measured_home_status=home_status if home_status in ("home", "away") else "unknown",
                   unknown_values_are_null=True)
    instructions = INSTRUCTIONS
    if isinstance(evidence, dict) and evidence.get("version") == "recovery-evidence-v1":
        context["version"] = EVIDENCE_VERSION
        allowed = ("version", "native_action_count", "native_steps_remaining", "no_native_finals",
                   "no_new_evidence_finals", "no_new_evidence_limit", "tested_candidate_count",
                   "earlier_candidate_count", "scope")
        context["tested_recovery"] = {key: evidence[key] for key in allowed if key in evidence}
        fields = ("label", "nominal_native_steps", "status", "reason", "exceeds_episode_budget",
                  "native_steps_remaining", "observation_id")
        context["tested_recovery"]["tested_candidates"] = [
            {key: candidate[key] for key in fields if key in candidate}
            for candidate in evidence.get("tested_candidates", [])[-6:] if isinstance(candidate, dict)]
        instructions = EVIDENCE_INSTRUCTIONS
    return context, "Recovery context: " + canonical(context) + "\n" + instructions
