"""Project model-facing robot JSON without changing stored or program evidence.

These pure projections retain measurements and diagnostic outcomes. They are
not an observation filter, an action validator, or a summary of task success.
"""

from copy import deepcopy
import math
import re


ACTOR_PAYLOAD_PROFILES = ("full_v1", "compact_v1", "lean_v1", "lean_v2")
LEAN_PROFILES = frozenset({"lean_v1", "lean_v2"})
DEFAULT_ACTOR_PAYLOAD_PROFILE = "lean_v1"


def _reference_availability(value, key):
    """Replace a known storage address, retaining absent/unknown/conflicting data."""
    available = key.removesuffix("_ref") + "_available"
    if key not in value or available in value:
        return
    reference = value[key]
    if reference is None or (isinstance(reference, str) and reference.startswith("artifact:")):
        value[available] = reference is not None
        del value[key]


def _omit_image_storage_metadata(value):
    for key in ("image_encoding", "sha256", "source_sha256"):
        value.pop(key, None)


def lean_observation(metadata):
    """Omit routine storage metadata, preserving exact measurements and semantics.

    Images remain separate content items. Observation/camera IDs still address
    history and geometry tools; programs receive the original artifact handles.
    Only the calibration/pose references actually expanded by the harness can
    be elided. Unknown fields, reconstruction warnings and geometry stay intact.
    """
    result = deepcopy(metadata)
    if not isinstance(result, dict):
        return result
    bundle = result.get("bundle")
    if isinstance(bundle, dict):
        if isinstance(result.get("robot_state"), dict):
            _reference_availability(bundle, "robot_state_ref")
        cameras = bundle.get("cameras")
        if isinstance(cameras, list):
            first = cameras[0].copy() if cameras and isinstance(cameras[0], dict) else {}
            for camera in cameras:
                if not isinstance(camera, dict):
                    continue
                _omit_image_storage_metadata(camera)
                _reference_availability(camera, "rgb_ref")
                for key, expanded in (("calibration_ref", "camera_calibration"),
                                      ("pose_sample_ref", "camera_pose_sample")):
                    if (isinstance(result.get(expanded), dict) and key in first
                            and camera.get(key) == first[key]):
                        _reference_availability(camera, key)
    view = result.get("image_view")
    if isinstance(view, dict):
        _omit_image_storage_metadata(view)
        view.pop("encoding", None)
        for key in ("rgb_ref", "original_rgb_ref"):
            _reference_availability(view, key)
    return result


_RAW_FRAME_ID = re.compile(r"raw-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?:-|\Z)")
# Restated in the program_run description; the job record keeps them.
_PROGRAM_DESCRIPTION_PROSE = frozenset({"result_semantics", "timeout_semantics"})


def _display_float(value):
    if not math.isfinite(value):
        return value
    return round(value, 6) + 0.0  # also turns -0.0 into 0.0


def _raw_frame_id(value):
    return isinstance(value, str) and _RAW_FRAME_ID.match(value) is not None


def lean_v2_display(value, *, tool=None, top=True):
    """lean_v2 display copy: 6-decimal floats, without worker frame IDs or empty residues.

    Applied after presentation to actor tool-reply parts only. Typed data,
    program replies, exact trace reads and detail='full' keep original values.
    """
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if isinstance(key, str) and key.endswith("frame_id") and _raw_frame_id(item):
                continue
            if (isinstance(key, str) and key.endswith("frame_ids") and isinstance(item, list) and item
                    and all(map(_raw_frame_id, item))):
                continue
            if key == "camera_pose_diagnostics" and item == {}:
                continue
            if top and tool in {"program_run", "program_status"} and key in _PROGRAM_DESCRIPTION_PROSE:
                continue
            item = lean_v2_display(item, tool=tool, top=False)
            if key in {"camera_calibration", "camera_pose_sample"} and isinstance(item, dict) and isinstance(item.get("cameras"), dict):
                # Referenced intrinsics or a removed frame ID can leave an empty camera entry.
                cameras = {name: camera for name, camera in item["cameras"].items() if camera != {}}
                item = dict(item, cameras=cameras) if cameras else {k: v for k, v in item.items() if k != "cameras"}
            result[key] = item
        return result
    if isinstance(value, list):
        return [lean_v2_display(item, tool=tool, top=False) for item in value]
    if type(value) is float:
        return _display_float(value)
    return value


def lean_action_result(result):
    """Keep the entire executed request and receipt semantics at full precision.

    Build provenance stays in storage. Trace availability stays visible and
    history_read_trace(action_id, kind) resolves the omitted addresses.
    """
    projected = deepcopy(result)
    if isinstance(projected, dict) and isinstance(projected.get("receipt"), dict):
        receipt = projected["receipt"]
        receipt.pop("versions", None)
        for kind in ("planned_trace", "dispatched_trace", "actual_trace", "before_state"):
            _reference_availability(receipt, kind + "_ref")
        _summarize_passed_motion_checks(projected)
    return projected


