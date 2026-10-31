"""Explicit X5 finger contact references, without object/contact inference.

The legacy reference is the distal flat pad center. A contact selection changes
which axial cross-section is placed at the caller's point, not the robot TCP or
the native planner. All references lie between the opposing inner finger faces.
"""

import math

import numpy as np

from . import roboprobe_pose


CONTACT_FIELDS = frozenset({"contact_region", "contact_depth_m"})
CONTACT_DEPTH_PRESETS_M = {"pad_center": 0.0, "finger_middle": 0.0275, "fingertip": -0.0075}
MIN_CONTACT_DEPTH_M = -0.0075
MAX_CONTACT_DEPTH_M = 0.0625


def _depth(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("invalid_contact_depth_m: requires a finite number")
    if not MIN_CONTACT_DEPTH_M <= value <= MAX_CONTACT_DEPTH_M:
        raise ValueError("invalid_contact_depth_m: outside exposed inner finger span")
    return float(value)


def _validated_spec(spec):
    depth = _depth(spec["contact_depth_m"])
    region = spec["contact_region"]
    if not isinstance(region, str) or (region != "custom" and (region not in CONTACT_DEPTH_PRESETS_M or depth != CONTACT_DEPTH_PRESETS_M[region])):
        raise ValueError("invalid_contact_region_spec")
    offset = roboprobe_pose.GRASP_POINT_OFFSET_M - depth
    if type(spec.get("offset_m")) not in (int, float) or not math.isfinite(spec["offset_m"]) or abs(spec["offset_m"] - offset) > 1e-12:
        raise ValueError("invalid_contact_offset_spec")
    return dict(contact_region=region, contact_depth_m=depth, offset_m=offset)


def resolve_contact_spec(selection, inherited=None):
    """Resolve a pose's explicit choice, otherwise inherit the request choice.

    Pose fields unrelated to contact selection are deliberately ignored. An
    explicit per-waypoint choice replaces either kind of inherited selection.
    """
    if not isinstance(selection, dict):
        raise ValueError("invalid_contact_selection")
    selected = CONTACT_FIELDS.intersection(selection)
    if len(selected) > 1:
        raise ValueError("contact_region_and_contact_depth_m_are_mutually_exclusive")
    if "contact_region" in selected:
        region = selection["contact_region"]
        if not isinstance(region, str) or region not in CONTACT_DEPTH_PRESETS_M:
            raise ValueError("invalid_contact_region")
        depth = CONTACT_DEPTH_PRESETS_M[region]
    elif "contact_depth_m" in selected:
        region, depth = "custom", _depth(selection["contact_depth_m"])
    elif inherited is not None:
        return _validated_spec(inherited)
    else:
        region, depth = "pad_center", 0.0
    return dict(contact_region=region, contact_depth_m=depth,
                offset_m=roboprobe_pose.GRASP_POINT_OFFSET_M - depth)


def values_to_contact_pose(values, spec):
    """Convert the selected cross-section's world target to native link6.

    Positive depth moves the flange forward along the TARGET tool axis. The
    exact legacy conversion remains unchanged when the depth is zero.
    """
    spec = _validated_spec(spec)
    pose = roboprobe_pose.values_to_pose(values)
    if spec["contact_depth_m"]:
        pose[:3] += spec["contact_depth_m"] * roboprobe_pose.tool_axis(pose[3:])
    return pose


def contact_capabilities():
    """Static disclosed robot geometry; no object state or grip guarantee."""
    return dict(
        default_reference="pad_center",
        regions={name: resolve_contact_spec({"contact_region": name}) for name in CONTACT_DEPTH_PRESETS_M},
        contact_depth_m=dict(min=MIN_CONTACT_DEPTH_M, max=MAX_CONTACT_DEPTH_M,
                             positive_direction="from distal pad toward palm along local -x"),
        inner_face_axial_span_from_link6_m=[0.08757, 0.15757],
        distal_flat_pad_axial_span_from_link6_m=[0.14257, 0.15757],
        inner_shank_slope_deg=8.735,
        semantics="grasp_position_m targets the selected cross-section between fingers; omitted selection uses the distal 15 mm pad center. finger_middle is the nominal midpoint of the exposed inner face, on its sloped portion. References do not certify clearance, contact or retention.",
        observation_reference="robot_state.arms[arm].grasp_pose always remains pad_center; receipt.grasp_contact_feedback reports the selected reference from the terminal measured link6 pose.",
    )


def _vector(value, size):
    array = np.asarray(value, dtype=float)
    if array.shape != (size,) or not np.isfinite(array).all():
        raise ValueError("invalid_contact_feedback_pose")
    return array


def contact_target_feedback(action, robot_state):
    """Compare final requested contact references to terminal proprioception.

    Final targets may be unattempted after a partial/failed request. This is
    explicitly a target comparison, never an arrival or object-contact claim.
    Use the caller's permitted measured state, not planner predictions.
    """
    metadata = action.get("input_grasp_pose")
    if not isinstance(metadata, dict):
        return {}
    points = action.get("path", {}).get("waypoints", [])
    if not points:
        return {}
    output = {}
    # Independent routes can end at different indices. The shorter arm still
    # needs its terminal measured comparison, even when absent from the tail.
    for arm in dict.fromkeys(arm for point in points for arm in point):
        grasp = metadata.get(arm) if arm in metadata else metadata
        if not isinstance(grasp, dict) or "position_m" not in grasp:
            continue
        frames = grasp.get("waypoint_contact_frames")
        if frames:
            spec = _validated_spec(frames[-1])
        else:
            # Legacy requests always use the pad. Reject inconsistent metadata
            # rather than silently describing a different target reference.
            spec = resolve_contact_spec({})
            if abs(grasp.get("offset_m", spec["offset_m"]) - spec["offset_m"]) > 1e-12:
                raise ValueError("contact_feedback_missing_selected_frame")
        target = _vector(grasp["position_m"], 3)
        angles = _vector(grasp["angles_deg"], 3)
        wanted_q = roboprobe_pose.angles_to_quat(*angles)
        target_axis = roboprobe_pose.tool_axis(wanted_q)
        entry = dict(**spec, requested_contact_position_m=target.tolist(), available=False)
        pose = robot_state.get("arms", {}).get(arm, {}).get("ee_pose", {})
        if (pose.get("source_kind") != "measurement" or pose.get("tcp_config_id") != "arx-x5-link6-v1"
                or pose.get("frame_id") != action.get("frame_id", "env_origin")):
            entry["unavailable_reason"] = "terminal_measured_link6_unavailable"
            output[arm] = entry
            continue
        ee_position = _vector(pose["position_m"], 3)
        xyzw = _vector(pose["quaternion_xyzw"], 4)
        actual_q = np.array([xyzw[3], *xyzw[:3]])
        actual = ee_position + spec["offset_m"] * roboprobe_pose.tool_axis(actual_q)
        error = actual - target
        entry.update(available=True, actual_contact_position_m=actual.tolist(),
                     position_error_vector_m=error.tolist(), position_error_m=float(np.linalg.norm(error)),
                     orientation_error_rad=math.radians(roboprobe_pose.orientation_error_deg(wanted_q, actual_q)),
                     approach_shortfall_m=float(np.dot(-error, target_axis)))
        output[arm] = entry
    if not output:
        return {}
    return dict(target_scope="requested_final_pose_not_completion_claim",
                source_kind="derived_from_terminal_measured_link6", frame_id=action.get("frame_id", "env_origin"),
                arms=output,
                interpretation="Positive approach_shortfall_m means the selected reference remains behind its requested final target along the requested approach axis. The final target may not have been attempted after failure/stop. Robot geometry and pose feedback do not establish object contact or retention.")
