import unittest

from auriga.runtime.actions.action_state_ledger import (
    attach_execution_ledger,
    attach_preview_ledger,
    record_execution_outcome,
)


class _DummyHarness:
    def __init__(self):
        self.identity = {"run_id": "r1", "episode_id": "ep0", "env_instance_id": "e1", "env_generation": 0}


class ActionStateLedgerTest(unittest.TestCase):
    def test_records_failed_hotspot_and_warns_on_repeated_preview_within_15mm(self):
        harness = _DummyHarness()
        action = {
            "stages": [
                {
                    "id": "descend_grasp",
                    "kind": "move",
                    "arm": "right",
                    "action": {
                        "path": {
                            "waypoints": [
                                {"right": {"position_m": [0.10, -0.30, 0.82], "quaternion_xyzw": [0, 0, 0, 1]}}
                            ]
                        }
                    },
                }
            ]
        }
        receipt = {
            "status": "stopped",
            "batch_status": "stopped",
            "stop_reason": "endpoint_not_reached",
            "failed_stage_id": "descend_grasp",
            "completed_stage_ids": [],
            "executed_native_steps": 18,
        }
        record_execution_outcome(harness, "plan-1", action, receipt)
        record_execution_outcome(harness, "plan-2", action, receipt)

        # Next preview targeting 8 mm away triggers repeated_hotspot warning
        next_action = {
            "stages": [
                {
                    "id": "retry_grasp",
                    "kind": "move",
                    "arm": "right",
                    "action": {
                        "path": {
                            "waypoints": [
                                {"right": {"position_m": [0.105, -0.305, 0.822], "quaternion_xyzw": [0, 0, 0, 1]}}
                            ]
                        }
                    },
                }
            ]
        }
        summary = attach_preview_ledger(harness, next_action, {"status": "planned"})
        self.assertIn("episode_ledger", summary)
        self.assertEqual(summary["episode_ledger"]["actions_executed"], 2)
        self.assertIn("repeat_failure_warning", summary)
        self.assertEqual(summary["repeat_failure_warning"][0]["severity"], "repeated_hotspot")
        self.assertEqual(summary["repeat_failure_warning"][0]["prior_failures"], 2)

        res = attach_execution_ledger(harness, {"status": "completed"})
        self.assertEqual(res["episode_ledger"]["actions_executed"], 2)

        # Vertical Z nudge (+3.2 cm at same XY) still matches cylindrical hotspot
        z_nudge_action = {
            "stages": [
                {
                    "id": "retry_z_nudge",
                    "kind": "move",
                    "arm": "right",
                    "action": {
                        "path": {
                            "waypoints": [
                                {"right": {"position_m": [0.102, -0.302, 0.852], "quaternion_xyzw": [0, 0, 0, 1]}}
                            ]
                        }
                    },
                }
            ]
        }
        z_summary = attach_preview_ledger(harness, z_nudge_action, {"status": "planned"})
        self.assertIn("repeat_failure_warning", z_summary)

        # Completing a move at the same XY evicts the hotspot
        completed_receipt = {
            "status": "completed",
            "batch_status": "completed",
            "completed_stage_ids": ["retry_z_nudge"],
            "executed_native_steps": 14,
        }
        record_execution_outcome(harness, "plan-3", z_nudge_action, completed_receipt)
        post_summary = attach_preview_ledger(harness, z_nudge_action, {"status": "planned"})
        self.assertNotIn("repeat_failure_warning", post_summary)

    def test_attribute_contact_block_3d_and_compute_object_table_delta(self):
        from auriga.runtime.actions.action_state_ledger import attribute_contact_block_3d, compute_object_table_delta
        harness = _DummyHarness()
        before_table = dict(
            columns=["id", "centre_m", "top_z", "bottom_z", "length", "width", "yaw_deg", "tilt_deg", "support", "quality"],
            table_z=0.765,
            rows=[
                ["o1", [0.10, -0.30, 0.785], 0.815, 0.765, 0.050, 0.050, 0.0, None, "table", "ok"],
                ["o2", [-0.15, -0.25, 0.780], 0.800, 0.765, 0.040, 0.040, 0.0, None, "table", "ok"],
            ],
        )
        harness._obs3d_memo = ("obs-1", {"object_table": (before_table, None, {})})
        action = {
            "based_on_observation_id": "obs-1",
            "stages": [
                {
                    "id": "descend_o1",
                    "kind": "move",
                    "arm": "right",
                    "action": {"input_grasp_pose": {"position_m": [0.112, -0.292, 0.790]}},
                }
            ],
        }
        receipt = {
            "status": "failed",
            "batch_status": "failed",
            "reason": "tracking_error",
            "failed_stage_id": "descend_o1",
            "completed_stage_ids": [],
            "executed_native_steps": 22,
            "contact_block_summary": {
                "stage_id": "descend_o1",
                "arm": "right",
                "blocked_above_target": True,
                "actual_contact_position_m": [0.112, -0.292, 0.819],
            },
        }
        record_execution_outcome(harness, "plan-3d", action, receipt)
        attr = receipt["contact_block_summary"]["contact_object_3d"]
        self.assertEqual(attr["id"], "o1")
        self.assertAlmostEqual(attr["height_above_top_m"], 0.004, places=3)
        self.assertEqual(attr["xy_offset_from_centre_m"], [0.012, 0.008])

        # Compute before/after 3D object table delta: o1 lifted by +6 cm, o2 unchanged
        after_table = dict(
            columns=before_table["columns"],
            table_z=0.765,
            rows=[
                ["o1", [0.105, -0.295, 0.845], 0.875, 0.825, 0.050, 0.050, 0.0, None, "table", "ok"],
                ["o2", [-0.150, -0.250, 0.781], 0.801, 0.765, 0.040, 0.040, 0.0, None, "table", "ok"],
                ["o3", [0.050, -0.100, 0.920], 0.950, 0.890, 0.050, 0.030, 0.0, None, "held_left", "held_fk_depth"],
            ],
        )
        delta = compute_object_table_delta(before_table, after_table)
        self.assertEqual(len(delta["moved"]), 1)
        self.assertEqual(delta["moved"][0]["before_id"], "o1")
        self.assertTrue(delta["moved"][0]["lifted"])
        self.assertEqual(delta["unchanged_count"], 1)
        self.assertEqual(delta["vacated"], [])
        self.assertEqual(delta["appeared"], [])


if __name__ == "__main__":
    unittest.main()