def _summarize_passed_motion_checks(projected):
    """Remove repeated successful guard internals; keep failures and unknowns.

    Only the known admission policy is projected. The full check chronology,
    remaining budgets, skipped checks and all non-pass evidence remain visible.
    Exact trajectories and successful per-joint checks can be read by action ID.
    """
    receipt = projected.get("receipt")
    if not isinstance(receipt, dict) or not isinstance(receipt.get("motion_safety_checks"), list):
        return
    checks = receipt["motion_safety_checks"]
    request = projected.get("request")
    action_id = receipt.get("action_id") or (request.get("action_id") if isinstance(request, dict) else None)
    if not isinstance(action_id, str) or not action_id:
        return
    changed = False
    policy_definitions = []
    known_arm_fields = {"policy", "status", "reasons", "joint_names", "joint_types", "joint_travel_rad",
                        "joint_net_delta_rad", "witnesses", "thresholds", "nominal_native_steps",
                        "request_remaining_native_steps", "episode_remaining_native_steps", "exceeded_budgets",
                        "periodic_check", "periodic_checks", "semantics"}
    for check in checks:
        if not isinstance(check, dict) or check.get("status") != "pass" or not isinstance(check.get("arms"), dict):
            continue
        for arm in check["arms"].values():
            if (not isinstance(arm, dict) or arm.get("status") != "pass"
                    or arm.get("policy") != "joint-travel-admission-v1"
                    or arm.get("witnesses") != [] or arm.get("reasons") != []
                    or arm.get("exceeded_budgets") != {}):
                continue
            ordinary_fields = not (set(arm) - known_arm_fields)
            omitted = []
            for key in ("joint_names", "joint_types", "joint_travel_rad", "joint_net_delta_rad"):
                if key in arm:
                    del arm[key]
                    omitted.append(key)
            periodic = arm.get("periodic_checks")
            if isinstance(periodic, list):
                eligible = [item for item in periodic if isinstance(item, dict) and item.get("eligible") is True
                            and not (set(item) - {"joint_name", "status", "eligible", "shortest_equivalent_goal_rad", "joint_limits_rad"})
                            and item.get("status") in ("continuous_joint", "bounded_equivalent_within_limits")]
                if eligible:
                    arm["periodic_checks"] = [item for item in periodic if item not in eligible]
                    arm["eligible_periodic_check_count"] = len(eligible)
                    omitted.append("eligible_periodic_checks")
                if ordinary_fields and periodic and len(eligible) == len(periodic):
                    definition = {key: deepcopy(arm[key]) for key in ("policy", "thresholds", "semantics") if key in arm}
                    if definition not in policy_definitions:
                        policy_definitions.append(definition)
                    arm["policy_definition_index"] = policy_definitions.index(definition)
                    for key in ("semantics", "thresholds", "reasons", "witnesses", "exceeded_budgets", "periodic_checks"):
                        arm.pop(key, None)
                    arm["detail"] = "passed_check_summary"
            if omitted:
                if arm.get("detail") != "passed_check_summary":
                    arm["omitted_pass_detail"] = omitted
                changed = True
    if changed:
        receipt["motion_safety_check_summary"] = dict(count=len(checks),
            pass_count=sum(isinstance(check, dict) and check.get("status") == "pass" for check in checks),
            last_status=checks[-1].get("status") if checks and isinstance(checks[-1], dict) else None,
            full_result=dict(tool="robot_action_status", action_id=action_id, detail="full"))
        if policy_definitions:
            receipt["motion_safety_check_summary"]["policy_definitions"] = policy_definitions


# These describe storage/encoding or repeat the enclosing run's identity. Pixel
# geometry, simulator clocks, observation lineage and reconstruction warnings stay.
_OBSERVATION_BUNDLE_OMIT = frozenset({
    "schema_version", "run_id", "episode_id", "env_instance_id", "robot_state_ref",
})
_CAMERA_OMIT = frozenset({
    "image_encoding", "sha256", "source_sha256", "capture_time", "clock_domain",
})
# Full traces remain addressable by action_id through history_read_trace. Keep
# every other receipt field, including future diagnostics and partial outcomes.
_RECEIPT_OMIT = frozenset({
    "versions", "step_semantics", "trace_sampling", "planned_trace_ref",
    "dispatched_trace_ref", "actual_trace_ref",
})


def _round_finite_floats(value):
    if type(value) is float:
        return round(value, 6) if math.isfinite(value) else value
    if isinstance(value, dict):
        return {key: _round_finite_floats(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_round_finite_floats(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_round_finite_floats(item) for item in value)
    return value


def _without(value, omitted):
    return {key: item for key, item in value.items() if key not in omitted}


def compact_observation(metadata):
    """Project one {bundle, robot_state, image_view?} model delivery.

Unknown fields and all measured state are retained. In particular a gripper
command is never promoted to a contact measurement. image_view and camera
reconstruction/crop metadata remain available, with only finite float rounding.
    """
    result = deepcopy(metadata)
    if not isinstance(result, dict):
        return _round_finite_floats(result)
    bundle = result.get("bundle")
    if isinstance(bundle, dict):
        bundle = _without(bundle, _OBSERVATION_BUNDLE_OMIT)
        if isinstance(bundle.get("cameras"), list):
            bundle["cameras"] = [
                _without(camera, _CAMERA_OMIT) if isinstance(camera, dict) else camera
                for camera in bundle["cameras"]
            ]
        result["bundle"] = bundle
    return _round_finite_floats(result)


def compact_action_result(result):
    """Project an action reply while retaining every non-provenance outcome.

The submitted command already appears in the model's tool call, so its echo is
reduced to action/observation identity. Receipt fields use an exclusion list:
new diagnostics, errors, unknown outcomes, timing and waypoint residuals survive.
    """
    projected = deepcopy(result)
    if not isinstance(projected, dict):
        return _round_finite_floats(projected)
    receipt = projected.get("receipt")
    if isinstance(receipt, dict):
        projected["receipt"] = _without(receipt, _RECEIPT_OMIT)
    request = projected.get("request")
    if isinstance(request, dict):
        projected["request"] = {key: request[key] for key in
                                ("action_id", "based_on_observation_id") if key in request}
    _summarize_passed_motion_checks(projected)
    return _round_finite_floats(projected)
