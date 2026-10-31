"""Conservative, CPU-only selection of already archived observation views.

The caller opts into this helper with ``images.feedback_policy='adaptive'``;
``all`` remains the delivery default. Completed automatic action and template
feedback is eligible across all three cameras. Explicit requests, failures and
support-changing handoff checkpoints remain full-view deliveries.

A view is suppressed only when its original RGB is below every
change threshold relative to that camera's last full original image actually
delivered to the model. Capturing, archiving, program delivery, crops and skipped
frames MUST NOT advance that baseline. Clear baselines when their model context
is no longer available (for example after compaction or session replacement).

Defaults use a 96 x 72 BOX thumbnail: mean absolute RGB delta >= 1 on the 0..255
scale, or a fraction >= .001 of pixels with max-channel delta > 12, is material.
Additionally, max-channel delta >= 32 at ORIGINAL resolution is material, so a
tiny high-contrast change cannot disappear solely through thumbnail averaging.
Any trigger includes the view. These are visual heuristics, not guarantees of
semantic equivalence or task performance. Thresholds are returned in metadata.

No image bytes are changed or returned by selection, and no cache is updated.
All images must still be captured/archived. Missing evidence, invalid settings,
decode/comparison failures, calibration changes and unknown contexts include
views. The caller validates synchronized calibration and passes per-camera
``calibration_valid`` booleans; anything other than explicit True includes.
"""

from collections.abc import Mapping
import io
import json
import math

import numpy as np
from PIL import Image


POLICY = "adaptive_image_feedback_v2"
THUMBNAIL_SIZE = (96, 72)
MEAN_ABS_RGB_THRESHOLD = 1.0
CHANGED_PIXEL_RGB_THRESHOLD = 12.0
CHANGED_AREA_FRACTION_THRESHOLD = 0.001
MAX_CHANNEL_DELTA_THRESHOLD = 32.0
DEFAULT_SETTINGS = {
    "thumbnail_size": list(THUMBNAIL_SIZE),
    "mean_abs_rgb_threshold": MEAN_ABS_RGB_THRESHOLD,
    "changed_pixel_rgb_threshold": CHANGED_PIXEL_RGB_THRESHOLD,
    "changed_area_fraction_threshold": CHANGED_AREA_FRACTION_THRESHOLD,
    "max_channel_delta_threshold": MAX_CHANNEL_DELTA_THRESHOLD,
}
_IDENTITY_KEYS = ("run_id", "episode_id", "env_instance_id", "env_generation")
_WRIST_ARMS = {"cam_left_wrist": "left", "cam_right_wrist": "right"}
_BASELINE_KIND = "model_delivered_full_original"


def baseline_record(bundle, camera_id, rgb_bytes):
    """Build an in-memory baseline; call only for actual full model delivery.

    ``rgb_bytes`` are the archived original RGB bytes before transport encoding,
    crop or resize. The record intentionally contains bytes and is not delivery
    metadata. Constructing it does not prove delivery: the caller owns admission,
    successful delivery bookkeeping and context invalidation. Missing provenance
    stays missing and prevents later suppression. Decode is deferred to selection
    so even matching corrupt images cannot qualify as unchanged.
    """
    matches = [camera for camera in bundle["cameras"] if camera.get("camera_id") == camera_id]
    if len(matches) != 1:
        raise ValueError("view_baseline_camera_not_unique")
    if not isinstance(rgb_bytes, bytes):
        raise ValueError("view_baseline_requires_original_bytes")
    camera = matches[0]
    return dict(kind=_BASELINE_KIND, observation_id=bundle.get("observation_id"),
                camera_id=camera_id, rgb_bytes=rgb_bytes, rgb_ref=camera.get("rgb_ref"),
                world_kind=bundle.get("world_kind"), pixel_transform=camera.get("pixel_transform"),
                width=camera.get("width"), height=camera.get("height"),
                calibration_id=bundle.get("calibration_id"), calibration_ref=camera.get("calibration_ref"),
                identity={key: bundle.get(key) for key in _IDENTITY_KEYS})


