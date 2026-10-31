"""Actionable input diagnostics without changing validation or retry behavior."""

from copy import deepcopy
import math

from jsonschema import Draft202012Validator, ValidationError

from .tool_output import ToolOutput, wire_response


class ToolInputError(ValueError):
    """Keep the established error string while carrying structured context."""
    def __init__(self, code, *, field_path="", actual=None, suggestion=None, minimal_example=None):
        super().__init__(code)
        self.code = code
        self.field_path = field_path
        self.actual = actual
        self.suggestion = suggestion
        self.minimal_example = minimal_example


def _pointer(parts):
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in parts)


def _bounded_actual(value):
    if isinstance(value, str):
        return value if len(value) <= 160 else dict(type="string", characters=len(value), prefix=value[:160])
    if isinstance(value, dict):
        if len(value) <= 8 and all(item is None or type(item) in (str, bool, int, float) for item in value.values()):
            return {key: _bounded_actual(item) for key, item in value.items()}
        return dict(type="object", keys=list(value)[:16], key_count=len(value))
    if isinstance(value, (list, tuple)):
        if len(value) <= 8 and all(item is None or type(item) in (str, bool, int, float) for item in value):
            return [_bounded_actual(item) for item in value]
        return dict(type="array", length=len(value))
    if type(value) is float and not math.isfinite(value):
        return str(value)
    if value is None or type(value) in (bool, int, float):
        return value
    return dict(type=type(value).__name__)


def _example_for(schema):
    if not isinstance(schema, dict):
        return "<value meeting the field schema>"
    if "enum" in schema:
        return "<one of: " + ", ".join(map(str, schema["enum"])) + ">"
    kind = schema.get("type")
    if kind == "object":
        return {key: _example_for(schema.get("properties", {}).get(key, {}))
                for key in schema.get("required", [])[:8]}
    if kind == "array":
        return "<array of " + str(schema.get("minItems", "permitted")) + "+ entries matching items schema>"
    if kind in ("number", "integer"):
        return "<caller-selected " + kind + " within the stated bounds>"
    if kind == "boolean":
        return "<true or false>"
    return "<current caller-provided value>"


def _schema_advice(error):
    schema = error.schema
    if not isinstance(schema, dict):
        return 'This field/value is not permitted by the schema.'
    advice = []
    if schema.get('description'):
        advice.append(schema['description'])
    if error.validator in ('required', 'additionalProperties'):
        fields = schema.get('properties', {})
        advice.append('Allowed fields here: ' + ', '.join(fields) + '.')
        if schema.get('required'):
            advice.append('Required fields: ' + ', '.join(schema['required']) + '.')
        kind_schema = fields.get('kind', {})
        kind = kind_schema.get('const') if isinstance(kind_schema, dict) else None
        if kind == 'gripper':
            advice.append('Use goal_normalized in [0,1] (0 closed, 1 open); gripper/goal/gripper_goal are not field names. Arm speed fields do not apply; use gripper_speed_scale or gripper_speed_preset.')
        elif kind == 'move':
            advice.append('Choose grasp_position_m + angles_deg OR position_m + quaternion_xyzw. Optional jaw target is gripper_goal_normalized. Routes are successive move stages, not via/joint fields.')
        elif kind == 'observe':
            advice.append('Supply a nonblank reason describing what to inspect; pose and jaw fields do not belong on observe.')
    elif error.validator == 'oneOf':
        choices = [item.get('required', []) if isinstance(item, dict) else [] for item in error.validator_value]
        if any(choices):
            advice.append('Choose exactly one complete required field group: ' + ' OR '.join(', '.join(fields) for fields in choices) + '.')
    elif error.validator == 'not':
        fields = error.validator_value.get('required', []) if isinstance(error.validator_value, dict) else []
        if fields:
            advice.append('Do not combine these fields: ' + ', '.join(fields) + '; choose one.')
    bounds = {key: schema[key] for key in ('type', 'enum', 'const', 'minimum', 'maximum',
        'exclusiveMinimum', 'exclusiveMaximum', 'minItems', 'maxItems', 'minLength', 'pattern') if key in schema}
    if bounds:
        advice.append('Field constraints: ' + str(bounds) + '.')
    return ' '.join(advice)


