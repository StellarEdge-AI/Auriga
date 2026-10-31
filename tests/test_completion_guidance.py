"""Public completion guidance is auditable and never executes robot actions."""
import asyncio
from contextlib import redirect_stderr
from copy import deepcopy
import io
from itertools import product
import json
from pathlib import Path
import re
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from auriga.cortex.supervision.completion_guidance import (
    COMPLETION_GUIDANCE_MODES, HOOK_COMPLETION_GUIDANCE, INITIAL_COMPLETION_GUIDANCE, hook_completion_guidance,
)
from auriga.apps.evaluation.episode import BENCHMARK_CONTINUATION, wait_benchmark
from auriga.apps.gateway.workbench import Workbench
from auriga.apps.gateway.cli import main
from auriga.runtime.harness import Harness
from auriga.runtime.actions.home_check import HomePoseCheck, HOME_CHECK_POLICY
from auriga.core.config import Config
from auriga.cortex.context.public_task_rules import public_task_rules
from auriga.cortex.supervision.recovery_guidance import (
    EVIDENCE_INSTRUCTIONS, EVIDENCE_VERSION, INSTRUCTIONS, VERSION, recovery_guidance,
)
from auriga.core.storage.store import Store
from auriga.runtime.tools.registry import validator_for
from test_harness import FixtureRobot

RETIRED_TOOLS = frozenset({
    "robot_move_ee", "robot_move_grasp", "robot_move_dual_grasp",
    "robot_execute_ee", "robot_gripper", "robot_execute_queue",
    "robot_preview_cost", "robot_preview_motion", "robot_execute_plan",
    "robot_inspect_trajectory", "grasp_plan", "grasp_memory",
    "manipulation_run", "skill_run",
    "operation_verify", "depth_move",
})


def measured_state(offset=0):
    return {"frame_id": "env_origin", "tcp_config_id": "arx-x5-link6-v1",
            "sim_step": 0, "step_domain": "adapter_control_step", "arms": {
        side: {"ee_pose": {"position_m": [offset, 0, 1], "quaternion_xyzw": [0, 0, 0, 1],
                           "frame_id": "env_origin", "tcp_config_id": "arx-x5-link6-v1",
                           "source_kind": "measurement"}} for side in ("left", "right")}}


def saved_observation(store, identifier, offset=0):
    return {**store.identity, "schema_version": "0.1", "world_kind": "observed",
            "observation_id": identifier, "sim_step": 0, "step_domain": "adapter_control_step",
            "robot_state_ref": store.put_artifact(json.dumps(measured_state(offset)).encode(),
                                                  "robot_state", "application/json"),
            "task_instruction": "Arrange the visible digits."}


class CompletionGuidanceContractCheck(unittest.TestCase):
    def test_final_home_is_conditional_on_public_task_rules(self):
        for prompt in (INITIAL_COMPLETION_GUIDANCE, HOOK_COMPLETION_GUIDANCE):
            self.assertIn("If the public task rules require return-home", prompt)
            self.assertIn("For tasks without a public return-home rule", prompt)
            self.assertNotIn("Returning home is still unmet", prompt)
        self.assertIn("pose measurement, not an additional task completion condition",
                      HOOK_COMPLETION_GUIDANCE)

    def test_make_kong_does_not_gain_a_final_home_requirement(self):
        rules = public_task_rules("make_kong")
        self.assertEqual(rules["status"], "documented")
        self.assertIsNone(re.search(r"return.*(?:home|origin|initial)|back.*(?:origin|initial)",
                                   " ".join(rules["public_rules"]), re.I))
        reminder = hook_completion_guidance("initial-observation")
        self.assertIn("If the public task rules require return-home", reminder)
        self.assertNotIn("Returning home is still unmet", reminder)
        self.assertIn('"observation_id": "initial-observation"', reminder)