def _settings(value):
    settings = {**DEFAULT_SETTINGS, "thumbnail_size": list(THUMBNAIL_SIZE)}
    if value is None:
        return settings, None
    if not isinstance(value, Mapping) or set(value) - set(settings):
        return settings, "unknown_or_invalid_settings"
    candidate = {**settings, **value}
    size = candidate["thumbnail_size"]
    if (not isinstance(size, (tuple, list)) or len(size) != 2
            or any(type(number) is not int or not 1 <= number <= 512 for number in size)):
        return settings, "invalid_thumbnail_size"
    for key in settings.keys() - {"thumbnail_size"}:
        number = candidate[key]
        upper = 1 if key == "changed_area_fraction_threshold" else 255
        if (type(number) not in (int, float) or not math.isfinite(number)
                or not 0 <= number <= upper
                or (key != "changed_pixel_rgb_threshold" and number == 0)):
            return settings, "invalid_" + key
    candidate["thumbnail_size"] = list(size)
    return candidate, None


def _identity_valid(identity):
    return (isinstance(identity, Mapping)
            and all(isinstance(identity.get(key), str) and identity[key] for key in _IDENTITY_KEYS[:-1])
            and type(identity.get("env_generation")) is int and identity["env_generation"] >= 0)


def _baseline_problem(bundle, camera, baseline, calibration_valid):
    camera_id = camera["camera_id"]
    if not isinstance(calibration_valid, Mapping) or calibration_valid.get(camera_id) is not True:
        return "calibration_not_validated"
    if not all(isinstance(value, str) and value for value in
               (bundle.get("calibration_id"), camera.get("calibration_ref"))):
        return "calibration_missing"
    if not isinstance(baseline, Mapping):
        return "no_model_delivery_baseline"
    if (baseline.get("kind") != _BASELINE_KIND or baseline.get("camera_id") != camera_id
            or not isinstance(baseline.get("observation_id"), str) or not baseline["observation_id"]
            or not isinstance(baseline.get("rgb_ref"), str) or not baseline["rgb_ref"]):
        return "baseline_provenance_unknown"
    if baseline.get("world_kind") != "observed" or baseline.get("pixel_transform") != "identity":
        return "baseline_not_original_observed_view"
    if not _identity_valid(baseline.get("identity")):
        return "baseline_identity_unknown"
    if any(baseline["identity"][key] != bundle[key] for key in _IDENTITY_KEYS):
        return "baseline_identity_changed"
    if (baseline.get("calibration_id") != bundle["calibration_id"]
            or baseline.get("calibration_ref") != camera["calibration_ref"]):
        return "calibration_changed"
    dimensions = (camera.get("width"), camera.get("height"))
    if any(type(number) is not int or number <= 0 for number in dimensions):
        return "image_dimensions_unknown"
    if (baseline.get("width"), baseline.get("height")) != dimensions:
        return "image_dimensions_changed"
    if camera.get("pixel_transform") != "identity":
        return "current_view_not_original"
    return None


def _decode(data, dimensions):
    if not isinstance(data, bytes) or not data:
        raise ValueError("original_rgb_bytes_unavailable")
    with Image.open(io.BytesIO(data)) as image:
        if image.size != dimensions:
            raise ValueError("encoded_image_dimensions_mismatch")
        if getattr(image, "n_frames", 1) != 1:
            raise ValueError("animated_image_not_supported")
        return image.convert("RGB")


def _compare(current_bytes, baseline_bytes, dimensions, settings):
    current = _decode(current_bytes, dimensions)
    baseline = _decode(baseline_bytes, dimensions)
    size = tuple(settings["thumbnail_size"])
    current_small = np.asarray(current.resize(size, Image.Resampling.BOX), dtype=np.int16)
    baseline_small = np.asarray(baseline.resize(size, Image.Resampling.BOX), dtype=np.int16)
    difference = np.abs(current_small - baseline_small)
    pixel_delta = difference.max(axis=2)
    original_delta = np.abs(np.asarray(current, dtype=np.int16) - np.asarray(baseline, dtype=np.int16))
    metrics = dict(mean_abs_rgb_delta=float(difference.mean()),
                   changed_area_fraction=float((pixel_delta > settings["changed_pixel_rgb_threshold"]).mean()),
                   max_channel_delta=int(original_delta.max()),
                   max_channel_delta_resolution="original", thumbnail_resampling="BOX")
    triggers = []
    for metric, threshold in (("mean_abs_rgb_delta", "mean_abs_rgb_threshold"),
                              ("changed_area_fraction", "changed_area_fraction_threshold"),
                              ("max_channel_delta", "max_channel_delta_threshold")):
        if metrics[metric] >= settings[threshold]:
            triggers.append(metric)
    metrics["material_change_triggers"] = triggers
    return metrics


