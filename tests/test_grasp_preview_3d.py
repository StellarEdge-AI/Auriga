"""3D 感知驱动的零回合税 grasp_hints 与 robot_action(mode='preview') grasp_3d_review 单元测试。"""
from types import SimpleNamespace
import unittest
import numpy as np

from auriga.runtime.actions.grasp_preview_3d import audit_batch_grasp_3d
from auriga.perception.external.object_table import grasp_hints_from
from auriga.perception.overlays.pose_axis_overlay import draw_object_long_axis_overlay

COLUMNS = ["id", "centre_m", "top_z", "bottom_z", "length", "width", "yaw_deg", "tilt_deg", "support", "quality"]
TABLE = dict(
    columns=COLUMNS,
    table_z=0.765,
    rows=[
        ["o1", [0.10, -0.20, 0.780], 0.792, 0.765, 0.140, 0.025, 30.0, 88.0, "table", "ok"],
    ],
)


class GraspPreview3DTest(unittest.TestCase):
    def test_zero_tax_grasp_hints_from_objects(self):
        objects = [
            dict(centre_m=[0.10, -0.20, 0.780], top_z_m=0.792, bottom_z_m=0.765, length_m=0.140, width_m=0.025, yaw_deg=30.0, axis_tilt_deg=88.0, elongation=2.5),
            dict(centre_m=[-0.21, -0.23, 0.778], top_z_m=0.791, bottom_z_m=0.765, length_m=0.045, width_m=0.038, yaw_deg=89.0),
            dict(centre_m=[-0.33, -0.02, 0.834], top_z_m=0.875, bottom_z_m=0.793, length_m=0.116, width_m=0.116, yaw_deg=13.0),
        ]
        hints = grasp_hints_from(objects, support_z_m=0.765)
        self.assertIn("o1", hints)
        self.assertEqual(hints["o1"]["across_yaw_deg"], [-60.0, 120.0])
        self.assertEqual(hints["o1"]["pad_z_m"], 0.78)
        self.assertEqual(hints["o1"]["opening_check"], "ok")
        self.assertTrue(hints["o1"]["elongated"])
        self.assertEqual(hints["o1"]["stand_upright_angles_deg"], [90.0, 0.0, -60.0])
        # Compact near-square object (45x38mm) omits noisy across_yaw_deg
        self.assertNotIn("across_yaw_deg", hints["o2"])
        self.assertEqual(hints["o2"]["pad_z_m"], 0.778)
        self.assertFalse(hints["o2"]["elongated"])
        # Too-wide container/merged blob (116x116mm > 88.8mm) omits across_yaw_deg and pad_z_m
        self.assertEqual(hints["o3"], {"opening_check": "too_wide", "elongated": False})
        # Support facts are reported beside pad_z_m, never folded into it
        self.assertAlmostEqual(hints["o1"]["fingertip_support_clearance_m"], 0.0075, delta=0.0006)
        self.assertNotIn("pad_z_floor_m", hints["o1"])

    def test_thin_block_pad_z_is_the_centre_not_the_support_floor(self):
        """A 15 mm thick block on the table: pad_z_m stays at the centre (7.5 mm above the table), the fingertips would
        reach the support (clearance about 0), and no "safe floor" is offered beside it."""
        block = dict(centre_m=[0.0, -0.20, 0.7725], top_z_m=0.780, bottom_z_m=0.765, length_m=0.080, width_m=0.060, yaw_deg=10.0)
        hints = grasp_hints_from([block], support_z_m=0.765)
        self.assertAlmostEqual(hints["o1"]["pad_z_m"], 0.7725, delta=0.0006)
        self.assertAlmostEqual(hints["o1"]["fingertip_support_clearance_m"], 0.0, places=3)
        self.assertNotIn("pad_z_floor_m", hints["o1"])
        thin = dict(centre_m=[0.0, -0.20, 0.768], top_z_m=0.771, bottom_z_m=0.765, length_m=0.060, width_m=0.040, yaw_deg=0.0)
        hints = grasp_hints_from([thin], support_z_m=0.765)
        self.assertEqual(hints["o1"]["pad_z_m"], 0.768)
        self.assertLess(hints["o1"]["fingertip_support_clearance_m"], 0)

    def test_audit_batch_grasp_3d_flags_along_axis_and_wide_guard_and_passes_aligned(self):
        harness = SimpleNamespace(_obs3d_memo=("obs-1", {"object_table": (TABLE, None, {})}))
        # Bad grasp: yaw=25 deg (closing along the 30 deg long axis!), z=0.820 (28 mm above top_z), wide_object guard (15 mm) on 25 mm object is ok, but min_aperture_m=0.040 > 0.025
        bad_action = {
            "based_on_observation_id": "obs-1",
            "stages": [
                {
                    "id": "grasp_tube",
                    "kind": "move",
                    "arm": "right",
                    "min_aperture_m": 0.040,
                    "action": {
                        "input_grasp_pose": {
                            "position_m": [0.105, -0.198, 0.820],
                            "angles_deg": [0.0, 0.0, 25.0],
                        },
                        "gripper_events": [{"goal_normalized": 0.0}],
                    },
                }
            ],
        }
        review, targets = audit_batch_grasp_3d(harness, bad_action, "obs-1")
        self.assertIsNotNone(review)
        self.assertEqual(review["status"], "warnings_detected")
        stage = review["stages"][0]
        self.assertEqual(stage["matched_object_id"], "o1")
        self.assertEqual(stage["axis_alignment"], "warn_along_long_axis")
        self.assertEqual(stage["recommended_across_yaw_deg"], -60.0)
        self.assertEqual(stage["height_check"], "warn_above_object_top")
        self.assertEqual(stage["grip_guard_check"], "warn_guard_exceeds_object_width")
        self.assertEqual(
            stage["risk_flags"],
            ["warn_along_long_axis", "warn_above_object_top", "warn_guard_exceeds_object_width"],
        )
        self.assertEqual(
            review["risk_flags"],
            ["warn_along_long_axis", "warn_above_object_top", "warn_guard_exceeds_object_width"],
        )
        self.assertEqual(len(targets), 1)

        # Aligned grasp: yaw=-60 deg (perpendicular to 30 deg), z=0.780, rigid_object guard
        good_action = {
            "based_on_observation_id": "obs-1",
            "stages": [
                {
                    "id": "grasp_tube_ok",
                    "kind": "move",
                    "arm": "right",
                    "grip_guard_preset": "rigid_object",
                    "action": {
                        "input_grasp_pose": {
                            "position_m": [0.10, -0.20, 0.780],
                            "angles_deg": [0.0, 0.0, -60.0],
                        },
                        "gripper_events": [{"goal_normalized": 0.0}],
                    },
                }
            ],
        }
        review_ok, _ = audit_batch_grasp_3d(harness, good_action, "obs-1")
        self.assertEqual(review_ok["status"], "aligned")
        self.assertEqual(review_ok["warnings"], [])
        self.assertEqual(review_ok["risk_flags"], [])

        # 当 object_table 中包含 held_fk_depth 夹持物行（含 None 几何字段）时，grasp_hints_from 与 audit_batch_grasp_3d 均应跳过该行
        held_objects = [
            dict(centre_m=[0.10, -0.20, 0.850], top_z_m=0.890, bottom_z_m=0.810, length_m=0.140, width_m=0.025,
                 yaw_deg=30.0, support="held_left", quality="held_fk_depth"),
        ]
        self.assertEqual(grasp_hints_from(held_objects, support_z_m=0.765), {})
        table_with_held = dict(
            columns=COLUMNS,
            table_z=0.765,
            rows=[
                ["o0", [0.10, -0.20, 0.850], None, None, None, None, None, None, "held_left", "held_fk_depth"],
                ["o1", [0.10, -0.20, 0.780], 0.792, 0.765, 0.140, 0.025, 30.0, 88.0, "table", "ok"],
            ],
        )
        harness_held = SimpleNamespace(_obs3d_memo=("obs-1", {"object_table": (table_with_held, None, {})}))
        review_held, _ = audit_batch_grasp_3d(harness_held, good_action, "obs-1")
        self.assertEqual(review_held["status"], "aligned")
        self.assertEqual(review_held["stages"][0]["matched_object_id"], "o1")

    def test_draw_object_long_axis_overlay_renders_on_panel(self):
        camera = dict(
            width=320,
            height=240,
            K=np.array([[250.0, 0.0, 160.0], [0.0, 250.0, 120.0], [0.0, 0.0, 1.0]]),
            D=np.zeros(5),
            T=np.array([[1, 0, 0, 0], [0, -1, 0, 0], [0, 0, -1, 1.2], [0, 0, 0, 1]], dtype=float),
        )
        img = np.zeros((240, 320, 3), dtype=np.uint8)
        draw_object_long_axis_overlay(
            img,
            camera,
            [dict(id="o1", centre_m=[0.0, 0.0, 0.78], length_m=0.14, width_m=0.03, yaw_deg=30.0)],
            320,
            240,
        )
        self.assertGreater(int(img.sum()), 0)


if __name__ == "__main__":
    unittest.main()