def schema_input_error(error, *, code="invalid_tool_arguments", prefix=()):
    """Convert a jsonschema error while retaining the exact offending path."""
    path = [*prefix, *error.absolute_path]
    actual = error.instance
    schema = error.schema
    if error.validator == "required" and isinstance(error.instance, dict):
        missing = [key for key in error.validator_value if key not in error.instance]
        if missing:
            path.append(missing[0])
            actual = dict(present=False)
            schema = error.schema.get("properties", {}).get(missing[0], {})
    elif error.validator == "additionalProperties" and isinstance(error.instance, dict):
        extras = [key for key in error.instance if key not in error.schema.get("properties", {})]
        if extras:
            path.append(extras[0])
            actual = error.instance[extras[0]]
    field_path = _pointer(path)
    return ToolInputError(code, field_path=field_path, actual=actual,
        suggestion=error.message[:512] + '. ' + _schema_advice(error) +
            (' ' + schema['description'] if schema is not error.schema and isinstance(schema, dict) and schema.get('description') else '') +
            " Correct this input field; no action is retried automatically.",
        minimal_example={"field_path": field_path, "value": _example_for(schema),
                         "example_kind": "structural_placeholder; choose values from current permitted evidence"})


def _lookup(arguments, path):
    value = arguments
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return dict(present=False)
        value = value[key]
    return value


