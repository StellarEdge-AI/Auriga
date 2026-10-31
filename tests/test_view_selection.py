"""Adaptive feedback remains conservative and compares with model delivery."""

from copy import deepcopy
import io
import json
import unittest

import numpy as np
from PIL import Image

from auriga.perception.view_selection import baseline_record, select_observation_views


CAMERAS = ("cam_head", "cam_left_wrist", "cam_right_wrist")


def encoded(array):
    buffer = io.BytesIO()
    Image.fromarray(array).save(buffer, format="PNG")
    return buffer.getvalue()


def fixture(size=(96, 72)):
    bundle = dict(run_id="run", episode_id="episode", env_instance_id="env", env_generation=1,
                  world_kind="observed", observation_id="obs-before", calibration_id="calibration",
                  cameras=[dict(camera_id=camera, rgb_ref="rgb-" + camera, width=size[0], height=size[1],
                                calibration_ref="calibration-ref", pixel_transform="identity") for camera in CAMERAS])
    image = encoded(np.full((size[1], size[0], 3), 100, dtype=np.uint8))
    images = {camera: image for camera in CAMERAS}
    baselines = {camera: baseline_record(bundle, camera, image) for camera in CAMERAS}
    bundle["observation_id"] = "obs-after"
    return bundle, images, baselines


def select(bundle, images, baselines, **kwargs):
    options = dict(active_arms=["left"], receipt_status="completed",
                   calibration_valid={camera: True for camera in CAMERAS})
    options.update(kwargs)
    return select_observation_views(bundle, images, baselines, **options)


def right_decision(result):
    return next(item for item in result["decisions"] if item["camera_id"] == "cam_right_wrist")


