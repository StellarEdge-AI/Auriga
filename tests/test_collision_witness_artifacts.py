"""CPU fixtures exercise native witness capture, real worker IPC and actor reads."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

import numpy as np

from auriga.robot.motion.motion_failure import archive_collision_witnesses, solver_snapshot
from auriga.runtime.programs.runner import ProgramRunner
from auriga.robot.backends.robodojo.robodojo import RobotBackend
from auriga.robot.backends.robodojo.robot_process import RobotProcess
from test_joint_replan_runtime import motion_action, replan_adapter
from test_motion_failure import native_result
from test_planner_witness import native_fk_fixture
import test_unified_actions as unified_fixture


def native_failure():
    """Keep eight FK samples from the native adapter's selected failed candidate."""
    adapter, robot, _, _ = replan_adapter(enabled=False)
    result = native_result("trajopt")
    result.js_solution.position = np.repeat(result.js_solution.position, 4, axis=2) / 4
    diagnostics = [dict(stage="trajopt", waypoint_index=0, **solver_snapshot(result, "trajopt"))]
    with native_fk_fixture(robot.arm_joints_name) as (planner, calls):
        adapter.robot.planner[robot.robot_name] = planner
        feedback = adapter._failure_feedback(motion_action(), "left", 0, diagnostics)
    assert calls["scene_reads"] == 0
    return dict(status="failed", reason="path_infeasible", failed_stage_id="reach", failure_feedback=feedback)


class WitnessWorker:
    """Transport fixture: the real worker dispatcher owns the native capture."""
    def __init__(self, arguments):
        pass

    def capabilities(self):
        return dict(ready=True)

    def preview_batch(self, action):
        return native_failure()

    def close(self, progress):
        pass


