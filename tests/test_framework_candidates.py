"""CPU integration boundaries for payload, default waiting, and control candidates."""
from program_fixture import program_services

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from auriga.apps.gateway.workbench import Workbench
from auriga.apps.gateway.cli import main
from auriga.runtime.harness import Harness
from auriga.core.observation_policy import ACTOR_POLICY, ACTOR_PROFILE
from auriga.robot.backends.robodojo import robodojo
from auriga.robot.backends.robodojo.robodojo import RobotBackend
from auriga.core.storage.store import Store
from scripts import benchmark_queue
from tests.test_harness import FixtureRobot


class WaitFixtureRobot(FixtureRobot):
    """A deterministic native-step/observation producer, without simulator or model."""

    def __init__(self, root):
        super().__init__(root)
        self.native_count = 0
        self.cancel_at = None

    def start(self):
        return super().start() | dict(path_kinds=["backend_free", "hold"],
            bounded_wait=dict(max_native_steps=50, native_control_hz=25))

    def observe(self):
        observation = super().observe()
        observation.update(sim_step=self.native_count * 10, sim_time_s=self.native_count / 25,
                           instruction="Observe the visible scene before acting.")
        observation["cameras"][0]["camera_id"] = "cam_head"
        observation["robot_state"]["arms"]["left"]["q_rad"] = [.123456789] * 6
        return observation

    def execute(self, action, progress_callback=None, cancel_flag=None):
        self.executions += 1
        start = self.native_count
        before = self.observe()
        count = 0
        for count in range(1, action["hold_native_steps"] + 1):
            self.native_count += 1
            if progress_callback and (count == 1 or count % 5 == 0):
                progress_callback(dict(phase="executing", executed_steps=count, observation=self.observe(),
                                       episode_terminal=self.terminal()))
            if count == self.cancel_at:
                cancel_flag.set()
                break
        cancelled = cancel_flag.is_set()
        return dict(status="stopped" if cancelled else "completed", reason="cancelled" if cancelled else None,
                    before_observation=before, before_state=before["robot_state"],
                    terminal_observation=self.observe(), episode_terminal=self.terminal(),
                    executed_native_steps=count, completed_native_steps=count, executed_control_steps=count * 10,
                    execution_interval=dict(start_sim_step=start * 10, end_sim_step=self.native_count * 10),
                    versions=dict(fixture="full-precision-source"), step_semantics=dict(executed_native_steps="native action count"),
                    planned_trace=dict(actions=action["hold_native_steps"]), dispatched_trace=dict(actions=count),
                    actual_trace=[dict(state=self.observe()["robot_state"])])

    def terminal(self):
        return dict(ended=False, success=None, reason=None, native_action_count=self.native_count, native_step_limit=1000)


def text_values(reply):
    return [json.loads(item["text"]) for item in reply["contentItems"] if item["type"] == "inputText"]


class CandidateHarnessTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.identity = dict(run_id="candidate-fixture", episode_id="ep", env_instance_id="env", env_generation=1)

    def harness(self, *, enable_wait=True, profile="compact_v1"):
        store = Store(self.root / "store", self.identity)
        self.addCleanup(store.close)
        robot = WaitFixtureRobot(self.root)
        harness = Harness(store, robot, actor_payload_profile=profile, enable_wait=enable_wait)
        harness.start()
        return harness, store, robot

    def wait(self, harness, identifier, steps=25, **values):
        arguments = dict(action_id=identifier, observation_id=harness.latest["observation_id"], native_steps=steps)
        arguments.update(values)
        return arguments, harness.dispatch("robot_wait", arguments, dict(tool_call_id=identifier))

    def test_default_harness_accepts_wait(self):
        store = Store(self.root / "store", self.identity)
        self.addCleanup(store.close)
        robot = WaitFixtureRobot(self.root)
        harness = Harness(store, robot)
        harness.start()
        _, reply = self.wait(harness, "default-wait", steps=5)
        self.assertEqual(text_values(reply)[0]["receipt"]["status"], "completed")
        self.assertEqual(robot.native_count, 5)

    def test_compact_model_reply_does_not_change_program_reply_or_stored_evidence(self):
        harness, store, robot = self.harness()
        args, model_reply = self.wait(harness, "first")
        model_values = text_values(model_reply)
        model_result = model_values[0]
        self.assertEqual(set(model_result["request"]), {"action_id", "based_on_observation_id"})
        self.assertNotIn("versions", model_result["receipt"])
        self.assertNotIn("actual_trace_ref", model_result["receipt"])
        model_observation = next(value for value in reversed(model_values) if "bundle" in value)
        self.assertNotIn("robot_state_ref", model_observation["bundle"])
        self.assertEqual(model_observation["robot_state"]["arms"]["left"]["q_rad"], [.123457] * 6)

        request = store.read("action_request", "first")
        receipt = store.read("action_receipt", "first")
        before = deepcopy((request, receipt))
        self.assertEqual(request["hold_native_steps"], 25)
        self.assertEqual(receipt["versions"], dict(fixture="full-precision-source"))
        program_reply = harness.dispatch("robot_wait", args, dict(tool_call_id="program-replay", program_id="program"))
        program_values = text_values(program_reply)
        self.assertEqual(program_values[0]["request"], request)
        self.assertEqual(program_values[0]["receipt"], receipt)
        program_observation = next(value for value in reversed(program_values) if "bundle" in value)
        self.assertIn("robot_state_ref", program_observation["bundle"])
        self.assertEqual(program_observation["robot_state"]["arms"]["left"]["q_rad"], [.123456789] * 6)
        self.assertEqual(before, (store.read("action_request", "first"), store.read("action_receipt", "first")))
        _, raw = store.resolve_artifact(program_observation["bundle"]["robot_state_ref"])
        self.assertEqual(json.loads(raw)["arms"]["left"]["q_rad"], [.123456789] * 6)
        self.assertEqual(robot.executions, 1)
        deliveries = store.records("delivery")
        self.assertTrue(any(value["audience"] == "astra_model" and value["delivered_to_actor"] for value in deliveries))
        self.assertTrue(any(value["audience"] == "actor_program" and not value["delivered_to_actor"] for value in deliveries))

    def test_wait_timeline_is_chronological_and_replay_does_not_make_old_result_current(self):
        harness, store, robot = self.harness()
        arguments, first = self.wait(harness, "first")
        values = text_values(first)
        temporal = [value for value in values if value.get("temporal_sample")]
        self.assertEqual([value["sim_step"] for value in temporal], [100, 200])
        self.assertEqual([value["sim_time_s"] for value in temporal], [.4, .8])
        self.assertTrue(all(value["current"] is False and value["camera_id"] == "cam_head" for value in temporal))
        final = next(value for value in reversed(values) if "bundle" in value)["bundle"]
        self.assertEqual(final["sim_step"], 250)
        self.assertEqual(final["observation_id"], harness.latest["observation_id"])
        self.assertTrue(values[0]["result_observation_is_current"])
        self.assertEqual(values[0]["current_observation_id"], final["observation_id"])
        stored = deepcopy(store.read("action_receipt", "first"))
        self.wait(harness, "second", steps=5)
        current = harness.latest["observation_id"]
        replay = harness.dispatch("robot_wait", arguments, dict(tool_call_id="duplicate"))
        replay_values = text_values(replay)
        self.assertEqual(robot.executions, 2)
        self.assertEqual(robot.native_count, 30)
        self.assertEqual(harness.latest["observation_id"], current)
        self.assertFalse(replay_values[0]["result_observation_is_current"])
        self.assertEqual(replay_values[0]["current_observation_id"], current)
        self.assertEqual(next(value for value in reversed(replay_values) if "bundle" in value)["bundle"]["observation_id"],
                         final["observation_id"])
        self.assertEqual(store.read("action_receipt", "first"), stored)
        with self.assertRaisesRegex(ValueError, "action_id_content_conflict"):
            harness.dispatch("robot_wait", dict(arguments, native_steps=26), dict(tool_call_id="conflict"))
        stale = harness.dispatch("robot_wait", dict(arguments, action_id="new-with-old-observation"), dict(tool_call_id="stale"))
        self.assertEqual(text_values(stale)[0]["receipt"]["reason"], "stale_observation")
        self.assertEqual(robot.executions, 2)

    def test_cancelled_wait_keeps_partial_timeline_and_actual_final_observation(self):
        harness, store, robot = self.harness()
        robot.cancel_at = 12
        _, result = self.wait(harness, "cancelled")
        values = text_values(result)
        receipt = values[0]["receipt"]
        self.assertEqual((receipt["status"], receipt["reason"]), ("stopped", "cancelled"))
        self.assertEqual(receipt["executed_native_steps"], 12)
        self.assertEqual(receipt["completed_native_steps"], 12)
        self.assertEqual([value["sim_step"] for value in values if value.get("temporal_sample")], [100])
        self.assertEqual(next(value for value in reversed(values) if "bundle" in value)["bundle"]["sim_step"], 120)
        self.assertEqual(harness.native_action_count, 12)
        self.assertEqual(store.read("action_receipt", "cancelled")["result_observation_id"], harness.latest["observation_id"])

    def test_default_program_budget_can_admit_default_wait_and_gets_full_reply(self):
        harness, store, robot = self.harness()
        from auriga.runtime.programs.runner import ProgramRunner
        harness.programs = ProgramRunner(program_services(harness))
        harness.programs.entries['program'] = dict(cancel=threading.Event(), deadline=time.monotonic() + 120)
        result = harness.dispatch("robot_wait", dict(action_id="program-wait", observation_id=harness.latest["observation_id"], native_steps=5),
                                  dict(tool_call_id="program-call", program_id="program"))
        value = text_values(result)[0]
        self.assertEqual(value["receipt"]["status"], "completed")
        self.assertEqual(value["request"]["timeout_s"], 30)
        self.assertIn("versions", value["receipt"])
        self.assertEqual(robot.executions, 1)

    def test_disabled_wait_is_hidden_and_cannot_bypass_admission_via_raw_action(self):
        harness, store, robot = self.harness(enable_wait=False)
        self.assertNotIn("bounded_wait", harness.capabilities)
        owner = Workbench.__new__(Workbench)
        owner.pi05_enabled, owner.visual_review_model, owner.enable_wait = False, None, False
        self.assertNotIn("robot_wait", [tool["name"] for tool in owner.actor_tools()])
        args = dict(action_id="disabled", observation_id=harness.latest["observation_id"], native_steps=5)
        with self.assertRaisesRegex(ValueError, "bounded_wait_not_enabled_for_run"):
            harness.dispatch("robot_wait", args, dict(tool_call_id="disabled"))
        low_level = harness._action_from_args("robot_wait", args)
        result = harness.execute(low_level, dict(tool_call_id="raw"))
        self.assertEqual(result["receipt"]["reason"], "unsupported_capability:bounded_wait")
        self.assertEqual(robot.executions, 0)


class CandidateGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_joint_replan_rejects_non_boolean_configuration_before_creating_run(self):
        for value in (0, 1, None, "true"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "run"
                with self.assertRaisesRegex(ValueError, "invalid_control_feature_flag"):
                    Workbench(root, joint_replan=value)
                self.assertFalse(root.exists())
                with self.assertRaisesRegex(ValueError, "invalid_joint_replan"):
                    RobotBackend(run_dir=root, joint_replan=value)
                self.assertFalse(root.exists())

    async def test_fresh_gateway_defaults_to_lean_and_wait_with_guards_off(self):
        with tempfile.TemporaryDirectory() as temporary:
            workbench = Workbench(Path(temporary) / "run")
            try:
                self.assertEqual(workbench.actor_payload_profile, "lean_v1")
                self.assertEqual(workbench.harness.actor_payload_profile, "lean_v1")
                self.assertTrue(workbench.enable_wait)
                self.assertTrue(workbench.harness.enable_wait)
                self.assertFalse(workbench.tracking_guard)
                self.assertFalse(workbench.robot.tracking_guard)
                self.assertTrue(workbench.joint_replan)
                self.assertTrue(workbench.robot.joint_replan)
                self.assertIn("robot_wait", [tool["name"] for tool in workbench.actor_tools()])
                self.assertIsNone(workbench.robot.process)
            finally:
                await workbench.close()

    async def test_resume_and_replay_restore_frozen_flags_and_legacy_defaults(self):
        for mode in ("resume", "replay"):
            for saved_wait, saved_profile in ((None, None), (False, None),
                                               (True, "compact_v1"), (True, "lean_v1"), (True, "full_v1")):
                configured = saved_wait is True
                with self.subTest(mode=mode, configured=configured, profile=saved_profile), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary) / "run"
                    root.mkdir()
                    manifest = dict(schema_version="0.1", run_id="run", episode_id="ep", env_instance_id="env", env_generation=1,
                                    actor_information_policy=ACTOR_POLICY, observation_profile=ACTOR_PROFILE)
                    if configured:
                        manifest.update(actor_payload_profile=saved_profile, enable_wait=True,
                                        tracking_guard=dict(enabled=True, threshold_rad=.5, consecutive_native_steps=5),
                                        joint_replan=dict(enabled=True))
                    if saved_wait is not None:
                        manifest["enable_wait"] = saved_wait
                    (root / "manifest.json").write_text(json.dumps(manifest))
                    # Opposite caller settings must not change the existing experiment.
                    workbench = Workbench(root, **{mode: True}, actor_payload_profile="full_v1" if configured else "compact_v1",
                                          enable_wait=not configured, tracking_guard=not configured, joint_replan=not configured)
                    try:
                        self.assertEqual(workbench.actor_payload_profile, saved_profile if configured else "full_v1")
                        self.assertEqual(workbench.harness.actor_payload_profile, workbench.actor_payload_profile)
                        self.assertEqual(workbench.enable_wait, configured)
                        self.assertEqual(workbench.harness.enable_wait, configured)
                        self.assertEqual(workbench.tracking_guard, configured)
                        self.assertEqual(workbench.robot.tracking_guard, configured)
                        self.assertEqual(workbench.joint_replan, configured)
                        self.assertEqual(workbench.robot.joint_replan, configured)
                        self.assertIsNone(workbench.robot.process)
                        self.assertEqual("robot_wait" in [tool["name"] for tool in workbench.actor_tools()], configured)
                        self.assertEqual(json.loads((root / "manifest.json").read_text()), manifest)
                        if mode == "resume":
                            _, content = workbench.store.resolve_artifact(workbench.source_ref, allowed_scopes=("observer",))
                            sources = json.loads(content)
                            for name in ("core/contracts.py", "core/presentation/actor_payload.py", "robot/motion/tracking_guard.py", "robot/motion/joint_replan.py", "robot/model/robot_model.py", "robot/motion/gripper_motion.py"):
                                self.assertIn(name, sources)
                                self.assertEqual(hashlib.sha256(sources[name].encode()).hexdigest(), workbench.source_hashes[name])
                    finally:
                        await workbench.close()


