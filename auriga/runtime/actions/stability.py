"""Explicit, bounded visible-motion settling using saved RGB and ordinary waits.

This estimates visible motion only. It never infers contact or physical stability,
and never changes the outcome of the action preceding the optional wait.
"""
import json
import math

import numpy as np


def validate_options(options):
    if not isinstance(options, dict) or set(options) - {"camera_id", "roi", "max_native_steps",
            "sample_steps", "stable_samples", "max_motion_px"}:
        raise ValueError("invalid_settle_options")
    value = {"camera_id": "cam_head", "max_native_steps": 25, "sample_steps": 5,
             "stable_samples": 3, "max_motion_px": .5, **options}
    for field, lower, upper in (("max_native_steps", 1, 50), ("sample_steps", 1, 10), ("stable_samples", 2, 5)):
        if type(value[field]) is not int or not lower <= value[field] <= upper:
            raise ValueError("invalid_settle_" + field)
    bound = value["max_motion_px"]
    if type(bound) not in (int, float) or not math.isfinite(bound) or not 0 <= bound <= 5:
        raise ValueError("invalid_settle_max_motion_px")
    roi = value.get("roi")
    if (not isinstance(roi, list) or len(roi) != 4 or any(type(v) is not int for v in roi)
            or min(roi[:2]) < 0 or min(roi[2:]) < 4):
        raise ValueError("settle_requires_target_roi")
    if not isinstance(value["camera_id"], str) or not value["camera_id"]:
        raise ValueError("settle_requires_camera_id")
    return value


def _camera_pose(harness, observation, camera_id):
    camera = next((c for c in observation["cameras"] if c["camera_id"] == camera_id), None)
    if camera is None:
        raise ValueError("settle_camera_unavailable")
    reference = camera.get("pose_sample_ref")
    if not reference:
        raise ValueError("settle_camera_pose_unknown")
    sample = json.loads(harness.store.resolve_artifact(reference, allowed_scopes=("actor",))[1])
    if sample.get("sim_step") != observation.get("sim_step"):
        raise ValueError("settle_camera_pose_unsynchronized")
    cam_sample = (sample.get("cameras") or {}).get(camera_id) if isinstance(sample.get("cameras"), dict) and "T_world_camera" not in sample else sample
    if not isinstance(cam_sample, dict) or "T_world_camera" not in cam_sample:
        raise ValueError("settle_camera_pose_invalid")
    pose = np.asarray(cam_sample["T_world_camera"], dtype=float)
    if pose.shape != (4, 4) or not np.isfinite(pose).all():
        raise ValueError("settle_camera_pose_invalid")
    return pose, camera


def bounded_settle(harness, options, correlation):
    from ...perception.visual_feedback import compare_observations
    settings = validate_options(options)
    used, quiet, samples = 0, 0, []
    previous = harness.latest
    result = dict(status="unknown", executed_native_steps=0, samples=samples,
                  interpretation="Visible ROI motion only; physical stability is not established.")
    try:
        pose, camera = _camera_pose(harness, previous, settings["camera_id"])
        x, y, width, height = settings["roi"]
        if x + width > camera["width"] or y + height > camera["height"]:
            raise ValueError("settle_roi_out_of_bounds")
        if not harness.enable_wait or "robot_wait" in harness.disabled_tools:
            raise ValueError("settle_wait_unavailable")
        while used < settings["max_native_steps"]:
            remaining = harness.action_budget().get("native_steps_remaining")
            count = min(settings["sample_steps"], settings["max_native_steps"] - used)
            if remaining is not None:
                count = min(count, remaining)
            if count <= 0:
                result.update(status="limit_reached", reason="native_budget_exhausted")
                break
            args = dict(observation_id=previous["observation_id"], native_steps=count)
            action = harness._action_from_args("robot_wait", args)
            receipt = harness.execute(action, correlation)["receipt"]
            completed = receipt.get("completed_native_steps", receipt.get("executed_native_steps", 0))
            used += completed if type(completed) is int else 0
            result["executed_native_steps"] = used
            result.setdefault("wait_action_ids", []).append(action["action_id"])
            if receipt.get("status") != "completed" or completed != count:
                result.update(status="unknown", reason="wait_incomplete")
                break
            current = harness.latest
            result["final_observation_id"] = current["observation_id"]
            new_pose, _ = _camera_pose(harness, current, settings["camera_id"])
            if not np.allclose(pose, new_pose, atol=1e-6, rtol=0):
                result.update(reason="camera_moved")
                break
            evidence = compare_observations(harness.store, harness.identity,
                before_observation_id=previous["observation_id"], after_observation_id=current["observation_id"],
                camera_id=settings["camera_id"], roi=settings["roi"])["target"]
            sample = dict(observation_id=current["observation_id"], status=evidence["status"])
            samples.append(sample)
            if evidence["status"] != "tracked_features":
                result.update(reason="visibility_or_tracking_insufficient")
                break
            # Use every retained correspondence: median-only motion can conceal rotation.
            delta = np.asarray(evidence["points_after_px"]) - np.asarray(evidence["points_before_px"])
            movement = float(np.max(np.linalg.norm(delta, axis=1)))
            sample["max_tracked_motion_px"] = movement
            quiet = quiet + 1 if movement <= settings["max_motion_px"] else 0
            if quiet >= settings["stable_samples"]:
                result.update(status="low_visible_motion", consecutive_samples=quiet)
                break
            previous, pose = current, new_pose
        else:
            result.update(status="limit_reached", reason="motion_did_not_meet_threshold")
    except (ValueError, KeyError, TypeError) as error:
        result.update(status="unknown", reason=str(error))
    return result
