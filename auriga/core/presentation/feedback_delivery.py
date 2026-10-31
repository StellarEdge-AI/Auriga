"""Model-facing feedback composition; execution and evidence remain untouched."""

def action_trajectory_feedback(harness, action, receipt, correlation, *, diagnostic=False):
    if correlation.get("program_id") or not harness.config.features.trajectory_preview:
        return None, []
    try:
        from ...runtime.actions.trajectory_feedback import build_action_trajectory_feedback
        if diagnostic:
            return build_action_trajectory_feedback(harness, action, receipt, correlation, diagnostic=True)
        return build_action_trajectory_feedback(harness, action, receipt, correlation)
    except Exception as error:
        # Optional rendering after execution must not turn a completed action
        # into a tool exception that invites accidental re-execution.
        return dict(available=False, reason="trajectory_feedback_failed:" + type(error).__name__,
                    action_id=action.get("action_id"), execution_status=receipt.get("status")), []


def execution_unconfirmed(value):
    """Keep unknown execution and negative stop acknowledgements authoritative."""
    if not isinstance(value, dict):
        return False
    if (value.get("status") in ("unknown", "status_unknown") or value.get("execution_uncertain") is True
            or value.get("stop_confirmed") is False or value.get("stop_confirmation") is False):
        return True
    confirmation = value.get("stop_confirmation")
    if isinstance(confirmation, dict) and confirmation.get("confirmed") is False:
        return True
    return any(execution_unconfirmed(arm) for field in ("arm_results", "gripper_execution", "stage_results")
               if isinstance(value.get(field), dict) for arm in value[field].values())


def execution_exception(value):
    """Public uncertainty flags override automatic image omission."""
    if not isinstance(value, dict):
        return False
    if value.get("reason") or value.get("error") or execution_unconfirmed(value):
        return True
    return any(execution_exception(arm) for field in ("arm_results", "gripper_execution")
               if isinstance(value.get(field), dict) for arm in value[field].values())