class WaitDefaultCLITests(unittest.TestCase):
    def test_default_and_explicit_payload_profiles_reach_serve(self):
        for profile in (None, "lean_v1", "full_v1", "compact_v1"):
            flags = ["--actor-payload-profile", profile] if profile else []
            with self.subTest(profile=profile), patch("sys.argv", ["auriga", *flags]), \
                    patch("auriga.apps.gateway.cli.serve", new_callable=AsyncMock) as serve:
                main()
                self.assertEqual(serve.call_args.args[0].actor_payload_profile, profile or "lean_v1")

    def test_default_and_explicit_wait_flags_reach_serve(self):
        for flags, enabled in (([], True), (["--enable-wait"], True), (["--no-enable-wait"], False)):
            with self.subTest(flags=flags), patch("sys.argv", ["auriga", *flags]), \
                    patch("auriga.apps.gateway.cli.serve", new_callable=AsyncMock) as serve:
                main()
                serve.assert_awaited_once()
                self.assertEqual(serve.call_args.args[0].enable_wait, enabled)


class CandidateQueueTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        checkout = self.root / "checkout"
        (checkout / "auriga/apps/gateway").mkdir(parents=True)
        (checkout / "auriga/apps/gateway/cli.py").write_text("# CPU fixture\n")
        self.case = dict(cwd=str(checkout), run_dir=str(self.root / "run"), task="synthetic", seed=0,
                         layout_id=0, title="candidate fixture", tag=["candidate"])

    def test_queue_forwards_only_explicit_candidate_flags(self):
        default = benchmark_queue.command(self.case, 2)
        self.assertIn("--enable-wait", default)
        self.assertNotIn("--no-enable-wait", default)
        disabled = benchmark_queue.command(dict(self.case, enable_wait=False), 2)
        self.assertIn("--no-enable-wait", disabled)
        self.assertNotIn("--enable-wait", disabled)
        self.assertNotIn("--tracking-guard", default)
        self.assertNotIn("--joint-replan", default)
        self.assertNotIn("--actor-payload-profile", default)
        explicit = benchmark_queue.command(dict(self.case, enable_wait=True, tracking_guard=True, joint_replan=True, actor_payload_profile="compact_v1"), 3)
        self.assertIn("--enable-wait", explicit)
        self.assertIn("--tracking-guard", explicit)
        self.assertIn("--joint-replan", explicit)
        self.assertEqual(explicit[explicit.index("--actor-payload-profile") + 1], "compact_v1")
        self.assertEqual(explicit[explicit.index("--gpu") + 1], "3")
        self.assertEqual(explicit[explicit.index("--run-dir") + 1], self.case["run_dir"])
        for profile in ("lean_v1", "full_v1"):
            args = benchmark_queue.command(dict(self.case, actor_payload_profile=profile), 3)
            self.assertEqual(args[args.index("--actor-payload-profile") + 1], profile)
        for name in ("enable_wait", "tracking_guard", "joint_replan"):
            for value in (0, 1, None, "true"):
                with self.subTest(name=name, value=value), self.assertRaisesRegex(ValueError, "Invalid " + name):
                    benchmark_queue.command(dict(self.case, **{name: value}), 0)
        with self.assertRaisesRegex(ValueError, "Invalid actor_payload_profile"):
            benchmark_queue.command(dict(self.case, actor_payload_profile="unknown"), 0)

    def test_invalid_later_case_fails_before_gpu_sampling_or_process_launch(self):
        invalid = dict(self.case, run_dir=str(self.root / "second"), tracking_guard="true")
        output = self.root / "queue"
        with patch.object(benchmark_queue, "sample_gpus") as sample, patch.object(benchmark_queue.subprocess, "Popen") as launch:
            with self.assertRaisesRegex(ValueError, "Invalid tracking_guard"):
                benchmark_queue.run_queue(dict(cases=[self.case, invalid]), output)
            sample.assert_not_called()
            launch.assert_not_called()
        self.assertFalse(output.exists())
        self.assertFalse(Path(self.case["run_dir"]).exists())


