from program_fixture import program_services
from tool_fixture import install_registry
"""CPU regression evidence for native termination and delayed policy admission."""

import asyncio
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from tool_fixture import install_registry

from auriga.apps.gateway.workbench import Workbench
from auriga.runtime.harness import Harness
from auriga.runtime.programs.runner import ProgramRunner, model_wait, program_remaining
from auriga.core.storage.store import Store
from test_harness import FixtureRobot


IDENTITY = dict(run_id="native-gate-fixture", episode_id="ep", env_instance_id="env", env_generation=1)
TERMINAL = dict(ended=True, success=True, reason="native_success", native_action_count=1, native_step_limit=200)


class JointRobot(FixtureRobot):
    def execute(self, action, progress_callback=None, cancel_flag=None):
        self.executions += 1
        return dict(status="completed", terminal_observation=self.observe(), executed_steps=len(action["actions"]))


class Policy:
    def __init__(self, before_return=None):
        self.calls = 0
        self.before_return = before_return

    def infer(self, *args, **kwargs):
        self.calls += 1
        if self.before_return:
            self.before_return()
        return dict(actions=[[0.0] * 14 for _ in range(50)])


class NativeTerminalGateCheck(unittest.TestCase):
    def harness(self, directory):
        store = Store(Path(directory) / "store", dict(IDENTITY))
        self.addCleanup(store.close)
        robot = JointRobot(directory)
        harness = Harness(store, robot)
        harness.start()
        return harness, robot

    def test_policy_retry_reuses_prediction_and_receipt_after_observation_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            harness, robot = self.harness(directory)
            harness.action_policy = Policy()
            args = dict(observation_id=harness.latest["observation_id"], action_id="same-id", prefix=1,
                        prompt="Close the gripper around the selected object.")
            with self.assertRaisesRegex(ValueError, "invalid_tool_arguments"):
                harness.dispatch("robot_policy_step", {key:value for key,value in args.items() if key != "prompt"},
                                 dict(tool_call_id="missing-local-intent"))
            self.assertEqual(harness.action_policy.calls, 0)
            first = harness.dispatch("robot_policy_step", args, dict(tool_call_id="first"))
            repeated = harness.dispatch("robot_policy_step", args, dict(tool_call_id="repeat"))
            # Idempotency preserves the request/receipt and saved RGB. Static
            # observation conventions may be referenced on the second model
            # delivery instead of retransmitted verbatim.
            self.assertEqual(first["success"], repeated["success"])
            self.assertEqual(first["contentItems"][0], repeated["contentItems"][0])
            self.assertEqual([item for item in first["contentItems"] if item["type"] == "inputImage"],
                             [item for item in repeated["contentItems"] if item["type"] == "inputImage"])
            observations = [next(json.loads(item["text"]) for item in reply["contentItems"]
                if item["type"] == "inputText" and "robot_state" in json.loads(item["text"]))
                for reply in (first, repeated)]
            for field in ("current_observation_id", "observation_is_current"):
                self.assertEqual(observations[0][field], observations[1][field])
            self.assertEqual(observations[0]["bundle"]["observation_id"], observations[1]["bundle"]["observation_id"])
            self.assertEqual(observations[0]["robot_state"]["arms"], observations[1]["robot_state"]["arms"])
            for field in ("native_steps_used", "native_steps_limit", "native_steps_remaining"):
                self.assertEqual(observations[0]["current_action_budget"][field],
                                 observations[1]["current_action_budget"][field])
            self.assertEqual(harness.action_policy.calls, 1)
            self.assertEqual(robot.executions, 1)
            with self.assertRaisesRegex(ValueError, "action_id_content_conflict"):
                harness.dispatch("robot_policy_step", {**args, "prefix": 2}, dict(tool_call_id="changed"))
            self.assertEqual(harness.action_policy.calls, 1)

    def test_delayed_inference_cannot_outlive_program_or_observation(self):
        for interruption in ("cancel", "deadline", "stale_observation", "native_terminal"):
            with self.subTest(interruption=interruption), tempfile.TemporaryDirectory() as directory:
                harness, robot = self.harness(directory)
                runner = ProgramRunner(program_services(harness))
                harness.programs = runner
                entry = dict(job_id="program-fixture", cancel=threading.Event(), deadline=time.monotonic() + 30,
                             call_lock=threading.Lock(), calls=0, max_calls=3,
                             correlation=dict(thread_id="thread", turn_id="turn", tool_call_id="outer"))
                runner.entries[entry["job_id"]] = entry
                harness.store.set_state("program", entry["job_id"], dict(job_id=entry["job_id"], status="running"))

                def interrupt():
                    if interruption == "cancel":
                        runner.cancel(entry["job_id"])
                    elif interruption == "deadline":
                        entry["deadline"] = time.monotonic() - 1
                    elif interruption == "stale_observation":
                        harness.observe()
                    else:
                        harness._native_progress(dict(TERMINAL))

                harness.action_policy = Policy(interrupt)
                # Exercise the retained backend gate directly. The actor program
                # broker now rejects legacy policy motion before inference.
                result = harness.dispatch("robot_policy_step",
                    dict(observation_id=harness.latest["observation_id"], prefix=1,
                         prompt="Make a short local grasp adjustment."),
                    dict(program_id=entry['job_id'],tool_call_id='one'))
                receipt = json.loads(result["contentItems"][0]["text"])["receipt"]
                self.assertEqual(receipt["status"], "rejected")
                self.assertEqual(robot.executions, 0)
                self.assertEqual(harness.action_count, 0)
                self.assertEqual(harness.action_policy.calls, 1)

    def test_program_reserves_action_time_before_starting_motion(self):
        for remaining, expected in ((4, "rejected"), (20, "completed")):
            with self.subTest(remaining=remaining), tempfile.TemporaryDirectory() as directory:
                harness, robot = self.harness(directory)
                runner = ProgramRunner(program_services(harness))
                harness.programs = runner
                identifier = "program-fixture"
                runner.entries[identifier] = dict(job_id=identifier, cancel=threading.Event(),
                    deadline=time.monotonic() + remaining)
                harness.action_policy = Policy()
                result = harness.dispatch("robot_policy_step", dict(observation_id=harness.latest["observation_id"],
                    prefix=1, timeout_s=5, prompt="Make a short local grasp adjustment."),
                    dict(program_id=identifier, tool_call_id="bounded"))
                receipt = json.loads(result["contentItems"][0]["text"])["receipt"]
                self.assertEqual(receipt["status"], expected)
                self.assertEqual(robot.executions, int(expected == "completed"))
                if expected == "rejected":
                    self.assertEqual(receipt["reason"], "program_time_insufficient_for_action")

    def test_next_action_reserves_original_remaining_time_after_own_sol_wait(self):
        with tempfile.TemporaryDirectory() as directory:
            harness, robot = self.harness(directory)
            runner = harness.programs = ProgramRunner(program_services(harness))
            entry = dict(job_id="program-fixture", cancel=threading.Event(), deadline=120.0)
            runner.entries[entry["job_id"]] = entry
            harness.action_policy = Policy()
            clock = [100.0]
            with patch("auriga.runtime.programs.runner.time.monotonic", side_effect=lambda: clock[0]):
                with model_wait(entry):
                    clock[0] += 1000
                self.assertEqual(program_remaining(entry), 20.0)
                result = harness.dispatch("robot_policy_step", dict(observation_id=harness.latest["observation_id"],
                    prefix=1, timeout_s=5, prompt="Make a short local grasp adjustment."),
                    dict(program_id=entry["job_id"], tool_call_id="after-sol"))
            receipt = json.loads(result["contentItems"][0]["text"])["receipt"]
            self.assertEqual(receipt["status"], "completed")
            self.assertEqual(robot.executions, 1)

    def test_native_success_survives_actor_interrupt_failure_and_can_be_evaluated(self):
        with tempfile.TemporaryDirectory() as directory:
            harness, robot = self.harness(directory)
            harness._native_progress(dict(TERMINAL))
            workbench = Workbench.__new__(Workbench)
            workbench.harness, workbench.store, workbench.robot = harness, harness.store, robot
            workbench.session = SimpleNamespace(state="running", interrupt=Mock(side_effect=RuntimeError("synthetic interrupt failure")), close=Mock())
            workbench.control_responses, workbench.status, workbench.starting = {}, "running", False
            workbench.identity, workbench.replay = dict(IDENTITY), False
            robot.evaluate = Mock(return_value=dict(task_success=True, normalized_score=1.0))
            response = asyncio.run(workbench.stop("native-stop"))
            self.assertEqual(response["status"], "stopped")
            workbench.session.close.assert_called_once()
            self.assertTrue(workbench.observer_evaluate()["result"]["task_success"])
            self.assertFalse(harness.accepting)

    def test_native_stop_waits_for_cancelled_program_cleanup_before_scoring(self):
        for interrupt_error in (False, True):
            with self.subTest(interrupt_error=interrupt_error), tempfile.TemporaryDirectory() as directory:
                harness, robot = self.harness(directory)
                harness._native_progress(dict(TERMINAL))
                entries = {"program": dict(job_id="program")}
                cleanup = threading.Event()
                def cancel(identifier):
                    def finish():
                        entries.pop(identifier, None)
                        cleanup.set()
                    threading.Timer(.05, finish).start()
                harness.programs = SimpleNamespace(
                    cancel_all=lambda: [cancel(identifier) for identifier in list(entries)],
                    unresolved_jobs=lambda: [],
                    snapshot=lambda: dict(active_jobs=list(entries), unresolved_jobs=[], pending_reviews=[]))
                workbench = Workbench.__new__(Workbench)
                workbench.harness, workbench.store, workbench.robot = harness, harness.store, robot
                workbench.session = SimpleNamespace(state="completed", close=Mock(),
                    interrupt=Mock(side_effect=RuntimeError("interrupt failed") if interrupt_error else None))
                workbench.control_responses, workbench.status, workbench.starting = {}, "running", False
                workbench.identity, workbench.replay = dict(IDENTITY), False
                robot.evaluate = Mock(return_value=dict(task_success=True, normalized_score=1.0))
                response = asyncio.run(workbench.stop("drain-program"))
                self.assertEqual(response["status"], "stopped")
                self.assertTrue(cleanup.is_set())
                self.assertFalse(entries)
                self.assertTrue(workbench.observer_evaluate()["result"]["task_success"])

    def test_terminal_during_first_turn_start_preserves_worker_and_manifest(self):
        from auriga.core.config import Config
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory, dict(IDENTITY))
            self.addCleanup(store.close)
            workbench = Workbench.__new__(Workbench)
            workbench.config = Config()
            harness = SimpleNamespace(accepting=True, episode_terminal=None, start=Mock(), capabilities={},
                latest=dict(observation_profile="rgb_proprio_v1"), scene=SimpleNamespace(capabilities=lambda: {}))
            install_registry(harness, workbench.config)
            workbench.harness, workbench.root, workbench.store = harness, Path(directory), store
            workbench.identity, workbench.replay, workbench.resume = dict(IDENTITY), False, False
            workbench.pi05_enabled, workbench.visual_review_model = False, None
            workbench.enable_wait, workbench.tracking_guard = False, False
            workbench.joint_replan = False
            workbench.completion_guidance = "none"
            workbench.observation_profile = "rgb_proprio_v1"
            workbench.motion_review = False
            workbench.vision_compare = True
            workbench.grasp_memory_store = None
            workbench.actor_payload_profile = "full_v1"
            workbench.robot = SimpleNamespace(close=Mock())
            workbench.terminal, workbench.starting = None, True
            workbench.task, workbench.seed, workbench.layout_id = "fixture", 0, 0
            workbench.source_hashes, workbench.source_ref = {}, None

            def first_turn(prompt):
                harness.episode_terminal, harness.accepting = dict(TERMINAL), False

            workbench.session = SimpleNamespace(start=Mock(return_value="thread"), run_turn=first_turn,
                close=Mock(), thread_id="thread", actor_dir=Path(directory) / "actor", env={}, socket_path="unused",
                cli_executable=Path("/fixture/native/codex"), command_prefix=["fixture-sandbox"],
                model="fixture", provider="fixture", effort="high", price_card=None, process=SimpleNamespace(pid=1))
            terminal = SimpleNamespace(start=AsyncMock(), close=AsyncMock(), session_id="terminal", process=SimpleNamespace(pid=2))
            with patch("auriga.apps.gateway.workbench.Terminal", return_value=terminal) as create_terminal:
                asyncio.run(workbench.start("synthetic startup"))
            self.assertEqual(create_terminal.call_args.kwargs["command_prefix"][:2], ["fixture-sandbox", "/fixture/native/codex"])
            workbench.robot.close.assert_not_called()
            workbench.session.close.assert_not_called()
            self.assertTrue((Path(directory) / "manifest.json").is_file())
            self.assertFalse(workbench.starting)
            self.assertTrue(harness.episode_terminal["success"])


if __name__ == "__main__":
    unittest.main()
