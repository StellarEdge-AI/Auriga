"""Calibrated RGB geometry. Optical axes are right/down/forward; distances are metres."""
from __future__ import annotations

import hashlib
import itertools
import json
import math
import time
from copy import deepcopy
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from ...core.contracts import IDENTITY, digest, validate_identity
from ...core.storage.images import encode_image, image_settings

ALGORITHM = "opencv-calibrated-sparse-v1"
IMPLEMENTATION_DIGEST = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def array(value, shape=None):
    result = np.asarray(value, dtype=np.float64)
    if (shape is not None and result.shape != shape) or not np.isfinite(result).all():
        raise ValueError("invalid_finite_array")
    return result


def transform(value):
    matrix = array(value, (4, 4))
    if not np.allclose(matrix[3], [0, 0, 0, 1], atol=1e-7) or not np.allclose(matrix[:3, :3].T @ matrix[:3, :3], np.eye(3), atol=1e-5) or abs(np.linalg.det(matrix[:3, :3]) - 1) > 1e-5:
        raise ValueError("invalid_rigid_transform")
    return matrix


def fk(model, state):
    """Evaluate a disclosed serial-joint manifest without importing a simulator."""
    joints = state.get("joints", {})
    names = joints.get("names", state.get("joint_names", []))
    values = joints.get("position_rad", joints.get("q", state.get("q_rad", state.get("joint_positions_rad", []))))
    source = joints.get("source_kind", state.get("source_kind"))
    if source not in ("measurement", "estimate") or len(names) != len(values) or len(set(names)) != len(names):
        raise ValueError("fk_requires_measured_or_estimated_named_joints")
    positions = dict(zip(names, values))
    poses = {model.get("base_link", "base"): transform(model.get("T_world_base", np.eye(4)))}
    for joint in model["joints"]:
        parent = poses[joint["parent"]]
        origin = transform(joint["T_parent_joint"])
        motion = np.eye(4)
        kind = joint["type"]
        if kind != "fixed":
            mimic = joint.get("mimic")
            value = float(positions[mimic["joint"]])*mimic["multiplier"]+mimic["offset"] if mimic else float(positions[joint["name"]])
            axis = array(joint["axis"], (3,))
            norm = np.linalg.norm(axis)
            if norm < 1e-9 or not math.isfinite(value):
                raise ValueError("invalid_joint_state_or_axis")
            axis /= norm
            if kind in ("revolute", "continuous"):
                motion[:3, :3] = cv2.Rodrigues(axis * value)[0]
            elif kind == "prismatic":
                motion[:3, 3] = axis * value
            else:
                raise ValueError("unsupported_joint_type")
        poses[joint["child"]] = parent @ origin @ motion
    return poses


def camera_fk(model, state, calibration, sample):
    arm=calibration.get("arm")
    if arm:
        state=deepcopy(state["arms"][arm])
        state.update(joint_positions_rad=state.pop("q_rad")+state["gripper"]["position_m"],joint_names=state["joint_names"]+state["gripper"]["joint_names"])
    base=sample.get("T_world_base",calibration.get("T_world_base",model.get("T_world_base")))
    if base is None: raise ValueError("missing_robot_base_transform")
    poses=fk({**model,"T_world_base":base},state)
    return poses[calibration["link_name"]] @ transform(calibration["T_link_camera"]),poses


def project(camera, points):
    points = array(points).reshape(-1, 3)
    if not 1<=len(points)<=2000: raise ValueError("invalid_projection_point_count")
    inverse = np.linalg.inv(camera["T"])
    local = points @ inverse[:3, :3].T + inverse[:3, 3]
    pixels = cv2.projectPoints(points, cv2.Rodrigues(inverse[:3, :3])[0], inverse[:3, 3], camera["K"], camera["D"])[0].reshape(-1, 2)
    visible = (local[:, 2] > 0) & (pixels[:, 0] >= 0) & (pixels[:, 0] < camera["width"]) & (pixels[:, 1] >= 0) & (pixels[:, 1] < camera["height"])
    return pixels, local[:, 2], visible


def ray(camera, pixels):
    pixels = array(pixels).reshape(-1, 1, 2)
    normalized = cv2.undistortPoints(pixels, camera["K"], camera["D"]).reshape(-1, 2)
    directions = np.column_stack((normalized, np.ones(len(normalized)))) @ camera["T"][:3, :3].T
    return directions / np.linalg.norm(directions, axis=1)[:, None]


def triangulate(first, second, pixels_a, pixels_b, *, min_angle_deg=0.5, max_error_px=2.0):
    if not (0 < min_angle_deg < 90 and 0 < max_error_px <= 100):
        raise ValueError("invalid_triangulation_threshold")
    a, b = array(pixels_a).reshape(-1, 2), array(pixels_b).reshape(-1, 2)
    if len(a) != len(b) or not 0 < len(a) <= 2000:
        raise ValueError("correspondence_shape_mismatch")
    for camera,pixels in ((first,a),(second,b)):
        if np.any(pixels[:,0]<0) or np.any(pixels[:,0]>=camera["width"]) or np.any(pixels[:,1]<0) or np.any(pixels[:,1]>=camera["height"]):
            raise ValueError("correspondence_outside_image")
    baseline = float(np.linalg.norm(first["T"][:3, 3] - second["T"][:3, 3]))
    if baseline < 1e-5:
        raise ValueError("insufficient_parallax:zero_translation_or_pure_rotation")
    ua = cv2.undistortPoints(a.reshape(-1, 1, 2), first["K"], first["D"]).reshape(-1, 2)
    ub = cv2.undistortPoints(b.reshape(-1, 1, 2), second["K"], second["D"]).reshape(-1, 2)
    relative=np.linalg.inv(second["T"]) @ first["T"]
    tx,ty,tz=relative[:3,3]
    essential=np.array([[0,-tz,ty],[tz,0,-tx],[-ty,tx,0]]) @ relative[:3,:3]
    ha,hb=np.column_stack((ua,np.ones(len(ua)))),np.column_stack((ub,np.ones(len(ub))))
    ea,eb=ha@essential.T,hb@essential
    denominator=np.sqrt(np.sum(ea[:,:2]**2,axis=1)+np.sum(eb[:,:2]**2,axis=1))
    epipolar=np.abs(np.sum(hb*ea,axis=1))/np.maximum(denominator,1e-12)
    homogeneous = cv2.triangulatePoints(np.linalg.inv(first["T"])[:3], np.linalg.inv(second["T"])[:3], ua.T, ub.T)
    finite = np.abs(homogeneous[3]) > 1e-10
    points = np.zeros((len(a), 3))
    points[finite] = (homogeneous[:3, finite] / homogeneous[3, finite]).T
    pa, za, _ = project(first, points)
    pb, zb, _ = project(second, points)
    errors = np.maximum(np.linalg.norm(pa - a, axis=1), np.linalg.norm(pb - b, axis=1))
    angles = np.degrees(np.arccos(np.clip(np.sum(ray(first, a) * ray(second, b), axis=1), -1, 1)))
    valid = finite & (za > 0) & (zb > 0) & (angles >= min_angle_deg) & (errors <= max_error_px)
    reasons = ["accepted" if valid[i] else "point_at_infinity" if not finite[i] else "negative_depth" if za[i] <= 0 or zb[i] <= 0 else "insufficient_parallax" if angles[i] < min_angle_deg else "inconsistent_correspondence" for i in range(len(a))]
    return {"points_m": points[valid].tolist(), "accepted_indices": np.flatnonzero(valid).tolist(), "reprojection_error_px": errors.tolist(), "parallax_deg": angles.tolist(), "point_status": reasons, "baseline_m": baseline, "epipolar_sampson_error_normalized":epipolar.tolist(), "accepted": int(valid.sum()), "total": len(a), "assumptions": ["Correspondences refer to the same physical points; scene is static between captures."], "thresholds": {"min_angle_deg": min_angle_deg, "max_error_px": max_error_px}}


