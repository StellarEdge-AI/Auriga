import unittest
import numpy as np

from auriga.robot.motion.scene_obstacles import (
    merge_and_persist_obstacles,
    plan_move_with_obstacle_clearance,
)


class _DummyHarness:
    def __init__(self):
        self.identity = {"run_id": "r1", "episode_id": "ep0", "env_instance_id": "e1", "env_generation": 0}


class _DummyAdapter:
    def __init__(self):
        self.calls = []

    def _plan_segment(self, robot, target, diagnostics, index, **options):
        self.calls.append((index, list(target["position_m"])))
        z = target["position_m"][2]
        q0 = np.zeros(6)
        q1 = np.array([0.1, 0.2, z, 0.0, 0.0, 0.0])
        return np.vstack([q0, q1]), 1

    def _resample_action_plan(self, positions, arm, own, *, start_q=None):
        return np.asarray(positions, dtype=float)


class SceneObstaclesTest(unittest.TestCase):
    def test_merge_and_persist_obstacles_across_calls(self):
        from auriga.robot.motion.proxy_contract import validate_proxies
        harness = _DummyHarness()
        args1 = {
            "obstacles": [
                {
                    "id": "tower_base",
                    "min_m": [-0.05, -0.35, 0.75],
                    "max_m": [0.05, -0.25, 0.85],
                    "evidence": "cam_head obs-1",
                },
            ]
        }
        out1 = merge_and_persist_obstacles(harness, args1)
        self.assertEqual(len(out1["obstacles"]), 1)
        validated1 = validate_proxies({"obstacles": out1["obstacles"]})
        self.assertEqual(len(validated1["obstacles"]), 1)

        args2 = {"mode": "preview"}
        out2 = merge_and_persist_obstacles(harness, args2)
        self.assertEqual(len(out2["obstacles"]), 1)
        self.assertEqual(out2["obstacles"][0]["id"], "tower_base")
        validated2 = validate_proxies({"obstacles": out2["obstacles"]})
        self.assertEqual(validated2["obstacles"][0]["id"], "tower_base")

        # Episode switch resets persistent obstacles
        harness.identity["episode_id"] = "ep1"
        out3 = merge_and_persist_obstacles(harness, {"mode": "preview"})
        self.assertNotIn("obstacles", out3)

    def test_plan_move_with_obstacle_clearance_builds_two_segment_arc(self):
        adapter = _DummyAdapter()
        obstacles = [
            {"id": "holder", "min_m": [-0.03, -0.32, 0.75], "max_m": [0.03, -0.28, 0.86], "margin_m": 0.005},
        ]
        target = {"position_m": [0.12, -0.30, 0.90], "quaternion_xyzw": [0.0, 0.7071, 0.0, 0.7071]}
        plan, resampled = plan_move_with_obstacle_clearance(
            adapter,
            None,
            "right",
            {"path": {"position_tolerance_m": 0.005}},
            target,
            [-0.12, -0.30, 0.90],
            [0.0] * 6,
            obstacles,
            None,
            [],
            cap_remaining=100,
        )
        self.assertIsNotNone(plan)
        self.assertIsNotNone(resampled)
        self.assertEqual(len(adapter.calls), 3)
        # Second call is the raised midpoint above holder_top + 0.13
        self.assertGreater(adapter.calls[1][1][2], 0.98)

    def test_merge_and_persist_exempts_placed_obstacles_at_grasp_position_m_stacking_target(self):
        harness = _DummyHarness()
        harness._persistent_obstacles_identity = dict(harness.identity)
        harness._persistent_obstacles = {
            "placed_left_set1": {
                "id": "placed_left_set1",
                "min_m": [0.076, -0.224, 0.75],
                "max_m": [0.124, -0.176, 0.80],
                "margin_m": 0.003,
                "evidence": "auto_recorded_release",
            },
            "placed_right_other": {
                "id": "placed_right_other",
                "min_m": [-0.224, -0.224, 0.75],
                "max_m": [-0.176, -0.176, 0.80],
                "margin_m": 0.003,
                "evidence": "auto_recorded_release",
            },
        }
        args = {
            "stages": [
                {"id": "stack2", "kind": "move", "arm": "left", "grasp_position_m": [0.10, -0.20, 0.83]},
            ]
        }
        out = merge_and_persist_obstacles(harness, args)
        self.assertEqual([b["id"] for b in out["obstacles"]], ["placed_right_other"])

    def test_multistage_batch_preserves_placed_obstacle_for_transit_arc(self):
        harness = _DummyHarness()
        harness._persistent_obstacles_identity = dict(harness.identity)
        harness._persistent_obstacles = {
            "placed_left_doll1": {
                "id": "placed_left_doll1",
                "min_m": [-0.02, -0.32, 0.75],
                "max_m": [0.02, -0.28, 0.88],
                "margin_m": 0.003,
                "evidence": "auto_recorded_release",
            },
        }
        args = {
            "stages": [
                {"id": "touch_doll1", "kind": "move", "arm": "left", "grasp_position_m": [0.0, -0.30, 0.80]},
                {"id": "carry_across", "kind": "move", "arm": "left", "grasp_position_m": [0.12, -0.30, 0.82]},
            ]
        }
        out = merge_and_persist_obstacles(harness, args)
        self.assertEqual([b["id"] for b in out["obstacles"]], ["placed_left_doll1"])
        adapter = _DummyAdapter()
        # Stage 1 targets doll1 directly -> bypassed (no arc)
        plan1, arc1 = plan_move_with_obstacle_clearance(
            adapter, None, "left", {"path": {"position_tolerance_m": 0.005}, "input_grasp_pose": {"position_m": [0.0, -0.30, 0.80]}},
            {"position_m": [0.0, -0.30, 0.80], "quaternion_xyzw": [0, 0.7071, 0, 0.7071]},
            [-0.12, -0.30, 0.82], [0.0] * 6, out["obstacles"], None, [], cap_remaining=100,
        )
        self.assertIsNone(arc1)
        # Stage 2 transits from [-0.12, -0.30] to [0.12, -0.30] across doll1 -> arcs over doll1
        adapter2 = _DummyAdapter()
        plan2, arc2 = plan_move_with_obstacle_clearance(
            adapter2, None, "left", {"path": {"position_tolerance_m": 0.005}, "input_grasp_pose": {"position_m": [0.12, -0.30, 0.82]}},
            {"position_m": [0.12, -0.30, 0.82], "quaternion_xyzw": [0, 0.7071, 0, 0.7071]},
            [-0.12, -0.30, 0.82], [0.0] * 6, out["obstacles"], None, [], cap_remaining=100,
        )
        self.assertIsNotNone(arc2)

    def test_record_placed_obstacles_does_not_fabricate_implicit_boxes(self):
        from auriga.robot.motion.scene_obstacles import record_placed_obstacles
        harness = _DummyHarness()
        place_action = {
            "stages": [
                {"id": "place_down", "kind": "move", "arm": "right", "action": {"input_grasp_pose": {"position_m": [0.05, 0.02, 0.82]}}},
                {"id": "release", "kind": "gripper", "arm": "right", "action": {"gripper_events": [{"goal_normalized": 1.0}]}},
                {"id": "withdraw", "kind": "move", "arm": "right", "action": {"input_grasp_pose": {"position_m": [0.05, 0.02, 0.94]}}},
            ]
        }
        record_placed_obstacles(harness, place_action, {
            "status": "completed",
            "completed_stage_ids": ["place_down", "release", "withdraw"],
            "stage_results": {},
        })
        self.assertEqual(getattr(harness, "_persistent_obstacles", {}), {})

    def test_obstacles_from_object_table_auto_bridges_and_exempts_target(self):
        from auriga.robot.motion.scene_obstacles import filter_review_obstacles, obstacles_from_object_table
        harness = _DummyHarness()
        table = dict(
            columns=["id", "centre_m", "top_z", "bottom_z", "length", "width", "yaw_deg", "tilt_deg", "support", "quality"],
            table_z=0.765,
            rows=[
                ["o1", [0.00, -0.30, 0.810], 0.860, 0.765, 0.060, 0.060, 0.0, None, "table", "ok"],
                ["o2", [0.12, -0.30, 0.790], 0.820, 0.765, 0.050, 0.050, 0.0, None, "table", "ok"],
                ["o3", [-0.15, -0.20, 0.780], 0.790, 0.765, 0.040, 0.040, 0.0, None, "table", "low:depth"],
            ],
        )
        harness._obs3d_memo = ("obs-1", {"object_table": (table, None, {})})
        auto_boxes = obstacles_from_object_table(harness, "obs-1")
        # o3 is skipped because quality starts with low:
        self.assertEqual([b["id"] for b in auto_boxes], ["vggt_auto_o1", "vggt_auto_o2"])
        # Without explicit obstacle_object_ids, merge_and_persist_obstacles does not inject unrequested vggt_auto_* boxes
        args_no_explicit = {
            "observation_id": "obs-1",
            "stages": [
                {"id": "grasp_o2", "kind": "move", "arm": "right", "grasp_position_m": [0.12, -0.30, 0.80]},
            ],
        }
        self.assertNotIn("obstacles", merge_and_persist_obstacles(harness, args_no_explicit))
        # With explicit obstacle_object_ids=['o1'], o1 is converted into an obstacle box
        args_explicit = dict(args_no_explicit, obstacle_object_ids=["o1"])
        merged = merge_and_persist_obstacles(harness, args_explicit)
        self.assertEqual([b["id"] for b in merged["obstacles"]], ["o1"])
        explicit = obstacles_from_object_table(harness, "obs-1", object_ids=["o1"])
        self.assertEqual([b["id"] for b in filter_review_obstacles(explicit, {})], ["o1"])

    def test_robot_action_input_schema_accepts_obstacle_and_compliance_controls(self):
        from auriga.runtime.tools.registry import validator_for
        from auriga.runtime.actions.robot_action_schema import ROBOT_ACTION_TOOL

        validator = validator_for(ROBOT_ACTION_TOOL["inputSchema"])
        payload = {
            "mode": "preview",
            "observation_id": "obs-1",
            "obstacle_object_ids": ["o1", "o2"],
            "obstacle_policy": "stateless",
            "ignore_obstacle_ids": ["o3"],
            "target_exemption_preset": "normal",
            "clearance_preset": "high",
            "max_upward_dz_m": 0.04,
            "min_aperture_m": 0.002,
            "max_aperture_m": 0.080,
            "max_joint_travel_rad": 4.5,
            "max_backtracking_rad": 2.0,
            "max_tracking_divergence_m": 0.025,
            "stages": [
                {
                    "id": "transit",
                    "kind": "move",
                    "arm": "right",
                    "grasp_position_m": [0.12, -0.30, 0.85],
                    "angles_deg": [0.0, 0.0, 0.0],
                    "clearance_height_m": 0.16,
                    "max_upward_dz_m": 0.03,
                }
            ],
        }
        errors = list(validator.iter_errors(payload))
        self.assertEqual(errors, [])

    def test_stateless_obstacle_policy_applies_only_to_current_call_without_persisting(self):
        harness = _DummyHarness()
        table = dict(
            columns=["id", "centre_m", "top_z", "bottom_z", "length", "width", "yaw_deg", "tilt_deg", "support", "quality"],
            table_z=0.765,
            rows=[
                ["o1", [0.00, -0.30, 0.810], 0.860, 0.765, 0.060, 0.060, 0.0, None, "table", "ok"],
                ["o2", [0.12, -0.30, 0.790], 0.820, 0.765, 0.050, 0.050, 0.0, None, "table", "ok"],
            ],
        )
        harness._obs3d_memo = ("obs-1", {"object_table": (table, None, {})})
        args = {
            "observation_id": "obs-1",
            "obstacle_policy": "stateless",
            "obstacle_object_ids": ["o1", "o2"],
            "ignore_obstacle_ids": ["o2"],
            "target_exemption_preset": "tight",
            "stages": [
                {"id": "move1", "kind": "move", "arm": "right", "grasp_position_m": [0.15, -0.25, 0.85]},
            ],
        }
        compiled_stages = [{"id": "move1", "kind": "move", "arm": "right", "action": {"path": {}}}]
        merged = merge_and_persist_obstacles(harness, args, compiled_stages)
        self.assertEqual([b["id"] for b in merged["obstacles"]], ["o1"])
        self.assertAlmostEqual(compiled_stages[0]["action"]["path"]["target_exemption_radius_m"], 0.030)
        # Registry remains empty because policy == 'stateless'
        self.assertEqual(getattr(harness, "_persistent_obstacles", {}), {})


if __name__ == "__main__":
    unittest.main()
