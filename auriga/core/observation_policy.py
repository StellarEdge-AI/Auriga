"""Profile-aware actor boundary: robot calibration is public; task truth is not."""

from copy import deepcopy

from .contracts import finite_vector

LEGACY_ACTOR_PROFILE = "rgb_proprio_v1"
ACTOR_PROFILE = "rgb_proprio_calibrated_v1"
DEPTH_ACTOR_PROFILE = "rgbd_proprio_calibrated_v1"
ACTOR_PROFILES = (ACTOR_PROFILE, LEGACY_ACTOR_PROFILE, DEPTH_ACTOR_PROFILE)
ACTOR_POLICY = "robodojo-calibrated-rgb-proprio-no-task-truth-v1"
DEPTH_ACTOR_POLICY = "robodojo-calibrated-rgbd-proprio-no-task-truth-v1"
LEGACY_ACTOR_POLICY = "robodojo-rgb-proprio-no-privilege-v1"
DEVELOPMENT_TOOLS = {"geometry_query"}
CALIBRATION_FIELDS = {"camera_calibration", "camera_pose_sample", "camera_poses", "calibration_ref",
                      "pose_sample_ref", "K", "T_world_camera", "T_link_camera", "T_world_link",
                      "T_world_base", "robot_model", "robot_model_ref"}
DEPTH_STORAGE_FIELDS = {"depth_path", "depth_ref", "depth_kind", "depth_unit", "depth_clip_m", "depth_shape"}
DEPTH_DERIVED_FIELDS = {"depth_derived", "depth_feedback"}
PERMITTED_DEPTH_FIELDS = DEPTH_STORAGE_FIELDS | DEPTH_DERIVED_FIELDS
DEPTH_FIELDS = PERMITTED_DEPTH_FIELDS | {"depth", "approximate_depth", "distance_to_image_plane",
    "distance_to_camera", "depth_valid_ref", "depth_validity_ref", "depth_mask_ref"}
# scene_truth: analysis-only ground-truth log (auriga/scene_truth.py); never actor-facing.
PRIVILEGED_FIELDS = {"native_usd_pose_diagnostic", "object_poses", "scene_truth",
                     "reward", "score", "task_state", "scene_graph", "planning_diagnostics",
                     "contact_forces", "contact_points", "object_contacts", "evaluator_state"}


def has_calibration(profile):
    return profile in (ACTOR_PROFILE, DEPTH_ACTOR_PROFILE)


def allows_depth(profile):
    return profile == DEPTH_ACTOR_PROFILE


def actor_policy(profile=ACTOR_PROFILE):
    if profile not in ACTOR_PROFILES:
        raise ValueError("actor_requires_supported_observation_profile")
    if allows_depth(profile):
        return DEPTH_ACTOR_POLICY
    return ACTOR_POLICY if has_calibration(profile) else LEGACY_ACTOR_POLICY


def reject_privileged(value, profile=ACTOR_PROFILE, *, stage_ids=None):
    actor_policy(profile)
    forbidden = PRIVILEGED_FIELDS | (CALIBRATION_FIELDS if profile == LEGACY_ACTOR_PROFILE else set())
    forbidden |= DEPTH_FIELDS - (PERMITTED_DEPTH_FIELDS if allows_depth(profile) else set())
    if isinstance(value, dict):
        if forbidden.intersection(value):
            raise ValueError("privileged_data_not_permitted_for_actor")
        feedback = value.get("contact_feedback")
        if feedback is not None and feedback != "unavailable":
            raise ValueError("contact_truth_not_permitted_for_actor")
        for key, item in value.items():
            if stage_ids is not None and key in {"stage_results", "stages", "stage_costs", "checkpoint_reasons"}:
                if not isinstance(item, dict) or not item.keys() <= set(stage_ids):
                    raise ValueError("action_stage_map_does_not_match_request")
                expected = str if key == "checkpoint_reasons" else dict
                if any(not isinstance(stage, expected) for stage in item.values()):
                    raise ValueError("invalid_action_stage_map_value")
                # Only this action-owned map has label keys; nested data has no exemption.
                for stage in item.values():
                    reject_privileged(stage, profile)
            elif stage_ids is not None and key == "checkpoints" and isinstance(item, list):
                for marker in item:
                    if isinstance(marker, dict) and "reasons" in marker:
                        marker = dict(marker, checkpoint_reasons=marker["reasons"])
                        del marker["reasons"]
                        reject_privileged(marker, profile, stage_ids=stage_ids)
                    else:
                        reject_privileged(marker, profile)
            else:
                reject_privileged(item, profile)
    elif isinstance(value, list):
        for item in value:
            reject_privileged(item, profile)