def sparse_track(image_a, image_b, roi=None, max_points=400, method="lk"):
    if image_a.shape != image_b.shape and method == "lk":
        raise ValueError("tracking_requires_equal_image_sizes")
    a, b = [cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) for image in (image_a, image_b)]
    mask = np.zeros_like(a)
    if roi is None:
        mask[:] = 255
    else:
        x, y, w, h = map(int, roi)
        if min(x, y) < 0 or min(w, h) <= 0 or x + w > a.shape[1] or y + h > a.shape[0]:
            raise ValueError("invalid_roi")
        mask[y:y+h, x:x+w] = 255
    blur = [float(cv2.Laplacian(image, cv2.CV_64F).var()) for image in (a, b)]
    if min(blur)<2:
        return {"pixels_a":[],"pixels_b":[],"accepted":0,"reason":"insufficient_texture_or_blur","laplacian_variance":blur}
    if method == "sift":
        detector=cv2.SIFT_create(nfeatures=min(int(max_points)*5,4000))
        ka,da=detector.detectAndCompute(a,mask)
        kb,db=detector.detectAndCompute(b,None)
        if da is None or db is None or min(len(da),len(db))<2:
            return {"pixels_a":[],"pixels_b":[],"accepted":0,"reason":"insufficient_texture","method":"sift_ratio_mutual"}
        matcher=cv2.BFMatcher(cv2.NORM_L2)
        forward=matcher.knnMatch(da,db,k=2)
        reverse=matcher.knnMatch(db,da,k=2)
        back={pair[0].queryIdx:pair[0].trainIdx for pair in reverse if len(pair)==2 and pair[0].distance<0.75*pair[1].distance}
        matches=[pair[0] for pair in forward if len(pair)==2 and pair[0].distance<0.75*pair[1].distance and back.get(pair[0].trainIdx)==pair[0].queryIdx]
        matches=sorted(matches,key=lambda item:item.distance)[:max_points]
        return {"pixels_a":[list(ka[item.queryIdx].pt) for item in matches],"pixels_b":[list(kb[item.trainIdx].pt) for item in matches],"accepted":len(matches),"descriptor_distance":[float(item.distance) for item in matches],"method":"sift_ratio_mutual","ratio_threshold":0.75,"laplacian_variance":blur}
    if method != "lk": raise ValueError("unsupported_tracking_method")
    points = cv2.goodFeaturesToTrack(a, maxCorners=min(int(max_points), 2000), qualityLevel=0.01, minDistance=5, mask=mask)
    if points is None or min(blur) < 2:
        return {"pixels_a": [], "pixels_b": [], "accepted": 0, "reason": "insufficient_texture_or_blur", "laplacian_variance": blur}
    target, status, error = cv2.calcOpticalFlowPyrLK(a, b, points, None, winSize=(31,31), maxLevel=4)
    back, reverse, _ = cv2.calcOpticalFlowPyrLK(b, a, target, None, winSize=(31,31), maxLevel=4)
    fb = np.linalg.norm(back.reshape(-1, 2) - points.reshape(-1, 2), axis=1)
    target2 = target.reshape(-1, 2)
    valid = (status.ravel() == 1) & (reverse.ravel() == 1) & (fb < 1.0) & (error.ravel() < 30) & (target2[:, 0] >= 0) & (target2[:, 0] < a.shape[1]) & (target2[:, 1] >= 0) & (target2[:, 1] < a.shape[0])
    return {"pixels_a": points.reshape(-1, 2)[valid].tolist(), "pixels_b": target2[valid].tolist(), "accepted": int(valid.sum()), "detected": len(points), "forward_backward_error_px": fb[valid].tolist(), "photometric_error": error.ravel()[valid].tolist(), "laplacian_variance": blur}