class ViewSelectionTests(unittest.TestCase):
    def test_all_unchanged_views_are_retrievable_with_original_camera_order(self):
        bundle, images, baselines = fixture()
        bundle["cameras"] = [bundle["cameras"][2], bundle["cameras"][0], bundle["cameras"][1]]
        result = select(bundle, images, baselines)
        self.assertEqual(result["selected_camera_ids"], [])
        self.assertEqual(result["skipped_camera_ids"], ["cam_right_wrist", "cam_head", "cam_left_wrist"])
        self.assertEqual([item["camera_id"] for item in result["decisions"]], [camera["camera_id"] for camera in bundle["cameras"]])
        decision = right_decision(result)
        self.assertEqual(decision["observation_id"], "obs-after")
        self.assertEqual(decision["baseline_observation_id"], "obs-before")
        self.assertEqual(decision["reason"], "view_below_change_thresholds")
        self.assertEqual(decision["retrieval"], dict(tool="history_view_observation",
            arguments=dict(observation_id="obs-after", camera_id="cam_right_wrist")))
        self.assertEqual(decision["retrieval_instruction"],
            'history_view_observation({"observation_id":"obs-after","camera_id":"cam_right_wrist"})')
        self.assertEqual(result["settings"]["thumbnail_size"], [96, 72])
        json.dumps(result, allow_nan=False)

    def test_participation_is_telemetry_and_all_seen_unchanged_views_can_be_omitted(self):
        bundle, images, baselines = fixture()
        for arms in (["right"], ["right", "left"], None, [], "left", ["new_arm"], [None]):
            with self.subTest(arms=arms):
                result = select(bundle, images, baselines, active_arms=arms)
                self.assertEqual(result["selected_camera_ids"], [])
                self.assertEqual(result["skipped_camera_ids"], list(CAMERAS))
                self.assertTrue(all(item["reason"] == "view_below_change_thresholds"
                                    for item in result["decisions"]))

    def test_changed_head_and_participating_wrist_are_included_independently(self):
        for camera in CAMERAS:
            bundle, images, baselines = fixture()
            images[camera] = encoded(np.full((72, 96, 3), 101, dtype=np.uint8))
            with self.subTest(camera=camera):
                result = select(bundle, images, baselines, active_arms=["left", "right"])
                self.assertEqual(result["selected_camera_ids"], [camera])
                self.assertEqual(result["skipped_camera_ids"], [other for other in CAMERAS if other != camera])

    def test_successful_ordinary_template_is_eligible_but_handoff_is_full(self):
        bundle, images, baselines = fixture()
        result = select(bundle, images, baselines, context="template", active_arms=None)
        self.assertTrue(result["eligible"])
        self.assertEqual(result["selected_camera_ids"], [])
        result = select(bundle, images, baselines, context="template", force_full=True,
                        full_reason="handoff_verification")
        self.assertEqual(result["selected_camera_ids"], list(CAMERAS))
        self.assertEqual(result["force_full_reason"], "handoff_verification")

    def test_unknown_camera_role_is_included(self):
        bundle, images, baselines = fixture()
        bundle["cameras"].append({**bundle["cameras"][0], "camera_id": "new_camera"})
        result = select(bundle, images, baselines)
        self.assertIn("new_camera", result["selected_camera_ids"])
        self.assertEqual(result["decisions"][-1]["reason"], "unknown_camera_role")

    def test_initial_explicit_history_wait_checkpoint_and_unknown_contexts_are_full(self):
        bundle, images, baselines = fixture()
        for context in ("initial", "explicit_observe", "history", "wait", "observation_checkpoint",
                        "handover", "template_boundary", "unknown", None):
            with self.subTest(context=context):
                result = select(bundle, images, baselines, context=context)
                self.assertEqual(result["selected_camera_ids"], list(CAMERAS))
                self.assertFalse(result["eligible"])
        result = select(bundle, images, baselines, force_full=True)
        self.assertEqual(result["force_full_reason"], "forced_full_context")
        self.assertEqual(result["skipped_camera_ids"], [])

    def test_failure_unknown_status_and_reason_are_full(self):
        bundle, images, baselines = fixture()
        variants = [dict(receipt_status=status) for status in (None, "failed", "stopped", "unknown", "rejected")]
        variants += [dict(receipt_reason=reason) for reason in ("observation_checkpoint", "tracking_error", "new_reason")]
        for options in variants:
            with self.subTest(options=options):
                result = select(bundle, images, baselines, **options)
                self.assertEqual(result["selected_camera_ids"], list(CAMERAS))
                self.assertEqual(result["skipped_camera_ids"], [])

    def test_global_change_in_inactive_view_is_included(self):
        bundle, images, baselines = fixture()
        images["cam_right_wrist"] = encoded(np.full((72, 96, 3), 101, dtype=np.uint8))
        result = select(bundle, images, baselines)
        self.assertEqual(result["selected_camera_ids"], ["cam_right_wrist"])
        self.assertEqual(right_decision(result)["comparison"]["material_change_triggers"], ["mean_abs_rgb_delta"])

    def test_small_changed_area_in_inactive_view_is_included(self):
        bundle, images, baselines = fixture()
        pixels = np.full((72, 96, 3), 100, dtype=np.uint8)
        pixels[0, :10] = 113
        images["cam_right_wrist"] = encoded(pixels)
        decision = right_decision(select(bundle, images, baselines))
        self.assertTrue(decision["include"])
        self.assertEqual(decision["comparison"]["material_change_triggers"], ["changed_area_fraction"])

    def test_one_high_contrast_original_pixel_survives_thumbnail_averaging(self):
        bundle, images, baselines = fixture(size=(640, 480))
        pixels = np.full((480, 640, 3), 100, dtype=np.uint8)
        pixels[201, 300, 0] = 132
        images["cam_right_wrist"] = encoded(pixels)
        decision = right_decision(select(bundle, images, baselines))
        self.assertTrue(decision["include"])
        self.assertEqual(decision["comparison"]["material_change_triggers"], ["max_channel_delta"])
        self.assertEqual(decision["comparison"]["max_channel_delta_resolution"], "original")

    def test_accumulated_changes_compare_to_last_delivered_not_skipped_capture(self):
        bundle, images, baselines = fixture()
        original = deepcopy(baselines)
        for value in range(1, 5):
            bundle["observation_id"] = "capture-" + str(value)
            pixels = np.full((72, 96, 3), 100, dtype=np.uint8)
            pixels[:18] += value
            images["cam_right_wrist"] = encoded(pixels)
            decision = right_decision(select(bundle, images, baselines))
            self.assertEqual(decision["include"], value == 4)
            self.assertEqual(decision["baseline_observation_id"], "obs-before")
            self.assertEqual(decision["comparison"]["mean_abs_rgb_delta"], value / 4)
            self.assertEqual(baselines, original)
        # Only after the last result is actually delivered does the caller advance it.
        baselines["cam_right_wrist"] = baseline_record(bundle, "cam_right_wrist", images["cam_right_wrist"])
        bundle["observation_id"] = "capture-5"
        decision = right_decision(select(bundle, images, baselines))
        self.assertFalse(decision["include"])
        self.assertEqual(decision["baseline_observation_id"], "capture-4")

    def test_missing_corrupt_or_wrong_size_images_always_include_even_if_identical(self):
        for data in (None, b"", b"not-an-image", encoded(np.zeros((12, 10, 3), dtype=np.uint8))):
            for corrupt_baseline in (False, True):
                bundle, images, baselines = fixture()
                images["cam_right_wrist"] = data
                if corrupt_baseline:
                    baselines["cam_right_wrist"]["rgb_bytes"] = data
                with self.subTest(data_type=type(data).__name__, corrupt_baseline=corrupt_baseline):
                    decision = right_decision(select(bundle, images, baselines))
                    self.assertTrue(decision["include"])
                    self.assertEqual(decision["reason"], "image_comparison_unavailable")

    def test_missing_changed_or_unvalidated_calibration_includes(self):
        for validity in (None, True, False, {}, {"cam_right_wrist": False}, {"cam_right_wrist": 1}):
            bundle, images, baselines = fixture()
            with self.subTest(validity=validity):
                decision = right_decision(select(bundle, images, baselines, calibration_valid=validity))
                self.assertTrue(decision["include"])
                self.assertEqual(decision["reason"], "calibration_not_validated")
        for target, key, value in (("bundle", "calibration_id", None), ("camera", "calibration_ref", None),
                                   ("bundle", "calibration_id", "new"), ("camera", "calibration_ref", "new")):
            bundle, images, baselines = fixture()
            (bundle if target == "bundle" else bundle["cameras"][2])[key] = value
            with self.subTest(target=target, key=key, value=value):
                decision = right_decision(select(bundle, images, baselines))
                self.assertTrue(decision["include"])
                self.assertEqual(decision["reason"], "calibration_missing" if value is None else "calibration_changed")

    def test_missing_delivery_identity_provenance_or_original_transform_includes(self):
        variants = [("kind", "actor_program"), ("camera_id", "cam_left_wrist"),
                    ("observation_id", None), ("rgb_ref", None), ("world_kind", "internal"),
                    ("pixel_transform", "crop_resize_lanczos"), ("identity", {}), ("width", 97)]
        for key, value in variants:
            bundle, images, baselines = fixture()
            baselines["cam_right_wrist"][key] = value
            with self.subTest(key=key):
                self.assertTrue(right_decision(select(bundle, images, baselines))["include"])
        for key, value in (("run_id", "different"), ("episode_id", "different"),
                           ("env_instance_id", "different"), ("env_generation", 2)):
            bundle, images, baselines = fixture()
            baselines["cam_right_wrist"]["identity"][key] = value
            self.assertEqual(right_decision(select(bundle, images, baselines))["reason"], "baseline_identity_changed")
        bundle, images, baselines = fixture()
        self.assertEqual(right_decision(select(bundle, images, {}))["reason"], "no_model_delivery_baseline")
        bundle["cameras"][2]["pixel_transform"] = "crop"
        self.assertTrue(right_decision(select(bundle, images, baselines))["include"])

    def test_unknown_observation_identity_or_internal_observation_is_full(self):
        for key, value in (("env_generation", None), ("run_id", ""), ("observation_id", None), ("world_kind", "internal")):
            bundle, images, baselines = fixture()
            bundle[key] = value
            result = select(bundle, images, baselines)
            self.assertFalse(result["eligible"])
            self.assertEqual(result["selected_camera_ids"], list(CAMERAS))

    def test_invalid_settings_include_and_valid_settings_are_explicit(self):
        bundle, images, baselines = fixture()
        for settings in ({"unknown": 1}, {"thumbnail_size": [0, 72]},
                         {"changed_area_fraction_threshold": float("nan")},
                         {"max_channel_delta_threshold": float("inf")},
                         {"mean_abs_rgb_threshold": True}, {"mean_abs_rgb_threshold": 0}):
            with self.subTest(settings=settings):
                result = select(bundle, images, baselines, settings=settings)
                self.assertEqual(result["selected_camera_ids"], list(CAMERAS))
                self.assertEqual(result["force_full_reason"], "invalid_settings")
                json.dumps(result, allow_nan=False)
        settings = dict(thumbnail_size=(48, 36), max_channel_delta_threshold=20)
        result = select(bundle, images, baselines, settings=settings)
        self.assertEqual(result["settings"]["thumbnail_size"], [48, 36])
        self.assertEqual(result["settings"]["max_channel_delta_threshold"], 20)
        self.assertEqual(settings["thumbnail_size"], (48, 36))

    def test_selector_does_not_mutate_inputs_and_metadata_contains_no_bytes(self):
        bundle, images, baselines = fixture()
        before = deepcopy((bundle, images, baselines))
        first = select(bundle, images, baselines)
        second = select(bundle, images, baselines)
        self.assertEqual(first, second)
        self.assertEqual((bundle, images, baselines), before)
        encoded_metadata = json.dumps(first, allow_nan=False)
        self.assertNotIn("rgb_bytes", encoded_metadata)
        first["settings"]["thumbnail_size"][0] = 1
        self.assertEqual(select(bundle, images, baselines)["settings"]["thumbnail_size"], [96, 72])


if __name__ == "__main__":
    unittest.main()
