"""Analytic sensor/geometry and conservative failure checks; no simulator needed."""
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from auriga.perception.depth.depth_geometry import DepthGeometryTools
from auriga.robot.model.roboprobe_pose import angles_to_quat, quat_to_matrix
from auriga.perception.scene.store import SceneStore
from auriga.core.storage.store import Store
from depth_fixture import IDENTITY, WIDTH, HEIGHT, observation, texture


class DepthGeometryCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.artifacts = Store(self.tmp.name, IDENTITY)
        self.addCleanup(self.artifacts.close)
        self.scene = SceneStore(Path(self.tmp.name)/"scene", self.artifacts.resolve_artifact, self.artifacts.put_artifact)
        self.tools = DepthGeometryTools(self.scene)
        self.counter = 0

    def add(self, **kwargs):
        item = observation(self.artifacts, self.counter, **kwargs)
        self.counter += 1
        self.scene.import_observation(item)
        return item

    def query(self, item, kind, **kwargs):
        return self.tools.execute({**IDENTITY, "kind": kind, "observation_id": item["observation_id"],
                                   "camera_id": "head", **kwargs})

    def compare(self, before, after, **kwargs):
        return self.tools.execute({**IDENTITY, "kind": "compare_regions", "camera_id": "head",
            "before_observation_id": before["observation_id"], "after_observation_id": after["observation_id"],
            "roi": [30, 25, 90, 65], "rgb_tolerance": 255., **kwargs})

    def test_optical_z_not_range_and_rotated_camera(self):
        rotation = cv2.Rodrigues(np.array([0., .4, .2]))[0]
        pose = np.eye(4); pose[:3, :3] = rotation; pose[:3, 3] = [.2, -.3, .5]
        item = self.add(pose=pose)
        result = self.query(item, "depth_points", pixels=[[120, 80], [80, 60]])
        self.assertEqual(result["status"], "supported")
        points = result["value"]["points"]
        np.testing.assert_allclose(points[0]["point_m"], rotation @ [.2, .1, 1.] + pose[:3, 3], atol=1e-7)
        self.assertGreater(np.linalg.norm(np.asarray(points[0]["point_m"])-pose[:3, 3]), 1.)
        np.testing.assert_allclose(points[1]["point_m"], rotation @ [0, 0, 1.] + pose[:3, 3], atol=1e-7)

    def test_invalid_depth_and_mixed_boundary_are_not_filled(self):
        depth = np.ones((HEIGHT, WIDTH), np.float32)
        depth[10, 10] = np.nan; depth[20, 20] = np.inf; depth[30, 30] = 0; depth[40, 40] = 10.1
        depth[:, 100:] = 2.
        item = self.add(depth=depth)
        result = self.query(item, "depth_points", pixels=[[10, 10], [20, 20], [30, 30], [40, 40], [99, 50], [80, 60]])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["quality"]["accepted"], 1)
        self.assertTrue(all(row["point_m"] is None for row in result["value"]["points"][:-1]))
        self.assertIn("mixed_depth", result["value"]["points"][4]["quality"]["reason"])
        json.dumps(result, allow_nan=False)

    def test_profile_identity_crop_and_depth_source_validation(self):
        item = self.add()
        bad = deepcopy(item); bad["observation_profile"] = "rgb_proprio_calibrated_v1"
        with patch.object(self.scene, "observation", return_value=bad):
            with self.assertRaises(ValueError):
                self.query(item, "depth_points", pixels=[[80, 60]])
        bad = deepcopy(item); bad["episode_id"] = "different"
        with patch.object(self.scene, "observation", return_value=bad):
            with self.assertRaisesRegex(ValueError, "identity"):
                self.query(item, "depth_points", pixels=[[80, 60]])
        bad = deepcopy(item); bad["cameras"][0]["pixel_transform"] = {"scale": 2}
        with patch.object(self.scene, "observation", return_value=bad):
            with self.assertRaises(ValueError):
                self.query(item, "depth_points", pixels=[[80, 60]])
        stream = BytesIO(); np.savez_compressed(stream, depth=np.ones((HEIGHT, WIDTH), np.float64), valid=np.ones((HEIGHT, WIDTH), bool))
        bad = deepcopy(item); bad["cameras"][0]["depth_ref"] = self.artifacts.put_artifact(stream.getvalue(), "depth", "application/x-npz")
        with patch.object(self.scene, "observation", return_value=bad):
            with self.assertRaisesRegex(ValueError, "depth_archive_member_size|dtype"):
                self.query(item, "depth_points", pixels=[[80, 60]])

    def test_depth_queries_validate_counts_types_and_original_roi(self):
        item = self.add()
        for pixels in ([[80, 60]]*17, [[True, 60]], [[WIDTH, 20]], []):
            with self.assertRaises(ValueError):
                self.query(item, "depth_points", pixels=pixels)
        for roi in ([0, 0, WIDTH+1, 10], [1., 0, 10, 10], [0, 0, 0, 10], [False, 0, 10, 10]):
            with self.assertRaises(ValueError):
                self.query(item, "region_geometry", roi=roi)

    def test_seeded_region_does_not_merge_depth_or_colour_boundaries(self):
        depth = np.ones((HEIGHT, WIDTH), np.float32)
        depth[40:80, 60:100] = .8
        image = np.full((HEIGHT, WIDTH, 3), 100, np.uint8)
        image[40:80, 60:100] = [30, 100, 180]
        item = self.add(depth=depth, image=image)
        result = self.query(item, "region_geometry", roi=[40, 20, 80, 80], seed_pixel=[80, 60])
        self.assertEqual(result["status"], "supported")
        self.assertEqual(result["quality"]["point_count"], 1600)
        self.assertAlmostEqual(result["value"]["center_m"][2], .8, places=6)
        self.assertAlmostEqual(result["value"]["visible_dimensions_m"][0], .156, places=5)
        np.testing.assert_allclose(result["value"]["plane"]["normal"], [0, 0, 1], atol=1e-6)

    def test_grasp_candidates_are_bounded_and_jaws_follow_the_width(self):
        depth = np.ones((HEIGHT, WIDTH), np.float32)
        depth[44:76, 73:87] = .8
        item = self.add(depth=depth, image=np.full((HEIGHT, WIDTH, 3), 100, np.uint8))
        result = self.query(item, "grasp_candidates", roi=[60, 35, 40, 50], count=3)
        self.assertEqual(result["status"], "partial")
        candidates = result["value"]["candidates"]
        self.assertGreaterEqual(len(candidates), 1); self.assertLessEqual(len(candidates), 3)
        for candidate in candidates:
            self.assertGreaterEqual(candidate["opening_margin_m"], 0)
            self.assertLess(candidate["observed_width_m"], .06)
            rot = quat_to_matrix(angles_to_quat(*candidate["angles_deg"]))
            np.testing.assert_allclose(rot[:, 0], [0, 0, -1], atol=1e-7)
            self.assertAlmostEqual(abs(rot[0, 1]), 1.)
        wide = self.query(item, "grasp_candidates", roi=[60, 35, 40, 50], max_opening_m=.03)
        self.assertEqual(wide["value"]["candidates"], [])
        with self.assertRaises(ValueError):
            self.query(item, "grasp_candidates", roi=[60, 35, 40, 50], count=4)

    def test_placement_requires_full_footprint_and_rejects_holes(self):
        image = np.full((HEIGHT, WIDTH, 3), 100, np.uint8)
        item = self.add(image=image)
        args = dict(roi=[40, 30, 80, 60], held_dimensions_m=[.08, .08, .05], grasp_offset_m=[0, 0, .03])
        result = self.query(item, "place_candidates", **args)
        self.assertEqual(result["status"], "partial")
        for candidate in result["value"]["candidates"]:
            self.assertAlmostEqual(candidate["object_center_m"][2], 1.028, places=6)
            self.assertAlmostEqual(candidate["grasp_position_m"][2], 1.058, places=6)
        small = self.query(item, "place_candidates", **{**args, "roi": [74, 54, 12, 12]})
        self.assertEqual(small["status"], "uncertain")
        mask = np.ones((HEIGHT, WIDTH), bool); mask[58:63, 78:83] = False
        holes = self.add(image=image, valid=mask)
        result = self.query(holes, "place_candidates", **args, seed_pixel=[60, 45], count=1)
        # Centre placement is rejected; a lateral candidate is legitimate only if its full footprint avoids the hole.
        for candidate in result["value"]["candidates"]:
            self.assertGreater(abs(candidate["object_center_m"][0]), .06)
        with self.assertRaises(ValueError):
            self.query(item, "place_candidates", **{**args, "held_dimensions_m": [-.1, .1, .1]})

    def test_camera_translation_is_compensated(self):
        a = self.add()
        pose = np.eye(4); pose[0, 3] = .04
        moved = cv2.warpAffine(texture(), np.array([[1., 0., -8.], [0., 1., 0.]]), (WIDTH, HEIGHT))
        b = self.add(image=moved, pose=pose)
        result = self.compare(a, b)
        self.assertEqual(result["status"], "supported", result)
        np.testing.assert_allclose(result["value"]["world_displacement_m"], [0, 0, 0], atol=.0005)
        self.assertAlmostEqual(result["quality"]["camera_translation_m"], .04)
        self.assertIsNone(result["value"]["relative_gripper_displacement_m"])
        self.assertAlmostEqual(result["value"]["after_roi"][0], 22, delta=1)

    def test_object_gripper_comotion_and_release_separation(self):
        a = self.add(arm_center=[0, 0, 1.05])
        moved = cv2.warpAffine(texture(), np.array([[1., 0., 5.], [0., 1., 0.]]), (WIDTH, HEIGHT))
        b = self.add(image=moved, arm_center=[.025, 0, 1.05])
        result = self.compare(a, b, arm="left")
        self.assertEqual(result["status"], "supported", result)
        np.testing.assert_allclose(result["value"]["world_displacement_m"], [.025, 0, 0], atol=.0005)
        np.testing.assert_allclose(result["value"]["relative_gripper_displacement_m"], [0, 0, 0], atol=.0005)
        self.assertAlmostEqual(result["value"]["gripper_aperture_after_m"], .030807166)
        c = self.add(image=moved, arm_center=[.025, 0, 1.10])
        released = self.compare(b, c, arm="left")
        self.assertEqual(released["status"], "supported", released)
        self.assertGreater(released["value"]["gripper_separation_delta_m"], .035)
        self.assertLess(np.linalg.norm(released["value"]["world_displacement_m"]), .001)

    def test_gripper_relative_geometry_requires_declared_link6_tcp_in_both_frames(self):
        for before_tcp, after_tcp in (("arx-x5-link6-v1", None),
                                      ("arx-x5-link6-v1", "already-at-grasp-center"),
                                      (None, "arx-x5-link6-v1"),
                                      ("already-at-grasp-center", "arx-x5-link6-v1")):
            with self.subTest(before_tcp=before_tcp, after_tcp=after_tcp):
                a = self.add(arm_center=[0, 0, 1.05], arm_tcp_config_id=before_tcp)
                b = self.add(arm_center=[0, 0, 1.10], arm_tcp_config_id=after_tcp)
                result = self.compare(a, b, arm="left")
                self.assertEqual(result["status"], "supported", result)
                np.testing.assert_allclose(result["value"]["world_displacement_m"], [0, 0, 0], atol=.0005)
                for key in ("relative_gripper_displacement_m", "relative_gripper_rotation_deg",
                            "gripper_separation_delta_m", "gripper_aperture_before_m", "gripper_aperture_after_m"):
                    self.assertIsNone(result["value"][key], key)
                self.assertFalse(result["quality"]["gripper_pose_supported"])
                self.assertIn("matching_tcp", result["quality"]["gripper_pose_reason"])

    def test_camera_rotation_is_compensated(self):
        image = texture()
        a = self.add(image=image)
        theta = .025
        rot = cv2.Rodrigues(np.array([0., theta, 0.]))[0]
        k = np.asarray([[200., 0, 80.], [0, 200., 60.], [0, 0, 1.]])
        homography = k @ rot.T @ np.linalg.inv(k)
        warped = cv2.warpPerspective(image, homography, (WIDTH, HEIGHT))
        vv, uu = np.indices((HEIGHT, WIDTH))
        rays = np.stack(((uu-80)/200, (vv-60)/200, np.ones_like(uu)), axis=-1)
        z = (1/(rays @ rot.T)[:, :, 2]).astype(np.float32)
        pose = np.eye(4); pose[:3, :3] = rot
        b = self.add(image=warped, depth=z, pose=pose)
        result = self.compare(a, b, depth_tolerance_m=.05)
        self.assertEqual(result["status"], "supported", result)
        np.testing.assert_allclose(result["value"]["world_displacement_m"], [0, 0, 0], atol=.001)
        self.assertLess(result["value"]["world_rotation_deg"], .1)

    def test_occlusion_texture_loss_and_reversed_time_are_uncertain(self):
        a = self.add()
        b = self.add(image=np.zeros((HEIGHT, WIDTH, 3), np.uint8))
        result = self.compare(a, b)
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(result["value"]["evidence"], "unknown")
        self.assertIsNone(result["value"]["world_displacement_m"])
        with self.assertRaisesRegex(ValueError, "ordered"):
            self.compare(b, a)

    def test_insufficient_collinear_and_nonrigid_correspondences_stay_unknown(self):
        a = self.add()
        b = self.add()
        square = np.array([[40., 40.], [50, 40], [60, 40], [40, 50], [50, 50], [60, 50],
                           [40, 60], [50, 60], [60, 60], [70, 70]])
        quality = {"detected_count": 10, "tracked_fraction": 1.}
        for pa, pb, expected in ((square[:5], square[:5], "insufficient"),
                                 (np.column_stack((np.arange(10)*5.+40, np.full(10, 40.))),
                                  np.column_stack((np.arange(10)*5.+40, np.full(10, 40.))), "nonrigid"),
                                 (square, square*np.array([1.8, .6]), "nonrigid")):
            fresh = DepthGeometryTools(self.scene)
            with patch.object(fresh, "_track", return_value=(pa, pb, quality)):
                result = fresh.execute({**IDENTITY, "kind": "compare_regions", "camera_id": "head",
                    "before_observation_id": a["observation_id"], "after_observation_id": b["observation_id"],
                    "roi": [30, 25, 90, 65], "rgb_tolerance": 255.})
            self.assertEqual(result["status"], "uncertain", result)
            self.assertEqual(result["value"]["evidence"], "unknown")
            self.assertIn(expected, result["reason"])

    def test_source_integrity_cannot_be_bypassed_by_cached_results(self):
        item = self.add()
        reference = item["cameras"][0]["depth_ref"]
        metadata, path = self.artifacts.artifact(reference)
        # A successfully cached result still validates the referenced artifact on reuse.
        self.query(item, "depth_points", pixels=[[80, 60]])
        path.write_bytes(path.read_bytes()+b"corrupt")
        with self.assertRaisesRegex(ValueError, "digest"):
            self.query(item, "depth_points", pixels=[[80, 60]])

    def test_boolean_candidate_options_and_unknown_fields_are_rejected(self):
        item = self.add()
        for options in ({"count": True}, {"grasp_depth_m": True}, {"max_opening_m": 1.}, {"arbitrary": 1}):
            with self.assertRaises(ValueError):
                self.query(item, "grasp_candidates", roi=[30, 25, 90, 65], **options)

    def test_offcenter_seed_stays_on_its_object_across_two_updates(self):
        source = texture()
        items = []
        for shift in (0, 6, 12):
            rgb = np.full((HEIGHT, WIDTH, 3), 30, np.uint8)
            depth = np.full((HEIGHT, WIDTH), 1.2, np.float32)
            rgb[30:90, 20+shift:55+shift] = source[30:90, 20:55]
            depth[30:90, 20+shift:55+shift] = .9
            rgb[30:90, 80:120] = source[30:90, 80:120]
            depth[30:90, 80:120] = .8
            items.append(self.add(image=rgb, depth=depth))
        first = self.compare(items[0], items[1], roi=[15, 25, 110, 70], seed_pixel=[36, 55])
        self.assertEqual(first["status"], "supported", first)
        self.assertAlmostEqual(first["value"]["after_seed_pixel"][0], 42, delta=.1)
        self.assertAlmostEqual(first["value"]["world_displacement_m"][0], .027, delta=.001)
        second = self.compare(items[1], items[2], roi=first["value"]["after_roi"],
                              seed_pixel=first["value"]["after_seed_pixel"])
        self.assertEqual(second["status"], "supported", second)
        self.assertAlmostEqual(second["value"]["after_seed_pixel"][0], 48, delta=.1)
        self.assertAlmostEqual(second["value"]["world_displacement_m"][0], .027, delta=.001)

    def test_cache_copies_results_and_outputs_never_contain_point_clouds(self):
        item = self.add()
        result = self.query(item, "region_geometry", roi=[40, 30, 80, 60], rgb_tolerance=255)
        expected = deepcopy(result)
        result["value"]["center_m"][0] = 999
        self.assertEqual(self.query(item, "region_geometry", roi=[40, 30, 80, 60], rgb_tolerance=255), expected)
        text = json.dumps(expected)
        for key in ("support_points_m", '"point_cloud"', '"depth"', '"pixels_a"', '"pixels_b"'):
            self.assertNotIn(key, text)
        self.assertLess(len(text), 5000)


if __name__ == "__main__":
    unittest.main()