def fit_geometry(points, kind="plane", threshold_m=0.01):
    points = array(points).reshape(-1, 3)
    if len(points) < 6:
        raise ValueError("insufficient_geometry_support")
    if kind == "box":
        low, high = points.min(axis=0), points.max(axis=0)
        if np.min(high-low) < 1e-5:
            raise ValueError("box_thickness_unobserved")
        return {"geometry": {"kind": "box", "min_m": low.tolist(), "max_m": high.tolist()}, "support_points_m": points.tolist(), "uncertainty": {"observed_point_envelope": True, "unobserved_back_faces": "unknown"}, "assumptions": ["Axis-aligned bounding proxy of observed points; complete object extent is unknown."]}
    if kind != "plane":
        raise ValueError("unsupported_primitive")
    rng = np.random.default_rng(0)
    best = np.zeros(len(points), dtype=bool)
    for _ in range(128):
        sample = points[rng.choice(len(points), 3, replace=False)]
        normal = np.cross(sample[1]-sample[0], sample[2]-sample[0])
        length = np.linalg.norm(normal)
        if length < 1e-9:
            continue
        inliers = np.abs((points-sample[0]) @ (normal/length)) < threshold_m
        if inliers.sum() > best.sum():
            best = inliers
    if best.sum() < 6 or best.sum() < len(points)*0.5:
        raise ValueError("inconsistent_plane_support")
    support = points[best]
    center = support.mean(axis=0)
    _, singular, axes = np.linalg.svd(support-center, full_matrices=False)
    if singular[1] < 1e-5:
        raise ValueError("degenerate_collinear_support")
    uv = (support-center) @ axes[:2].T
    residual = np.abs((support-center) @ axes[2])
    return {"geometry": {"kind": "plane", "origin_m": center.tolist(), "axis_u": axes[0].tolist(), "axis_v": axes[1].tolist(), "bounds_uv_m": [[float(uv[:,i].min()),float(uv[:,i].max())] for i in range(2)]}, "support_points_m": support.tolist(), "support_polygon_uv_m": cv2.convexHull(uv.astype(np.float32)).reshape(-1,2).tolist(), "uncertainty": {"fit_rms_m": float(np.sqrt(np.mean(residual**2))), "max_residual_m": float(residual.max()), "inlier_count": int(best.sum()), "input_count": len(points)}, "assumptions": ["Finite planar proxy spans observed support; holes and unseen regions remain unknown."]}


def geometry_center(geometry):
    return (array(geometry["min_m"])+array(geometry["max_m"]))/2 if geometry["kind"]=="box" else array(geometry["origin_m"])


def appearance(image, pixels):
    points = np.rint(array(pixels).reshape(-1,2)).astype(int)
    mask=np.zeros(image.shape[:2],np.uint8)
    if len(points)>=3: cv2.fillConvexPoly(mask,cv2.convexHull(points),255)
    hsv=cv2.cvtColor(image,cv2.COLOR_BGR2HSV)
    histogram=cv2.calcHist([hsv],[0,1],mask,[16,16],[0,180,0,256])
    cv2.normalize(histogram,histogram,alpha=1,norm_type=cv2.NORM_L1)
    return histogram.ravel().tolist()


