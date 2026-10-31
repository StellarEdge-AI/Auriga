"""ARX request compilation from explicit identity/capability/argument values."""
from ...core.contracts import new_id
from ...core.action_validation import finite_vector, validate_gripper_motion
from ..model import roboprobe_pose
from ..model.grasp_contact import resolve_contact_spec, values_to_contact_pose
from .motion_speed import SPEED_INPUT_FIELDS

DEFAULT_ACTION_TIMEOUT_S = 120.0
DEFAULT_WAIT_TIMEOUT_S = 30.0

CONTACT_FIELDS = ("contact_region", "contact_depth_m")


def compile_action(kind, args, identity, capabilities):
    """Build a request from explicit data, without reading live state or executing."""
    if kind not in {"move", "grasp", "gripper", "wait"}:
        raise ValueError("unsupported_arx_action_kind")
    if "dual_arm_progression" in args:
        raise ValueError("dual_arm_progression_requires_dual_arm_motion")
    if kind != "grasp" and any(field in args for field in CONTACT_FIELDS):
        raise ValueError("contact_selection_requires_grasp_pose")
    grasp_input = None
    waypoints = []
    if kind == "grasp":
        if capabilities["tcp_config_id"] != "arx-x5-link6-v1": raise ValueError("unsupported_grasp_tcp")
        via = args.get("via_grasp_poses", [])
        if not isinstance(via, list) or len(via) > 7: raise ValueError("invalid_via_grasp_poses: at most seven intermediate poses")
        pose_fields = {"grasp_position_m", "angles_deg"}
        if any(not isinstance(point, dict) or not pose_fields.issubset(point)
               or set(point) - pose_fields - set(CONTACT_FIELDS) for point in via):
            raise ValueError("invalid_via_grasp_pose: requires grasp_position_m, angles_deg and optional contact selection")
        contact = resolve_contact_spec(args)
        contact_frames = []
        for point in [*via, args]:
            finite_vector(point.get("grasp_position_m"), 3, "grasp_position_m")
            finite_vector(point.get("angles_deg"), 3, "angles_deg")
            for value, (lower, upper) in zip(point["angles_deg"], roboprobe_pose.ANGLE_BOUNDS.values()):
                if not lower <= value <= upper: raise ValueError("grasp_angle_out_of_bounds")
            values = dict(zip((*roboprobe_pose.POSITION_AXES, *roboprobe_pose.ANGLE_AXES),
                              [*point["grasp_position_m"], *point["angles_deg"]]))
            selected_contact = resolve_contact_spec(point, inherited=contact)
            contact_frames.append(selected_contact)
            pose = values_to_contact_pose(values, selected_contact).tolist()
            waypoints.append({args["arm"]: dict(position_m=pose[:3], quaternion_xyzw=[*pose[4:], pose[3]])})
        grasp_input = dict(position_m=args["grasp_position_m"], angles_deg=args["angles_deg"],
                           convention="roboprobe_top_down_world_xyz_degrees", offset_m=contact["offset_m"])
        if any(field in point for point in [*via, args] for field in CONTACT_FIELDS):
            grasp_input.update(contact_region=contact["contact_region"], contact_depth_m=contact["contact_depth_m"],
                contact_selection={field: args[field] for field in CONTACT_FIELDS if field in args},
                waypoint_contact_frames=contact_frames)
        if via: grasp_input["via_grasp_poses"] = via
    elif kind not in ("gripper", "wait"):
        waypoints = [{args["arm"]: dict(position_m=args["position_m"], quaternion_xyzw=args["quaternion_xyzw"])}]
    action = dict(schema_version="0.1", **identity, world_kind="observed",
                  action_id=args.get("action_id", new_id("action")), based_on_observation_id=args["observation_id"],
                  frame_id=capabilities["frame_id"], tcp_config_id=capabilities["tcp_config_id"], mode="absolute",
                  path=dict(kind="hold" if kind in ("gripper", "wait") else "backend_free", orientation="backend_free", blend_radius_m=0.0, position_tolerance_m=0.005, waypoints=waypoints),
                  gripper_events=[], limits_profile=capabilities.get("limits_profile", "simulation-slow-v1"), timeout_s=args.get("timeout_s", DEFAULT_WAIT_TIMEOUT_S if kind == "wait" else DEFAULT_ACTION_TIMEOUT_S))
    for key in SPEED_INPUT_FIELDS:
        if key in args: action[key] = args[key]
    if grasp_input is not None: action["input_grasp_pose"] = grasp_input
    if kind == "gripper":
        action["gripper_events"] = [dict(arm=args["arm"], phase="after_motion", goal_normalized=args["goal_normalized"])]
    if kind == "wait":
        action["hold_native_steps"] = args["native_steps"]
    if "gripper_goal_normalized" in args:
        action["gripper_events"] = [dict(arm=args["arm"], phase="after_motion", goal_normalized=args["gripper_goal_normalized"])]
    if "gripper_motion" in args:
        if kind not in ("move", "grasp") or "gripper_goal_normalized" not in args:
            raise ValueError("invalid_gripper_motion: requires a move and gripper_goal_normalized")
        timing = validate_gripper_motion(args["gripper_motion"])
        action["gripper_events"][0].update(phase="during_motion", **timing)
    return action