def reject_recorded_action_privileged(value, store, profile=ACTOR_PROFILE):
    """Bind archived action label maps to the owner's saved request, never the artifact's claim."""
    if not isinstance(value, dict):
        return reject_privileged(value, profile)
    claimed = value.get("request")
    identifier = value.get("action_id") or (claimed.get("action_id") if isinstance(claimed, dict) else None)
    if isinstance(identifier, str):
        request = store.read("action_request", identifier)
        receipt = store.read("action_receipt", identifier)
        if claimed is not None and claimed != request:
            raise ValueError("archived_action_request_mismatch")
        stage_ids = ([stage["id"] for stage in request["stages"]]
                     if request.get("type") == "BatchAction" and receipt.get("status") != "rejected" else None)
    elif isinstance(value.get("plan_id"), str):
        plan_record = store.read("unified_action_plan", value["plan_id"])
        request = plan_record.get("action", {})
        stage_ids = [stage["id"] for stage in request.get("stages", [])] if request.get("type") == "BatchAction" else None
    else:
        return reject_privileged(value, profile)
    outer = {key: item for key, item in value.items() if key != "receipt"}
    # A request's stages are a list, not the display's stage-label map.
    if "request" in outer:
        reject_privileged(outer.pop("request"), profile)
    reject_privileged(outer, profile, stage_ids=stage_ids)
    if "receipt" in value:
        reject_privileged(value["receipt"], profile, stage_ids=stage_ids)


def require_actor_observation(value, profile=None):
    declared = value.get("observation_profile", profile or ACTOR_PROFILE)
    expected = profile or declared
    actor_policy(expected)
    if declared != expected:
        raise ValueError("actor_requires_matching_observation_profile")
    if expected == LEGACY_ACTOR_PROFILE and value.get("calibration_id"):
        raise ValueError("actor_requires_unprivileged_rgb_proprio")
    reject_privileged(value, expected)
    if allows_depth(expected):
        from .contracts import validate_depth_metadata
        for camera in value.get("cameras", []):
            validate_depth_metadata(camera, "depth_path" if "depth_path" in camera else "depth_ref")


def actor_robot_state(value, profile=ACTOR_PROFILE):
    """Project robot proprioception without exposing object/contact/evaluator truth."""
    reject_privileged(value, profile)

    def fields(item, allowed):
        if not isinstance(item, dict) or set(item) - set(allowed):
            raise ValueError("non_actor_robot_state_field")
        return deepcopy(item)

    result = fields(value, ("arms", "frame_id", "tcp_config_id", "sim_step", "step_domain", "data_origin", "source_kind"))
    if not isinstance(result.get("arms"), dict) or set(result["arms"]) - {"left", "right"}:
        raise ValueError("invalid_actor_robot_arms")
    for name, state in result["arms"].items():
        state = fields(state, ("joint_names", "q_rad", "qdot_rad_s", "source_kind", "ee_pose", "gripper"))
        if profile == LEGACY_ACTOR_PROFILE:
            state.pop("qdot_rad_s", None)
            state.pop("joint_names", None)
        elif "qdot_rad_s" in state:
            finite_vector(state["qdot_rad_s"], len(state.get("q_rad", [])), "qdot_rad_s")
        if "q_rad" in state:
            finite_vector(state["q_rad"], len(state["q_rad"]), "q_rad")
        pose = fields(state.get("ee_pose", {}), ("position_m", "quaternion_xyzw", "frame_id", "tcp_config_id", "source_kind"))
        finite_vector(pose.get("position_m"), 3, "position_m")
        finite_vector(pose.get("quaternion_xyzw"), 4, "quaternion_xyzw")
        state["ee_pose"] = pose
        if "gripper" in state:
            gripper = fields(state["gripper"], ("goal_normalized", "goal_source_kind", "command_normalized",
                "command_source_kind", "joint_names", "position_m", "position_source_kind", "contact_feedback", "contact_feedback_reason"))
            if gripper.get("contact_feedback") is not None:
                raise ValueError("contact_truth_not_permitted_for_actor")
            allowed = ("goal_normalized", "goal_source_kind", "command_normalized", "command_source_kind")
            if has_calibration(profile):
                allowed += ("joint_names", "position_m", "position_source_kind")
                if "position_m" in gripper:
                    finite_vector(gripper["position_m"], len(gripper["position_m"]), "position_m")
            state["gripper"] = {key: gripper[key] for key in allowed if key in gripper}
        result["arms"][name] = state
    return result
