"""Public dispatch keeps summary and full evidence paths separate from execution."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from auriga.core.contracts import canonical
from auriga.runtime.harness import Harness
from auriga.core.storage.store import Store
from auriga.runtime.tools.definitions import TOOLS
from tests.test_harness import FixtureRobot


class ToolOutputIntegrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.store = Store(root / "store", dict(run_id="output-fixture", episode_id="ep", env_instance_id="env", env_generation=1))
        self.addCleanup(self.store.close)
        self.robot = FixtureRobot(root)
        self.harness = Harness(self.store, self.robot)
        self.harness.start()

    def call(self, name, args, program=False):
        correlation = dict(thread_id="t", turn_id="turn", tool_call_id=name)
        if program:
            correlation["program_id"] = "program-caller"
        reply = self.harness.dispatch(name, args, correlation)
        self.assertTrue(reply["success"])
        return json.loads(reply["contentItems"][0]["text"])

    def test_history_schema_and_dispatch_expose_full_and_paged_readbacks(self):
        for name in ("robot_action_status", "history_list_actions", "history_read_trace", "program_status"):
            definition = next(item for item in TOOLS if item["name"] == name)
            self.assertEqual(definition["inputSchema"]["properties"]["detail"]["enum"], ["summary", "full"])
        trace = [dict(sim_step=i, q_rad=[i+.123456789]) for i in range(40)]
        ref = self.store.put_artifact(canonical(trace).encode(), "actual_trace", "application/json")
        request = dict(action_id="action", based_on_observation_id=self.harness.latest["observation_id"],
                       path=dict(waypoints=[dict(position_m=[.123456789]*3)]))
        receipt = dict(status="unknown", reason="stop_unconfirmed", actual_trace_ref=ref,
                       executed_native_steps=4, completed_native_steps=3, versions=dict(worker="fixture"))
        self.store.save("action_request", "action", request)
        self.harness.states["action"] = deepcopy(receipt)
        original = dict(request=request, receipt=receipt)
        self.assertNotIn("versions", self.call("robot_action_status", dict(action_id="action"))["receipt"])
        self.assertEqual(self.call("robot_action_status", dict(action_id="action", detail="full")), original)
        self.assertEqual(self.call("robot_action_status", dict(action_id="action"), program=True), original)
        listing = self.call("history_list_actions", {})
        self.assertEqual(listing["actions"][0]["status"], "unknown")
        self.assertEqual(self.call("history_list_actions", dict(detail="full")), [original])
        self.assertEqual(self.call("history_list_actions", {}, program=True), [original])
        args = dict(action_id="action", kind="actual_trace")
        self.assertEqual(len(self.call("history_read_trace", args)["trace"]), 25)
        self.assertEqual(self.call("history_read_trace", {**args, "detail": "full"}), trace)
        self.assertEqual(self.call("history_read_trace", args, program=True), trace)
        self.assertEqual(self.robot.executions, 0)

    def test_program_dispatch_reads_all_logs_without_changing_run_arguments(self):
        raw = b"start\n" + b"x"*40000 + b"\nterminal evidence"
        ref = self.store.put_artifact(raw, "program_stdout", "text/plain")
        job = dict(job_id="program", status="cancelled", stop_confirmed=False,
                   stdout_ref=ref, stdout_text=raw[:8192].decode(), stdout_truncated=True)
        calls = []
        def run(args, correlation):
            calls.append(deepcopy(args))
            return deepcopy(job)
        self.harness.programs = SimpleNamespace(run=run, status=lambda _: deepcopy(job))
        summary = self.call("program_run", dict(source="print('caller output')"))
        self.assertTrue(summary["stdout_text"].endswith("terminal evidence"))
        self.assertFalse(summary["stdout_truncated"])
        self.assertEqual(summary["stdout_text"].encode(), raw)
        full = self.call("program_status", dict(job_id="program", detail="full"))
        self.assertEqual(full["stdout_text"].encode(), raw)
        self.assertEqual(full["status"], "cancelled")
        self.call("program_run", dict(source="print('caller output')", detail="full"))
        self.assertEqual(calls, [dict(source="print('caller output')")]*2)
        self.assertEqual(self.robot.executions, 0)


if __name__ == "__main__":
    unittest.main()