def _known_error(error, tool, arguments):
    code = str(error)
    if code.startswith("invalid_task_plan: provide"):
        from ..cortex.context.task_planning import PLAN_SCHEMA
        if isinstance(arguments.get("plan"), dict):
            from ..cortex.context.legacy_task_planning import PLAN_SCHEMA
        issue = next(Draft202012Validator(PLAN_SCHEMA).iter_errors(arguments.get("plan")), None)
        if issue is not None:
            return schema_input_error(issue, code="invalid_task_plan", prefix=("plan",))
    if code == "operation_context_release_evidence_required":
        paths = []
        def locate(value, path):
            if isinstance(value, dict):
                if value.get("state") in ("retained", "released_unverified") and not value.get("release_observation_id"):
                    paths.append(path + ["release_observation_id"])
                for key, item in value.items():
                    locate(item, path + [key])
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    locate(item, path + [index])
        locate(arguments, [])
        path = paths[0] if paths else ["operation_context", "release_observation_id"]
        return ToolInputError(code, field_path=_pointer(path), actual=dict(present=False),
            suggestion="Released_unverified and retained states both need an existing same-episode release observation. Retained additionally needs visible review. If release evidence is unavailable, keep state unknown; do not fabricate an observation ID or physical claim.",
            minimal_example={"state": "unknown", "example_kind": "only when release evidence is unavailable; preserve actual current evidence and uncertainty"})
    rules = {
        "stale_observation": (["observation_id"], "Read robot_observe and reassess the intended action against the current observation. Preview a new batch if needed; do not merely relabel old poses with a newer ID or retry an uncertain execution.", {"tool": "robot_observe", "arguments": {}}),
        "stale_environment_identity": ([], "This evidence or plan belongs to another run/episode/environment generation. Read current observations and build a new plan from current permitted evidence; old IDs cannot be made current by renaming.", {"tool": "robot_observe", "arguments": {}}),
        "action_id_content_conflict": (["action_id"], "This action_id already identifies a different request or plan cursor. Inspect its stored status first. Only a genuinely new intended action may use a new ID; do not use a new ID to retry unknown physical execution.", {"tool": "robot_action_status", "action_id": "<conflicting existing action ID>"}),
        "unknown_program_job": (["job_id"], "Use the job_id actually returned by program_run in this run. An unknown job is not proof that execution did not start; inspect recorded calls before considering new execution.", {"tool": "program_status", "job_id": "<existing job ID>"}),
        "program_review_not_active": (["review_id"], "Read program_status for this job. Resume only when status is awaiting_review, using its current pending_review.review_id and decision=approve or reject. Do not execute the paused request separately.", {"tool": "program_status", "job_id": "<existing job ID>"}),
        "program_time_insufficient_for_action": (["timeout_s"], "The inner action timeout must leave at least 5 seconds in the program work-time budget. Inspect job/action status before choosing a feasible shorter action deadline or a new bounded program; never blindly repeat execution.", {"tool": "program_status", "job_id": "<existing job ID>"}),
        "robot_resource_id_required": (["resource_id"], "Read robot_information with section=resources, then copy an existing resource_id with section=resource.", {"tool": "robot_information", "section": "resources"}),
        "robot_resource_id_requires_resource_section": (["section"], "resource_id is accepted only with section=resource. Use section=resources to list available IDs.", {"section": "resources"}),
        "depth_requires_current_observation": (["request", "observation_id"], "Read robot_observe. Refresh an existing target with target_id, current observation_id and a newly verified RGB ROI, retaining its registered camera; or supply current explicit observation, camera and geometry fields. Do not relabel stale candidates.", {"request": {"kind": "help"}}),
        "depth_target_requires_refresh": (["request", "target_id"], "Refresh this target using refresh_target with target_id, current observation_id and a newly verified ROI before querying candidates. The target retains its original camera; omit camera_id.", {"request": {"kind": "refresh_target", "target_id": "<existing target ID>", "observation_id": "<current observation ID>", "roi": [0, 0, 8, 8]}}),
        "unknown_depth_query": (["request", "kind"], "Choose help, depth_points, region_geometry, grasp_candidates, place_candidates, compare_regions, register_target, refresh_target, target_status, forget_target or skill_help. help returns the required fields.", {"request": {"kind": "help"}}),
        "robot_action_requires_stages": (["stages"], "preview/direct require 1..32 stages. Each has a unique id and kind=move/gripper/observe. Use inspect with a returned reference to read status without creating an action.", {"mode": "inspect", "plan_id": "<existing plan ID>"}),
        "robot_action_requires_observation_id": (["observation_id"], "Call robot_observe, then supply its current observation_id with stages for preview/direct.", {"observation_id": "<current observation ID>"}),
        "robot_action_requires_plan_id": (["plan_id"], "Execute requires the plan_id returned by a successful preview. Review the plan first and use its next_action fields.", {"mode": "execute", "plan_id": "<reviewed existing plan ID>"}),
        "robot_action_preview_has_execute_or_inspect_fields": (["mode"], "preview/direct/execute_if_clean accept observation_id, action_id, stages, source_plan_id, start_from_stage_id, stage_overrides, timeout_s, camera_id, detail, settle, obstacles, obstacle_object_ids, obstacle_policy, ignore_obstacle_ids, target_exemption_preset, target_exemption_radius_m, clearance_preset, clearance_height_m, held_object/held_objects, contact_entity_id, initial_contact, placement_contact. Use execute for plan_id/cursor/revision and inspect for trajectory selection.", {"mode": "preview", "observation_id": "<current observation ID>", "stages": "<intended stages>"}),
        "cached_batch_commands_cannot_be_changed": (["mode"], "execute accepts only mode, plan_id, action_id, expected_cursor, expected_revision. To change commands, inspect current state and preview a new batch; do not replay an uncertain execution.", {"mode": "execute", "plan_id": "<reviewed existing plan ID>"}),
        "robot_action_inspect_has_command_fields": (["mode"], "inspect accepts mode, exactly one plan_id/action_id/trajectory_ref, and optional path_kind, camera_id, detail, native_steps OR start_native_step/max_samples. Omit command fields.", {"mode": "inspect", "action_id": "<existing action ID>"}),
        "inspect_requires_one_plan_action_or_trajectory_reference": (["mode"], "Supply exactly one existing plan_id, action_id, or trajectory_ref with mode=inspect.", {"mode": "inspect", "action_id": "<existing action ID>"}),
        "robot_action_plan_unknown_or_expired": (["plan_id"], "Only 16 recent plans in the active process are executable. Read current observation and preview a new batch if needed. Use an existing action_id or trajectory_ref to inspect archived execution; never replay merely because a plan expired.", {"mode": "inspect", "action_id": "<existing executed action ID>"}),
        "robot_action_preview_requires_idle_active_run": (["mode"], "Preview requires an idle active run. Inspect the in-flight action and wait for its terminal status before planning. Do not repeat a physical request whose result is unknown.", {"mode": "inspect", "action_id": "<in-flight action ID>"}),
        "trajectory_native_steps_and_pagination_are_mutually_exclusive": (["native_steps"], "Use native_steps (1..3 existing sample IDs) OR start_native_step/max_samples (up to 25 samples); do not combine them.", {"mode": "inspect", "action_id": "<existing action ID>", "max_samples": 25}),
        "trajectory_path_kind_requires_action_feedback": (["path_kind"], "path_kind selects measured/planned only on an executed action feedback artifact. Omit it when inspecting a preview plan or candidate trajectory.", {"mode": "inspect", "plan_id": "<existing plan ID>"}),
        "trajectory_start_native_step_must_exist": (["start_native_step"], "Use an existing native_step from the inspected trajectory, or follow returned next_native_step. First inspect without pagination to see available key samples.", {"mode": "inspect", "action_id": "<existing action ID>"}),
        "current_observation_unavailable": (["observation_id"], "A current observation is unavailable. Inspect stored action status and call robot_observe if available. If observation remains unavailable, report the blocker; do not invent an ID or repeat an uncertain physical action.", {"tool": "robot_observe", "arguments": {}}),
        "tool_arguments_must_be_object": ([], "Pass a JSON object of named tool arguments.", {}),
        "program_arguments_require_object": ([], "Pass a JSON object of named tool arguments.", {}),
        "operation_context_requires_current_evidence": (["operation_context", "evidence", "observation_id"], "Use the actual current observation supporting this claim; review it before revising the context.", {"evidence": {"observation_id": "<current existing observation ID>"}}),
        "invalid_output_detail": (["detail"], "Choose summary or full.", {"detail": "summary"}),
        "invalid_page_start": (["start"], "Use a non-negative integer sequence offset, not a boolean.", {"start": 0}),
        "invalid_page_limit": (["limit"], "Use an integer limit in the tool's documented range.", {"limit": 25}),
    }
    if code in rules:
        path, suggestion, example = rules[code]
        return ToolInputError(code, field_path=_pointer(path), actual=_lookup(arguments, path),
                              suggestion=suggestion, minimal_example=example)
    if tool in ('geometry_query', 'depth_query'):
        return ToolInputError(code, field_path='/request', actual=arguments.get('request'),
            suggestion='Read request={"kind":"help"} on this same tool for the supported query kinds and required fields. Preserve actual observation/camera/target IDs; use only capabilities and evidence enabled for this run. No geometry estimate proves contact or task success.',
            minimal_example={'request': {'kind': 'help'}})
    return None


