"""Conservative actor projections and their immutable evidence boundary."""

import base64
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from auriga.core.presentation.actor_payload import lean_action_result, lean_observation
from auriga.runtime.harness import Harness
from auriga.core.storage.store import Store
from tests.test_framework_candidates import WaitFixtureRobot, text_values


TRACE_KINDS = ("planned_trace", "dispatched_trace", "actual_trace", "before_state")


class LeanProjectionTests(unittest.TestCase):
    def test_action_preserves_precision_request_errors_and_unknown_diagnostics(self):
        source = dict(
            request=dict(action_id="action", based_on_observation_id="observation", timeout_s=1.123456789,
                         path=dict(waypoints=[dict(position_m=[.12345678912345678, -1e-12, 1e20])])),
            receipt=dict(status="stopped", reason="tracking_guard", completed_native_steps=3,
                         versions=dict(adapter="v1"), step_semantics=dict(native_hz=25.123456789),
                         trace_sampling=dict(stride=2, measured=True),
                         tracking_guard=dict(error_rad=.0000000123456789, limit_rad=.123456789),
                         diagnostics=dict(versions="diagnostic version", actual_trace_ref="artifact:nested",
                                          sha256="diagnostic hash", residual_m=1e-11),
                         unknown_trace_ref="artifact:future"),
            preview=dict(trace_ref="artifact:preview"),
            error=dict(code="partial_execution", value=.9876543212345678),
        )
        source["receipt"].update({kind + "_ref": "artifact:" + kind for kind in TRACE_KINDS})
        original = deepcopy(source)
        expected = deepcopy(source)
        del expected["receipt"]["versions"]
        for kind in TRACE_KINDS:
            del expected["receipt"][kind + "_ref"]
            expected["receipt"][kind + "_available"] = True
        projected = lean_action_result(source)
        self.assertEqual(projected, expected)
        self.assertEqual(source, original)
        projected["request"]["path"]["waypoints"][0]["position_m"].append(42)
        projected["receipt"]["diagnostics"]["versions"] = "changed"
        self.assertEqual(source, original)

    def test_trace_availability_distinguishes_null_absent_and_unknown_values(self):
        for kind in TRACE_KINDS:
            reference, available = kind + "_ref", kind + "_available"
            for value in (None, "artifact:abc", "https://example/trace", "", 0, False,
                          {"artifact": "abc"}, ["artifact:abc"]):
                with self.subTest(kind=kind, value=value):
                    source = dict(receipt={reference: value})
                    expected = {available: value is not None} if value is None or value == "artifact:abc" else {reference: value}
                    self.assertEqual(lean_action_result(source), dict(receipt=expected))
            self.assertEqual(lean_action_result(dict(receipt={})), dict(receipt={}))
            for conflict in (False, True, None, "unknown"):
                source = dict(receipt={reference: "artifact:abc", available: conflict})
                self.assertEqual(lean_action_result(source), source)

    def observation(self):
        camera = dict(camera_id="head", rgb_ref="artifact:rgb", calibration_ref="artifact:calibration",
                      pose_sample_ref="artifact:pose", width=640, height=480,
                      image_encoding=dict(format="webp"), sha256="hash", source_sha256="source",
                      capture_time=1.123456789123, clock_domain="sim",
                      reconstruction=dict(sha256="nested-hash", warning="lossy", quality=90))
        return dict(bundle=dict(schema_version="0.1", run_id="run", episode_id="episode",
                                env_instance_id="env", robot_state_ref="artifact:state",
                                robot_model_ref="artifact:model", cameras=[camera],
                                image_reconstruction=dict(source_sha256="retain", error=.123456789)),
                    robot_state=dict(q_rad=[.123456789123, -1e-12]),
                    camera_calibration=dict(K=[[1.123456789, 0, 320.123456789]]),
                    camera_pose_sample=dict(T_world_camera=[1e-12, .987654321]),
                    image_view=dict(camera_id="head", rgb_ref="artifact:crop", original_rgb_ref="artifact:rgb",
                                    encoding=dict(format="webp"), image_encoding="jpeg", sha256="crop-hash",
                                    source_sha256="crop-source", source_roi=[10, 20, 100, 200], display_scale=2,
                                    pixel_transform=[[2, 0, -19.5], [0, 2, -39.5], [0, 0, 1]],
                                    reconstruction=dict(warning="reconstructed", sha256="nested", quality=80)),
                    unknown=dict(rgb_ref="artifact:unknown", sha256="unknown-hash"))

    def test_observation_removes_only_known_encoding_and_resolved_references(self):
        source = self.observation()
        original = deepcopy(source)
        expected = deepcopy(source)
        del expected["bundle"]["robot_state_ref"]
        expected["bundle"]["robot_state_available"] = True
        camera = expected["bundle"]["cameras"][0]
        for key in ("image_encoding", "sha256", "source_sha256"):
            del camera[key]
        for kind in ("rgb", "calibration", "pose_sample"):
            del camera[kind + "_ref"]
            camera[kind + "_available"] = True
        view = expected["image_view"]
        for key in ("encoding", "image_encoding", "sha256", "source_sha256"):
            del view[key]
        for kind in ("rgb", "original_rgb"):
            del view[kind + "_ref"]
            view[kind + "_available"] = True
        projected = lean_observation(source)
        self.assertEqual(projected, expected)
        self.assertEqual(source, original)
        projected["robot_state"]["q_rad"].append(42)
        projected["image_view"]["reconstruction"]["warning"] = "changed"
        self.assertEqual(source, original)

    def test_expansion_required_before_state_or_calibration_reference_elision(self):
        for key in ("robot_state", "camera_calibration", "camera_pose_sample"):
            for value in (None, [], "not expanded", 7):
                with self.subTest(key=key, value=value):
                    source = self.observation()
                    source[key] = value
                    projected = lean_observation(source)
                    if key == "robot_state":
                        self.assertEqual(projected["bundle"]["robot_state_ref"], "artifact:state")
                        self.assertNotIn("robot_state_available", projected["bundle"])
                    else:
                        reference = "calibration_ref" if key == "camera_calibration" else "pose_sample_ref"
                        self.assertEqual(projected["bundle"]["cameras"][0][reference], source["bundle"]["cameras"][0][reference])
        source = self.observation()
        for key in ("robot_state", "camera_calibration", "camera_pose_sample"):
            del source[key]
        projected = lean_observation(source)
        self.assertIn("robot_state_ref", projected["bundle"])
        self.assertIn("calibration_ref", projected["bundle"]["cameras"][0])
        self.assertIn("pose_sample_ref", projected["bundle"]["cameras"][0])

    def test_second_camera_distinct_calibration_and_pose_refs_remain_addressable(self):
        source = self.observation()
        same = deepcopy(source["bundle"]["cameras"][0])
        same["camera_id"] = "same-calibration"
        different = dict(same, camera_id="wrist", calibration_ref="artifact:wrist-calibration", pose_sample_ref="artifact:wrist-pose")
        source["bundle"]["cameras"].extend([same, different])
        cameras = lean_observation(source)["bundle"]["cameras"]
        self.assertTrue(cameras[1]["calibration_available"])
        self.assertTrue(cameras[1]["pose_sample_available"])
        self.assertEqual(cameras[2]["calibration_ref"], "artifact:wrist-calibration")
        self.assertEqual(cameras[2]["pose_sample_ref"], "artifact:wrist-pose")
        self.assertNotIn("calibration_available", cameras[2])
        self.assertNotIn("pose_sample_available", cameras[2])

    def test_observation_reference_null_unknown_and_conflict_fallbacks(self):
        locations = (("bundle", "robot_state"), ("camera", "rgb"), ("camera", "calibration"),
                     ("camera", "pose_sample"), ("image_view", "rgb"), ("image_view", "original_rgb"))
        for location, kind in locations:
            source = self.observation()
            target = source["bundle"]["cameras"][0] if location == "camera" else source[location]
            del target[kind + "_ref"]
            projected = lean_observation(source)
            actual = projected["bundle"]["cameras"][0] if location == "camera" else projected[location]
            self.assertNotIn(kind + "_available", actual)
            for value in (None, "artifact:known", "opaque-reference", {"ref": "artifact:dict"}):
                for conflict in (False, True):
                    with self.subTest(location=location, kind=kind, value=value, conflict=conflict):
                        source = self.observation()
                        target = source["bundle"]["cameras"][0] if location == "camera" else source[location]
                        target[kind + "_ref"] = value
                        if conflict:
                            target[kind + "_available"] = "existing diagnostic"
                        projected = lean_observation(source)
                        actual = projected["bundle"]["cameras"][0] if location == "camera" else projected[location]
                        if not conflict and (value is None or value == "artifact:known"):
                            self.assertNotIn(kind + "_ref", actual)
                            self.assertIs(actual[kind + "_available"], value is not None)
                        else:
                            self.assertEqual(actual[kind + "_ref"], value)
                            if conflict:
                                self.assertEqual(actual[kind + "_available"], "existing diagnostic")

    def test_nonstandard_shapes_are_deep_copied_without_recursive_elision(self):
        for source in (None, [dict(versions="keep", rgb_ref="artifact:keep", value=.123456789)],
                       dict(bundle=[{"sha256": "keep"}], receipt="unknown", image_view=[{"encoding": "keep"}])):
            for project in (lean_observation, lean_action_result):
                with self.subTest(source=source, project=project.__name__):
                    self.assertEqual(project(source), source)
                    if isinstance(source, (list, dict)):
                        self.assertIsNot(project(source), source)


class LeanHarnessTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        identity = dict(run_id="lean-fixture", episode_id="ep", env_instance_id="env", env_generation=1)
        self.store = Store(self.root / "store", identity)
        self.addCleanup(self.store.close)
        self.robot = WaitFixtureRobot(self.root)
        self.harness = Harness(self.store, self.robot)
        self.harness.start()

    def test_default_lean_preserves_program_storage_and_reads_traces_by_action_id(self):
        self.assertEqual(self.harness.actor_payload_profile, "lean_v1")
        args = dict(action_id="wait", observation_id=self.harness.latest["observation_id"], native_steps=5)
        model_values = text_values(self.harness.dispatch("robot_wait", args, dict(tool_call_id="model")))
        request = self.store.read("action_request", "wait")
        receipt = self.store.read("action_receipt", "wait")
        evidence_before = deepcopy((request, receipt, self.store.records("observation")))
        self.assertEqual(model_values[0]["request"], request)
        self.assertNotIn("versions", model_values[0]["receipt"])
        self.assertEqual(model_values[0]["receipt"]["step_semantics"], receipt["step_semantics"])
        for kind in TRACE_KINDS:
            self.assertNotIn(kind + "_ref", model_values[0]["receipt"])
            self.assertTrue(model_values[0]["receipt"][kind + "_available"])
            trace = text_values(self.harness.dispatch("history_read_trace", dict(action_id="wait", kind=kind),
                                                     dict(tool_call_id="read-" + kind)))[0]
            expected = json.loads(self.store.resolve_artifact(receipt[kind + "_ref"])[1])
            self.assertEqual(trace, expected)
        model_observation = next(value for value in reversed(model_values) if "bundle" in value)
        self.assertEqual(model_observation["robot_state"]["arms"]["left"]["q_rad"], [.123456789] * 6)
        self.assertTrue(model_observation["bundle"]["robot_state_available"])
        program_values = text_values(self.harness.dispatch("robot_wait", args, dict(tool_call_id="program", program_id="p")))
        self.assertEqual(program_values[0]["request"], request)
        self.assertEqual(program_values[0]["receipt"], receipt)
        self.assertIn("robot_state_ref", program_values[-1]["bundle"])
        self.assertEqual(evidence_before, (self.store.read("action_request", "wait"),
                                         self.store.read("action_receipt", "wait"), self.store.records("observation")))
        self.assertEqual(self.robot.executions, 1)

    def test_lean_delivery_keeps_actual_image_bytes_and_crop_geometry(self):
        bundle_before = deepcopy(self.harness.latest)
        for crop in ({}, dict(camera_id="cam_head", roi=[0, 0, 1, 1], scale=2)):
            with self.subTest(crop=crop):
                lean = self.harness.observation_content(self.harness.latest, dict(tool_call_id="lean"), **crop)
                full = self.harness.observation_content(self.harness.latest, dict(tool_call_id="full", program_id="p"), **crop)
                lean_images = [base64.b64decode(item["imageUrl"].split(",", 1)[1]) for item in lean if item["type"] == "inputImage"]
                full_images = [base64.b64decode(item["imageUrl"].split(",", 1)[1]) for item in full if item["type"] == "inputImage"]
                self.assertTrue(lean_images)
                self.assertEqual(lean_images, full_images)
                metadata = json.loads(lean[0]["text"])
                self.assertTrue(metadata["bundle"]["cameras"][0]["rgb_available"])
                if crop:
                    view = metadata["image_view"]
                    original = json.loads(full[0]["text"])["image_view"]
                    for key in ("camera_id", "source_roi", "display_scale", "pixel_transform", "width", "height"):
                        self.assertEqual(view[key], original[key])
                    self.assertTrue(view["rgb_available"])
                    self.assertTrue(view["original_rgb_available"])
                    self.assertNotIn("encoding", view)
        self.assertEqual(self.harness.latest, bundle_before)
        deliveries = self.store.records("delivery")
        self.assertTrue(all("rgb_ref" in row["images"][0] and "encoding" in row["images"][0] for row in deliveries))


if __name__ == "__main__":
    unittest.main()
