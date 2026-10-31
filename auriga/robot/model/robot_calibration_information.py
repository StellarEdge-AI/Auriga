"""Read exact calibration from the latest published, permitted observation.

This reader neither captures a new observation nor reconstructs a transform.
Static robot-file defaults are intentionally not a fallback for episode data.
"""

from contextlib import nullcontext
from copy import deepcopy
import json
import math

from ...core.contracts import IDENTITY, canonical, validate_observation
from ...core.observation_policy import has_calibration, reject_privileged, require_actor_observation


def _document(store, reference, kind, profile, cache):
    if not isinstance(reference, str) or not reference:
        raise ValueError("calibration_artifact_reference_missing")
    key = (reference, kind)
    if key not in cache:
        info, raw = store.resolve_artifact(reference, allowed_scopes=("actor",))
        mime = info.get("mime_type", "").split(";", 1)[0].strip().lower()
        if info.get("kind") != kind or not (mime == "application/json" or mime.endswith("+json")):
            raise ValueError("calibration_artifact_kind_mismatch")
        value = json.loads(raw)
        reject_privileged(value, profile)
        canonical(value)  # Reject non-finite data before selecting any public fields.
        if not isinstance(value, dict):
            raise ValueError("calibration_artifact_requires_object")
        cache[key] = value
    return cache[key]


def _matrix(value, size, name):
    if (not isinstance(value, list) or len(value) != size
            or any(not isinstance(row, list) or len(row) != size for row in value)
            or any(type(item) not in (int, float) or not math.isfinite(item)
                   for row in value for item in row)):
        raise ValueError("calibration_invalid_" + name)


