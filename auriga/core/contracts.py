"""Shared v0.1 trust-boundary checks; unsupported motion stays unsupported."""

import hashlib
import json
import math
import uuid
from datetime import datetime, timezone

SCHEMA_VERSION = "0.1"
IDENTITY = ("run_id", "episode_id", "env_instance_id", "env_generation")
# Stable import paths for existing callers and archived source snapshots.
from .action_validation import (MAX_HOLD_NATIVE_STEPS, finite_vector, validate_gripper_motion,
    dual_path_shape, validate_gripper_events, validate_hold_native_steps, validate_arm_options,
    validate_dual_arm_progression, validate_action_speed)
OBSERVATION_PROFILES = (
    "rgb_proprio_v1", "rgb_proprio_calibrated_v1", "rgbd_proprio_calibrated_v1",
    "rgb_proprio_calibrated_synthetic_v1",
)


def validate_arm_queue_action(action, capabilities=None):
    from ..robot.motion.arm_queue_contract import validate_arm_queue_action as validate
    return validate(action, capabilities)


def new_id(prefix):
    return f"{prefix}-{uuid.uuid4().hex}"


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def validate_identity(value):
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported_schema_version")
    for key in IDENTITY[:-1]:
        if not isinstance(value.get(key), str) or not value[key]:
            raise ValueError(f"invalid_{key}")
    generation = value.get("env_generation")
    if type(generation) is not int or generation < 1:
        raise ValueError("invalid_env_generation")


def validate_observation(value):
    from .observation_policy import DEPTH_FIELDS, DEPTH_STORAGE_FIELDS, PERMITTED_DEPTH_FIELDS, PRIVILEGED_FIELDS, allows_depth, has_calibration
    if not isinstance(value, dict):
        raise ValueError("observation_must_be_object")
    validate_identity(value)
    if value.get("world_kind") != "observed":
        raise ValueError("observation_requires_observed_world")
    if not value.get("observation_id") or not value.get("robot_state_ref"):
        raise ValueError("missing_observation_or_robot_state")
    profile = value.get("observation_profile", "rgb_proprio_v1")
    if not isinstance(profile, str) or profile not in OBSERVATION_PROFILES:
        raise ValueError("unsupported_observation_profile")
    forbidden = PRIVILEGED_FIELDS | (DEPTH_FIELDS - (PERMITTED_DEPTH_FIELDS if allows_depth(profile) else set())) | {"depth_path"}
    def inspect_fields(item):
        if isinstance(item, dict):
            if forbidden.intersection(item):
                raise ValueError("forbidden_observation_field")
            if DEPTH_STORAGE_FIELDS.intersection(item) and not any(item is camera for camera in value.get("cameras", [])):
                raise ValueError("depth_metadata_requires_camera")
            for child in item.values(): inspect_fields(child)
        elif isinstance(item, list):
            for child in item: inspect_fields(child)
    inspect_fields(value)
    if value.get("sim_step") is not None and not value.get("step_domain"):
        raise ValueError("sim_step_requires_domain")
    calibrated = has_calibration(profile) or profile == "rgb_proprio_calibrated_synthetic_v1"
    if calibrated and not value.get("calibration_id"):
        raise ValueError("calibrated_profile_requires_calibration")
    if not calibrated and value.get("calibration_id"):
        raise ValueError("calibration_not_permitted_by_observation_profile")
    for camera in value.get("cameras", []):
        if forbidden.intersection(camera) or not all(camera.get(k) for k in ("camera_id", "frame_id", "rgb_ref", "width", "height")):
            raise ValueError("invalid_or_privileged_camera")
        if calibrated and not camera.get("calibration_ref"):
            raise ValueError("calibrated_camera_requires_reference")
        if not calibrated and any(camera.get(key) for key in ("calibration_ref", "pose_sample_ref", "K", "T_world_camera")):
            raise ValueError("calibration_not_permitted_by_observation_profile")
        if allows_depth(profile):
            validate_depth_metadata(camera)
    return value


