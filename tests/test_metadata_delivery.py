"""Session references require a delivered baseline, never a stored observation."""
from copy import deepcopy
import json
import unittest

from auriga.core.presentation.observation_output import MetadataDelivery, observation_metadata


def observation(identifier="obs-1", generation=1):
    matrix = [[1., 0., 0., .123456789012345], [0., 1., 0., .2],
              [0., 0., 1., .3], [0., 0., 0., 1.]]
    calibration = dict(K=[[397.041259765625, 0., 320.], [0., 397.041259765625, 240.], [0., 0., 1.]],
        distortion=[0., 0., 0., 0., 0.], distortion_model="pinhole_no_distortion", image_size=[640, 480],
        optical_axes="+x right,+y down,+z forward", calibration_source="public sensor calibration")
    arm = dict(joint_names=["joint" + str(i) for i in range(1, 7)], source_kind="measurement",
        q_rad=[.123456789012345] * 6, qdot_rad_s=[.00000123456789] * 6,
        ee_pose=dict(position_m=[.1, .2, .3], quaternion_xyzw=[0., 0., 0., 1.],
            frame_id="env_origin", tcp_config_id="arx-x5-link6-v1", source_kind="measurement"),
        gripper=dict(joint_names=["joint7", "joint8"], position_m=[.012345678901, .02],
            position_source_kind="measurement", goal_normalized=.5, goal_source_kind="command"),
        grasp_pose=dict(position_m=[.1, .2, .4], angles_deg=[0., 0., 90.], frame_id="env_origin",
            source_kind="derived_from_measured_link6", convention="robot_move_grasp", contact_region="pad_center",
            contact_depth_m=0., contact_point_meaning="center_between_fingers_at_distal_15mm_pad_center"))
    value = dict(bundle=dict(observation_id=identifier, run_id="run", episode_id="episode", env_instance_id="env",
        env_generation=generation, sim_step=10, action_budget=dict(native_steps_remaining=700)),
        robot_state=dict(arms=dict(left=deepcopy(arm), right=deepcopy(arm)), sim_step=10,
            frame_id="env_origin", tcp_config_id="arx-x5-link6-v1", units=dict(position="m", joints="rad")),
        camera_calibration=dict(profile_id="public-cal-v1", reference_frame_id="env_origin",
            source_kind="public_calibration", full_ref="artifact:calibration", cameras={
                "head": deepcopy(calibration), "left": deepcopy(calibration), "right": deepcopy(calibration)}),
        camera_pose_sample=dict(sim_step=10, reference_frame_id="env_origin", full_ref="artifact:pose-" + identifier,
            cameras={name: dict(T_world_camera=deepcopy(matrix), frame_id=identifier + "-" + name,
                source_kind="measurement") for name in ("head", "left", "right")}))
    return observation_metadata(value, current_observation_id=identifier,
        current_action_budget=dict(native_steps_remaining=700), compact_geometry=True)


def clocked_observation(identifier="obs-1"):
    value = observation(identifier)
    bundle = value["bundle"]
    bundle.update(schema_version="0.1", robot_model_id="arx-x5",
                  observation_profile="rgb_proprio_calibrated_v1",
                  clock_domain="env/generation-1", step_domain="adapter_control_step",
                  calibration_id="a" * 64, robot_model_ref="artifact:model",
                  data_origin="robodojo_runtime", task_instruction="Keep the task visible.")
    bundle["cameras"] = [dict(camera_id=name, width=640, height=480,
        pixel_transform="identity", control_dt_s=.004, step_domain="adapter_control_step",
        frame_id=identifier + "-" + name, capture_time=identifier + "-time",
        sim_step=10, clock_domain=None, rgb_available=True, calibration_available=True,
        pose_sample_available=True, synchronization="physics paused; camera fence")
        for name in ("head", "left", "right")]
    for budget in (bundle["action_budget"], value["current_action_budget"]):
        budget.update(semantics="official native steps; separate from tool requests and model-call budget; reserves are not included",
                      source="native_execution_accounting", unknown_values_are_null=True,
                      native_steps_limit=1000, native_steps_used=300, per_request_native_limit=250)
    value["robot_state"]["step_domain"] = "adapter_control_step"
    value["camera_pose_sample"]["step_domain"] = "adapter_control_step"
    return value


class MetadataDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.delivery = MetadataDelivery()
        self.epoch = ({"compaction": "epoch-1"}, "app-1", "transport-1", None)

    def prepare(self, value, **options):
        return self.delivery.prepare(value, epoch=options.pop("epoch", self.epoch), **options)

    def establish(self, value=None):
        first = self.prepare(value or observation())
        self.assertTrue(self.delivery.commit(first))
        return first

    def test_first_delivery_is_exact_and_preparation_without_commit_cannot_be_referenced(self):
        first = observation()
        before = deepcopy(first)
        prepared = self.prepare(first)
        self.assertEqual(prepared.metadata, first)
        # A discarded/overwritten wait sample has not entered the actor history.
        second = self.prepare(observation("obs-2"))
        self.assertNotIn("metadata_delivery", second.metadata)
        self.assertIn("K", second.metadata["camera_calibration"]["cameras"]["head"])
        self.assertEqual(first, before)

    def test_references_only_static_fields_and_preserves_live_measurement_precision(self):
        self.establish()
        second = observation("obs-2")
        second["robot_state"]["arms"]["left"]["q_rad"][0] = .987654321098765
        second["camera_pose_sample"]["cameras"]["left"]["T_world_camera"][0][3] = .987654321098765
        second["current_action_budget"]["native_steps_remaining"] = 612
        before = deepcopy(second)
        projected = self.prepare(second).metadata
        self.assertEqual(second, before)
        refs = projected["metadata_delivery"]["static_refs"]
        self.assertEqual(set(refs), {"camera_calibration", "robot_state"})
        self.assertTrue(all(ref["observation_id"] == "obs-1" for ref in refs.values()))
        self.assertNotIn("K", projected["camera_calibration"]["cameras"]["head"])
        self.assertEqual(projected["camera_calibration"]["full_ref"], "artifact:calibration")
        self.assertEqual(projected["camera_pose_sample"], second["camera_pose_sample"])
        state = projected["robot_state"]["arms"]["left"]
        self.assertNotIn("joint_names", state)
        for field in ("q_rad", "qdot_rad_s", "source_kind"):
            self.assertEqual(state[field], second["robot_state"]["arms"]["left"][field])
        self.assertEqual(state["gripper"]["position_m"], second["robot_state"]["arms"]["left"]["gripper"]["position_m"])
        self.assertEqual(state["gripper"]["position_source_kind"], "measurement")
        self.assertEqual(state["gripper"]["goal_source_kind"], "command")
        self.assertEqual(state["grasp_pose"]["source_kind"], "derived_from_measured_link6")
        self.assertEqual(projected["current_action_budget"], second["current_action_budget"])
        self.assertTrue(projected["observation_is_current"])
        self.assertIn("unchanged intrinsics use metadata_delivery.static_refs", projected["geometry_delivery"]["omitted"])

    def test_changed_intrinsics_or_joint_order_are_delivered_inline_before_new_reference(self):
        self.establish()
        changed = observation("obs-2")
        changed["camera_calibration"]["cameras"]["head"]["K"][0][0] = 500.123456789
        changed["robot_state"]["arms"]["left"]["joint_names"].reverse()
        projection = self.prepare(changed)
        self.assertEqual(projection.metadata, changed)
        self.assertTrue(self.delivery.commit(projection))
        third = deepcopy(changed)
        third["bundle"]["observation_id"] = "obs-3"
        refs = self.prepare(third).metadata["metadata_delivery"]["static_refs"]
        self.assertTrue(all(ref["observation_id"] == "obs-2" for ref in refs.values()))

    def test_each_epoch_component_and_environment_generation_invalidates_baseline(self):
        epochs = [(dict(compaction="epoch-2"), "app-1", "transport-1", None),
                  (self.epoch[0], "app-2", "transport-1", None),
                  (self.epoch[0], "app-1", "transport-2", None),
                  (self.epoch[0], "app-1", "transport-1", "compact-call")]
        for epoch in epochs:
            with self.subTest(epoch=epoch):
                self.establish()
                changed = observation("obs-2")
                self.assertEqual(self.prepare(changed, epoch=epoch).metadata, changed)
        self.establish()
        reset = observation("obs-reset", generation=2)
        self.assertEqual(self.prepare(reset).metadata, reset)

    def test_old_pending_delivery_cannot_restore_baseline_after_context_invalidation(self):
        pending = self.prepare(observation())
        self.delivery.invalidate()
        self.assertFalse(self.delivery.commit(pending))
        current = self.prepare(observation("obs-2"), epoch="new-context")
        self.assertNotIn("metadata_delivery", current.metadata)
        self.assertFalse(self.delivery.commit(pending))

    def test_full_exception_delivery_ignores_reference_and_teaches_only_on_commit(self):
        self.establish()
        exception = observation("obs-error")
        exception["robot_state"]["arms"]["left"]["future_warning"] = dict(status="unknown", reason="sensor_fault")
        full = self.prepare(exception, force_full=True)
        self.assertEqual(full.metadata, exception)
        self.assertTrue(self.delivery.commit(full))
        next_value = observation("obs-next")
        refs = self.prepare(next_value).metadata["metadata_delivery"]["static_refs"]
        self.assertTrue(all(ref["observation_id"] == "obs-error" for ref in refs.values()))

    def test_unknown_fields_live_sources_and_historical_budget_never_use_baseline_values(self):
        self.establish()
        history = observation("obs-history")
        history["current_observation_id"] = "obs-current"
        history["observation_is_current"] = False
        history["current_action_budget"] = dict(native_steps_remaining=12)
        history["camera_calibration"]["future_warning"] = dict(status="uncertain")
        history["camera_calibration"]["cameras"]["head"]["future_quality"] = "degraded"
        history["robot_state"]["arms"]["left"]["source_kind"] = "estimate"
        projected = self.prepare(history).metadata
        self.assertEqual(projected["camera_calibration"]["future_warning"], dict(status="uncertain"))
        self.assertEqual(projected["camera_calibration"]["cameras"]["head"]["future_quality"], "degraded")
        self.assertEqual(projected["robot_state"]["arms"]["left"]["source_kind"], "estimate")
        self.assertFalse(projected["observation_is_current"])
        self.assertEqual(projected["current_observation_id"], "obs-current")
        self.assertEqual(projected["bundle"]["action_budget"]["native_steps_remaining"], 700)
        self.assertEqual(projected["current_action_budget"]["native_steps_remaining"], 12)
        self.assertEqual(projected["metadata_delivery"]["read_full"]["observation_id"], "obs-history")

    def test_missing_observation_identity_cannot_establish_reference_and_nonfinite_static_is_inline(self):
        value = observation()
        value["bundle"].pop("observation_id")
        prepared = self.prepare(value)
        self.delivery.commit(prepared)
        self.assertEqual(self.prepare(value).metadata, value)
        value = observation("obs-nan")
        value["camera_calibration"]["cameras"]["head"]["K"][0][0] = float("nan")
        prepared = self.prepare(value)
        self.delivery.commit(prepared)
        next_value = self.prepare(value).metadata
        self.assertIn("K", next_value["camera_calibration"]["cameras"]["head"])

    def test_measured_payload_reduction_does_not_require_float_rounding(self):
        first = self.establish().metadata
        second = self.prepare(observation("obs-2")).metadata
        encode = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        self.assertLess(len(encode(second)), len(encode(first)) * .85)
        self.assertEqual(second["robot_state"]["arms"]["left"]["q_rad"][0], .123456789012345)

    def test_unknown_context_does_not_reuse_or_preserve_an_older_baseline(self):
        self.establish()
        value = observation("obs-unknown-context")
        unknown = self.prepare(value, epoch=None)
        self.assertEqual(unknown.metadata, value)
        self.assertFalse(self.delivery.commit(unknown))
        restored = self.prepare(observation("obs-restored"))
        self.assertNotIn("metadata_delivery", restored.metadata)

    def test_unknown_static_values_and_unfamiliar_projection_metadata_stay_inline(self):
        self.establish()
        value = observation("obs-unknown-values")
        value["camera_calibration"]["cameras"]["head"]["K"] = None
        value["robot_state"]["arms"]["left"]["joint_names"] = []
        self.delivery.commit(self.prepare(value))
        value["bundle"]["observation_id"] = "obs-next"
        projected = self.prepare(value).metadata
        self.assertIsNone(projected["camera_calibration"]["cameras"]["head"]["K"])
        self.assertEqual(projected["robot_state"]["arms"]["left"]["joint_names"], [])
        value["metadata_delivery"] = dict(future_warning="unknown producer")
        projected = self.prepare(value)
        self.assertEqual(projected.metadata, value)
        self.assertFalse(self.delivery.commit(projected))

    def establish_clocked_conventions(self):
        # Geometry wording and delivery prose change on the second reply.
        # Both strings must actually be delivered before this group can cite them.
        for identifier in ("obs-1", "obs-2"):
            projection = self.prepare(clocked_observation(identifier))
            self.assertTrue(self.delivery.commit(projection))
        return self.prepare(clocked_observation("obs-3"))

    def test_grouped_bundle_prose_and_clock_conventions_preserve_live_evidence(self):
        projection = self.establish_clocked_conventions()
        actual = projection.metadata
        original = clocked_observation("obs-3")
        ref = actual["metadata_delivery"]["static_refs"]["bundle"]
        self.assertEqual(ref["observation_id"], "obs-2")
        for key in ("run_id", "schema_version", "clock_domain", "calibration_id"):
            self.assertNotIn(key, actual["bundle"])
        for key in ("observation_id", "sim_step", "robot_model_ref", "data_origin", "task_instruction"):
            self.assertEqual(actual["bundle"][key], original["bundle"][key])
        for delivered, source in zip(actual["bundle"]["cameras"], original["bundle"]["cameras"]):
            for key in ("camera_id", "capture_time", "frame_id", "sim_step", "clock_domain",
                        "synchronization", "rgb_available", "calibration_available", "pose_sample_available"):
                self.assertEqual(delivered[key], source[key])
            self.assertNotIn("width", delivered)
        for delivered, source in ((actual["bundle"]["action_budget"], original["bundle"]["action_budget"]),
                                  (actual["current_action_budget"], original["current_action_budget"])):
            for key in ("source", "native_steps_remaining", "native_steps_used", "native_steps_limit",
                        "per_request_native_limit"):
                self.assertEqual(delivered[key], source[key])
            self.assertNotIn("semantics", delivered)
        self.assertNotIn("observation_budget_semantics", actual)
        self.assertNotIn("omitted", actual["geometry_delivery"])
        self.assertNotIn("semantics", actual["metadata_delivery"])
        self.assertEqual(actual["camera_pose_sample"]["cameras"], original["camera_pose_sample"]["cameras"])
        self.assertEqual(actual["geometry_delivery"]["read_full"]["observation_id"], "obs-3")
        encode = lambda v: json.dumps(v, sort_keys=True, separators=(",", ":")).encode()
        self.assertLess(len(encode(actual)), len(encode(original)) - 1000)

    def test_camera_conventions_follow_ids_and_keep_original_list_order(self):
        self.establish_clocked_conventions()
        value = clocked_observation("obs-reordered")
        value["bundle"]["cameras"].reverse()
        actual = self.prepare(value).metadata
        self.assertIn("bundle", actual["metadata_delivery"]["static_refs"])
        self.assertEqual([camera["camera_id"] for camera in actual["bundle"]["cameras"]],
                         [camera["camera_id"] for camera in value["bundle"]["cameras"]])
        self.assertTrue(all("width" not in camera for camera in actual["bundle"]["cameras"]))
        value = clocked_observation("obs-resized")
        value["bundle"]["cameras"][0]["width"] = 800
        pending = self.prepare(value)
        self.assertNotIn("bundle", pending.metadata["metadata_delivery"]["static_refs"])
        self.assertEqual(pending.metadata["bundle"]["cameras"], value["bundle"]["cameras"])
        # Discarding the resized sample cannot teach the changed camera convention.
        changed_again = self.prepare(value)
        self.assertNotIn("bundle", changed_again.metadata["metadata_delivery"]["static_refs"])
        self.assertTrue(self.delivery.commit(changed_again))
        next_value = deepcopy(value)
        next_value["bundle"]["observation_id"] = "obs-resized-next"
        self.assertEqual(self.prepare(next_value).metadata["metadata_delivery"]["static_refs"]["bundle"]["observation_id"],
                         "obs-resized")

    def test_unknown_camera_budget_and_prose_fields_remain_inline(self):
        self.establish_clocked_conventions()
        value = clocked_observation("obs-unknown")
        value["bundle"]["future_diagnostic"] = {"uncertain": True}
        value["bundle"]["schema_version"] = "future"
        value["bundle"]["cameras"][0].update(width=None, pixel_transform=[[2, 0], [0, 2]],
                                            control_dt_s=float("nan"), future_fault="unknown")
        value["current_action_budget"]["semantics"] = "unknown budget accounting"
        value["observation_budget_semantics"] = "future historical budget rule"
        value["geometry_delivery"]["omitted"] = {"unknown": "geometry rule"}
        self.assertTrue(self.delivery.commit(self.prepare(value)))
        actual = self.prepare(value).metadata
        # Nonfinite live values make byte comparison conservative, never an error.
        self.assertNotIn("bundle", actual["metadata_delivery"]["static_refs"])
        self.assertEqual(actual["bundle"]["future_diagnostic"], value["bundle"]["future_diagnostic"])
        self.assertEqual(actual["bundle"]["schema_version"], "future")
        self.assertIsNone(actual["bundle"]["cameras"][0]["width"])
        self.assertEqual(actual["bundle"]["cameras"][0]["future_fault"], "unknown")
        self.assertEqual(actual["current_action_budget"]["semantics"], "unknown budget accounting")
        self.assertEqual(actual["geometry_delivery"]["omitted"], value["geometry_delivery"]["omitted"])

    def test_duplicate_camera_ids_and_changed_membership_never_remove_camera_list(self):
        self.establish_clocked_conventions()
        value = clocked_observation("obs-duplicate")
        value["bundle"]["cameras"][1]["camera_id"] = value["bundle"]["cameras"][0]["camera_id"]
        self.assertTrue(self.delivery.commit(self.prepare(value)))
        projected = self.prepare(value).metadata
        self.assertEqual(projected["bundle"]["cameras"], value["bundle"]["cameras"])
        value = clocked_observation("obs-missing-camera")
        value["bundle"]["cameras"].pop()
        projected = self.prepare(value).metadata
        self.assertEqual(projected["bundle"]["cameras"], value["bundle"]["cameras"])

    def test_grouped_conventions_respect_full_historical_and_invalidation_contracts(self):
        pending = self.establish_clocked_conventions()
        value = clocked_observation("obs-history")
        value["current_observation_id"] = "obs-current"
        value["observation_is_current"] = False
        value["current_action_budget"]["native_steps_remaining"] = 12
        actual = self.prepare(value).metadata
        self.assertFalse(actual["observation_is_current"])
        self.assertEqual(actual["current_action_budget"]["native_steps_remaining"], 12)
        self.assertEqual(actual["bundle"]["action_budget"]["native_steps_remaining"], 700)
        self.assertEqual(self.prepare(value, force_full=True).metadata, value)
        self.delivery.invalidate()
        self.assertFalse(self.delivery.commit(pending))
        self.assertEqual(self.prepare(value).metadata, value)

    def test_incomplete_identity_and_unprofitable_small_groups_remain_inline(self):
        value = dict(bundle=dict(observation_id="obs-small", run_id="r", episode_id="e",
                                env_instance_id="i", env_generation=1))
        self.delivery.commit(self.prepare(value))
        self.assertEqual(self.prepare(value).metadata, value)
        self.establish_clocked_conventions()
        value = clocked_observation("obs-missing-identity")
        value["bundle"].pop("episode_id")
        self.assertEqual(self.prepare(value).metadata, value)


if __name__ == "__main__":
    unittest.main()