class CandidateWorkerLaunchTests(unittest.TestCase):
    def test_worker_launch_uses_current_backend_source_and_independent_flags(self):
        for guard, replan in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(guard=guard, replan=replan), tempfile.TemporaryDirectory() as temporary:
                native_root = Path(temporary) / "robodojo"
                backend = RobotBackend(robodojo_root=native_root, python="/fixture/python", gpu_id=3,
                                       run_dir=Path(temporary) / "worker", tracking_guard=guard, joint_replan=replan)
                process = SimpleNamespace(pid=999999999)
                try:
                    with patch("auriga.robot.backends.robodojo.robot_process.subprocess.Popen", return_value=process) as launch, \
                         patch("auriga.robot.backends.robodojo.robot_process.threading.Thread") as reader, \
                         patch.object(backend, "_request", return_value={"fixture": True}) as ready, \
                         patch.dict(os.environ, {"PYTHONPATH": "/unrelated/checkout"}):
                        backend.start()
                    args = launch.call_args.args[0]
                    environment = launch.call_args.kwargs["env"]
                    self.assertEqual(args[:4], ["/fixture/python", "-u", "-m", "auriga.robot.backends.robodojo.robot_worker"])
                    self.assertEqual("--tracking-guard" in args, guard)
                    self.assertEqual("--joint-replan" in args, replan)
                    self.assertEqual(launch.call_args.kwargs["cwd"], str(native_root))
                    self.assertEqual(environment["PYTHONPATH"].split(os.pathsep)[0], str(Path(robodojo.__file__).resolve().parents[4]))
                    self.assertEqual(environment["CUDA_VISIBLE_DEVICES"], "3")
                    ready.assert_called_once_with("ready", timeout=600)
                    reader.return_value.start.assert_called_once()
                finally:
                    # The fake Popen never owns a process; only its real log needs closing.
                    backend.process = None
                    if getattr(backend, 'log', None) is not None:
                        backend.log.close()


if __name__ == "__main__":
    unittest.main()
