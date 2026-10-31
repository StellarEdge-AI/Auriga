"""Bounded, calibrated RGB-D measurements from immutable permitted observations.

Depth is optical-axis z, in metres. Point clouds stay inside this module;
actor results are small measurements and explicitly conditional hypotheses.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
import math

import cv2
import numpy as np

from ...core.contracts import IDENTITY, digest, validate_identity, validate_observation
from .depth_capture import validate_depth_archive
from ...robot.model.geometry import GeometryTools, project
from ...robot.backends.robodojo.robodojo import TCP_CONFIG_ID
from ...robot.model.roboprobe_pose import GRASP_POINT_OFFSET_M, angles_to_quat, quat_to_matrix

PROFILE = "rgbd_proprio_calibrated_v1"
KINDS = frozenset(("depth_points", "region_geometry", "grasp_candidates",
                   "place_candidates", "compare_regions"))
MAX_POINTS = 4096
APERTURE_ZERO_M = 0.000807166
MAX_OPENING_M = 0.088807166

# The numerical owner and published tool use these same bounds/defaults.
REQUEST_PARAMETERS = dict(
    observation_id=dict(type="string", minLength=1), camera_id=dict(type="string", minLength=1),
    before_observation_id=dict(type="string", minLength=1), after_observation_id=dict(type="string", minLength=1),
    frame_id=dict(type="string", enum=["env_origin"], default="env_origin"),
    arm=dict(type=["string", "null"], enum=["left", "right", None]),
    pixels=dict(type="array", minItems=1, maxItems=16,
        items=dict(type="array", minItems=2, maxItems=2, items=dict(type="number", minimum=0))),
    roi=dict(type="array", minItems=4, maxItems=4, prefixItems=[
        dict(type="integer", minimum=0), dict(type="integer", minimum=0),
        dict(type="integer", minimum=1), dict(type="integer", minimum=1)]),
    seed_pixel=dict(type="array", minItems=2, maxItems=2, items=dict(type="number", minimum=0)),
    radius_px=dict(type="integer", minimum=0, maximum=4, default=1),
    depth_tolerance_m=dict(type="number", minimum=.0005, maximum=.15, default=.015),
    rgb_tolerance=dict(type="number", minimum=1., maximum=255., default=60.),
    count=dict(type="integer", minimum=1, maximum=3, default=3),
    max_opening_m=dict(type="number", minimum=.005, maximum=MAX_OPENING_M, default=MAX_OPENING_M),
    clearance_m=dict(type="number", minimum=.001, maximum=.03, default=.005),
    grasp_depth_m=dict(type="number", minimum=0., maximum=.04, default=.01),
    approach_height_m=dict(type="number", minimum=.02, maximum=.20, default=.06),
    held_dimensions_m=dict(type="array", minItems=3, maxItems=3,
        items=dict(type="number", minimum=.002, maximum=.75)),
    grasp_offset_m=dict(type="array", minItems=3, maxItems=3,
        items=dict(type="number", minimum=-.75, maximum=.75)),
    margin_m=dict(type="number", minimum=.002, maximum=.10, default=.01),
    yaw_deg=dict(type="number", minimum=-180, maximum=180, default=0.),
    support_gap_m=dict(type="number", minimum=.001, maximum=.02, default=.003))
GRASP_FIELDS = ("count", "max_opening_m", "clearance_m", "grasp_depth_m", "approach_height_m")
PLACE_FIELDS = ("count", "held_dimensions_m", "grasp_offset_m", "margin_m", "yaw_deg", "support_gap_m")


def request_fields(kind):
    fields = ({"before_observation_id", "after_observation_id", "arm"} if kind == "compare_regions"
              else {"observation_id"})
    fields |= ({"pixels", "radius_px"} if kind == "depth_points" else
               {"roi", "seed_pixel", "depth_tolerance_m", "rgb_tolerance"})
    if kind == "grasp_candidates":
        fields.update(GRASP_FIELDS)
    elif kind == "place_candidates":
        fields.update(PLACE_FIELDS)
    return fields


def _number(value, name, low, high):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError("invalid_" + name)
    return float(value)


def _vector(value, size, name, low=-1000., high=1000.):
    if not isinstance(value, list) or len(value) != size:
        raise ValueError("invalid_" + name)
    return np.asarray([_number(x, name, low, high) for x in value])


def _integer(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError("invalid_" + name)
    return value


def _parameter(request, name):
    schema = REQUEST_PARAMETERS[name]
    value = request.get(name, schema.get("default"))
    if schema["type"] == "array":
        return _vector(value, schema["minItems"], name, schema["items"]["minimum"], schema["items"]["maximum"])
    validate = _integer if schema["type"] == "integer" else _number
    return validate(value, name, schema["minimum"], schema["maximum"])


def _roi(value, camera):
    if not isinstance(value, list) or len(value) != 4 or any(type(x) is not int for x in value):
        raise ValueError("invalid_roi")
    x, y, w, h = value
    if min(x, y) < 0 or min(w, h) < 1 or x + w > camera["width"] or y + h > camera["height"]:
        raise ValueError("roi_outside_original_image")
    if w*h > 1048576:
        raise ValueError("roi_exceeds_depth_query_pixel_budget")
    return x, y, w, h


def _pixel(value, camera):
    p = _vector(value, 2, "pixel", 0., max(camera["width"], camera["height"]))
    if p[0] > camera["width"] - 1 or p[1] > camera["height"] - 1:
        raise ValueError("pixel_outside_original_image")
    return p


def _unproject(camera, pixels, z):
    """Do not normalize rays: the sensor reports z, not distance along a ray."""
    uv = cv2.undistortPoints(np.asarray(pixels, np.float64).reshape(-1, 1, 2),
                             camera["K"], camera["D"]).reshape(-1, 2)
    local = np.column_stack((uv, np.ones(len(uv)))) * np.asarray(z)[:, None]
    return local @ camera["T"][:3, :3].T + camera["T"][:3, 3]


def _rotation_angle(rotation):
    return float(np.degrees(np.arccos(np.clip((np.trace(rotation) - 1) / 2, -1, 1))))


def _rigid(a, b):
    ca, cb = a.mean(axis=0), b.mean(axis=0)
    u, _, vt = np.linalg.svd((a-ca).T @ (b-cb))
    rotation = vt.T @ u.T
    if np.linalg.det(rotation) < 0:
        vt[-1] *= -1
        rotation = vt.T @ u.T
    return rotation, cb - rotation @ ca


def _gripper(camera, arm):
    """Measured link6 pose -> gripping-face centre, with measured opening."""
    state = camera["state"].get("arms", {}).get(arm, {})
    pose = state.get("ee_pose", {})
    if (state.get("source_kind") != "measurement" or pose.get("source_kind") != "measurement"
            or pose.get("frame_id") != "env_origin" or pose.get("tcp_config_id") != TCP_CONFIG_ID):
        return None
    try:
        xyz = _vector(pose["position_m"], 3, "ee_position")
        q = _vector(pose["quaternion_xyzw"], 4, "ee_quaternion", -1, 1)
        if abs(float(q @ q) - 1) > 1e-4:
            return None
        rotation = quat_to_matrix(np.r_[q[3], q[:3]])
        center = xyz + rotation[:, 0] * GRASP_POINT_OFFSET_M
        grip = state.get("gripper", {})
        aperture = None
        if grip.get("position_source_kind") == "measurement":
            fingers = _vector(grip["position_m"], 2, "finger_position", 0., .0441)
            aperture = APERTURE_ZERO_M + float(fingers.sum())
        return center, rotation, aperture
    except (KeyError, TypeError, ValueError):
        return None


class DepthGeometryTools:
    """Read-only measurements. Identity, profile and source checks precede cache use."""

    def __init__(self, scene_store):
        self.store = scene_store
        self.geometry = GeometryTools(scene_store)
        self._cache = OrderedDict()

    def _camera(self, request, observation_id):
        observation = self.store.observation(request, observation_id)
        validate_observation(observation)
        if any(observation.get(k) != request[k] for k in IDENTITY):
            raise ValueError("observation_identity_mismatch")
        if observation.get("observation_id") != observation_id:
            raise ValueError("observation_id_mismatch")
        if observation.get("observation_profile") != PROFILE:
            raise ValueError("depth_profile_required")
        camera = self.geometry.camera(request, observation_id, request["camera_id"])
        if camera["reference_frame_id"] != "env_origin" or request.get("frame_id", "env_origin") != "env_origin":
            raise ValueError("depth_requires_env_origin")
        frame = camera["frame"]
        if frame.get("depth_kind") != "z" or frame.get("depth_unit") != "m":
            raise ValueError("depth_requires_optical_z_metres")
        metadata, content = self.store.resolve_artifact(frame.get("depth_ref"))
        if metadata.get("access_scope") != "actor":
            raise ValueError("depth_artifact_not_actor_permitted")
        depth, valid = validate_depth_archive(content, frame)
        camera["depth"] = depth
        camera["valid"] = valid
        camera["image"] = self.geometry.image(camera)
        camera["source_digest"] = digest(observation)
        return camera

    @staticmethod
    def _sample(camera, pixel, radius=1, tolerance=.015):
        px, py = np.rint(pixel).astype(int)
        depth, valid = camera["depth"], camera["valid"]
        if not valid[py, px]:
            return None, {"valid_fraction": 0., "reason": "invalid_center_depth"}
        x0, x1 = max(0, px-radius), min(camera["width"], px+radius+1)
        y0, y1 = max(0, py-radius), min(camera["height"], py+radius+1)
        z, mask = depth[y0:y1, x0:x1], valid[y0:y1, x0:x1]
        quality = {"valid_fraction": float(mask.mean()), "depth_spread_m": float(np.ptp(z[mask]))}
        if quality["valid_fraction"] < 1 or quality["depth_spread_m"] > tolerance:
            quality["reason"] = "mixed_depth_boundary_or_invalid_neighbours"
            return None, quality
        return float(np.median(z[mask])), quality

    def _points(self, request, camera):
        pixels = request.get("pixels")
        if not isinstance(pixels, list) or not 1 <= len(pixels) <= 16:
            raise ValueError("depth_points_requires_1_to_16_pixels")
        radius = _parameter(request, "radius_px")
        rows = []
        for value in pixels:
            pixel = _pixel(value, camera)
            z, quality = self._sample(camera, pixel, radius)
            row = {"pixel": pixel.tolist(), "point_m": None, "depth_m": z,
                   "status": "uncertain" if z is None else "supported", "quality": quality}
            if z is not None:
                row["point_m"] = _unproject(camera, [pixel], [z])[0].tolist()
            rows.append(row)
        accepted = sum(row["point_m"] is not None for row in rows)
        status = "supported" if accepted == len(rows) else "partial" if accepted else "uncertain"
        return {"points": rows}, {"accepted": accepted, "requested": len(rows)}, status

    def _region(self, request, camera):
        x, y, w, h = _roi(request.get("roi"), camera)
        depth_tol = _parameter(request, "depth_tolerance_m")
        rgb_tol = _parameter(request, "rgb_tolerance")
        seed = _pixel(request.get("seed_pixel", [x + (w-1)/2, y + (h-1)/2]), camera)
        sx, sy = np.rint(seed).astype(int)
        if not x <= sx < x+w or not y <= sy < y+h:
            raise ValueError("seed_outside_roi")
        valid = camera["valid"][y:y+h, x:x+w]
        quality = {"valid_fraction": float(valid.mean()), "selected_fraction": 0., "point_count": 0}
        if not camera["valid"][sy, sx]:
            return None, quality, "invalid_seed_depth"
        z = camera["depth"][y:y+h, x:x+w]
        lab = cv2.cvtColor(camera["image"][y:y+h, x:x+w], cv2.COLOR_BGR2LAB).astype(float)
        compatible = (valid & (np.abs(z - camera["depth"][sy, sx]) <= depth_tol)
                      & (np.linalg.norm(lab-lab[sy-y, sx-x], axis=2) <= rgb_tol))
        _, labels = cv2.connectedComponents(compatible.astype(np.uint8), connectivity=4)
        mask = labels == labels[sy-y, sx-x]
        # A valid seed always belongs to foreground; never select background label 0.
        mask &= compatible
        v, u = np.nonzero(mask)
        quality.update(selected_fraction=float(mask.mean()), point_count=int(len(u)),
                       region_touches_roi=bool(mask[0].any() or mask[-1].any() or mask[:, 0].any() or mask[:, -1].any()))
        if len(u) < 16:
            return None, quality, "insufficient_connected_depth_support"
        index = np.linspace(0, len(u)-1, min(len(u), MAX_POINTS), dtype=int)
        pixels = np.column_stack((u[index]+x, v[index]+y))
        points = _unproject(camera, pixels, z[v[index], u[index]])
        center = points.mean(axis=0)
        _, singular, axes = np.linalg.svd(points-center, full_matrices=False)
        quality["fit_rms_m"] = float(np.sqrt(np.mean(((points-center) @ axes[2])**2)))
        quality["sample_count"] = len(points)
        if singular[1] < .0001:
            return None, quality, "degenerate_region_geometry"
        normal = axes[2]
        if normal[2] < 0:
            normal = -normal
        _, _, axes_xy = np.linalg.svd(points[:, :2]-center[:2], full_matrices=False)
        major = axes_xy[0]
        if major[0] < 0:
            major = -major
        minor = np.array([-major[1], major[0]])
        low, high = points.min(axis=0), points.max(axis=0)
        width = float(np.ptp(points[:, :2] @ minor))
        plane = None
        if quality["fit_rms_m"] <= .005:
            uv = (points-center) @ axes[:2].T
            plane = {"origin_m": center.tolist(), "normal": normal.tolist(),
                     "rms_m": quality["fit_rms_m"],
                     "extent_axes_m": np.ptp(uv, axis=0).tolist()}
        value = {"center_m": center.tolist(), "visible_aabb_m": {"min_m": low.tolist(), "max_m": high.tolist()},
                 "visible_dimensions_m": (high-low).tolist(), "principal_axis_xy": major.tolist(),
                 "local_width_m": width, "plane": plane}
        return {"value": value, "points": points, "mask": mask, "roi": (x, y, w, h),
                "major": major, "minor": minor, "center": center}, quality, None

    @staticmethod
    def _grasp(request, region):
        count = _parameter(request, "count")
        opening = _parameter(request, "max_opening_m")
        clearance = _parameter(request, "clearance_m")
        descent = _parameter(request, "grasp_depth_m")
        above = _parameter(request, "approach_height_m")
        points, major, minor, center = region["points"], region["major"], region["minor"], region["center"]
        longitudinal = (points[:, :2]-center[:2]) @ major
        half_span = float(np.ptp(longitudinal)) / 2
        candidates = []
        for fraction in (0., -.3, .3):
            along = fraction * half_span
            nearby = np.abs(longitudinal-along) <= max(.005, half_span*.18)
            local = points[nearby]
            if len(local) < 8:
                continue
            transverse = local[:, :2] @ minor
            width = float(np.ptp(transverse))
            margin = opening - width - 2*clearance
            if width < .002 or margin < 0:
                continue
            xy = center[:2] + along*major
            xy += ((float(transverse.min()+transverse.max())/2) - xy @ minor) * minor
            point = np.r_[xy, float(np.median(local[:, 2]))-descent]
            yaw = (math.degrees(math.atan2(minor[1], minor[0])) + 180) % 180
            angles = [0., 0., yaw]
            rotation = quat_to_matrix(angles_to_quat(*angles))
            # roboprobe reference has body +x down and body +y as the jaw axis.
            if not np.allclose(rotation[:, 0], [0, 0, -1], atol=1e-7):
                raise ValueError("top_down_convention_mismatch")
            if abs(float(rotation[:2, 1] @ minor)) < .999:
                raise ValueError("closing_axis_convention_mismatch")
            candidates.append({"grasp_position_m": point.tolist(), "angles_deg": angles,
                               "observed_width_m": width, "opening_margin_m": margin,
                               "approach_position_m": (point+[0, 0, above]).tolist()})
            if len(candidates) == count:
                break
        return {"candidates": candidates, "assumptions": [
            "Top-down pinch at the observed local section; grasp depth is a caller hypothesis.",
            "Observed width is a lower bound; unseen thickness, table clearance, contact and retention are unknown."]}

    @staticmethod
    def _place(request, camera, region):
        dimensions = _parameter(request, "held_dimensions_m")
        offset = _parameter(request, "grasp_offset_m")
        count = _parameter(request, "count")
        margin = _parameter(request, "margin_m")
        yaw = _parameter(request, "yaw_deg")
        gap = _parameter(request, "support_gap_m")
        plane = region["value"]["plane"]
        assumptions = ["Held dimensions must conservatively bound the full object in its yaw-aligned box; offset is world object-centre to grasp-centre.",
                       "Observed footprint support only; unseen obstacles, containment, contact, stability and route clearance are unknown."]
        if plane is None or plane["normal"][2] < math.cos(math.radians(15)):
            return {"candidates": [], "assumptions": assumptions}, "support_not_nearly_horizontal_plane"
        normal, origin = np.asarray(plane["normal"]), np.asarray(plane["origin_m"])
        theta = math.radians(yaw)
        rot = np.array([[math.cos(theta), -math.sin(theta)], [math.sin(theta), math.cos(theta)]])
        half = dimensions[:2]/2 + margin
        corners = np.array([[-1, -1], [1, -1], [1, 1], [-1, 1]]) * half
        corners = corners @ rot.T
        x, y, w, h = region["roi"]
        full_mask = np.zeros((camera["height"], camera["width"]), np.uint8)
        full_mask[y:y+h, x:x+w] = region["mask"].astype(np.uint8)
        span = float(np.ptp((region["points"][:, :2]-region["center"][:2]) @ region["major"]))
        candidates = []
        for fraction in (0., -.2, .2):
            xy = region["center"][:2] + fraction*span*region["major"]
            # Sample complete edges before projection: distortion need not preserve straight lines.
            boundary = np.concatenate([corners[i] + np.linspace(0, 1, 17, endpoint=False)[:, None]
                                       * (corners[(i+1) % 4]-corners[i]) for i in range(4)])
            footprint_xy = xy + boundary
            footprint_z = origin[2] - (footprint_xy-origin[:2]) @ normal[:2]/normal[2]
            footprint = np.column_stack((footprint_xy, footprint_z))
            pixels, _, visible = project(camera, footprint)
            if not visible.all():
                continue
            polygon = np.rint(pixels).astype(np.int32)
            if np.any(polygon[:, 0] >= camera["width"]) or np.any(polygon[:, 1] >= camera["height"]):
                continue
            raster = np.zeros_like(full_mask)
            cv2.fillPoly(raster, [polygon], 1)
            # Include touched border pixels instead of claiming support from rounded-inward edges.
            raster = cv2.dilate(raster, np.ones((3, 3), np.uint8), iterations=1)
            selected = raster.astype(bool)
            if int(selected.sum()) < 9 or not np.all(full_mask[selected]):
                continue
            # All projected footprint pixels, including holes, must support this plane.
            vv, uu = np.nonzero(selected)
            plane_supported = True
            for start in range(0, len(uu), MAX_POINTS):
                cu, cv = uu[start:start+MAX_POINTS], vv[start:start+MAX_POINTS]
                pp = _unproject(camera, np.column_stack((cu, cv)), camera["depth"][cv, cu])
                if np.max(np.abs((pp-origin) @ normal)) > .005:
                    plane_supported = False
                    break
            if not plane_supported:
                continue
            # A horizontal object must clear the highest support under its footprint.
            center_z = float(footprint_z.max()) + dimensions[2]/2 + gap
            object_center = np.r_[xy, center_z]
            candidates.append({"grasp_position_m": (object_center+offset).tolist(),
                               "object_center_m": object_center.tolist(), "angles_deg": [0., 0., yaw],
                               "support_gap_m": gap, "footprint_margin_m": margin})
            if len(candidates) == count:
                break
        return {"candidates": candidates, "assumptions": assumptions}, None if candidates else "footprint_not_fully_observed_on_support"

    @staticmethod
    def _track(before, after, region):
        a, b = [cv2.cvtColor(c["image"], cv2.COLOR_BGR2GRAY) for c in (before, after)]
        mask = np.zeros_like(a)
        x, y, w, h = region["roi"]
        mask[y:y+h, x:x+w] = region["mask"].astype(np.uint8)*255
        points = cv2.goodFeaturesToTrack(a, maxCorners=120, qualityLevel=.02, minDistance=4, mask=mask)
        if points is None:
            return np.empty((0, 2)), np.empty((0, 2)), {"detected_count": 0, "tracked_fraction": 0.}
        target, status, error = cv2.calcOpticalFlowPyrLK(a, b, points, None, winSize=(21, 21), maxLevel=4)
        if target is None:
            return np.empty((0, 2)), np.empty((0, 2)), {"detected_count": len(points), "tracked_fraction": 0.}
        back, reverse, _ = cv2.calcOpticalFlowPyrLK(b, a, target, None, winSize=(21, 21), maxLevel=4)
        if back is None:
            return np.empty((0, 2)), np.empty((0, 2)), {"detected_count": len(points), "tracked_fraction": 0.}
        pa, pb = points.reshape(-1, 2), target.reshape(-1, 2)
        fb = np.linalg.norm(back.reshape(-1, 2)-pa, axis=1)
        good = ((status.ravel() == 1) & (reverse.ravel() == 1) & (fb < .75) & (error.ravel() < 20)
                & np.isfinite(pb).all(axis=1) & (pb[:, 0] >= 1) & (pb[:, 0] < before["width"]-1)
                & (pb[:, 1] >= 1) & (pb[:, 1] < before["height"]-1))
        return pa[good], pb[good], {"detected_count": len(points), "tracked_fraction": float(good.mean()),
                                  "max_forward_backward_error_px": float(fb[good].max()) if good.any() else None}

    def _compare(self, request, before, after):
        empty = {"world_displacement_m": None, "world_rotation_deg": None, "lift_m": None,
                 "relative_gripper_displacement_m": None, "relative_gripper_rotation_deg": None,
                 "gripper_separation_delta_m": None, "gripper_aperture_before_m": None,
                 "gripper_aperture_after_m": None, "support_gap_m": None, "after_roi": None,
                 "after_seed_pixel": None, "evidence": "unknown"}
        if before["image"].shape != after["image"].shape:
            raise ValueError("compare_requires_equal_image_sizes")
        if (before["observation"].get("clock_domain") != after["observation"].get("clock_domain")
                or before["observation"].get("step_domain") != after["observation"].get("step_domain")
                or after["observation"]["sim_step"] <= before["observation"]["sim_step"]):
            raise ValueError("compare_requires_ordered_same_clock_observations")
        arm = request.get("arm")
        if arm is not None and arm not in ("left", "right"):
            raise ValueError("invalid_arm")
        region, quality, reason = self._region(request, before)
        quality.update(correspondence_count=0, inlier_count=0)
        if region is None:
            return empty, quality, "uncertain", reason
        pa, pb, track_quality = self._track(before, after, region)
        quality.update(track_quality)
        points_a, points_b, pixels_a, pixels_b = [], [], [], []
        for a, b in zip(pa, pb):
            za, _ = self._sample(before, a)
            zb, _ = self._sample(after, b)
            if za is not None and zb is not None:
                pixels_a.append(a); pixels_b.append(b)
                points_a.append(_unproject(before, [a], [za])[0])
                points_b.append(_unproject(after, [b], [zb])[0])
        a, b = np.asarray(points_a), np.asarray(points_b)
        quality["correspondence_count"] = len(a)
        if len(a) < 6 or quality["tracked_fraction"] < .55:
            return empty, quality, "uncertain", "insufficient_rgb_depth_correspondence_or_occlusion"
        rng, best = np.random.default_rng(0), np.zeros(len(a), bool)
        for _ in range(96):
            chosen = rng.choice(len(a), 3, replace=False)
            if np.linalg.norm(np.cross(a[chosen[1]]-a[chosen[0]], a[chosen[2]]-a[chosen[0]])) < 1e-7:
                continue
            rotation, translation = _rigid(a[chosen], b[chosen])
            inliers = np.linalg.norm(a @ rotation.T+translation-b, axis=1) <= .006
            if inliers.sum() > best.sum():
                best = inliers
        quality["inlier_count"] = int(best.sum())
        quality["inlier_fraction"] = float(best.mean())
        if best.sum() < 6 or best.mean() < .75:
            return empty, quality, "uncertain", "nonrigid_or_inconsistent_correspondences"
        aa, bb = a[best], b[best]
        for cloud in (aa, bb):
            singular = np.linalg.svd(cloud-cloud.mean(axis=0), compute_uv=False)
            if singular[1]/math.sqrt(len(cloud)) < .003:
                return empty, quality, "uncertain", "degenerate_correspondence_geometry"
        rotation, translation = _rigid(aa, bb)
        residual = np.linalg.norm(aa @ rotation.T+translation-bb, axis=1)
        quality["fit_rms_m"] = float(np.sqrt(np.mean(residual**2)))
        displacement = (bb-aa).mean(axis=0)
        ca, cb = aa.mean(axis=0), bb.mean(axis=0)
        value = {**empty, "world_displacement_m": displacement.tolist(),
                 "world_rotation_deg": _rotation_angle(rotation), "lift_m": float(displacement[2]),
                 "evidence": "rigid_surface_motion"}
        # Affine image warp of the original ROI retains context, not just feature hull.
        pixels_a, pixels_b = np.asarray(pixels_a)[best], np.asarray(pixels_b)[best]
        affine, _ = cv2.estimateAffinePartial2D(pixels_a, pixels_b, method=cv2.RANSAC, ransacReprojThreshold=2.)
        if affine is None:
            return empty, quality, "uncertain", "cannot_update_region_correspondence"
        x, y, w, h = region["roi"]
        corners = np.array([[x, y, 1], [x+w-1, y, 1], [x+w-1, y+h-1, 1], [x, y+h-1, 1]]) @ affine.T
        lo = np.floor(corners.min(axis=0)).astype(int)
        hi = np.ceil(corners.max(axis=0)).astype(int)
        clipped_lo, clipped_hi = np.maximum(lo, [0, 0]), np.minimum(hi, [after["width"]-1, after["height"]-1])
        if np.any(clipped_lo != lo) or np.any(clipped_hi != hi):
            return empty, quality, "uncertain", "tracked_region_leaves_image"
        value["after_roi"] = [int(lo[0]), int(lo[1]), int(hi[0]-lo[0]+1), int(hi[1]-lo[1]+1)]
        seed_a = np.asarray(request.get("seed_pixel", [x+(w-1)/2, y+(h-1)/2]), float)
        seed_b = affine @ np.r_[seed_a, 1.]
        if (np.any(seed_b < 1) or seed_b[0] >= after["width"]-1 or seed_b[1] >= after["height"]-1):
            return empty, quality, "uncertain", "tracked_seed_leaves_image"
        za, _ = self._sample(before, seed_a)
        zb, _ = self._sample(after, seed_b)
        if za is None or zb is None:
            return empty, quality, "uncertain", "tracked_seed_depth_unavailable"
        seed_world_a = _unproject(before, [seed_a], [za])[0]
        seed_world_b = _unproject(after, [seed_b], [zb])[0]
        quality["seed_rigid_residual_m"] = float(np.linalg.norm(rotation @ seed_world_a+translation-seed_world_b))
        if quality["seed_rigid_residual_m"] > .01:
            return empty, quality, "uncertain", "tracked_seed_inconsistent_with_target"
        after_region, _, after_reason = self._region({**request, "roi": value["after_roi"],
                                                       "seed_pixel": seed_b.tolist()}, after)
        if after_region is None:
            return empty, quality, "uncertain", "tracked_seed_region_unavailable:" + after_reason
        ax, ay, aw, ah = after_region["roi"]
        local = np.rint(pixels_b).astype(int)-[ax, ay]
        inside = (local[:, 0] >= 0) & (local[:, 1] >= 0) & (local[:, 0] < aw) & (local[:, 1] < ah)
        matched = np.zeros(len(local), bool)
        matched[inside] = after_region["mask"][local[inside, 1], local[inside, 0]]
        quality["seed_region_correspondence_fraction"] = float(matched.mean())
        if matched.mean() < .75:
            return empty, quality, "uncertain", "tracked_seed_region_no_longer_matches_target"
        value["after_seed_pixel"] = seed_b.tolist()
        if arm:
            ga, gb = _gripper(before, arm), _gripper(after, arm)
            quality["gripper_pose_supported"] = ga is not None and gb is not None
            if ga is not None and gb is not None:
                xa, ra, aperture_a = ga; xb, rb, aperture_b = gb
                relative_a, relative_b = (ca-xa) @ ra, (cb-xb) @ rb
                value.update(relative_gripper_displacement_m=(relative_b-relative_a).tolist(),
                             relative_gripper_rotation_deg=_rotation_angle(rb.T @ rotation @ ra),
                             gripper_separation_delta_m=float(np.linalg.norm(cb-xb)-np.linalg.norm(ca-xa)),
                             gripper_aperture_before_m=aperture_a, gripper_aperture_after_m=aperture_b)
            else:
                quality["gripper_pose_reason"] = "missing_measured_link6_pose_or_matching_tcp"
        quality["camera_translation_m"] = float(np.linalg.norm(after["T"][:3, 3]-before["T"][:3, 3]))
        quality["camera_rotation_deg"] = _rotation_angle(after["T"][:3, :3] @ before["T"][:3, :3].T)
        return value, quality, "supported", None

    def execute(self, request):
        if not isinstance(request, dict):
            raise ValueError("depth_request_requires_object")
        validate_identity({**request, "schema_version": request.get("schema_version", "0.1")})
        kind = request.get("kind")
        if kind not in KINDS:
            raise ValueError("unsupported_depth_query_kind")
        common = set(IDENTITY) | {"schema_version", "kind", "camera_id", "frame_id"}
        fields = request_fields(kind)
        if request.keys() - common - fields:
            raise ValueError("unsupported_depth_query_fields")
        # Invalid candidate commands are errors even if sensor evidence is unavailable.
        if "count" in request:
            _parameter(request, "count")
        if kind == "grasp_candidates":
            for name in GRASP_FIELDS[1:]:
                if name in request:
                    _parameter(request, name)
        elif kind == "place_candidates":
            _parameter(request, "held_dimensions_m")
            _parameter(request, "grasp_offset_m")
            for name in PLACE_FIELDS[3:]:
                if name in request:
                    _parameter(request, name)
        if not isinstance(request.get("camera_id"), str) or not request["camera_id"]:
            raise ValueError("invalid_camera_id")
        ids = ([request.get("before_observation_id"), request.get("after_observation_id")]
               if kind == "compare_regions" else [request.get("observation_id")])
        if any(not isinstance(item, str) or not item for item in ids):
            raise ValueError("invalid_observation_id")
        cameras = [self._camera(request, item) for item in ids]
        key = digest({"request": request, "sources": [c["source_digest"] for c in cameras]})
        if key in self._cache:
            self._cache.move_to_end(key)
            return deepcopy(self._cache[key])
        result = {"schema_version": "0.1", **{k: request[k] for k in IDENTITY}, "kind": kind,
                  "status": "supported", "observation_ids": ids, "camera_id": request["camera_id"],
                  "frame_id": "env_origin", "unit": "m", "value": None, "quality": {},
                  "unknowns": ["Unseen surfaces, contact forces and task success."]}
        if kind == "compare_regions":
            result.update(before_observation_id=ids[0], after_observation_id=ids[1])
            value, quality, status, reason = self._compare(request, *cameras)
            result["unknowns"].append("RGB correspondence can be ambiguous under occlusion or repeated texture; rigid visible motion is not grasp proof.")
        else:
            result["observation_id"] = ids[0]
            reason = None
            if kind == "depth_points":
                value, quality, status = self._points(request, cameras[0])
            else:
                region, quality, reason = self._region(request, cameras[0])
                value, status = None, "uncertain"
                if region is not None:
                    if kind == "region_geometry":
                        value = region["value"]
                        status = "partial" if quality["region_touches_roi"] else "supported"
                        result["unknowns"].append("Visible dimensions are lower bounds, not complete object dimensions.")
                    elif kind == "grasp_candidates":
                        value = self._grasp(request, region)
                        status = "partial" if value["candidates"] else "uncertain"
                        if not value["candidates"]:
                            reason = "no_observed_section_fits_requested_aperture"
                    else:
                        value, reason = self._place(request, cameras[0], region)
                        status = "partial" if value["candidates"] else "uncertain"
        result.update(value=value, quality=quality, status=status)
        if reason:
            result["reason"] = reason
        # Validate JSON finiteness and make mutable caller edits independent of cache.
        digest(result)
        self._cache[key] = deepcopy(result)
        while len(self._cache) > 32:
            self._cache.popitem(last=False)
        return result
