"""Independent ablation boundaries: one config survives queue, CLI and archive paths."""
from copy import deepcopy
import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from auriga.core.config import Config, ConfigError
from auriga.apps.gateway.workbench import Workbench
from auriga.apps.gateway.cli import main
from auriga.core.observation_policy import ACTOR_POLICY, ACTOR_PROFILE
from auriga.runtime.tools.definitions import TOOLS
from scripts.benchmark_queue import command, queue_config


FLAGS = ("task_scheduling", "operation_candidates", "preview_intent", "recovery_evidence")


class OperationAblationConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config_path = self.root / "config.json"
        self.config = Config.from_dict({"features": {"motion_review": False},
                                       "model": {"native_memories": False}})

    def case(self, **features):
        return dict(cwd=str(Path(__file__).resolve().parents[1]),
                    run_dir=str(self.root / "not-started"), task="build_tower",
                    seed=1, layout_id=0, title="Synthetic ablation config",
                    tag=["config-test"], **features)

    def resolve_cli(self, args):
        output = io.StringIO()
        with patch("sys.argv", ["auriga", *args, "--print-config"]), \
                patch("auriga.apps.gateway.cli.serve", new_callable=AsyncMock) as serve, \
                contextlib.redirect_stdout(output):
            main()
        serve.assert_not_called()
        return Config.from_dict(json.loads(output.getvalue()))

    def test_fresh_run_defaults_remove_candidate_and_purpose_but_keep_motion_evidence(self):
        from auriga.cortex.context.task_notes import OPERATION_CANDIDATE_GUIDANCE, default_notes_for
        from auriga.cortex.context.task_planning import instructions_for, plan_schema_for

        expected = dict(task_scheduling=True, operation_candidates=False,
                        preview_intent=False, recovery_evidence=True)
        effective = self.resolve_cli([])
        self.assertEqual({name: getattr(effective.features, name) for name in FLAGS}, expected)
        self.assertTrue(effective.features.trajectory_preview)
        self.assertEqual(effective.features.action_trajectory_delivery, "automatic")
        self.assertTrue(effective.features.bounded_continuation)
        self.assertTrue(effective.features.arm_queue)
        workbench = SimpleNamespace(config=effective, pi05_enabled=False,
            enable_wait=True, visual_review_model=None, motion_review=False,
            observation_profile=ACTOR_PROFILE)
        tools = {item["name"]: item for item in Workbench.actor_tools(workbench)}
        action = tools["robot_action"]["inputSchema"]["properties"]
        self.assertIn("stages", action)
        self.assertEqual(set(action["mode"]["enum"]), {"preview", "execute", "inspect", "direct", "execute_if_clean"})
        for retired in ("robot_preview_cost", "robot_move_dual_grasp", "robot_execute_queue", "robot_inspect_trajectory"):
            self.assertNotIn(retired, tools)
        plan = tools["task_plan"]["inputSchema"]["properties"]["plan"]
        self.assertEqual(plan["type"], "string")
        self.assertNotIn("properties", plan)
        for text in (default_notes_for(), instructions_for()):
            self.assertNotIn(OPERATION_CANDIDATE_GUIDANCE, text)

    def test_each_leave_one_out_flag_roundtrips_through_queue_and_actual_cli(self):
        self.config_path.write_text(json.dumps(self.config.to_dict()))
        full = {name: True for name in FLAGS}
        for disabled in (None, *FLAGS):
            with self.subTest(disabled=disabled):
                selected = {name: name != disabled for name in FLAGS}
                manifest = dict(auriga_config=self.config.to_dict())
                args = command(self.case(**selected, action_trajectory_delivery="automatic"),
                               2, config=queue_config(manifest), config_path=self.config_path)
                gateway_args = args[args.index("auriga.apps.gateway") + 1:]
                effective = self.resolve_cli(gateway_args)
                self.assertEqual({name: getattr(effective.features, name) for name in FLAGS}, selected)
                self.assertEqual(effective.features.action_trajectory_delivery, "automatic")
                self.assertEqual(effective.run.gpu, 2)
                self.assertTrue(effective.run.benchmark)
                self.assertIsNone(effective.run.max_actions)
                self.assertIsNone(effective.run.wall_budget_s)
                self.assertEqual({name for name in FLAGS if selected[name] != full[name]},
                                 {disabled} if disabled else set())

    def test_explicit_cli_true_and_delivery_override_false_config_values(self):
        saved = self.config.with_overrides({"features": {
            **{name: False for name in FLAGS}, "action_trajectory_delivery": "diagnostic"}})
        self.config_path.write_text(json.dumps(saved.to_dict()))
        effective = self.resolve_cli(["--config", str(self.config_path),
            *["--" + name.replace("_", "-") for name in FLAGS],
            "--action-trajectory-delivery", "automatic"])
        self.assertTrue(all(getattr(effective.features, name) for name in FLAGS))
        self.assertEqual(effective.features.action_trajectory_delivery, "automatic")
        untouched = self.resolve_cli(["--config", str(self.config_path)])
        self.assertTrue(all(not getattr(untouched.features, name) for name in FLAGS))
        self.assertEqual(untouched.features.action_trajectory_delivery, "diagnostic")

    def test_new_settings_reject_wrong_types_before_any_queue_dispatch(self):
        for name in FLAGS:
            for invalid in (0, 1, None, "false"):
                with self.subTest(name=name, invalid=invalid):
                    with self.assertRaises(ConfigError):
                        Config.from_dict({"features": {name: invalid}})
                    with self.assertRaises(ValueError):
                        command(self.case(**{name: invalid}), 0, config=self.config)
        for invalid in (None, True, "all", ""):
            with self.subTest(delivery=invalid):
                with self.assertRaises(ConfigError):
                    Config.from_dict({"features": {"action_trajectory_delivery": invalid}})
                with self.assertRaises(ValueError):
                    command(self.case(action_trajectory_delivery=invalid), 0, config=self.config)

    def test_actor_schema_switches_are_independent_and_do_not_mutate_global_tools(self):
        original = deepcopy(TOOLS)
        for disabled in (None, *FLAGS):
            with self.subTest(disabled=disabled):
                flags = {name: name != disabled for name in FLAGS}
                config = self.config.with_overrides({"features": flags})
                workbench = SimpleNamespace(config=config, pi05_enabled=False,
                    enable_wait=True, visual_review_model=None, motion_review=False,
                    observation_profile=ACTOR_PROFILE)
                tools = {item["name"]: item for item in Workbench.actor_tools(workbench)}
                # Archived planning switches remain configurable but do not
                # reintroduce old forms or parallel motion entry points.
                action = tools["robot_action"]["inputSchema"]["properties"]
                self.assertIn("stages", action)
                self.assertIn("inspect", action["mode"]["enum"])
                plan = tools["task_plan"]["inputSchema"]["properties"]["plan"]
                self.assertEqual(plan["type"], "string")
                for retired in ("robot_preview_cost", "robot_move_dual_grasp", "robot_execute_queue", "robot_inspect_trajectory"):
                    self.assertNotIn(retired, tools)
        self.assertEqual(TOOLS, original)


