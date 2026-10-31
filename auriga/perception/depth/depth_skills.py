"""Opt-in RGB-D program templates using the existing single action gateway.

The module only constructs bounded programs. The harness enforces the run-level
RGB-D profile and depth_skills feature gate before admitting a program. Every
sensor query inside a program is independently gated by the gateway as well.
"""
GRASP_VERSION = "depth-grasp-lift-v1"
PLACE_VERSION = "depth-place-release-retreat-v1"
VERSIONS = frozenset((GRASP_VERSION, PLACE_VERSION))
DESCRIPTION = (
    "Disabled by default; requires explicit RGB-D observation profile and depth_skills enablement. "
    "Use skill='depth-grasp-lift-v1' or 'depth-place-release-retreat-v1'; "
    "depth_query request.kind='skill_help' lists parameters and bounds. "
    "Fixed, bounded RGB-D grasp/lift or place/release/retreat sequences use the sole action gateway. "
    "Caller supplies current observation, camera, target ROI, arm and every pose. Each action requires "
    "completed receipt, fresh measured grasp pose and supported surface correspondence before continuing. "
    "Returns compact numeric motion evidence and action/observation IDs; full calls remain in program evidence. "
    "No automatic retries, nested skills, hidden poses, contact claim or native task-success judgment. "
    "max_path_m limits requested chords, not the planner's executed physical path or native row count."
)
CATALOG = {
    "versions": sorted(VERSIONS),
    "default_enabled": False,
    "required_profile": "rgbd_proprio_calibrated_v1",
    "required_feature": "depth_skills",
    "description": DESCRIPTION,
    "common_required": ["observation_id", "camera_id", "roi", "arm", "max_path_m"],
    "common_optional": ["seed_pixel", "action_timeout_s", "timeout_s", "pose_tolerance_m", "pose_tolerance_deg",
                        "max_relative_translation_m", "max_relative_rotation_deg"],
    "pose_fields": ["grasp_position_m", "angles_deg"],
    "pose_convention": "robot_move_grasp; env_origin metres; angles_deg=[pitch,roll,yaw]",
    "grasp_required": ["approach_pose", "grasp_pose", "lift_pose"],
    "grasp_optional": ["open_goal_normalized", "close_goal_normalized", "min_lift_m"],
    "place_required": ["place_pose", "retreat_pose"],
    "place_optional": ["via_poses", "open_goal_normalized", "max_placed_translation_m",
                       "max_placed_rotation_deg", "min_separation_delta_m"],
    "defaults": {"action_timeout_s": 40, "pose_tolerance_m": .01,
                 "pose_tolerance_deg": 8, "max_relative_translation_m": .005,
                 "max_relative_rotation_deg": 8, "min_lift_m": .005,
                 "max_placed_translation_m": .005, "max_placed_rotation_deg": 8,
                 "min_separation_delta_m": .005, "open_goal_normalized": 1, "close_goal_normalized": 0},
    "adaptive_threshold_defaults": "min_lift_m/min_separation_delta_m are capped at half the requested vertical lift/retreat distance",
    "limits": {"timeout_s": 300, "max_path_m": 1, "max_via_poses": 2,
               "max_short_lift_m": .05, "max_retreat_m": .15},
    "native_task_success": "not_assessed",
}