class CompletionGuidanceStartupCheck(unittest.IsolatedAsyncioTestCase):
    def assert_current_public_guidance(self, prompt, definitions, *, has_pose_guidance):
        tools = {item['name']: item for item in definitions}
        for retired in RETIRED_TOOLS:
            self.assertIsNone(re.search(r'\b' + re.escape(retired) + r'\b', prompt))
        for name in re.findall(r'\b(?:robot|history|vision|program)_[a-z_]+\b', prompt):
            if name != 'robot_state':
                self.assertIn(name, tools)
        if has_pose_guidance:
            self.assertIn("kind='move'", prompt)
            for field in ('id', 'arm', 'position_m', 'quaternion_xyzw', 'plan_id'):
                self.assertIn(field, prompt)
            schema = tools['robot_action']['inputSchema']
            pose = measured_state()['arms']['left']['ee_pose']
            validator_for(schema).validate(dict(mode='preview', observation_id='current-observation',
                stages=[dict(id='return_left', kind='move', arm='left',
                             position_m=pose['position_m'], quaternion_xyzw=pose['quaternion_xyzw'])]))
            validator_for(schema).validate(dict(mode='execute', plan_id='returned-cached-plan'))

    async def test_four_conditions_record_actual_initial_and_continuation_prompts(self):
        for requested_mode, recovery_evidence in product((None, *COMPLETION_GUIDANCE_MODES), (False, True)):
            mode = requested_mode or "hook"
            with self.subTest(requested_mode=requested_mode, recovery_evidence=recovery_evidence), \
                    tempfile.TemporaryDirectory() as temporary:
                robot = SimpleNamespace(close=Mock(), execute=Mock(), reset=Mock(), evaluate=Mock())
                options = {} if requested_mode is None else {"completion_guidance": requested_mode}
                config = Config().with_overrides({"features": {"recovery_evidence": recovery_evidence}})
                workbench = Workbench(Path(temporary) / "run", robot=robot, visual_review_model=None,
                                      config=config, **options)
                self.assertEqual(workbench.completion_guidance, mode)
                self.assertEqual(workbench.config.features.recovery_evidence, recovery_evidence)
                self.assertEqual(workbench.harness.home_check is not None, mode in {"hook", "both"})
                initial = saved_observation(workbench.store, "initial-observation")
                home_check = HomePoseCheck(workbench.store) if mode in {"hook", "both"} else None
                if home_check is not None:
                    home_check.capture_initial(initial)
                done = threading.Event()
                done.set()
                harness = SimpleNamespace(
                    accepting=True, active_action=None, states={}, action_count=0,
                    latest=initial, home_check=home_check, capabilities={},
                    action_budget=lambda: dict(native_steps_used=0, native_steps_limit=None, native_steps_remaining=None),
                    episode_done=threading.Event(), episode_terminal=None,
                    native_action_count=0, started_at=time.monotonic(), last_native_progress_at=time.monotonic(),
                    scene=SimpleNamespace(capabilities=lambda: {}), start=Mock(),
                )
                for field in ('tool_registry', 'config', 'disabled_tools', 'observation_profile',
                              'motion_review', 'enable_wait', 'vision_compare', 'visual_reviewer',
                              'action_policy', 'programs'):
                    setattr(harness, field, getattr(workbench.harness, field))
                harness.request_stop = lambda: setattr(harness, "accepting", False)
                session = SimpleNamespace(
                    thread_id="fixture-thread", turn_id="first-turn", state="completed", turn_done=done,
                    model="fixture", provider="fixture", effort="high", price_card=None,
                    cli_executable=Path("/fixture/codex"), command_prefix=["fixture-sandbox"], actor_dir=workbench.root,
                    socket_path=workbench.root / "fixture.sock", env={}, process=SimpleNamespace(pid=-1),
                    start=Mock(return_value="fixture-thread"), run_turn=Mock(), close=Mock(),
                    model_proxy=SimpleNamespace(limit_notified=False, request_count=1),
                )
                workbench.harness, workbench.session = harness, session
                terminal = SimpleNamespace(start=AsyncMock(), close=AsyncMock(),
                    session_id="fixture-terminal", process=SimpleNamespace(pid=-1))
                try:
                    with patch("auriga.apps.gateway.workbench.Terminal", return_value=terminal):
                        await workbench.start("unused", benchmark=True, task_notes="Fixture public note.",
                                              task_notes_source="fixture-public")
                    session.run_turn.assert_called_once()
                    initial_prompt = session.run_turn.call_args.args[0]
                    definitions = session.start.call_args.args[0]
                    self.assert_current_public_guidance(initial_prompt, definitions,
                        has_pose_guidance=mode in {'initial', 'both'})
                    self.assertIn("Arrange the visible digits.", initial_prompt)
                    self.assertIn("Fixture public note.", initial_prompt)
                    self.assertEqual(initial_prompt.count(INITIAL_COMPLETION_GUIDANCE),
                                     int(mode in {"initial", "both"}))
                    self.assertNotIn(HOOK_COMPLETION_GUIDANCE, initial_prompt)
                    manifest = json.loads((workbench.root / "manifest.json").read_text())
                    record = manifest["completion_guidance"]
                    expected_recovery_version = EVIDENCE_VERSION if recovery_evidence else VERSION
                    expected_recovery_instructions = EVIDENCE_INSTRUCTIONS if recovery_evidence else INSTRUCTIONS
                    self.assertEqual(record["recovery_guidance"], dict(
                        version=expected_recovery_version, instructions=expected_recovery_instructions,
                        context="current actor observation and native budget; measured home status"))
                    self.assertEqual(record["mode"], mode)
                    self.assertEqual(record["initial_prompt"],
                                     INITIAL_COMPLETION_GUIDANCE if mode in {"initial", "both"} else None)
                    self.assertEqual(record["initial_trigger"],
                                     "initial_actor_prompt" if mode in {"initial", "both"} else None)
                    expected = hook_completion_guidance("initial-observation") if mode in {"hook", "both"} else BENCHMARK_CONTINUATION
                    if home_check is not None:
                        self.assertIsNone(record["continuation_prompt"])
                        self.assertEqual(record["continuation_policy"], HOME_CHECK_POLICY)
                        self.assertEqual(record["continuation_prompts"], {
                            "away": expected, "home": BENCHMARK_CONTINUATION, "unknown": BENCHMARK_CONTINUATION})
                        self.assertEqual(record["home_reference"], home_check.reference_record())
                    else:
                        self.assertEqual(record["continuation_prompt"], expected)
                    self.assertEqual(record["initial_observation_id"], "initial-observation")
                    self.assertNotIn("initial-observation", initial_prompt)
                    self.assertEqual(record["continuation_trigger"], "turn.completed")
                    self.assertEqual(record["continuation_condition"], "actor_turn_completed_before_native_terminal")
                    prompt_records = [value for value in workbench.store.records("configuration") if "prompt" in value]
                    self.assertEqual(len(prompt_records), 1)
                    self.assertEqual(prompt_records[0]["prompt"], initial_prompt)
                    self.assertEqual(prompt_records[0]["completion_guidance"], record)
                    self.assertIn("cortex/supervision/completion_guidance.py", manifest["source_sha256"])
                    self.assertIn("runtime/actions/home_check.py", manifest["source_sha256"])

                    # Later observations cannot replace the saved episode-start reference.
                    harness.latest = saved_observation(workbench.store, "current-observation", .2)
                    if home_check is not None:
                        home_check.update_current(harness.latest)
                    session.run_turn.side_effect = lambda prompt: harness.episode_done.set()
                    self.assertFalse(await wait_benchmark(workbench, asyncio.Event()))
                    self.assertEqual(session.run_turn.call_count, 2)
                    if workbench.config.features.bounded_continuation:
                        evidence = None
                        if recovery_evidence:
                            evidence = dict(version="recovery-evidence-v1", native_action_count=0,
                                native_steps_remaining=None, no_native_finals=0, no_new_evidence_finals=0,
                                no_new_evidence_limit=2, tested_candidate_count=0, tested_candidates=[],
                                earlier_candidate_count=0,
                                scope="nominal candidates at recorded measured state and budget; not global task infeasibility")
                            self.assertEqual(harness.actor_stall["recovery_evidence"], evidence)
                            self.assertEqual(harness.actor_stall["version"], "bounded_completed_final_evidence_v2")
                        else:
                            self.assertNotIn("recovery_evidence", harness.actor_stall)
                            self.assertEqual(harness.actor_stall["version"], "bounded_completed_final_progress_v1")
                        expected_context, recovery = recovery_guidance(harness,
                            home_check.check(workbench.identity) if home_check else None, evidence=evidence)
                        self.assertEqual(expected_context["version"], expected_recovery_version)
                        expected += "\n" + recovery
                    self.assertEqual(session.run_turn.call_args.args, (expected,))
                    self.assert_current_public_guidance(session.run_turn.call_args.args[0], definitions,
                        has_pose_guidance=mode in {'hook', 'both'})
                    self.assertNotIn(INITIAL_COMPLETION_GUIDANCE, expected)
                    event = next(event for event in workbench.store.events()
                                 if event["type"] == "benchmark.actor_turn_continuation")
                    self.assertEqual(event["payload"]["completion_guidance"], mode)
                    self.assertEqual(event["payload"]["prompt"], expected)
                    self.assertEqual(event["payload"]["recovery_context"], expected_context)
                    self.assertEqual(event["payload"]["trigger"], "turn.completed")
                    self.assertEqual(event["payload"]["initial_observation_id"], "initial-observation")
                    self.assertEqual(event["payload"]["continuation_kind"],
                                     "home_reminder" if home_check is not None else "generic")
                    if home_check is not None:
                        self.assertEqual(event["payload"]["home_check"]["status"], "away")
                    robot.execute.assert_not_called()
                    robot.reset.assert_not_called()
                    robot.evaluate.assert_not_called()
                finally:
                    await workbench.close()

    async def test_replay_restores_recorded_mode_without_starting_an_actor(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "run"
            workbench = Workbench(root, robot=SimpleNamespace(close=Mock()), completion_guidance="both")
            (root / "manifest.json").write_text(json.dumps({**workbench.identity,
                "completion_guidance": {"mode": "both"}}))
            workbench.store.set_state("runtime", "capabilities", {"data_origin": "synthetic_fixture"})
            await workbench.close()
            replay = Workbench(root, robot=SimpleNamespace(close=Mock()), replay=True)
            replay.session.run_turn = Mock()
            try:
                await replay.start("unused")
                self.assertEqual(replay.completion_guidance, "both")
                replay.session.run_turn.assert_not_called()
            finally:
                await replay.close()


class CompletionGuidanceCLICheck(unittest.TestCase):
    def test_default_and_all_explicit_modes_reach_serve(self):
        for mode in (None, *COMPLETION_GUIDANCE_MODES):
            with self.subTest(mode=mode):
                arguments = ["auriga", "--benchmark"]
                if mode is not None:
                    arguments += ["--completion-guidance", mode]
                with patch("sys.argv", arguments), patch("auriga.apps.gateway.cli.serve", new_callable=AsyncMock) as serve:
                    main()
                serve.assert_awaited_once()
                self.assertEqual(serve.call_args.args[0].completion_guidance, mode or "hook")

    def test_invalid_mode_and_nonbenchmark_use_are_rejected_before_startup(self):
        for arguments in (["--benchmark", "--completion-guidance", "invalid"],
                          ["--completion-guidance", "initial"], ["--completion-guidance", "both"]):
            with self.subTest(arguments=arguments), patch("sys.argv", ["auriga", *arguments]), \
                    patch("auriga.apps.gateway.cli.serve", new_callable=AsyncMock) as serve, redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main()
                self.assertEqual(error.exception.code, 2)
                serve.assert_not_called()


class CompletionGuidanceObservationCheck(unittest.TestCase):
    def test_hook_reference_can_recover_undelivered_initial_poses_in_all_payload_profiles(self):
        for profile in ("full_v1", "compact_v1", "lean_v1"):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as temporary:
                identity = dict(run_id="fixture", episode_id="episode", env_instance_id="env", env_generation=1)
                store = Store(Path(temporary) / "store", identity)
                robot = FixtureRobot(temporary)
                observe = robot.observe
                def bimanual_observation():
                    raw = observe()
                    raw["robot_state"].update(frame_id="env_origin", tcp_config_id="arx-x5-link6-v1",
                                              sim_step=raw["sim_step"], step_domain=raw["step_domain"])
                    raw["robot_state"]["arms"]["left"]["ee_pose"].update(
                        frame_id="env_origin", tcp_config_id="arx-x5-link6-v1")
                    raw["robot_state"]["arms"]["right"] = deepcopy(raw["robot_state"]["arms"]["left"])
                    raw["robot_state"]["arms"]["right"]["ee_pose"]["position_m"] = [-.009, 0, 1]
                    return raw
                robot.observe = bimanual_observation
                config = Config().with_overrides({"features": {"completion_guidance": "hook"}})
                harness = Harness(store, robot, actor_payload_profile=profile,
                                  observation_profile="rgb_proprio_v1", config=config)
                try:
                    initial = harness.start()
                    self.assertFalse(store.records("delivery"))
                    self.assertEqual(harness.home_check.check(identity)["status"], "home")
                    current = robot.observe()
                    for arm in current["robot_state"]["arms"].values():
                        arm["ee_pose"]["position_m"] = [1, 2, 3]
                        arm["ee_pose"]["quaternion_xyzw"] = [0, 0, 1, 0]
                    current_bundle = harness.publish_observation(current)
                    prompt = hook_completion_guidance(initial["observation_id"])
                    self.assertIn(json.dumps({"observation_id": initial["observation_id"]}), prompt)
                    reply = harness.dispatch("history_view_observation", {"observation_id": initial["observation_id"]},
                                             {"tool_call_id": "history-fixture"})
                    self.assertTrue(reply["success"])
                    recovered = json.loads(reply["contentItems"][0]["text"])["robot_state"]["arms"]
                    self.assertEqual(recovered["left"]["ee_pose"]["position_m"], [.009, 0, 1])
                    self.assertEqual(recovered["right"]["ee_pose"]["position_m"], [-.009, 0, 1])
                    for arm in recovered.values():
                        self.assertEqual(arm["ee_pose"]["quaternion_xyzw"], [0, 0, 0, 1])
                    self.assertEqual(harness.latest["observation_id"], current_bundle["observation_id"])
                    checked = harness.home_check.check(identity)
                    self.assertEqual(checked["status"], "away")
                    self.assertEqual(checked["initial_observation_id"], initial["observation_id"])
                    self.assertEqual(checked["current_observation_id"], current_bundle["observation_id"])
                    self.assertEqual(robot.executions, 0)
                finally:
                    store.close()


class HomeCheckLifecycleCheck(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        identity = dict(run_id="fixture", episode_id="episode", env_instance_id="env", env_generation=1)
        self.store = Store(Path(temporary.name) / "store", identity)
        self.addCleanup(self.store.close)
        self.robot = FixtureRobot(temporary.name)
        observe = self.robot.observe
        self.offset = 0

        def bimanual_observation():
            raw = observe()
            raw["robot_state"] = measured_state(self.offset)
            raw["robot_state"]["sim_step"] = raw["sim_step"]
            return raw

        self.robot.observe = bimanual_observation
        config = Config().with_overrides({"features": {"completion_guidance": "hook"}})
        self.harness = Harness(self.store, self.robot, observation_profile="rgb_proprio_v1", config=config)
        self.initial = self.harness.start()
        self.check = lambda: self.harness.home_check.check(identity)

    def action(self):
        return dict(schema_version="0.1", **self.store.identity, world_kind="observed", action_id="move",
                    based_on_observation_id=self.harness.latest["observation_id"], frame_id="env_origin",
                    tcp_config_id="arx-x5-link6-v1", mode="absolute", limits_profile="simulation-slow-v1",
                    timeout_s=10, path=dict(kind="backend_free", orientation="backend_free", blend_radius_m=0,
                    position_tolerance_m=.005, waypoints=[dict(left=dict(position_m=[.3, 0, 1],
                    quaternion_xyzw=[0, 0, 0, 1]))]), gripper_events=[])

    def test_progress_is_not_terminal_evidence_and_fresh_observe_recovers_missing_capture(self):
        for terminal_capture in (True, False):
            with self.subTest(terminal_capture=terminal_capture):
                def execute(action, progress_callback=None, cancel_flag=None):
                    self.assertEqual(self.check()["status"], "unknown")
                    self.robot.executions += 1
                    self.offset = .3
                    progress_callback({"phase": "progress", "observation": self.robot.observe()})
                    self.assertEqual(self.check()["status"], "unknown")
                    return {"status": "completed", **({"terminal_observation": self.robot.observe()}
                                                       if terminal_capture else {})}
                self.robot.execute = execute
                result = self.harness.execute({**self.action(), "action_id": f"move-{terminal_capture}"})
                self.assertEqual(result["receipt"]["status"], "completed")
                self.assertEqual(self.check()["status"], "away" if terminal_capture else "unknown")
                if not terminal_capture:
                    # Progress remains historical evidence; it cannot authorize
                    # motion after the terminal capture failed.
                    self.assertIsNone(self.harness.latest)
                    self.assertIsNone(result["receipt"]["result_observation_id"])
                    self.harness.observe()
                    self.assertEqual(self.check()["status"], "away")
                self.assertEqual(self.check()["initial_observation_id"], self.initial["observation_id"])

    def test_rejected_action_preserves_evidence_but_lost_acknowledgement_invalidates_it(self):
        self.assertEqual(self.check()["status"], "home")
        rejected = self.harness.execute({**self.action(), "action_id": "rejected", "based_on_observation_id": "stale"})
        self.assertEqual(rejected["receipt"]["status"], "rejected")
        self.assertEqual(self.check()["status"], "home")
        self.robot.execute = Mock(side_effect=RuntimeError("lost acknowledgement"))
        result = self.harness.execute(self.action())
        self.assertEqual(result["receipt"]["status"], "unknown")
        self.assertEqual(self.check()["status"], "unknown")
        self.assertFalse(self.harness.accepting)

    def test_explicit_reset_records_new_episode_reference_before_further_actions(self):
        old_identity = dict(self.store.identity)
        self.harness.capabilities["reset"] = True
        def reset(seed, layout_id):
            self.assertEqual(self.check()["status"], "unknown")
            self.offset = .7
            return {"observation": self.robot.observe()}
        self.robot.reset = reset
        fresh = self.harness.reset(seed=1, layout_id=2)
        self.assertEqual(self.check()["status"], "home")
        self.assertEqual(self.check()["initial_observation_id"], fresh["observation_id"])
        self.assertNotEqual(fresh["episode_id"], self.initial["episode_id"])
        self.assertEqual(self.harness.home_check.check(old_identity)["status"], "unknown")
        records = [event for event in self.store.events() if event["type"] == "completion_guidance.home_reference"]
        self.assertEqual(len(records), 2)


if __name__ == "__main__":
    unittest.main()