def error_payload(error, *, tool=None, arguments=None):
    """Describe a rejected call; missing runtime state is never called no motion."""
    if isinstance(error, ValidationError):
        structured = schema_input_error(error)
        legacy_error = error.message
    elif isinstance(error, ToolInputError):
        structured, legacy_error = error, str(error)
    else:
        try:
            structured = _known_error(error, tool, arguments) if isinstance(arguments, dict) else None
        except (TypeError, ValueError, KeyError, AttributeError):
            # An unusually malformed request must still receive the original
            # error; presenting advice cannot turn a handled failure into a new one.
            structured = None
        legacy_error = str(error)
    if structured is None:
        structured = ToolInputError(legacy_error.split(":", 1)[0], actual=arguments,
            suggestion="Check the named tool's argument schema and this error. If execution may have started, inspect its stored status before considering a corrected request; no automatic retry is implied.",
            minimal_example={"tool": tool, "arguments": "<correct only the field identified by the error; preserve valid caller choices>"})
    result = dict(error=legacy_error, code=structured.code, field_path=structured.field_path,
                  actual=_bounded_actual(structured.actual), suggestion=structured.suggestion,
                  minimal_example=deepcopy(structured.minimal_example),
                  outcome_semantics="Input/runtime error only; no motion or task outcome is inferred.")
    if tool:
        result["tool"] = tool
    return result


def error_response(error, *, tool=None, arguments=None):
    response = ToolOutput(error_payload(error, tool=tool, arguments=arguments)).response()
    response.success = False
    return wire_response(response)
