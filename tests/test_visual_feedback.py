"""Pixel tracking and actor-source boundary, without simulator truth."""
import tempfile
import unittest
from copy import deepcopy

import cv2
import numpy as np

from auriga.core.storage.store import Store
from auriga.perception.visual_feedback import compare_observations, track_region


class VisualFeedbackTest(unittest.TestCase):
    def test_translation_occlusion_and_source_boundaries(self):
        rng = np.random.default_rng(12)
        before = np.full((120, 160, 3), 80, np.uint8)
        before[30:80, 40:100] = rng.integers(0, 255, (50, 60, 3), dtype=np.uint8)
        after = cv2.warpAffine(before, np.float32([[1, 0, 7], [0, 1, -4]]), (160, 120), borderValue=(80, 80, 80))
        roi = [42, 32, 56, 46]
        result = track_region(before, after, roi)
        self.assertEqual(result["status"], "tracked_features")
        np.testing.assert_allclose(result["displacement_px"], [7, -4], atol=.25)
        self.assertNotEqual(track_region(before, np.full_like(before, 80), roi)["status"], "tracked_features")
        self.assertEqual(track_region(before, after, roi, max_motion_px=2)["status"], "ambiguous")
        for bad in ([True, 0, 10, 10], [0, 0, 0, 10], [100, 100, 99, 99]):
            with self.assertRaises(ValueError):
                track_region(before, after, bad)
        identity = dict(run_id="test", episode_id="ep", env_instance_id="env", env_generation=1)
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory, identity)
            self.addCleanup(store.close)
            for i, image in enumerate((before, after)):
                ref = store.put_artifact(cv2.imencode(".png", image)[1].tobytes(), "rgb", "image/png")
                bundle = dict(**identity, observation_id=f"obs{i}", observation_profile="rgb_proprio_v1",
                              sim_step=i, step_domain="test_control_step", cameras=[dict(camera_id="head",
                              frame_id=f"frame{i}", rgb_ref=ref, width=160, height=120)])
                store.save("observation", f"obs{i}", bundle)
            args = dict(before_observation_id="obs0", after_observation_id="obs1", camera_id="head", roi=roi)
            result = compare_observations(store, identity, **args)
            self.assertEqual(result["target"]["status"], "tracked_features")
            self.assertEqual(len(result["sources"]), 2)
            with self.assertRaisesRegex(ValueError, "identity"):
                compare_observations(store, {**identity, "env_generation": 2}, **args)
            with self.assertRaisesRegex(ValueError, "order"):
                compare_observations(store, identity, **{**args, "before_observation_id": "obs1", "after_observation_id": "obs0"})
            for index in (0, 1):
                calibrated = {**store.read("observation", f"obs{index}"),
                              "observation_id": f"calibrated{index}",
                              "observation_profile": "rgb_proprio_calibrated_v1",
                              "camera_calibration": {"K": [[1, 0, 0], [0, 1, 0], [0, 0, 1]]}}
                store.save("observation", calibrated["observation_id"], calibrated)
            calibrated_result = compare_observations(store, identity, **{**args,
                "before_observation_id": "calibrated0", "after_observation_id": "calibrated1"})
            self.assertEqual(calibrated_result["target"]["status"], "tracked_features")
            for suffix, changes in (("calibrated", {"observation_profile": "rgb_proprio_calibrated_v1"}),
                                    ("private", {"cameras": [dict(camera_id="head", width=160, height=120,
                                     rgb_ref=store.put_artifact(cv2.imencode('.png', before)[1].tobytes(), "rgb", "image/png", "observer"))]})):
                value = {**deepcopy(bundle), **changes, "observation_id": suffix}
                store.save("observation", suffix, value)
                with self.assertRaises(ValueError):
                    compare_observations(store, identity, **{**args, "after_observation_id": suffix})


if __name__ == "__main__":
    unittest.main()