class OperationAblationArchivedConfigTests(unittest.IsolatedAsyncioTestCase):
    async def check_archive(self, saved, expected, *, mode, delivery="automatic"):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = dict(schema_version="0.1", run_id="run", episode_id="ep",
                env_instance_id="env", env_generation=1,
                actor_information_policy=ACTOR_POLICY, observation_profile=ACTOR_PROFILE,
                task_notes="Exact archived notes", task_notes_source="archived-fixture")
            if saved is not None:
                manifest["effective_config"] = saved
            path = root / "manifest.json"
            path.write_text(json.dumps(manifest))
            before = path.read_bytes()
            incoming = Config.from_dict({"features": {"motion_review": False,
                **{name: not expected[name] for name in FLAGS},
                "action_trajectory_delivery": "diagnostic" if delivery == "automatic" else "automatic"}})
            workbench = Workbench(root, config=incoming, robot=SimpleNamespace(close=lambda: None), **{mode: True})
            try:
                self.assertEqual({name: getattr(workbench.config.features, name) for name in FLAGS}, expected)
                self.assertEqual(workbench.config.features.action_trajectory_delivery, delivery)
                self.assertEqual(workbench.harness.config.features, workbench.config.features)
                self.assertEqual(workbench.archived_task_notes["text"], "Exact archived notes")
                self.assertEqual(path.read_bytes(), before)
            finally:
                await workbench.close()

    async def test_older_unified_archive_does_not_silently_enable_new_features(self):
        saved = Config.from_dict({"features": {"motion_review": False}}).to_dict()
        for name in (*FLAGS, "action_trajectory_delivery"):
            saved["features"].pop(name)
        for mode in ("resume", "replay"):
            with self.subTest(mode=mode):
                await self.check_archive(saved, {name: False for name in FLAGS}, mode=mode)

    async def test_legacy_archive_without_effective_config_keeps_new_flags_off(self):
        for mode in ("resume", "replay"):
            with self.subTest(mode=mode):
                await self.check_archive(None, {name: False for name in FLAGS}, mode=mode)

    async def test_current_archive_preserves_its_mixed_ablation_and_delivery_mode(self):
        selected = dict(task_scheduling=True, operation_candidates=False,
                        preview_intent=True, recovery_evidence=False)
        saved = Config.from_dict({"features": {"motion_review": False,
            **selected, "action_trajectory_delivery": "diagnostic"}}).to_dict()
        for mode in ("resume", "replay"):
            with self.subTest(mode=mode):
                await self.check_archive(saved, selected, mode=mode, delivery="diagnostic")
