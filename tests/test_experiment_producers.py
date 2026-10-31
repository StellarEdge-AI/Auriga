"""CPU evidence checks: passive reference recording and immutable archive reads."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image

from auriga.apps.library.experiments import allocate_run
from auriga.apps.evaluation.recording import ReferenceRecorder
from auriga.core.storage.store import Store


class ExperimentProducerCheck(unittest.TestCase):
    def test_passive_recording_preserves_actual_receipt_and_zero_task_score(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = allocate_run(temporary, category="reference", task="fixture", title="CPU fixture")
            image = directory / "source.png"
            Image.new("RGB", (1, 1)).save(image)
            raw = {"raw_observation_id": "raw-input", "data_origin": "synthetic_fixture", "observation_profile": "rgb_proprio_v1",
                   "sim_step": 0, "step_domain": "adapter_control_step", "robot_state": {"arms": {}},
                   "cameras": [{"camera_id": "head", "frame_id": "input-frame", "width": 1, "height": 1, "png_path": str(image)}]}
            recorder = ReferenceRecorder(directory)
            try:
                recorder.start({"data_origin": "synthetic_fixture", "observation_profile": "rgb_proprio_v1"})
                first = recorder.observe(raw)
                action = {"mode": "absolute", "path": {"kind": "hold", "waypoints": []}, "timeout_s": 1}
                terminal = deepcopy(raw)
                terminal.update(raw_observation_id="raw-result", sim_step=10)
                terminal["cameras"][0]["frame_id"] = "result-frame"
                receipt = {"status": "completed", "executed_control_steps": 10, "terminal_observation": terminal,
                           "before_observation": raw, "actual_trace": [{"sim_step": 10}], "position_error_m": 0.001,
                           "episode_terminal": {"ended": True, "success": False, "reason": "native_step_limit",
                                                "native_action_count": 800, "native_step_limit": 800}}
                unchanged = deepcopy(receipt)
                calls, updates = [], []
                cancellation = object()
                class Backend:
                    def execute(self, request, *, progress_callback, cancel_flag):
                        calls.append((request, cancel_flag))
                        if request.get("reject"): raise ValueError("synthetic_backend_refusal")
                        progress_callback({"phase": "executing", "episode_terminal": {
                            "ended": False, "success": None, "native_action_count": 799, "native_step_limit": 800}})
                        return receipt
                returned = recorder.execute(Backend(), action, progress_callback=updates.append, cancel_flag=cancellation)
                self.assertIs(returned, receipt)
                self.assertEqual(calls, [(action, cancellation)])
                self.assertEqual(updates[0]["episode_terminal"]["native_action_count"], 799)
                self.assertEqual(receipt, unchanged)
                request = recorder.store.records("action_request")[0]
                recorded = recorder.store.records("action_receipt")[0]
                self.assertEqual(request["based_on_observation_id"], first["observation_id"])
                self.assertEqual(recorded["position_error_m"], receipt["position_error_m"])
                self.assertNotIn("episode_terminal", recorded)
                events = recorder.store.events()
                progress_event = next(item for item in events if item["type"] == "action.progress")
                self.assertNotIn("episode_terminal", json.dumps(progress_event))
                native_event = next(item for item in events if item["type"] == "environment.ended")
                metadata, artifact = recorder.store.artifact(native_event["payload"]["result_ref"])
                self.assertEqual(metadata["access_scope"], "observer")
                self.assertEqual(json.loads(artifact.read_text()), receipt["episode_terminal"])
                self.assertEqual(recorder.store.read("observation", recorded["result_observation_id"])["cameras"][0]["frame_id"], "result-frame")
                self.assertIsNotNone(recorded["actual_trace_ref"])
                with self.assertRaisesRegex(ValueError, "synthetic_backend_refusal"):
                    recorder.execute(Backend(), {**action, "reject": True})
                self.assertEqual(len(calls), 2)
                self.assertEqual(recorder.store.records("action_receipt")[-1]["status"], "unknown")
                recorder.record_results({"evaluation": {"episode_index": 1, "task_success": False, "normalized_score": 0}})
                prior_generation = recorder.identity["env_generation"]
                recorder.identity.update(env_generation=2, episode_id="synthetic-new-episode")
                event = next(item for item in recorder.store.events() if item["type"] == "observer.evaluated")
                self.assertEqual(event["env_generation"], prior_generation)
                recorder.finish({"errors": [], "termination_reason": "done", "loop_verified": True, "action_count": 1,
                                 "llm_calls": 2, "evaluation": {"task_success": False, "normalized_score": 0, "valid_episode": True}}, record_results=False)
                summary = json.loads((directory / "summary.json").read_text())
                self.assertEqual(summary.get("summary", summary)["normalized_score"], 0)
                self.assertIs(summary.get("summary", summary)["task_success"], False)
                runtime = json.loads((directory / "runtime.json").read_text())
                self.assertEqual(runtime["status"], "completed")
                self.assertIsNone(runtime.get("endpoint"))
                self.assertFalse((directory / "terminal.bin").exists())
            finally:
                recorder.close()

    def test_passive_recorder_binds_native_profile_and_keeps_calibration(self):
        from auriga.core.observation_policy import ACTOR_PROFILE, LEGACY_ACTOR_PROFILE
        from test_harness import FixtureRobot
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            robot = FixtureRobot(root)
            recorder = ReferenceRecorder(root/"calibrated", kind="validation")
            try:
                self.assertEqual(recorder.harness.observation_profile, ACTOR_PROFILE)
                self.assertFalse(recorder.harness.motion_review)
                recorder.start({"observation_profile": ACTOR_PROFILE})
                raw = robot.observe()
                raw.update(raw_observation_id="raw-calibrated-fixture", observation_profile=ACTOR_PROFILE,
                    camera_calibration={"cameras": {"fixture_camera": {"K": [[100,0,.5],[0,100,.5],[0,0,1]]}}},
                    camera_pose_sample={"cameras": {"fixture_camera": {"T_world_camera": [[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]]}}},
                    robot_model={"base_link": "base", "joints": []})
                bundle = recorder.observe(raw)
                self.assertEqual(bundle["observation_profile"], ACTOR_PROFILE)
                metadata = json.loads(recorder.harness.observation_content(bundle, {}, detail="full")[0]["text"])
                self.assertEqual(metadata["camera_calibration"], raw["camera_calibration"])
                with self.assertRaisesRegex(ValueError, "profile_changed"):
                    recorder.start({"observation_profile": LEGACY_ACTOR_PROFILE})
                recorded = recorder.store.read_state("runtime", "recording_policy")
                self.assertEqual(recorded["observation_profile"], ACTOR_PROFILE)
                self.assertFalse(recorded["motion_review"])
            finally:
                recorder.close()
            explicit = ReferenceRecorder(root/"legacy", observation_profile=LEGACY_ACTOR_PROFILE)
            try:
                with self.assertRaisesRegex(ValueError, "matching_observation_profile"):
                    explicit.start({"observation_profile": ACTOR_PROFILE})
                explicit.start({"observation_profile": LEGACY_ACTOR_PROFILE})
                self.assertEqual(explicit.harness.observation_profile, LEGACY_ACTOR_PROFILE)
            finally:
                explicit.close()

    def test_native_controller_ee_smoke_uses_calibration_without_model_review_gate(self):
        from scripts.probes import native_policy_validate as diagnostic
        from test_harness import FixtureRobot
        from auriga.core.observation_policy import ACTOR_PROFILE
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            class Backend(FixtureRobot):
                observation_profile = ACTOR_PROFILE
                def start(self):
                    return {**super().start(), "observation_profile": ACTOR_PROFILE}
                def observe(self):
                    raw = super().observe()
                    raw.update(observation_profile=ACTOR_PROFILE,
                        camera_calibration={"source": "synthetic calibration"},
                        camera_pose_sample={"source": "synthetic pose"},
                        robot_model={"base_link": "base", "joints": []})
                    return raw
                def preview_motion(self, action):
                    raise AssertionError("fixed controller diagnostic must not claim model review")
                def execute(self, action, progress_callback=None, cancel_flag=None):
                    return {**super().execute(action, progress_callback, cancel_flag),
                            "executed_native_steps": 1, "executed_control_steps": 10}
                def evaluate(self, **kwargs): return {}
                def save_video(self): return {}
                def close(self): pass
            backend = Backend(root/"fixture")
            output = root/"run"
            with patch.object(diagnostic, "RobotBackend", return_value=backend) as constructor, \
                 patch("sys.argv", ["native_policy_validate", "--run-dir", str(output), "--ee-smoke"]):
                diagnostic.main()
            self.assertEqual(constructor.call_args.kwargs["observation_profile"], ACTOR_PROFILE)
            report = json.loads((output/"native-policy-report.json").read_text())
            self.assertEqual(report["status"], "completed")
            self.assertFalse(report["motion_review"])
            self.assertEqual(report["cases"][0]["receipt"]["status"], "completed")
            self.assertEqual(backend.executions, 1)

    def test_read_only_store_sees_wal_and_cannot_write_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            identity = {"run_id": "fixture", "episode_id": "ep", "env_instance_id": "env", "env_generation": 1}
            writer = Store(temporary, identity)
            writer.save("test", "one", {"value": 1})
            reader = Store(temporary, identity, read_only=True)
            try:
                writer.save("test", "two", {"value": 2})
                self.assertEqual(reader.read("test", "two"), {"value": 2})
                for operation in (lambda: reader.save("test", "three", {}), lambda: reader.set_state("x", "x", {}),
                                  lambda: reader.event("unexpected", {}), lambda: reader.put_artifact(b"x", "x", "text/plain"),
                                  lambda: reader.ingest_event({}), lambda: reader.raw_codex("x", {})):
                    with self.assertRaisesRegex(ValueError, "store_is_read_only"):
                        operation()
                with self.assertRaises(sqlite3.OperationalError):
                    reader.db.execute("DELETE FROM records")
                reference = writer.put_artifact(b"fixture secret", "fixture", "text/plain")
                _, blob = writer.artifact(reference)
                outside = Path(temporary) / "outside.txt"
                outside.write_bytes(b"fixture secret")
                blob.unlink()
                blob.symlink_to(outside)
                with self.assertRaisesRegex(ValueError, "artifact_path_outside_store"):
                    reader.resolve_artifact(reference)
            finally:
                reader.close()
                writer.close()
            database = Path(temporary) / "events.sqlite"
            before = hashlib.sha256(database.read_bytes()).hexdigest()
            reader = Store(temporary, identity, read_only=True)
            try:
                self.assertEqual(len(reader.records("test")), 2)
            finally:
                reader.close()
            self.assertEqual(before, hashlib.sha256(database.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