def validate_depth_metadata(camera, storage_field="depth_ref"):
    """Depth belongs to the same raster/frame as RGB; arrays remain in an NPZ artifact."""
    if storage_field not in ("depth_ref", "depth_path"):
        raise ValueError("invalid_depth_storage_field")
    reference = camera.get(storage_field)
    if not isinstance(reference, str) or not reference or ("depth_path" if storage_field == "depth_ref" else "depth_ref") in camera:
        raise ValueError("invalid_depth_reference")
    if camera.get("depth_kind") != "z" or camera.get("depth_unit") != "m":
        raise ValueError("invalid_depth_semantics")
    width, height = camera.get("width"), camera.get("height")
    if any(type(value) is not int or not 0 < value <= 4096 for value in (width, height)):
        raise ValueError("invalid_depth_dimensions")
    shape = camera.get("depth_shape")
    if not isinstance(shape, list) or any(type(value) is not int for value in shape) or shape != [height, width]:
        raise ValueError("depth_rgb_shape_mismatch")
    finite_vector(camera.get("depth_clip_m"), 2, "depth_clip_m")
    near, far = camera["depth_clip_m"]
    if not 0 <= near < far:
        raise ValueError("invalid_depth_clip_m")
    return camera


def validate_action(value, identity, observation_id, capabilities):
    validate_identity(value)
    validate_dual_arm_progression(value, capabilities)
    if value.get("world_kind", "observed") != "observed":
        raise ValueError("internal_action_cannot_execute_observed_world")
    if any(value.get(k) != identity[k] for k in IDENTITY):
        raise ValueError("stale_environment_identity")
    if value.get("based_on_observation_id") != observation_id:
        raise ValueError("stale_observation")
    if not isinstance(value.get("action_id"), str) or not value["action_id"]:
        raise ValueError("missing_action_id")
    if value.get("mode") != "absolute" or value.get("frame_id") != capabilities["frame_id"] or value.get("tcp_config_id") != capabilities["tcp_config_id"]:
        raise ValueError("unsupported_frame_tcp_or_mode")
    if value.get("type") == "BatchAction":
        from ..robot.motion.batch_contract import validate_batch_action
        validate_batch_action(value, capabilities)
        return value
    if value.get("type") == "ArmQueueAction":
        timeout = value.get("timeout_s")
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not .1 <= timeout <= capabilities.get("max_timeout_s", 300):
            raise ValueError("invalid_timeout_s")
        return validate_arm_queue_action(value, capabilities)
    if "hold_native_steps" in value:
        waiting = capabilities.get("bounded_wait")
        if not isinstance(waiting, dict) or type(waiting.get("max_native_steps")) is not int:
            raise ValueError("unsupported_capability:bounded_wait")
        validate_hold_native_steps(value, waiting["max_native_steps"])
    path = value.get("path", {})
    if not isinstance(path, dict):
        raise ValueError("invalid_action: path must be an object")
    if path.get("kind") not in capabilities["path_kinds"]:
        raise ValueError("unsupported_capability:path_kind")
    waypoints = path.get("waypoints", [])
    if not isinstance(waypoints, list):
        raise ValueError("invalid_action: waypoints must be a list")
    if (not waypoints and path.get("kind") != "hold") or len(waypoints) > capabilities.get("max_waypoints", 1):
        raise ValueError("unsupported_capability:waypoints")
    arm_set = None
    for waypoint in waypoints:
        if not isinstance(waypoint, dict) or not waypoint or set(waypoint) - {"left", "right"} or (len(waypoint) == 2 and not capabilities.get("dual_arm", False)):
            raise ValueError("unsupported_capability:arms")
        if (arm_set is not None and set(waypoint) != arm_set
                and value.get("dual_arm_progression") != "independent"):
            raise ValueError("unsupported_capability: all waypoints must use the same arms")
        arm_set = set(waypoint)
        for pose in waypoint.values():
            if not isinstance(pose, dict):
                raise ValueError("invalid_pose: pose must be an object")
            finite_vector(pose.get("position_m"), 3, "position_m")
            finite_vector(pose.get("quaternion_xyzw"), 4, "quaternion_xyzw")
            if abs(sum(x*x for x in pose["quaternion_xyzw"]) - 1) > 1e-4:
                raise ValueError("quaternion_must_be_normalized_xyzw")
    timeout = value.get("timeout_s")
    if type(timeout) not in (float, int) or not math.isfinite(timeout) or not 0 < timeout <= capabilities.get("max_timeout_s", 300):
        raise ValueError("invalid_timeout_s")
    validate_gripper_events(value, capabilities)
    validate_action_speed(value, capabilities)
    return value
