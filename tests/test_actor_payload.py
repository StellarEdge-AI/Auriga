"""Control-information preservation for the opt-in actor JSON projection."""

from copy import deepcopy
import math
import unittest

from auriga.core.presentation.actor_payload import compact_action_result, compact_observation


class ActorPayloadTests(unittest.TestCase):
    def observation(self):
        return dict(
            bundle=dict(
                observation_id="obs-current", last_action_id="action-last", sim_step=123456789,
                sim_time_s=7.123456789, step_domain="adapter_control_step",
                task_instruction="Place the object inside the box, front facing left.",
                run_id="run", episode_id="ep", env_instance_id="env", env_generation=3,
                schema_version="0.1", robot_state_ref="artifact:state", world_kind="observed",
                cameras=[dict(camera_id="cam_head", frame_id="frame-current", rgb_ref="artifact:rgb",
                              width=640, height=480, pixel_transform="identity", sim_step=123456789,
                              capture_time="2026-10-03T00:00:00Z", clock_domain=None,
                              image_encoding=dict(quality=85, sha256="encoding-hash"),
                              reconstruction=dict(source_sha256="source-hash", warning="decoded from video"),
                              future_camera_warning="image may be stale")],
                future_bundle_warning="synchronization unknown"),
            robot_state=dict(frame_id="env_origin", tcp_config_id="arx-x5-link6-v1",
                             sim_step=123456789, arms=dict(left=dict(
                                 q_rad=[-1.234567891, 0.00000049], source_kind="measurement",
                                 ee_pose=dict(position_m=[0.123456789, -0.11111119, 0.9],
                                              quaternion_xyzw=[-0.5, 0.5, 0.5, 0.5], frame_id="env_origin",
                                              tcp_config_id="arx-x5-link6-v1", source_kind="measurement"),
                                 grasp_pose=dict(position_m=[0.123456789, -0.11111119, 0.7499],
                                                 angles_deg=[0.0, 0.0, 89.123456789], frame_id="env_origin",
                                                 source_kind="derived_from_measured_link6"),
                                 gripper=dict(command_normalized=1.0, command_source_kind="command",
                                              contact=None, retained_grasp=None)))),
            image_view=dict(source_roi=[10, 20, 30, 40], display_scale=4, width=120, height=160,
                            pixel_transform=[[4, 0, -41.5], [0, 4, -81.5], [0, 0, 1]],
                            original_rgb_ref="artifact:original", rgb_ref="artifact:crop",
                            reconstruction=dict(source_sha256="original-hash", warning="reconstructed")),
        )

    def test_observation_keeps_control_state_crop_geometry_and_unknown_fields(self):
        original = self.observation()
        before = deepcopy(original)
        result = compact_observation(original)
        self.assertEqual(original, before)
        self.assertEqual(set(result), set(original))
        for key in ("observation_id", "last_action_id", "sim_step", "task_instruction", "world_kind",
                    "env_generation", "future_bundle_warning"):
            self.assertEqual(result["bundle"][key], original["bundle"][key])
        self.assertEqual(result["bundle"]["sim_time_s"], 7.123457)
        camera = result["bundle"]["cameras"][0]
        for key in ("camera_id", "frame_id", "rgb_ref", "width", "height", "pixel_transform", "sim_step",
                    "reconstruction", "future_camera_warning"):
            self.assertEqual(camera[key], original["bundle"]["cameras"][0][key])
        self.assertNotIn("image_encoding", camera)
        self.assertNotIn("capture_time", camera)
        self.assertNotIn("robot_state_ref", result["bundle"])
        self.assertEqual(result["image_view"], original["image_view"])
        left = result["robot_state"]["arms"]["left"]
        self.assertEqual(left["gripper"], original["robot_state"]["arms"]["left"]["gripper"])
        self.assertEqual(left["ee_pose"]["source_kind"], "measurement")
        self.assertEqual(left["grasp_pose"]["source_kind"], "derived_from_measured_link6")
        self.assertEqual(left["ee_pose"]["frame_id"], "env_origin")
        self.assertEqual(left["ee_pose"]["tcp_config_id"], "arx-x5-link6-v1")
        self.assertEqual(left["q_rad"], [-1.234568, 0.0])
        result["image_view"]["source_roi"][0] = 999
        result["robot_state"]["arms"]["left"]["gripper"]["contact"] = True
        self.assertEqual(original, before)

    def test_action_keeps_partial_execution_failure_and_future_diagnostics(self):
        receipt = dict(status="unknown", reason="worker_disconnected_after_dispatch",
                       executed_steps=3, executed_native_steps=3, completed_native_steps=2,
                       executed_control_steps=27, execution_interval=dict(start_sim_step=100, end_sim_step=127),
                       execution_start_observation_id="obs-start", result_observation_id=None,
                       planner_failure_stage="execution", failed_waypoint_index=1,
                       position_error_m=0.004567891, orientation_error_rad=0.019876543,
                       waypoint_results=[dict(index=0, status="completed", position_error_m=0.000123456),
                                         dict(index=1, status="unknown", error="disconnect")],
                       future_diagnostic=dict(unknown=True, measured_force=None, phase="partial"),
                       collision_scope="No task-object collision proof", versions=dict(adapter="abc"),
                       step_semantics=dict(executed_steps="native count"), trace_sampling=dict(every=1),
                       planned_trace_ref="artifact:planned", dispatched_trace_ref="artifact:dispatched",
                       actual_trace_ref="artifact:actual", before_state_ref="artifact:before",
                       future_diagnostic_ref="artifact:future")
        original = dict(receipt=receipt, request=dict(action_id="action-a", based_on_observation_id="obs-a",
                                                      path=dict(waypoints=[dict(left=dict(position_m=[1, 2, 3]))])),
                        future_top_level=dict(error="preserved"))
        before = deepcopy(original)
        result = compact_action_result(original)
        self.assertEqual(original, before)
        self.assertEqual(result["request"], dict(action_id="action-a", based_on_observation_id="obs-a"))
        omitted = {"versions", "step_semantics", "trace_sampling", "planned_trace_ref", "dispatched_trace_ref",
                   "actual_trace_ref"}
        self.assertEqual(set(result["receipt"]), set(receipt) - omitted)
        for key in set(receipt) - omitted - {"position_error_m", "orientation_error_rad", "waypoint_results"}:
            self.assertEqual(result["receipt"][key], receipt[key])
        self.assertEqual(result["receipt"]["position_error_m"], 0.004568)
        self.assertEqual(result["receipt"]["orientation_error_rad"], 0.019877)
        self.assertEqual(result["receipt"]["waypoint_results"][1], receipt["waypoint_results"][1])
        self.assertEqual(result["future_top_level"], original["future_top_level"])
        result["receipt"]["future_diagnostic"]["unknown"] = False
        self.assertEqual(original, before)

    def test_only_finite_float_values_are_rounded(self):
        value = dict(extra=[True, False, 9876543210123456789, None, "1.23456789", 1.23456789,
                           float("nan"), float("inf"), float("-inf")])
        for project in (compact_observation, compact_action_result):
            result = project(value)["extra"]
            self.assertIs(result[0], True)
            self.assertIs(result[1], False)
            self.assertEqual(result[2:5], value["extra"][2:5])
            self.assertEqual(result[5], 1.234568)
            self.assertTrue(math.isnan(result[6]))
            self.assertEqual(result[7:], value["extra"][7:])
            self.assertLessEqual(abs(result[5] - value["extra"][5]), .5e-6)

    def test_missing_or_unknown_shapes_are_retained_without_invented_values(self):
        for project in (compact_observation, compact_action_result):
            for value in ({}, dict(receipt=None, request=None, bundle=None), [None, True, 3], None):
                self.assertEqual(project(value), value)
        action = dict(receipt=dict(status="failed", reason=None), request=dict(action_id="a"))
        result = compact_action_result(action)
        self.assertEqual(result["receipt"], action["receipt"])
        self.assertNotIn("based_on_observation_id", result["request"])


if __name__ == "__main__":
    unittest.main()
