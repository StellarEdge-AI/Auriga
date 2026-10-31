"""Read-only pixel correspondence from permitted RGB, with explicit uncertainty."""
import hashlib
import math

import cv2
import numpy as np

from ..core.contracts import IDENTITY
from ..robot.model.geometry import sparse_track
from ..core.observation_policy import ACTOR_PROFILES, require_actor_observation, reject_privileged
from ..core.presentation.observation_output import saved_observation_state


def _roi(value, width, height):
    if not isinstance(value, list) or len(value) != 4 or any(type(v) is not int for v in value):
        raise ValueError("visual_roi_requires_four_integers")
    x, y, w, h = value
    if min(x, y) < 0 or min(w, h) < 4 or x + w > width or y + h > height:
        raise ValueError("visual_roi_out_of_bounds_or_too_small")
    return value


def track_region(before, after, roi, *, max_motion_px=160):
    """Track texture selected by the caller; never infer object identity or grasp."""
    if (not isinstance(before, np.ndarray) or not isinstance(after, np.ndarray)
            or before.shape != after.shape or before.ndim != 3 or before.shape[2] != 3
            or before.dtype != np.uint8 or after.dtype != np.uint8):
        raise ValueError("visual_tracking_requires_equal_uint8_bgr_images")
    if type(max_motion_px) not in (int, float) or not math.isfinite(max_motion_px) or not 1 <= max_motion_px <= 320:
        raise ValueError("visual_motion_bound_must_be_1_to_320_pixels")
    roi = _roi(roi, before.shape[1], before.shape[0])
    tracks = sparse_track(before, after, roi=roi, max_points=80, method="lk")
    first = np.asarray(tracks["pixels_a"], dtype=float).reshape(-1, 2)
    second = np.asarray(tracks["pixels_b"], dtype=float).reshape(-1, 2)
    count = len(first)
    result = dict(status="lost", source_roi=roi, detected_points=tracks.get("detected", 0),
                  surviving_points=count, displacement_px=None, accepted_points=0,
                  source_kind="rgb_feature_estimate", method="lk_forward_backward_translation_consensus",
                  thresholds=dict(max_motion_px=max_motion_px, residual_px=3, min_points=4,
                                  min_survival_fraction=0.5, min_consensus_fraction=0.7))
    if count < 4:
        result["reason"] = tracks.get("reason", "too_few_correspondences")
        return result
    delta = second - first
    median = np.median(delta, axis=0)
    residual = np.linalg.norm(delta - median, axis=1)
    inliers = residual <= 3
    survival = count / max(count, tracks.get("detected", count))
    consensus = float(inliers.mean())
    result.update(survival_fraction=survival, consensus_fraction=consensus,
                  residual_p90_px=float(np.percentile(residual, 90)),
                  forward_backward_max_px=max(tracks.get("forward_backward_error_px", [0])))
    # ponytail: local translation only; report rotation/deformation as ambiguous before adding a motion model.
    if survival < 0.5 or consensus < 0.7 or int(inliers.sum()) < 4:
        result.update(status="ambiguous", reason="partial_visibility_or_inconsistent_motion")
    elif float(np.linalg.norm(median)) > max_motion_px:
        result.update(status="ambiguous", reason="motion_exceeds_caller_bound")
    else:
        result.update(status="tracked_features", reason=None,
                      displacement_px=np.median(delta[inliers], axis=0).tolist(),
                      accepted_points=int(inliers.sum()),
                      points_before_px=first[inliers][:40].tolist(), points_after_px=second[inliers][:40].tolist())
    return result


def comparison_inputs(store, identity, *, before_observation_id, after_observation_id, camera_id, roi):
    """Resolve only original actor RGB from one episode/camera, retaining its hashes."""
    bundles, images, sources = [], [], []
    for identifier in (before_observation_id, after_observation_id):
        if not isinstance(identifier, str) or not identifier:
            raise ValueError("visual_observation_id_required")
        bundle = store.read("observation", identifier)
        if bundle.get("observation_profile") not in ACTOR_PROFILES:
            raise ValueError("visual_requires_explicit_actor_rgb_profile")
        require_actor_observation(bundle)
        if any(bundle.get(k) != identity[k] for k in IDENTITY):
            raise ValueError("visual_observation_identity_mismatch")
        camera = next((c for c in bundle["cameras"] if c["camera_id"] == camera_id), None)
        if camera is None:
            raise ValueError("visual_unknown_camera")
        artifact, data = store.resolve_image(camera["rgb_ref"], allowed_scopes=("actor",))
        if artifact["kind"] != "rgb" or not artifact["mime_type"].startswith("image/"):
            raise ValueError("visual_requires_original_rgb")
        image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if image is None or image.shape[:2] != (camera["height"], camera["width"]):
            raise ValueError("visual_rgb_dimensions_mismatch")
        _roi(roi, camera["width"], camera["height"])
        bundles.append(bundle)
        images.append(image)
        sources.append(dict(observation_id=identifier, camera_id=camera_id, frame_id=camera.get("frame_id"),
                            rgb_ref=camera["rgb_ref"], sha256=hashlib.sha256(data).hexdigest(),
                            width=camera["width"], height=camera["height"], sim_step=bundle.get("sim_step"),
                            step_domain=bundle.get("step_domain"), pixel_coordinates="original_image",
                            observation_state=saved_observation_state(store, identifier),
                            reconstruction=artifact.get("reconstruction")))
    if bundles[0]["observation_profile"] != bundles[1]["observation_profile"]:
        raise ValueError("visual_observation_profiles_differ")
    if bundles[0].get("step_domain") != bundles[1].get("step_domain"):
        raise ValueError("visual_step_domains_differ")
    a, b = [v.get("sim_step") for v in bundles]
    if type(a) is not int or type(b) is not int or b < a:
        raise ValueError("visual_observation_order_unknown_or_reversed")
    return images, sources


def compare_observations(store, identity, *, before_observation_id, after_observation_id,
                         camera_id, roi, reference_roi=None, max_motion_px=160):
    images, sources = comparison_inputs(store, identity, before_observation_id=before_observation_id,
                                       after_observation_id=after_observation_id, camera_id=camera_id, roi=roi)
    target = track_region(*images, roi, max_motion_px=max_motion_px)
    reference = track_region(*images, reference_roi, max_motion_px=max_motion_px) if reference_roi is not None else None
    relative = None
    if reference and target["status"] == reference["status"] == "tracked_features":
        relative = (np.asarray(target["displacement_px"]) - reference["displacement_px"]).tolist()
    result = dict(schema_version="0.1", source_kind="rgb_feature_estimate", sources=sources,
                  target=target, reference=reference, relative_displacement_px=relative,
                  limitations=["Correspondences identify image texture, not a semantic object or retained grasp.",
                               "Camera motion, depth, occlusion, rotation and background texture can change pixel motion.",
                               "No metric displacement, contact, task completion or physical stability is inferred."])
    reject_privileged(result)
    return result