def robot_calibration_information(harness) -> dict:
    """Return exact optical/base calibration anchored to the last published frame.

    ``available`` requires permitted, readable calibration and pose artifacts for
    every published camera. Bases include only current sampled transforms; if no
    camera sample supplies one, ``unavailable_fields`` explicitly says so.
    The caller may apply ActorPresentation after this function's scope checks.
    """
    profile = getattr(harness, "observation_profile", None)
    result = dict(available=False, observation_profile=profile if isinstance(profile, str) else None)
    if not has_calibration(profile):
        return dict(result, reason="calibration_not_permitted_by_observation_profile")
    lock = getattr(harness, "lock", None)
    with (lock if lock is not None else nullcontext()):
        latest = getattr(harness, "latest", None)
        if not isinstance(latest, dict):
            return dict(result, reason="latest_observation_unavailable")
        bundle = deepcopy(latest)
        identity = deepcopy(getattr(harness, "identity", None))
    store = getattr(harness, "store", None)
    if store is None or not callable(getattr(store, "resolve_artifact", None)):
        return dict(result, reason="calibration_store_unavailable")
    try:
        require_actor_observation(bundle, profile)
        validate_observation(bundle)
        if not isinstance(identity, dict) or any(bundle.get(key) != identity.get(key) for key in IDENTITY):
            raise ValueError("calibration_observation_identity_mismatch")
        if type(bundle.get("sim_step")) is not int or not bundle.get("step_domain"):
            raise ValueError("calibration_observation_step_unavailable")
        frames = bundle.get("cameras")
        if not isinstance(frames, list) or not frames:
            raise ValueError("calibration_cameras_unavailable")
        cameras, bases, cache, unavailable = {}, {}, {}, []
        reference_frame = None
        for frame in frames:
            name = frame["camera_id"]
            if name in cameras:
                raise ValueError("calibration_duplicate_camera")
            if any(frame.get(key) != bundle[key] for key in ("sim_step", "step_domain")):
                raise ValueError("calibration_camera_step_mismatch")
            calibration = _document(store, frame.get("calibration_ref"), "camera_calibration", profile, cache)
            poses = _document(store, frame.get("pose_sample_ref"), "camera_pose_sample", profile, cache)
            frame_name = calibration.get("reference_frame_id")
            if (not isinstance(frame_name, str) or not frame_name
                    or poses.get("reference_frame_id") != frame_name
                    or reference_frame is not None and frame_name != reference_frame):
                raise ValueError("calibration_reference_frame_mismatch")
            reference_frame = frame_name
            if any(poses.get(key) != bundle[key] for key in ("sim_step", "step_domain")):
                raise ValueError("calibration_pose_step_mismatch")
            static, sample = calibration["cameras"][name], poses["cameras"][name]
            if not isinstance(static, dict) or not isinstance(sample, dict):
                raise ValueError("calibration_camera_record_missing")
            if sample.get("frame_id") != frame["frame_id"]:
                raise ValueError("calibration_pose_frame_mismatch")
            _matrix(static.get("K"), 3, "intrinsics")
            _matrix(sample.get("T_world_camera"), 4, "camera_transform")
            if static.get("image_size") != [frame["width"], frame["height"]]:
                raise ValueError("calibration_image_size_mismatch")
            camera = {key: deepcopy(static[key]) for key in (
                "K", "distortion", "distortion_model", "image_size", "optical_axes",
                "arm", "link_name", "T_link_camera") if key in static}
            for key in ("arm", "link_name"):
                if key in sample:
                    if key in camera and camera[key] != sample[key]:
                        raise ValueError("calibration_mount_identity_mismatch")
                    camera[key] = deepcopy(sample[key])
            if "T_link_camera" in camera:
                _matrix(camera["T_link_camera"], 4, "hand_eye_transform")
            for key in ("distortion", "distortion_model", "optical_axes"):
                if key not in camera:
                    unavailable.append("cameras." + name + "." + key)
            if "arm" in camera and "T_link_camera" not in camera:
                unavailable.append("cameras." + name + ".T_link_camera")
            camera.update({key: deepcopy(sample[key]) for key in (
                "frame_id", "T_world_camera", "T_world_link", "source_kind") if key in sample})
            if "T_world_link" in camera:
                _matrix(camera["T_world_link"], 4, "link_transform")
            # Never substitute calibration's initialization-time T_world_base.
            if "T_world_base" in sample:
                _matrix(sample["T_world_base"], 4, "base_transform")
                arm = camera.get("arm")
                if not isinstance(arm, str) or not arm:
                    raise ValueError("calibration_base_arm_unavailable")
                base = dict(T_world_base=deepcopy(sample["T_world_base"]))
                if arm in bases and bases[arm] != base:
                    raise ValueError("calibration_current_base_conflict")
                bases[arm] = base
            cameras[name] = camera
        result.update(available=True, **{key: bundle[key] for key in IDENTITY},
            observation_id=bundle["observation_id"], sim_step=bundle["sim_step"],
            step_domain=bundle["step_domain"], reference_frame_id=reference_frame,
            cameras=cameras, bases=bases,
            units=dict(K="focal lengths and principal point in pixels; homogeneous last row dimensionless",
                       image_size="[width,height] in pixels", transform_translation="m",
                       transform_rotation="dimensionless rotation matrix",
                       distortion="dimensionless coefficients in the published distortion_model"),
            transform_convention="T_A_B maps coordinates in B into A; world denotes reference_frame_id; camera is the optical frame described by optical_axes.",
            freshness="Latest published observation only; no new capture or time advancement. Intrinsics and hand-eye are the episode calibration; world transforms come from this observation's pose sample.")
        missing_bases = sorted({camera["arm"] for camera in cameras.values()
                                if isinstance(camera.get("arm"), str)} - set(bases))
        if missing_bases or not bases:
            unavailable.extend(["bases." + arm + ".T_world_base" for arm in missing_bases] or ["T_world_base"])
        if unavailable:
            result["unavailable_fields"] = unavailable
        return result
    except (AttributeError, KeyError, TypeError, ValueError, OSError) as error:
        # Do not echo artifact references, storage paths or arbitrary file text.
        reason = str(error) if isinstance(error, ValueError) and str(error).startswith("calibration_") else "calibration_artifacts_unavailable"
        return dict(result, reason=reason)