def select_observation_views(bundle, images, last_delivered, *, active_arms=None,
                             context="post_action", receipt_status=None, receipt_reason=None,
                             force_full=False, full_reason=None, calibration_valid=None, settings=None):
    """Return JSON-safe selection metadata in the bundle's original camera order.

    ``images`` maps camera IDs to archived original encoded bytes. ``last_delivered``
    maps IDs to :func:`baseline_record` results. Neither input is modified.
    ``active_arms`` is participation telemetry, not a condition for suppression:
    a stationary camera can observe moving objects and a moving arm can return
    to a previously seen view. Compare every original camera against actual
    model delivery. Automatic ``post_action`` and ``template`` contexts permit
    comparison; explicit queries and ``initial``/``history`` remain full.
    Use ``force_full=True`` for key handoff/support verification checkpoints and
    supply a bounded ``full_reason``. Failed, uncertain or stopped receipts are
    always full. Context invalidation forces first-delivery baselines again.

    Calibration checks, dispatch provenance and preservation of model context are
    caller responsibilities. The helper never invokes a model, captures images,
    advances physics, changes archival storage or updates delivery baselines.
    """
    config, settings_error = _settings(settings)
    cameras = bundle["cameras"]
    observation_id = bundle.get("observation_id")
    arms_valid = (isinstance(active_arms, (list, tuple, set, frozenset))
                  and bool(active_arms)
                  and all(isinstance(arm, str) and arm in ("left", "right") for arm in active_arms))
    requested_full_reason = full_reason
    full_reason = None
    if force_full:
        full_reason = requested_full_reason or "forced_full_context"
    elif context not in ("post_action", "template"):
        full_reason = "context_requires_full_views"
    elif receipt_status != "completed":
        full_reason = "receipt_not_completed"
    elif receipt_reason not in (None, ""):
        full_reason = "receipt_reason_requires_full_views"
    elif settings_error:
        full_reason = "invalid_settings"
    elif bundle.get("world_kind") != "observed":
        full_reason = "observation_not_observed"
    elif not isinstance(observation_id, str) or not observation_id:
        full_reason = "observation_id_unknown"
    elif not _identity_valid(bundle):
        full_reason = "observation_identity_unknown"
    decisions = []
    for camera in cameras:
        camera_id = camera["camera_id"]
        decision = dict(camera_id=camera_id, observation_id=observation_id, include=True)
        baseline = last_delivered.get(camera_id) if isinstance(last_delivered, Mapping) else None
        if full_reason:
            reason = full_reason
        elif camera_id not in (*_WRIST_ARMS, "cam_head"):
            reason = "unknown_camera_role"
        else:
            reason = _baseline_problem(bundle, camera, baseline, calibration_valid)
            if reason is None:
                decision["baseline_observation_id"] = baseline["observation_id"]
                try:
                    current = images.get(camera_id) if isinstance(images, Mapping) else None
                    comparison = _compare(current, baseline.get("rgb_bytes"),
                                          (camera["width"], camera["height"]), config)
                except Exception as error:
                    reason = "image_comparison_unavailable"
                    decision["comparison_error_type"] = type(error).__name__
                else:
                    decision["comparison"] = comparison
                    decision["include"] = bool(comparison["material_change_triggers"])
                    reason = "view_materially_changed" if decision["include"] else "view_below_change_thresholds"
        decision["reason"] = reason
        if not decision["include"]:
            args = dict(observation_id=observation_id, camera_id=camera_id)
            decision["retrieval"] = dict(tool="history_view_observation", arguments=args)
            decision["retrieval_instruction"] = "history_view_observation(" + json.dumps(args, separators=(",", ":")) + ")"
        decisions.append(decision)
    result = dict(policy=POLICY, context=context if isinstance(context, str) else None,
                  eligible=full_reason is None, force_full_reason=full_reason,
                  active_arms=sorted(set(active_arms)) if arms_valid else None,
                  settings=config, decisions=decisions,
                  selected_camera_ids=[item["camera_id"] for item in decisions if item["include"]],
                  skipped_camera_ids=[item["camera_id"] for item in decisions if not item["include"]],
                  baseline_semantics="last_full_original_view_actually_delivered_to_model",
                  limitation="Pixel-change heuristics do not establish semantic equivalence or preserved task performance.")
    if settings_error:
        result["settings_error"] = settings_error
    return result