class CollisionWitnessArtifactTests(unittest.TestCase):
    setUp = unified_fixture.UnifiedActionTests.setUp
    request = unified_fixture.UnifiedActionTests.request
    preview = unified_fixture.UnifiedActionTests.preview
    native_execution = unified_fixture.UnifiedActionTests.native_execution

    def actor_call(self, arguments, call="witness"):
        self.h.thread_id = "thread"
        return self.h.handle_tool(dict(threadId="thread", turnId="turn", callId=call,
                                      tool="robot_action", arguments=arguments))

    def payload(self, reply):
        from auriga.core.presentation.lean_compact import expand_tables
        return expand_tables(json.loads(reply["contentItems"][0]["text"]))

    def witness(self, result):
        return result["failure_feedback"]["candidates"][0]["constraint_witness"]

    def test_worker_preview_full_witnesses_are_readable_by_actor_program(self):
        # Use the actual backend operation method, JSONL dispatcher and reader.
        backend = RobotBackend.__new__(RobotBackend)
        RobotProcess.__init__(backend, self.store.root / "worker")
        self.addCleanup(backend.close)
        source = ("import json\nfrom auriga.robot.backends.robodojo.worker_protocol import serve_worker, WIRE_PREFIX\n"
                  "from test_collision_witness_artifacts import WitnessWorker\n"
                  "serve_worker(WitnessWorker, None, lambda value: "
                  "print(WIRE_PREFIX + json.dumps(value, allow_nan=False), flush=True))\n")
        root = Path(__file__).resolve().parents[1]
        environment = dict(os.environ, PYTHONPATH=os.pathsep.join((str(root), str(root / "tests"))))
        backend.start_process([sys.executable, "-c", source], cwd=root, environment=environment)
        self.robot.preview_batch = backend.preview_batch
        reply = self.actor_call(self.request())
        result = self.payload(reply)
        self.assertEqual((result["status"], result["reason"]), ("failed", "path_infeasible"))
        witness = self.witness(result)
        self.assertEqual(len(witness["collision_model_witnesses"]), 8)
        self.assertEqual(witness["collision_model_witness_count"], 8)
        self.assertEqual(witness["omitted_witness_count"], 0)
        self.assertNotIn("_full_collision_model_witnesses", json.dumps(reply))
        reference = witness["collision_model_witnesses_ref"]
        self.assertTrue(reference.startswith("artifact:ref_"), reference)
        runner = ProgramRunner(self.h.program_services())
        self.h.programs = runner
        self.addCleanup(runner.close)
        source = ("import json\nfrom auriga_client import AurigaClient\n"
                  f"details = json.loads(AurigaClient().artifact({reference!r}))\n"
                  "assert details['candidate_only'] and details['sampled_only']\n"
                  "assert details['frame_id'] == 'planner_base'\n"
                  "assert details['arm'] == 'left' and details['failed_stage_id'] == 'reach'\n"
                  "hits = details['collision_model_witnesses']\n"
                  "assert len(hits) == 8\n"
                  "assert sorted(hit['trajectory_sample_index'] for hit in hits) == list(range(8))\n"
                  "assert all(hit['kind'] == 'self_collision' for hit in hits)\n"
                  "print('read all eight collision witnesses')\n")
        job = runner.run(dict(source=source, timeout_s=60),
                         dict(thread_id="thread", turn_id="turn", tool_call_id="read-witnesses"))
        self.assertEqual(job["status"], "succeeded", job.get("stderr_text"))
        self.assertIn("read all eight collision witnesses", job["stdout_text"])
        self.assertEqual(self.robot.executions, 0)

    def test_optional_archive_failure_keeps_preview_and_execution_results(self):
        original_put = self.store.put_artifact
        def fail_witness(data, kind, *args, **kwargs):
            if kind == "planner_collision_witnesses":
                raise OSError("private filesystem path must not enter actor context")
            return original_put(data, kind, *args, **kwargs)

        self.robot.preview_batch = Mock(side_effect=lambda _: native_failure())
        with patch.object(self.store, "put_artifact", side_effect=fail_witness):
            reply = self.actor_call(self.request(), "failed-preview")
        self.assert_unavailable(self.payload(reply))
        plan = self.preview()
        self.native_execution()
        self.robot.execute.return_value.update(native_failure())
        args = dict(mode="execute", plan_id=plan["plan_id"], action_id="failed-execution")
        with patch.object(self.store, "put_artifact", side_effect=fail_witness):
            reply = self.actor_call(args, "failed-execution")
        self.assert_unavailable(self.payload(reply)["receipt"])
        persisted = self.store.read("action_receipt", "failed-execution")
        self.assert_unavailable(persisted)
        self.assertEqual(persisted["completed_native_steps"], 2)
        self.assertEqual(self.actor_call(args, "failed-execution"), reply)
        self.assertEqual(self.robot.execute.call_count, 1)

    def assert_unavailable(self, result):
        self.assertEqual((result["status"], result["reason"]), ("failed", "path_infeasible"))
        witness = self.witness(result)
        self.assertEqual(len(witness["collision_model_witnesses"]), 8)
        self.assertEqual(witness["collision_model_witness_count"], 8)
        self.assertEqual(witness["collision_model_witnesses_unavailable"],
                         dict(reason="full_witness_archive_failed", error_type="OSError"))
        self.assertNotIn("collision_model_witnesses_ref", witness)
        self.assertNotIn("_full_collision_model_witnesses", json.dumps(result))
        self.assertNotIn("private filesystem", json.dumps(result))

    def test_privileged_complete_witness_is_not_archived_or_exposed(self):
        result = native_failure()
        # Only the optional full evidence is tainted, not the bounded public summary.
        self.witness(result)["_full_collision_model_witnesses"][-1]["object_poses"] = {"hidden": [1, 2, 3]}
        with patch.object(self.store, "put_artifact", wraps=self.store.put_artifact) as put:
            archive_collision_witnesses(self.store, result, self.h.observation_profile)
        put.assert_not_called()
        self.assertEqual(result["status"], "failed")
        witness = self.witness(result)
        self.assertEqual(witness["collision_model_witnesses_unavailable"]["error_type"], "ValueError")
        self.assertNotIn("object_poses", json.dumps(result))
        self.assertNotIn("_full_collision_model_witnesses", json.dumps(result))

    def test_tainted_short_and_full_witnesses_do_not_leak_through_preview(self):
        result = native_failure()
        witness = self.witness(result)
        witness['_full_collision_model_witnesses'][0]['object_poses'] = {'hidden': [1, 2, 3]}
        self.assertIn('object_poses', witness['collision_model_witnesses'][0])
        self.robot.preview_batch = Mock(return_value=result)
        reply = self.actor_call(self.request(), 'unsafe-witness-preview')
        self.assertNotIn('object_poses', json.dumps(reply))
        shown = self.payload(reply)
        self.assertEqual(shown['status'], 'failed')
        self.assertNotIn('collision_model_witnesses', self.witness(shown))
        self.assertEqual(self.witness(shown)['collision_model_witnesses_unavailable']['error_type'], 'ValueError')
        self.assertEqual(self.robot.executions, 0)

    def test_invalid_optional_evidence_does_not_erase_acknowledged_execution(self):
        plan = self.preview()
        self.native_execution()
        self.robot.execute.return_value.update(native_failure())
        self.witness(self.robot.execute.return_value)["_full_collision_model_witnesses"][-1]["evaluator_state"] = {"hidden": True}
        reply = self.actor_call(dict(mode="execute", plan_id=plan["plan_id"], action_id="invalid-evidence"))
        receipt = self.payload(reply)["receipt"]
        persisted = self.store.read("action_receipt", "invalid-evidence")
        for result in (receipt, persisted):
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["completed_native_steps"], 2)
            witness = self.witness(result)
            self.assertEqual(witness["collision_model_witnesses_unavailable"]["error_type"], "ValueError")
            self.assertNotIn("collision_model_witnesses_ref", witness)
            self.assertNotIn("evaluator_state", json.dumps(result))
            self.assertNotIn("_full_collision_model_witnesses", json.dumps(result))
        self.assertEqual(self.robot.execute.call_count, 1)

    def test_legacy_feedback_without_complete_field_stays_unchanged(self):
        result = native_failure()
        self.witness(result).pop("_full_collision_model_witnesses")
        original = deepcopy(result)
        with patch.object(self.store, "put_artifact") as put:
            archive_collision_witnesses(self.store, result, self.h.observation_profile)
        put.assert_not_called()
        self.assertEqual(result, original)


if __name__ == "__main__":
    unittest.main()