class GeometryTools:
    def __init__(self, scene_store, *, settings=None):
        self.store = scene_store
        self.image_settings = image_settings(settings)

    def camera(self, identity, observation_id, camera_id):
        observation = self.store.observation(identity, observation_id)
        frame = next((item for item in observation["cameras"] if item["camera_id"] == camera_id), None)
        if frame is None:
            raise ValueError("unknown_camera")
        if not frame.get("calibration_ref"):
            raise ValueError("missing_calibration")
        calibration = self.store.resolve_json(frame["calibration_ref"])
        if not isinstance(calibration,dict): raise ValueError("invalid_calibration_object")
        reference_frame_id=calibration.get("reference_frame_id","env_origin")
        calibration = calibration.get("cameras", {}).get(camera_id, calibration)
        if not isinstance(calibration,dict): raise ValueError("invalid_calibration_object")
        if "distortion" not in calibration:
            raise ValueError("missing_distortion_calibration")
        if isinstance(calibration["distortion"], dict) and calibration["distortion"].get("model") != "opencv":
            raise ValueError("unsupported_distortion_model")
        k = array(calibration.get("K"), (3,3))
        if k[0,0] <= 0 or k[1,1] <= 0 or not np.allclose(k[2], [0,0,1]):
            raise ValueError("invalid_camera_intrinsics")
        width, height = frame["width"], frame["height"]
        if type(width) is not int or type(height) is not int or not 0<width<=4096 or not 0<height<=4096: raise ValueError("invalid_camera_dimensions")
        size = calibration.get("image_size", [calibration.get("width"), calibration.get("height")])
        if list(size) != [width, height] or frame.get("pixel_transform", "identity") != "identity":
            raise ValueError("calibration_image_size_or_crop_mismatch")
        state = self.store.resolve_json(observation["robot_state_ref"])
        if not isinstance(state,dict): raise ValueError("invalid_robot_state_object")
        if calibration.get("arm") and state.get("arms",{}).get(calibration["arm"],{}).get("source_kind") not in ("measurement","estimate"):
            raise ValueError("camera_pose_requires_measured_or_estimated_robot_state")
        sample = self.store.resolve_json(frame["pose_sample_ref"]) if frame.get("pose_sample_ref") else state.get("camera_poses", {}).get(camera_id)
        if sample is None and "T_world_camera" in calibration:
            sample = {"T_world_camera": calibration["T_world_camera"], "source_kind": "fixed_calibration", "sim_step": observation.get("sim_step"), "step_domain": observation.get("step_domain")}
        if sample is None and calibration.get("link_name"):
            model = self.store.resolve_json(observation["robot_model_ref"])
            poses = fk(model, state)
            sample = {"T_world_camera": poses[calibration["link_name"]] @ transform(calibration["T_link_camera"]), "source_kind": "robot_kinematics", "sim_step": state.get("sim_step"), "step_domain": state.get("step_domain")}
        if sample is None:
            raise ValueError("missing_camera_pose")
        if not isinstance(sample,dict): raise ValueError("invalid_camera_pose_sample")
        if sample and "cameras" in sample:
            common = {key:value for key,value in sample.items() if key != "cameras"}
            common.pop("frame_id", None)
            sample = {**common, **sample["cameras"][camera_id]}
        if sample.get("source_kind") not in ("robot_kinematics", "fixed_calibration", "measurement", "estimate", "simulator_camera_transform"):
            raise ValueError("untrusted_camera_pose_source")
        step = observation.get("sim_step")
        domain = observation.get("step_domain")
        if step is None or frame.get("sim_step") != step or frame.get("step_domain") != domain or sample.get("sim_step") != step or sample.get("step_domain") != domain or state.get("sim_step") != step or state.get("step_domain") != domain:
            raise ValueError("unsynchronized_observation")
        if sample.get("frame_id", frame["frame_id"]) != frame["frame_id"]:
            raise ValueError("pose_frame_mismatch")
        if calibration.get("T_world_camera") is not None and not np.allclose(transform(calibration["T_world_camera"]),transform(sample["T_world_camera"]),atol=1e-5):
            raise ValueError("fixed_camera_calibration_inconsistent")
        pose=transform(sample["T_world_camera"])
        pose_source=sample["source_kind"]
        diagnostics={}
        if calibration.get("link_name") and calibration.get("T_link_camera") is not None:
            model=self.store.resolve_json(observation["robot_model_ref"])
            computed,_=camera_fk(model,state,calibration,sample)
            diagnostics["reported_camera_translation_residual_m"]=float(np.linalg.norm(computed[:3,3]-pose[:3,3]))
            diagnostics["reported_camera_rotation_residual_rad"]=float(np.linalg.norm(cv2.Rodrigues(computed[:3,:3].T@pose[:3,:3])[0]))
            diagnostics["reported_camera_source"]=sample["source_kind"]
            if sample.get("T_world_link") is not None:
                measured=transform(sample["T_world_link"]) @ transform(calibration["T_link_camera"])
                diagnostics["fk_vs_measured_link_translation_m"]=float(np.linalg.norm(computed[:3,3]-measured[:3,3]))
                diagnostics["fk_vs_measured_link_rotation_rad"]=float(np.linalg.norm(cv2.Rodrigues(computed[:3,:3].T@measured[:3,:3])[0]))
                if not np.allclose(computed,measured,atol=1e-4): raise ValueError("robot_fk_link_measurement_inconsistent")
            # The disclosed q/model/static mount is authoritative. USD camera transforms
            # can remain at authored rest pose while Fabric renders an articulation.
            if sample["source_kind"]!="simulator_camera_transform" and not np.allclose(computed,pose,atol=1e-4):
                raise ValueError("camera_link_calibration_inconsistent")
            pose,pose_source=computed,"robot_kinematics"
            diagnostics["pose_authority"]="FK(measured_q) and disclosed fixed camera mount; reported USD pose is diagnostic only"
        return {"K": k, "D": array(calibration.get("distortion", {}).get("coefficients", []) if isinstance(calibration.get("distortion"), dict) else calibration.get("distortion", [])), "T": pose, "width": width, "height": height, "frame": frame, "observation": observation, "reference_frame_id":reference_frame_id, "pose_source": pose_source, "pose_diagnostics":diagnostics, "sample":sample, "calibration":calibration, "state":state}

    def image(self, camera):
        _, data = self.store.resolve_artifact(camera["frame"]["rgb_ref"])
        image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if image is None or image.shape[:2] != (camera["height"], camera["width"]):
            raise ValueError("invalid_rgb_or_image_size")
        return image

    def alignment_matches(self, identity, scene, target_camera):
        """Track exact construction pixels into an unused image; no ground truth input."""
        collected_points,collected_pixels,attempted=[],[],0
        target=cv2.cvtColor(self.image(target_camera),cv2.COLOR_BGR2GRAY)
        for entity in scene["entities"].values():
            evidence=entity.get("evidence",{})
            if entity["state"]!="tracked" or not evidence.get("source_views"):
                continue
            source=evidence["source_views"][-1]
            if source["camera_id"]!=target_camera["frame"]["camera_id"]:
                continue
            camera=self.camera(identity,source["observation_id"],source["camera_id"])
            original=cv2.cvtColor(self.image(camera),cv2.COLOR_BGR2GRAY)
            if original.shape!=target.shape: raise ValueError("alignment_image_size_mismatch")
            pixels=np.asarray(evidence["tracks"]["pixels_b"],np.float32)[evidence["accepted_indices"]].reshape(-1,1,2)
            points=array(evidence["points_m"])
            attempted+=len(pixels)
            tracked,status,error=cv2.calcOpticalFlowPyrLK(original,target,pixels,None,winSize=(31,31),maxLevel=4)
            back,reverse,_=cv2.calcOpticalFlowPyrLK(target,original,tracked,None,winSize=(31,31),maxLevel=4)
            good=(status.ravel()==1)&(reverse.ravel()==1)&(np.linalg.norm(back.reshape(-1,2)-pixels.reshape(-1,2),axis=1)<1)&(error.ravel()<30)
            if entity["geometry"]["kind"]=="plane":
                geometry=entity["geometry"]
                normal=np.cross(geometry["axis_u"],geometry["axis_v"])
                good &= np.abs((points-array(geometry["origin_m"]))@normal)<0.01
            collected_points.extend(points[good].tolist())
            collected_pixels.extend(tracked.reshape(-1,2)[good].tolist())
        return collected_points,collected_pixels,attempted

    def _attach_plane_projections(self, request, first, obs_id, value, point, alternate_point):
        """计算同帧其余标定相机的重投影坐标，并在指定 target_camera_id 时生成叠加图。"""
        d_point = np.asarray(value["d_point_d_normal_offset_m"], dtype=float)
        pts = [point.tolist()] + ([alternate_point.tolist()] if alternate_point is not None else [])
        cross = {}
        for cam_entry in first["observation"].get("cameras", []):
            cam_id = cam_entry.get("camera_id")
            if not cam_id or cam_id == request["camera_id"] or not cam_entry.get("calibration_ref"):
                continue
            other = self.camera(request, obs_id, cam_id)
            if other["reference_frame_id"] != first["reference_frame_id"]:
                continue
            px, dp, vis = project(other, pts + [(point + 0.01 * d_point).tolist()])
            d_px = ((px[-1] - px[0]) * 100.0).tolist() if np.isfinite(px[-1]).all() and np.isfinite(px[0]).all() else [0.0, 0.0]
            entry = dict(pixel=px[0].tolist(), depth_m=float(dp[0]), in_image=bool(vis[0]),
                         d_pixel_d_normal_offset_px=d_px)
            if alternate_point is not None:
                entry.update(alternate_pixel=px[1].tolist(), alternate_depth_m=float(dp[1]), alternate_in_image=bool(vis[1]))
            cross[cam_id] = entry
        value["cross_camera_projections"] = cross
        if "target_camera_id" in request:
            target = self.camera(request, obs_id, request["target_camera_id"])
            if target["reference_frame_id"] != first["reference_frame_id"]:
                raise ValueError("unsupported_frame: target_camera_id and source camera frames differ")
            pixels, depths, visible = project(target, pts)
            image = self.image(target)
            if len(pts) == 2 and visible[0] and visible[1]:
                cv2.line(image, tuple(np.rint(pixels[0]).astype(int)), tuple(np.rint(pixels[1]).astype(int)), (255, 255, 0), 1)
            elif len(pts) == 1:
                band_px, _, band_vis = project(target, [(point - 0.02 * d_point).tolist(), (point + 0.02 * d_point).tolist()])
                if band_vis[0] and band_vis[1]:
                    cv2.line(image, tuple(np.rint(band_px[0]).astype(int)), tuple(np.rint(band_px[1]).astype(int)), (255, 255, 0), 1)
            for px, ok in zip(pixels, visible):
                if ok:
                    cv2.circle(image, tuple(np.rint(px).astype(int)), 5, (0, 255, 255), 2)
            encoded, _ = encode_image(Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB)), self.image_settings, storage=True)
            overlay = self.store.put_artifact(encoded, "geometry_overlay", "image/jpeg")
            value.update(target_camera_id=request["target_camera_id"], pixels=pixels.tolist(),
                         depth_m=depths.tolist(), in_image=visible.tolist(),
                         overlay_ref=overlay, original_rgb_ref=target["frame"]["rgb_ref"])

    def _attach_gripper_relative(self, first, value, points_list):
        """计算三维点相对于同帧实测夹爪中心（pad_center）的世界偏移、工具系偏移及夹爪中垂面法向。"""
        from .roboprobe_pose import GRASP_POINT_OFFSET_M, quat_to_matrix
        arms = first.get("state", {}).get("arms", {})
        if not isinstance(arms, dict):
            return
        rel = {}
        first_pt = np.asarray(points_list[0], dtype=float) if points_list else None
        for arm_name in ("left", "right"):
            pose = arms.get(arm_name, {}).get("ee_pose")
            if not isinstance(pose, dict) or "position_m" not in pose or "quaternion_xyzw" not in pose:
                continue
            try:
                origin = array(pose["position_m"], (3,))
                xyzw = array(pose["quaternion_xyzw"], (4,))
                if abs(float(np.linalg.norm(xyzw)) - 1.0) > 1e-3:
                    continue
                rot = quat_to_matrix(np.r_[xyzw[3], xyzw[:3]])
                grasp_pos = origin + GRASP_POINT_OFFSET_M * rot[:, 0]
                entry = {
                    "grasp_position_m": [round(float(x), 5) for x in grasp_pos],
                    "jaw_midplane_normal": [round(float(x), 5) for x in rot[:, 1]],
                }
                if first_pt is not None and first_pt.shape == (3,) and np.isfinite(first_pt).all():
                    delta_world = first_pt - grasp_pos
                    delta_local = rot.T @ delta_world
                    entry.update(
                        point_minus_grasp_world_m=[round(float(x), 5) for x in delta_world],
                        point_in_gripper_frame_m=[round(float(x), 5) for x in delta_local],
                        distance_from_grasp_m=round(float(np.linalg.norm(delta_world)), 5),
                    )
                rel[arm_name] = entry
            except (ValueError, TypeError):
                continue
        if rel:
            value["gripper_relative"] = rel

    def execute(self, request):
        started = time.monotonic()
        if request.get("kind") == "help":
            validate_identity({**request, "schema_version": request.get("schema_version", "0.1")})
            return {"status": "supported", "world_kind": "internal", "frame_id": "env_origin", "unit": "m",
                    "common": "Set kind, camera_id and observation_id (or observation_ids). Pixels are [u,v] in the original image.",
                    "requests": {
                        "camera_pose": "observation_id, camera_id; returns T_world_camera (optical right/down/forward)",
                        "fk": "observation_id, camera_id; returns robot link poses and camera FK diagnostics",
                        "pixel_ray": "observation_id, camera_id, pixel:[u,v]; origin_m/direction, depth unknown",
                        "pixel_plane": "observation_id, camera_id, pixel:[u,v], plane_origin_m:[x,y,z], plane_normal:[nx,ny,nz]; conditional intersection with your explicit infinite-plane hypothesis, not measured depth. Returns d_point_d_normal_offset_m and cross_camera_projections in other calibrated views of the same observation. Optional plane_origin_alt_m evaluates a second plane with the same normal and reports assumption_delta_m; optional target_camera_id projects the candidate(s) and returns an overlay image in one call.",
                        "project_points": "observation_id, camera_id, points_m:[[x,y,z],...]; pixels and overlay",
                        "preview_ee": "observation_id, camera_id, action:EEAction; projection only, not IK/collision",
                        "triangulate": "observation_ids:[id_a,id_b], camera_id, camera_id_b (optional), pixels_a:[[u,v],...], pixels_b:[[u,v],...]; returns value.points_m in env_origin, matched by accepted_indices, with quality diagnostics (no further camera transform). Same observation can use two cameras. Temporal views require static_scene_assumption:true. Without pixels uses RGB sparse tracking, optionally roi:[x,y,w,h]",
                        "sparse_track": "observation_ids:[id_a,id_b], camera_id, camera_id_b (optional), roi:[x,y,w,h] (optional)",
                        "build_scene": "triangulate fields plus base_scene_version, primitive:'plane'|'box', entity_id (optional)",
                        "ray_surface": "observation_id, camera_id, pixel:[u,v], scene_version, entity_id; needs an explicitly built finite scene",
                        "project_geometry": "observation_id, camera_id, scene_version, entity_ids (optional)",
                        "alignment": "held-out observation_ids, camera_id, scene_version; never use training observations"},
                    "limitation": "Only disclosed camera calibration and permitted RGB/proprioception; no object truth or physics."}
        kind = request["kind"]
        ids = request.get("observation_ids", [request.get("observation_id")])
        result = {"schema_version": "0.1", **{key:request[key] for key in IDENTITY}, "world_kind": "internal", "query_type": kind, "algorithm": ALGORITHM, "implementation_digest":IMPLEMENTATION_DIGEST, "library_versions":{"opencv":cv2.__version__,"numpy":np.__version__}, "observation_ids": ids, "status": "supported", "frame_id": "env_origin", "unit": "m", "unknown_regions": ["Outside measured image support; unseen surfaces and depth remain unknown."], "assumptions": [], "value": None}
        try:
            validate_identity({"schema_version":request.get("schema_version","0.1"),**{key:request[key] for key in IDENTITY}})
            first = self.camera(request, ids[0], request["camera_id"])
            from ...core.presentation.observation_output import same_frame_robot_state
            result["observation_states"] = [same_frame_robot_state(first["observation"], first["state"])]
            if request.get("frame_id",first["reference_frame_id"]) != first["reference_frame_id"]: raise ValueError("unsupported_frame: geometry uses calibration reference frame")
            result["frame_id"]=first["reference_frame_id"]
            result["frame_ids"] = [first["frame"]["frame_id"]]
            result["calibration_id"] = first["observation"]["calibration_id"]
            result["camera_pose_source"]=first["pose_source"]
            result["camera_pose_diagnostics"]=first["pose_diagnostics"]
            if kind in ("fk", "camera_pose"):
                value = {"T_world_camera": first["T"].tolist(), "source_kind": first["pose_source"]}
                if kind == "fk" and "link_name" not in first["calibration"]:
                    if "T_world_camera" not in first["calibration"]: raise ValueError("missing_robot_link_calibration")
                    value.update(fk_path="fixed_camera_calibration",translation_residual_m=0.0,rotation_residual_rad=0.0)
                elif kind == "fk":
                    model=self.store.resolve_json(first["observation"]["robot_model_ref"])
                    computed,poses=camera_fk(model,first["state"],first["calibration"],first["sample"])
                    value.update(T_world_camera_fk=computed.tolist(),link_poses={key:item.tolist() for key,item in poses.items()},translation_residual_m=first["pose_diagnostics"].get("fk_vs_measured_link_translation_m"),rotation_residual_rad=first["pose_diagnostics"].get("fk_vs_measured_link_rotation_rad"),validation_reference="independently sampled articulation link pose")
                value["pose_diagnostics"]=first["pose_diagnostics"]
            elif kind in ("pixel_ray", "pixel_plane", "ray_surface"):
                pixel = array(request["pixel"], (2,))
                if not (0 <= pixel[0] < first["width"] and 0 <= pixel[1] < first["height"]):
                    raise ValueError("pixel_out_of_bounds")
                value = {"origin_m": first["T"][:3,3].tolist(), "direction": ray(first,[pixel])[0].tolist(), "depth": "unknown"}
                if kind == "pixel_plane":
                    plane_origin = array(request["plane_origin_m"], (3,))
                    plane_origin_alt = array(request["plane_origin_alt_m"], (3,)) if "plane_origin_alt_m" in request else None
                    normal = array(request["plane_normal"], (3,))
                    scale = float(np.max(np.abs(normal)))
                    if scale == 0:
                        raise ValueError("invalid_plane_normal: must be nonzero")
                    normal = normal / scale
                    normal /= np.linalg.norm(normal)
                    divisor = float(np.dot(value["direction"], normal))
                    result["assumptions"] = [
                        "The selected pixel lies on the caller-supplied infinite plane; this is a hypothesis, not measured depth.",
                        "No object extent, visibility, contact, clearance or reachability is established. Source-view reprojection cannot verify the plane."]
                    if abs(divisor) < .01:
                        raise ValueError("ray_plane_near_parallel")
                    direction = np.asarray(value["direction"])
                    def intersect(origin):
                        with np.errstate(over="ignore", invalid="ignore"):
                            distance = float(np.dot(origin - first["T"][:3,3], normal) / divisor)
                            point = first["T"][:3,3] + distance * direction
                        if not math.isfinite(distance) or not np.isfinite(point).all():
                            raise ValueError("invalid_plane_intersection: nonfinite result")
                        if distance <= 0:
                            raise ValueError("ray_plane_not_in_front_of_camera")
                        return point, distance

                    point, distance = intersect(plane_origin)
                    value = dict(point_m=point.tolist(), ray_distance_m=distance,
                        plane_origin_m=plane_origin.tolist(), plane_normal=normal.tolist(),
                        incidence_angle_deg=math.degrees(math.acos(min(1., abs(divisor)))),
                        ray_plane_condition_factor=1 / abs(divisor),
                        d_point_d_normal_offset_m=(direction / divisor).tolist(),
                        scope="caller_assumed_infinite_plane")
                    alternate_point = None
                    if plane_origin_alt is not None:
                        alternate_point, alternate_distance = intersect(plane_origin_alt)
                        value.update(plane_origin_alt_m=plane_origin_alt.tolist(),
                            alternate_point_m=alternate_point.tolist(),
                            alternate_ray_distance_m=alternate_distance,
                            assumption_delta_m=float(np.linalg.norm(alternate_point - point)))
                        result["assumptions"].append(
                            "The alternate intersection changes only the caller-supplied plane hypothesis; assumption_delta_m is not a depth error bound.")
                    self._attach_plane_projections(request, first, ids[0], value, point, alternate_point)
                    self._attach_gripper_relative(first, value, [point])
                    result["status"] = "conditional"
                elif kind == "ray_surface":
                    answer = self.store.query(request,request["scene_version"],{**request,**value,"kind":"ray_surface","frame_id":first["reference_frame_id"]})
                    result.update(status=answer["status"],assumptions=answer["assumptions"],scene_version=answer["scene_version"],scene_digest=answer["scene_digest"])
                    if answer.get("reason"): result["reason"]=answer["reason"]
                    value = answer["value"]
            elif kind in ("project_points", "project_geometry", "preview_ee"):
                points = request.get("points_m")
                edges=[]
                if kind == "project_geometry" and points is None:
                    scene=self.store.version(request,request["scene_version"])
                    if scene["frame_id"]!=first["reference_frame_id"]: raise ValueError("unsupported_frame: scene and camera frames differ")
                    identifiers=request.get("entity_ids",[request["entity_id"]] if request.get("entity_id") else list(scene["entities"]))
                    points=[]; unknown=[]
                    for identifier in identifiers:
                        entity=scene["entities"].get(identifier)
                        if not entity or entity["state"]!="tracked": unknown.append(identifier); continue
                        primitive=entity["geometry"]; start=len(points)
                        if primitive["kind"]=="box":
                            points.extend([list(point) for point in itertools.product(*zip(primitive["min_m"],primitive["max_m"]))])
                            edges.extend((start+i,start+j) for i in range(8) for j in range(i+1,8) if i^j in (1,2,4))
                        else:
                            points.extend([(array(primitive["origin_m"])+u*array(primitive["axis_u"])+v*array(primitive["axis_v"])).tolist() for u,v in itertools.product(*primitive["bounds_uv_m"])])
                            edges.extend((start+i,start+j) for i,j in ((0,1),(1,3),(3,2),(2,0)))
                    result.update(scene_version=scene["scene_version"],scene_digest=digest(scene),unknown_entity_ids=unknown)
                    if not points: raise ValueError("no_tracked_geometry_to_project")
                if kind == "preview_ee":
                    action=request["action"]
                    if action.get("frame_id")!=first["reference_frame_id"] or action.get("mode")!="absolute": raise ValueError("unsupported_frame_or_mode")
                    if not 1<=len(action["path"]["waypoints"])<=100: raise ValueError("invalid_waypoint_count")
                    points=[]; indices={}
                    for waypoint in action["path"]["waypoints"]:
                        for arm,pose in waypoint.items():
                            indices.setdefault(arm,[]).append(len(points)); points.append(pose["position_m"])
                    edges=[(left,right) for sequence in indices.values() for left,right in zip(sequence,sequence[1:])]
                pixels, depths, visible = project(first, points)
                image = self.image(first)
                if request.get("connect_points") and not edges: edges=list(zip(range(len(pixels)-1),range(1,len(pixels))))
                for left,right in edges:
                    if visible[left] and visible[right]: cv2.line(image,tuple(np.rint(pixels[left]).astype(int)),tuple(np.rint(pixels[right]).astype(int)),(255,255,0),1)
                for pixel, ok in zip(pixels,visible):
                    if ok:
                        cv2.circle(image, tuple(np.rint(pixel).astype(int)), 5, (0,255,255), 2)
                encoded, _ = encode_image(Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB)), self.image_settings, storage=True)
                overlay = self.store.put_artifact(encoded, "geometry_overlay", "image/jpeg")
                value = {"pixels":pixels.tolist(), "depth_m":depths.tolist(), "in_image":visible.tolist(), "overlay_ref":overlay, "original_rgb_ref":first["frame"]["rgb_ref"], "frame_id":first["frame"]["frame_id"]}
                if kind == "preview_ee":
                    axes = []
                    for waypoint in request["action"]["path"]["waypoints"]:
                        for pose in waypoint.values():
                            q = array(pose["quaternion_xyzw"],(4,))
                            if abs(np.linalg.norm(q)-1)>1e-4: raise ValueError("quaternion_must_be_normalized_xyzw")
                            x,y,z,w=q
                            rotation=np.array([[1-2*(y*y+z*z),2*(x*y-z*w),2*(x*z+y*w)],[2*(x*y+z*w),1-2*(x*x+z*z),2*(y*z-x*w)],[2*(x*z-y*w),2*(y*z+x*w),1-2*(x*x+y*y)]])
                            origin=array(pose["position_m"],(3,))
                            ends=origin+rotation.T*request.get("axis_length_m",0.05)
                            projected,_,on_image=project(first,np.vstack((origin,ends)))
                            axes.append({"origin_px":projected[0].tolist(),"axis_endpoints_px":projected[1:].tolist()})
                            for index,color in enumerate(((0,0,255),(0,255,0),(255,0,0))):
                                if on_image[0] and on_image[index+1]: cv2.line(image,tuple(np.rint(projected[0]).astype(int)),tuple(np.rint(projected[index+1]).astype(int)),color,2)
                    value["tool_axes"]=axes
                    encoded, _ = encode_image(Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB)), self.image_settings, storage=True)
                    value["overlay_ref"]=self.store.put_artifact(encoded,"geometry_overlay","image/jpeg")
                    result["status"] = "conditional"
                    value.update(ik="not_checked", collision="not_checked", semantics="projection_only", limitation="2D projection does not establish reachability or free space.")
            elif kind in ("sparse_track", "triangulate", "build_scene"):
                second = self.camera(request, ids[-1], request.get("camera_id_b", request["camera_id"]))
                if second["observation"]["observation_id"] != first["observation"]["observation_id"]:
                    result["observation_states"].append(same_frame_robot_state(second["observation"], second["state"]))
                if first["observation"]["calibration_id"] != second["observation"]["calibration_id"] or first["reference_frame_id"] != second["reference_frame_id"]:
                    raise ValueError("inconsistent_calibration")
                result["frame_ids"].append(second["frame"]["frame_id"])
                tracks = {"pixels_a":request["pixels_a"], "pixels_b":request["pixels_b"], "source_kind":"caller_correspondences"} if "pixels_a" in request else sparse_track(self.image(first),self.image(second),request.get("roi"),method=request.get("tracking_method","sift" if request.get("camera_id_b",request["camera_id"]) != request["camera_id"] else "lk"))
                result["diagnostics"]={"tracks":tracks}
                if kind == "sparse_track":
                    value = tracks
                    if not tracks.get("accepted",len(tracks["pixels_a"])):
                        result.update(status="unknown",reason=tracks.get("reason","no_consistent_tracks"))
                else:
                    if first["observation"]["observation_id"] != second["observation"]["observation_id"] and request.get("static_scene_assumption") is not True:
                        raise ValueError("temporal_triangulation_requires_explicit_static_scene_assumption")
                    value = triangulate(first,second,tracks["pixels_a"],tracks["pixels_b"], min_angle_deg=request.get("min_angle_deg",0.5),max_error_px=request.get("max_error_px",2.0))
                    value["tracks"] = tracks
                    value["source_views"] = [{"observation_id":first["observation"]["observation_id"],"camera_id":first["frame"]["camera_id"],"frame_id":first["frame"]["frame_id"]},{"observation_id":second["observation"]["observation_id"],"camera_id":second["frame"]["camera_id"],"frame_id":second["frame"]["frame_id"]}]
                    if kind == "triangulate":
                        self._attach_gripper_relative(first, value, value.get("points_m") or [])
                    result["assumptions"] = value["assumptions"]
                    result["status"] = "conditional" if value["accepted"] else "unknown"
                    if kind == "build_scene":
                        result["diagnostics"]={"triangulation":deepcopy(value)}
                        fitted = fit_geometry(value["points_m"],request.get("primitive","plane"),request.get("plane_threshold_m",0.01))
                        entity_id = request.get("entity_id") or "entity-" + digest({"observation_id":ids[0],"camera":request["camera_id"],"roi":request.get("roi")})[:16]
                        accepted_pixels=np.asarray(tracks["pixels_b"])[value["accepted_indices"]]
                        histogram=appearance(self.image(second),accepted_pixels)
                        association={"kind":"explicit_roi_seed" if request.get("entity_id") else "deterministic_observation_roi_seed","appearance_histogram":histogram,"velocity":"unknown"}
                        operation = {"op":"upsert_geometry","entity_id":entity_id,"source_kind":"observation_inference",**fitted,"evidence":value,"observation_ids":list(dict.fromkeys(ids)),"association":association}
                        if request["base_scene_version"] and not request.get("rebuild",False):
                            previous=self.store.version(request,request["base_scene_version"])
                            old=previous["entities"].get(entity_id)
                            if old:
                                displacement=float(np.linalg.norm(geometry_center(fitted["geometry"])-geometry_center(old["geometry"])))
                                old_hist=old.get("association",{}).get("appearance_histogram")
                                distance=float(cv2.compareHist(np.asarray(old_hist,np.float32),np.asarray(histogram,np.float32),cv2.HISTCMP_BHATTACHARYYA)) if old_hist else None
                                association.update(kind="appearance_and_geometry_gate",displacement_m=displacement,appearance_distance=distance)
                                ambiguous=[]
                                for other_id,other in previous["entities"].items():
                                    other_hist=other.get("association",{}).get("appearance_histogram")
                                    if other_id!=entity_id and other["state"]=="tracked" and other_hist and np.linalg.norm(geometry_center(other["geometry"])-geometry_center(fitted["geometry"]))<0.03 and cv2.compareHist(np.asarray(other_hist,np.float32),np.asarray(histogram,np.float32),cv2.HISTCMP_BHATTACHARYYA)<0.2:
                                        ambiguous.append(other_id)
                                if displacement>request.get("max_displacement_m",0.3) or (distance is not None and distance>0.7) or ambiguous or old["state"] in ("lost","retired"):
                                    operation={"op":"mark_lost","entity_id":entity_id,"source_kind":"observation_inference","reason":"identity_ambiguous_or_motion_gate_failed; explicit new seed required","assumptions":[],"association":association}
                                    value["association_failure"]={"ambiguous_entities":ambiguous,**association}
                                else:
                                    previous_obs=self.store.observation(request,old["last_observation_id"])
                                    current_obs=second["observation"]
                                    if previous_obs.get("clock_domain")==current_obs.get("clock_domain") and isinstance(previous_obs.get("sim_time_s"),(int,float)) and isinstance(current_obs.get("sim_time_s"),(int,float)) and current_obs["sim_time_s"]>previous_obs["sim_time_s"]:
                                        association["velocity"]={"m_s":((geometry_center(fitted["geometry"])-geometry_center(old["geometry"]))/ (current_obs["sim_time_s"]-previous_obs["sim_time_s"])).tolist(),"source_kind":"observed_proxy_centroid_difference","error":"unquantified","limitation":"Changing visible support can move a proxy centroid without rigid object motion."}
                        patch = {"schema_version":"0.1",**{key:request[key] for key in IDENTITY},"world_kind":"internal","base_scene_version":request["base_scene_version"],"rebuild":request.get("rebuild",False),"frame_id":first["reference_frame_id"],"observation_ids":list(dict.fromkeys(ids)),"builder_digest":digest({"algorithm":ALGORITHM,"implementation_digest":IMPLEMENTATION_DIGEST,"opencv":cv2.__version__,"numpy":np.__version__,"request":request}),"operations":[operation,{"op":"mark_unknown","source_kind":"observation_inference","region":"outside_observed_surface_support","reason":"RGB sparse reconstruction does not establish occupancy or unobserved faces."}],"build_kind":"calibrated_rgb"}
                        value = {"proposal":patch,"geometry_result":value}
            elif kind == "alignment":
                scene = self.store.version(request,request["scene_version"])
                if scene["frame_id"]!=first["reference_frame_id"]: raise ValueError("unsupported_frame: scene and camera frames differ")
                if any(item in scene["observation_ids"] for item in ids):
                    raise ValueError("alignment_requires_heldout_observations")
                views=[]
                for observation_id in ids:
                    target_camera=self.camera(request,observation_id,request["camera_id"])
                    if observation_id != first["observation"]["observation_id"]:
                        result["observation_states"].append(same_frame_robot_state(target_camera["observation"], target_camera["state"]))
                    heldout_sha=self.store.resolve_artifact(target_camera["frame"]["rgb_ref"])[0]["sha256"]
                    for training_id in scene["observation_ids"]:
                        training_observation=self.store.observation(request,training_id)
                        for training_frame in training_observation["cameras"]:
                            if training_frame["camera_id"]==request["camera_id"] and self.store.resolve_artifact(training_frame["rgb_ref"])[0]["sha256"]==heldout_sha:
                                raise ValueError("heldout_duplicate_rgb_not_independent")
                    if "points_m" in request:
                        points=request["points_m"]
                        measured=array(request["pixels"]).reshape(-1,2)
                        source=request.get("correspondence_source","caller_annotation")
                        attempted=len(points)
                    else:
                        points, measured, attempted=self.alignment_matches(request,scene,target_camera)
                        source="heldout_rgb_forward_backward_lk"
                    if not len(points): raise ValueError("insufficient_heldout_correspondences")
                    pixels,_,visible=project(target_camera,points)
                    measured=array(measured,pixels.shape)
                    errors=np.linalg.norm(pixels-measured,axis=1)
                    views.append({"observation_id":observation_id,"camera_id":request["camera_id"],"frame_id":target_camera["frame"]["frame_id"],"errors_px":errors.tolist(),"rms_px":float(np.sqrt(np.mean(errors**2))),"median_px":float(np.median(errors)),"max_px":float(errors.max()),"visible_count":int(visible.sum()),"count":len(errors),"attempted_count":attempted,"coverage":len(errors)/attempted if attempted else 0,"correspondence_source":source,"projected_pixels":pixels.tolist(),"measured_pixels":measured.tolist()})
                all_errors=np.concatenate([item["errors_px"] for item in views])
                value={"views":views,"rms_px":float(np.sqrt(np.mean(all_errors**2))),"median_px":float(np.median(all_errors)),"max_px":float(all_errors.max()),"count":len(all_errors),"heldout":True,"scene_digest":digest(scene),"scope":"Reprojection of reconstructed surface support to heldout RGB; not task success or full occupancy accuracy."}
            else:
                raise ValueError("unsupported_capability")
            result["value"] = value
            result.pop("diagnostics",None)
        except (ValueError,KeyError,TypeError,cv2.error,np.linalg.LinAlgError) as error:
            reason=str(error)
            status="invalid_input" if isinstance(error,(KeyError,TypeError)) or reason.startswith(("invalid_","unsupported_schema","pixel_out_of_bounds","correspondence_outside_image","unsupported_frame")) else "unknown"
            result.update(status=status,reason=reason)
            from ...core.tool_errors import error_payload
            result.setdefault('diagnostics', {})['input_guidance'] = error_payload(
                error, tool='geometry_query', arguments={'request': {
                    key: value for key, value in request.items() if key not in (*IDENTITY, 'schema_version')}})
        result["duration_s"] = time.monotonic()-started
        return result
