"""Operator configuration parsing, precedence and archived-run behavior."""
from program_fixture import program_services
from dataclasses import FrozenInstanceError
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from auriga.core.config import Config, ConfigError
from auriga.apps.gateway.workbench import Workbench
from auriga.apps.gateway.cli import main
from auriga.runtime.harness import Harness
from auriga.core.observation_policy import ACTOR_POLICY, ACTOR_PROFILE, DEPTH_ACTOR_PROFILE
from auriga.runtime.programs.runner import ProgramRunner
from auriga.core.storage.store import Store
from tests.test_framework_candidates import WaitFixtureRobot


class ConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / 'config.json'

    def save(self, value):
        self.path.write_text(json.dumps(value))
        return self.path

    def test_precedence_and_path_origins(self):
        self.save({'features': {'enable_wait': False}, 'model': {'codex_home': 'auth'},
                   'images': {'reuse_exact_images': False}, 'run': {'gpu': 2}})
        env = {'CODEX_HOME': '/old/auth', 'AURIGA_CONFIG': str(self.path),
               'AURIGA_IMAGE_SETTINGS': '{"transport_format":"jpeg","reuse_exact_images":true}'}
        resolved = Config.load(environ=env, overrides={'run': {'gpu': 0}, 'features': {'enable_wait': True}})
        self.assertEqual(resolved.run.gpu, 0)
        self.assertTrue(resolved.features.enable_wait)
        self.assertEqual(resolved.model.codex_home, str(self.root / 'auth'))
        self.assertEqual(resolved.images.transport_format, 'jpeg')
        self.assertFalse(resolved.images.reuse_exact_images)
        self.assertEqual(Config.load(self.path, environ={}, overrides={'model': {'codex_home': 'local-auth'}}).model.codex_home,
                         str(Path.cwd() / 'local-auth'))

    def test_snapshot_is_immutable_and_environment_independent(self):
        config = Config.from_dict({'run': {'tag': ['paired']}, 'features': {'disabled_tools': ['program_run']}})
        with self.assertRaises(FrozenInstanceError):
            config.features.enable_wait = False
        with patch.dict(os.environ, {'AURIGA_IMAGE_SETTINGS': 'broken JSON', 'CODEX_HOME': '/different/auth'}):
            restored = Config.from_dict(config.to_dict())
        self.assertEqual(restored, config)
        self.assertIsInstance(restored.features.disabled_tools, tuple)
        changed = config.with_overrides({'features': {'enable_wait': False}})
        self.assertTrue(config.features.enable_wait)
        self.assertFalse(changed.features.enable_wait)

    def test_sandbox_switch_is_strict_and_preserved_in_snapshots(self):
        self.assertFalse(Config.from_dict({}).features.sandbox_enabled)
        config = Config.load(self.save({'features': {'sandbox_enabled': True}}), environ={})
        self.assertTrue(config.features.sandbox_enabled)
        self.assertTrue(Config.from_dict(config.to_dict()).features.sandbox_enabled)
        disabled = Config.load(self.save({'features': {'sandbox_enabled': False}}), environ={})
        self.assertFalse(disabled.features.sandbox_enabled)
        self.assertFalse(Config.from_dict(disabled.to_dict()).features.sandbox_enabled)
        for value in ('false', 0, None):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                Config.from_dict({'features': {'sandbox_enabled': value}})

    def test_gateway_repository_default_uses_local_codex_home_without_reinterpreting_archives(self):
        for explicit in (None, {'features': {'enable_wait': False}}):
            with self.subTest(explicit=explicit):
                out = io.StringIO()
                argv = ['auriga', '--print-config']
                if explicit is not None:
                    argv.extend(['--config', str(self.save(explicit))])
                with patch.dict(os.environ, {'CODEX_HOME': '/unrelated/native-home',
                        'AURIGA_PRICE_CARD': '/unrelated/old-price-card.json'}, clear=True), \
                        patch('auriga.core.config.LOCAL_CONFIG_PATH', self.root / 'absent.local.json'), \
                        patch('sys.argv', argv), \
                        patch('auriga.apps.gateway.cli.serve', new_callable=AsyncMock) as serve, \
                        contextlib.redirect_stdout(out):
                    main()
                serve.assert_not_called()
                config = json.loads(out.getvalue())
                model = config['model']
                self.assertEqual((model['name'], model['effort']), ('gpt-6.1-sol', 'high'))
                self.assertEqual(model['codex_home'], str(Path('~/.codex').expanduser()))
                self.assertIsNone(model['price_card'])
                self.assertTrue(model['native_memories'])
                if explicit is not None:
                    self.assertFalse(config['features']['enable_wait'])
        archived = Config.from_dict({'version': 1})
        self.assertEqual(archived.model.name, 'gpt-6-astra')
        self.assertEqual(archived.model.codex_home, str(Path('~/.codex').expanduser()))
        self.assertTrue(archived.model.native_memories)

    def test_local_defaults_and_explicit_overrides_resolve_each_file_origin(self):
        local = self.root / 'local/auriga.local.json'
        local.parent.mkdir()
        local.write_text(json.dumps({'model': {'codex_home': 'provider', 'effort': 'low'}}))
        with patch('auriga.core.config.LOCAL_CONFIG_PATH', local):
            defaults = Config.load(environ={})
            self.assertEqual(defaults.model.name, 'gpt-6.1-sol')
            self.assertEqual(defaults.model.effort, 'low')
            self.assertEqual(defaults.model.codex_home, str(local.parent / 'provider'))
            self.save({'features': {'enable_wait': False}})
            partial = Config.load(environ={'AURIGA_CONFIG': str(self.path)})
            self.assertFalse(partial.features.enable_wait)
            self.assertEqual(partial.model, defaults.model)
            self.save({'model': {'codex_home': 'explicit-provider', 'effort': 'medium'}})
            explicit = Config.load(self.path, environ={'AURIGA_CONFIG': str(self.root / 'missing.json')})
            self.assertEqual(explicit.model.codex_home, str(self.root / 'explicit-provider'))
            self.assertEqual(explicit.model.effort, 'medium')
            override = Config.load(self.path, environ={}, overrides={'model': {'codex_home': 'cli-provider'}})
            self.assertEqual(override.model.codex_home, str(Path.cwd() / 'cli-provider'))

    def test_missing_local_file_preserves_public_defaults(self):
        with patch('auriga.core.config.LOCAL_CONFIG_PATH', self.root / 'missing.local.json'):
            config = Config.load(environ={})
        model = config.model
        self.assertEqual(config.queue.max_parallel, 60)
        self.assertEqual(Config().queue.max_parallel, 60)
        self.assertIsNone(Config.from_dict({'queue': {'max_parallel': None}}).queue.max_parallel)
        self.assertEqual((model.name, model.effort), ('gpt-6.1-sol', 'high'))
        self.assertEqual(model.codex_home, str(Path('~/.codex').expanduser()))
        self.assertFalse(config.features.sandbox_enabled)
        self.assertTrue(config.features.external_perception)
        self.assertTrue(config.features.vggt_height)
        self.assertTrue(config.features.object_table)
        self.assertTrue(config.features.scene3d)
        self.assertTrue(config.features.object_table_observe)
        self.assertTrue(config.features.scene3d_observe)
        self.assertTrue(config.features.held_perception)
        self.assertTrue(config.features.self_briefing)
        self.assertEqual(config.features.grip_non_empty_min_aperture_m, 0.0015)
        self.assertEqual(config.features.grip_rigid_min_aperture_m, 0.002)
        self.assertEqual(config.features.grip_wide_min_aperture_m, 0.003)

    def test_local_file_is_strictly_validated_before_explicit_overrides(self):
        local = self.root / 'auriga.local.json'
        self.save({'model': {'effort': 'high'}})
        for document in ('broken JSON', '[]', '{"model":{"api_key":"synthetic-secret"}}',
                         '{"model":{"effort":false}}'):
            with self.subTest(document=document), patch('auriga.core.config.LOCAL_CONFIG_PATH', local):
                local.write_text(document)
                with self.assertRaises(ConfigError):
                    Config.load(self.path, environ={}, overrides={'model': {'effort': 'high'}})

    def test_archived_config_and_queue_ignore_changed_local_defaults(self):
        from scripts.benchmark_queue import queue_config
        local = self.root / 'auriga.local.json'
        local.write_text(json.dumps({'model': {'codex_home': 'original-provider'}}))
        with patch('auriga.core.config.LOCAL_CONFIG_PATH', local):
            frozen = Config.load(environ={})
            snapshot = frozen.to_dict()
            local.write_text('broken JSON')
            self.assertEqual(Config.from_dict(snapshot), frozen)
            with patch.object(Config, 'load', side_effect=AssertionError('archive must not reload')):
                self.assertEqual(queue_config({'auriga_config': snapshot}), frozen)
            self.assertEqual(Config.from_dict({}).model.name, 'gpt-6-astra')
            self.assertEqual(Config.from_dict({}).model.codex_home, str(Path('~/.codex').expanduser()))

    def test_packaged_defaults_without_checkout_still_accept_explicit_config(self):
        package = self.root / 'package'
        package.mkdir()
        (package / 'default_config.json').write_text(json.dumps({'model': {'name': 'package-model'}}))
        self.save({'model': {'effort': 'medium'}})
        with patch('auriga.core.config.__file__', str(package / 'config.py')), \
                patch('auriga.core.config.LOCAL_CONFIG_PATH', self.root / 'absent.local.json'):
            self.assertEqual(Config.load(environ={}).model.name, 'package-model')
            explicit = Config.load(self.path, environ={})
        self.assertEqual((explicit.model.name, explicit.model.effort), ('package-model', 'medium'))

    def test_gateway_explicit_model_preserves_old_configuration_and_relative_home(self):
        self.save({'model': {'name': 'gpt-6-astra', 'effort': 'xhigh',
                             'codex_home': 'archived-auth', 'price_card': None}})
        out = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), \
                patch('sys.argv', ['auriga', '--config', str(self.path), '--print-config']), \
                patch('auriga.apps.gateway.cli.serve', new_callable=AsyncMock) as serve, \
                contextlib.redirect_stdout(out):
            main()
        serve.assert_not_called()
        model = json.loads(out.getvalue())['model']
        self.assertEqual((model['name'], model['effort']), ('gpt-6-astra', 'xhigh'))
        self.assertEqual(model['codex_home'], str(self.root / 'archived-auth'))
        self.assertIsNone(model['price_card'])

    def test_strict_schema_types_and_bounds(self):
        for value in ([], {'version': 2}, {'unknown': {}}, {'features': {'typo': True}},
                      {'features': {'enable_wait': 1}}, {'features': {'enable_wait': None}},
                      {'features': {'disabled_tools': 'robot_wait'}}, {'images': {'jpeg_quality': 0}},
                      {'run': {'gpu': True}}, {'run': {'wall_budget_s': float('nan')}},
                      {'model': {'name': ''}}, {'queue': {'oom_retry': {'max_retries': 21}}},
                      {'queue': {'oom_retry': {'initial_delay_s': 0}}}):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                Config.from_dict(value)

    def test_invalid_file_not_masked_by_explicit_override(self):
        self.save({'features': {'enable_wait': 'yes'}})
        with self.assertRaises(ConfigError):
            Config.load(self.path, environ={}, overrides={'features': {'enable_wait': True}})

    def test_legacy_profile_disables_calibration_dependent_tools(self):
        config = Config.from_dict({'features': {'observation_profile': 'rgb_proprio_v1'}})
        self.assertFalse(config.features.motion_review)
        self.assertTrue(config.features.enable_wait)

    def test_depth_features_default_off_and_require_explicit_sensor_profile(self):
        defaults = Config.from_dict({})
        self.assertEqual(defaults.features.observation_profile, ACTOR_PROFILE)
        for feature in ("depth_skills", "depth_auto_feedback", "depth_event_payload"):
            self.assertFalse(getattr(defaults.features, feature))
            with self.subTest(feature=feature):
                with self.assertRaisesRegex(ConfigError, "depth_features_require_rgbd"):
                    Config.from_dict({"features": {feature: True}})
                enabled = Config.from_dict({"features": {"observation_profile": DEPTH_ACTOR_PROFILE, feature: True,
                    **({"depth_auto_feedback": True} if feature == "depth_event_payload" else {})}})
                self.assertTrue(getattr(enabled.features, feature))
        with self.assertRaisesRegex(ConfigError, "requires_depth_auto_feedback"):
            Config.from_dict({"features": {"observation_profile": DEPTH_ACTOR_PROFILE, "depth_event_payload": True}})
        rgbd = Config.from_dict({"features": {"observation_profile": DEPTH_ACTOR_PROFILE}})
        self.assertTrue(rgbd.features.motion_review)
        self.assertFalse(rgbd.features.depth_skills)
        self.assertFalse(rgbd.features.depth_auto_feedback)
        self.assertFalse(rgbd.features.depth_event_payload)

    def test_cli_file_and_explicit_false_are_resolved_before_serve(self):
        self.save({'run': {'task': 'configured', 'tag': ['from-file']},
                   'features': {'tracking_guard': True, 'visual_review_model': 'gpt-6.1-sol', 'enable_wait': False}})
        with patch('sys.argv', ['auriga', '--config', str(self.path), '--enable-wait',
                                '--no-tracking-guard', '--no-visual-review-model', '--clear-tags']), \
                patch('auriga.apps.gateway.cli.serve', new_callable=AsyncMock) as serve:
            main()
        actual = serve.call_args.args[0]
        self.assertEqual(actual.task, 'configured')
        self.assertTrue(actual.enable_wait)
        self.assertFalse(actual.tracking_guard)
        self.assertIsNone(actual.visual_review_model)
        self.assertEqual(actual.tag, ())

    def test_print_config_starts_nothing_and_rejects_unknown_tools(self):
        self.save({'features': {'terminal': False}})
        out = io.StringIO()
        with patch('sys.argv', ['auriga', '--config', str(self.path), '--print-config']), \
                patch('auriga.apps.gateway.cli.serve', new_callable=AsyncMock) as serve, contextlib.redirect_stdout(out):
            main()
        serve.assert_not_called()
        self.assertFalse(json.loads(out.getvalue())['features']['terminal'])
        self.save({'features': {'disabled_tools': ['misspelled_tool']}})
        with patch('sys.argv', ['auriga', '--config', str(self.path), '--print-config']), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main()

    def test_tool_disabled_in_direct_and_program_dispatch(self):
        identity = dict(run_id='config', episode_id='episode', env_instance_id='env', env_generation=1)
        store = Store(self.root / 'run', identity)
        self.addCleanup(store.close)
        config = Config.from_dict({'features': {'disabled_tools': ['robot_wait', 'robot_capabilities'], 'observation_profile': 'rgb_proprio_v1'}})
        harness = Harness(store, WaitFixtureRobot(self.root), config=config)
        harness.start()
        with self.assertRaisesRegex(ValueError, 'tool_disabled_by_config:robot_wait'):
            harness.dispatch('robot_wait', {'native_steps': 1, 'observation_id': harness.latest['observation_id']}, {'tool_call_id': 'direct'})
        runner = ProgramRunner(program_services(harness))
        try:
            result = runner.run({'source': "from auriga_client import AurigaClient\nr = AurigaClient().call('robot_capabilities')\nassert r['success'] is False\nassert 'tool_disabled_by_config:robot_capabilities' in r['contentItems'][0]['text']", 'timeout_s': 5}, {'thread_id': 'thread', 'turn_id': 'turn', 'tool_call_id': 'program'})
            self.assertEqual(result['status'], 'succeeded', store.resolve_artifact(result['stderr_ref'])[1])
            self.assertEqual(harness.robot.native_count, 0)
        finally:
            runner.close()


class ArchivedConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_workbench_consumes_one_config_and_persists_effective_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = Config.from_dict({'features': {'observation_profile': 'rgb_proprio_v1', 'enable_wait': False,
                                                   'disabled_tools': ['program_run']},
                                       'run': {'task': 'configured', 'seed': 3, 'layout_id': 2},
                                       'images': {'reuse_exact_images': False}, 'model': {'name': 'configured-model'}})
            with patch.object(Config, 'load', side_effect=AssertionError('unexpected reload')):
                workbench = Workbench(root / 'new', config=config)
            try:
                self.assertEqual((workbench.task, workbench.seed, workbench.layout_id), ('configured', 3, 2))
                self.assertNotIn('robot_wait', [tool['name'] for tool in workbench.actor_tools()])
                self.assertNotIn('program_run', [tool['name'] for tool in workbench.actor_tools()])
                self.assertEqual(workbench.session.model, 'configured-model')
                snapshot = Config.from_dict(json.loads((root / 'new/effective_config.json').read_text()))
                self.assertEqual(snapshot, workbench.config)
                self.assertEqual(workbench.harness.image_settings, snapshot.images.to_dict())
            finally:
                await workbench.close()

    async def test_resume_and_replay_restore_snapshot_instead_of_new_flags(self):
        for mode in ('resume', 'replay'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                saved = Config.from_dict({'features': {'enable_wait': False, 'motion_review': False,
                                                       'pi05': True, 'disabled_tools': ['program_run'],
                                                       'completion_guidance': 'none'},
                                          'run': {'task': 'archived', 'seed': 7, 'layout_id': 3},
                                          'model': {'name': 'archived-model'}, 'images': {'transport_format': 'jpeg'}})
                manifest = dict(schema_version='0.1', run_id='run', episode_id='ep', env_instance_id='env', env_generation=1,
                                actor_information_policy=ACTOR_POLICY, observation_profile=ACTOR_PROFILE,
                                effective_config=saved.to_dict())
                (root / 'manifest.json').write_text(json.dumps(manifest))
                incoming = Config.from_dict({'features': {'enable_wait': True}, 'run': {'task': 'different'}})
                workbench = Workbench(root, config=incoming, **{mode: True})
                try:
                    self.assertFalse(workbench.enable_wait)
                    self.assertEqual(workbench.completion_guidance, 'none')
                    self.assertTrue(workbench.pi05_enabled)
                    self.assertEqual((workbench.task, workbench.seed, workbench.layout_id), ('archived', 7, 3))
                    self.assertEqual(workbench.session.model, 'archived-model')
                    self.assertNotIn('program_run', [tool['name'] for tool in workbench.actor_tools()])
                    self.assertEqual(workbench.config.images.transport_format, 'jpeg')
                    self.assertEqual(json.loads((root / 'manifest.json').read_text()), manifest)
                finally:
                    await workbench.close()

class LatestFeatureConfigTests(unittest.IsolatedAsyncioTestCase):
    def test_latest_main_defaults_and_cli_overrides(self):
        config = Config.from_dict({})
        self.assertEqual(config.features.actor_payload_profile, 'lean_v1')
        self.assertTrue(config.features.default_task_notes)
        self.assertTrue(config.features.vision_compare)
        self.assertEqual(config.features.completion_guidance, 'hook')
        self.assertEqual(config.queue.memory_per_case_mib, 11264)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'config.json'
            path.write_text(json.dumps({'features': {'default_task_notes': False, 'vision_compare': False,
                                                    'actor_payload_profile': 'full_v1'}}))
            for flags, expected in (([], False), (['--default-task-notes', '--vision-compare'], True)):
                with patch('sys.argv', ['auriga', '--config', str(path), *flags]), \
                        patch('auriga.apps.gateway.cli.serve', new_callable=AsyncMock) as serve:
                    main()
                args = serve.call_args.args[0]
                self.assertEqual(args.default_task_notes, expected)
                self.assertEqual(args.vision_compare, expected)
                self.assertEqual(args.actor_payload_profile, 'full_v1')
                self.assertEqual(args.completion_guidance, 'hook')
                self.assertFalse(args.benchmark)  # Default hook must not block development launches.

    async def test_older_unified_snapshot_does_not_enable_new_prompt_notes(self):
        for mode in ('resume', 'replay'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                saved = Config.from_dict({'features': {'motion_review': False, 'actor_payload_profile': 'full_v1'}}).to_dict()
                del saved['features']['default_task_notes']
                del saved['features']['vision_compare']
                del saved['features']['completion_guidance']
                manifest = dict(schema_version='0.1', run_id='run', episode_id='ep', env_instance_id='env', env_generation=1,
                                actor_information_policy=ACTOR_POLICY, observation_profile=ACTOR_PROFILE,
                                task_notes='archived exact notes', task_notes_source='archived source',
                                effective_config=saved)
                (root / 'manifest.json').write_text(json.dumps(manifest))
                workbench = Workbench(root, config=Config.from_dict({}), **{mode: True})
                try:
                    self.assertFalse(workbench.default_task_notes)
                    self.assertTrue(workbench.vision_compare)
                    self.assertTrue(workbench.harness.vision_compare)
                    self.assertEqual(workbench.actor_payload_profile, 'full_v1')
                    self.assertEqual(workbench.completion_guidance, 'none')
                    self.assertIsNone(workbench.harness.home_check)
                    self.assertEqual(workbench.archived_task_notes['text'], 'archived exact notes')
                    self.assertEqual(json.loads((root / 'manifest.json').read_text()), manifest)
                finally:
                    await workbench.close()

    async def test_config_disables_comparison_in_harness_and_model_tools(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Config.from_dict({'features': {'vision_compare': False, 'default_task_notes': False,
                                                    'motion_review': False}})
            workbench = Workbench(Path(temporary) / 'run', config=config)
            try:
                self.assertFalse(workbench.default_task_notes)
                self.assertFalse(workbench.harness.vision_compare)
                available = {tool['name'] for tool in workbench.actor_tools()}
                self.assertNotIn('vision_compare', available)
                self.assertNotIn('skill_run', available)
                with self.assertRaisesRegex(ValueError, 'vision_compare_not_enabled_for_run'):
                    workbench.harness.dispatch('vision_compare', {}, {'tool_call_id': 'disabled'})
            finally:
                await workbench.close()

    async def test_legacy_resolved_notes_remain_separate_from_custom_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            custom = 'x' * 8000
            combined = 'archived default notes\n\n' + custom
            manifest = dict(schema_version='0.1', run_id='run', episode_id='ep', env_instance_id='env', env_generation=1,
                            actor_information_policy=ACTOR_POLICY, observation_profile=ACTOR_PROFILE,
                            benchmark=True, task_notes=combined, task_notes_source='default; custom',
                            task_notes_config=dict(default_enabled=True, default_version='archived-version',
                                                   custom_text=custom, custom_source='custom'))
            (root / 'manifest.json').write_text(json.dumps(manifest))
            workbench = Workbench(root, replay=True, config=Config.from_dict({}))
            try:
                self.assertEqual(workbench.config.run.task_notes, custom)
                self.assertEqual(workbench.config.run.task_notes_source, 'custom')
                self.assertEqual(workbench.archived_task_notes['text'], combined)
                self.assertEqual(workbench.archived_task_notes['config']['default_version'], 'archived-version')
                # The archived custom settings can still be reused for a fresh episode.
                workbench.config.with_overrides({'run': {'replay': False}}).validate_launch()
            finally:
                await workbench.close()

    def test_motion_thresholds_unified_in_config_and_propagated_to_consumers(self):
        from auriga.core.config import MOTION_THRESHOLD_FIELDS, motion_thresholds
        from auriga.robot.backends.robodojo.robodojo import RobotBackend

        repo_json = json.loads((Path(__file__).resolve().parents[1] / 'configs/auriga.json').read_text())
        for field_name in MOTION_THRESHOLD_FIELDS:
            self.assertIn(field_name, repo_json['features'], field_name)
        custom = Config.from_dict({
            'features': {
                'default_position_tolerance_m': 0.006,
                'soft_position_tolerance_m': 0.012,
                'relaxed_position_tolerance_m': 0.014,
                'compliant_position_tolerance_m': 0.020,
                'grip_empty_threshold_mm': 0.8,
                'grip_holding_threshold_mm': 4.0,
                'orientation_tolerance_rad': 0.15,
            }
        })
        thresholds = motion_thresholds(custom)
        self.assertEqual(thresholds['default_position_tolerance_m'], 0.006)
        self.assertEqual(thresholds['soft_position_tolerance_m'], 0.012)
        robot = RobotBackend(config=custom)
        self.assertEqual(robot.motion_thresholds['orientation_tolerance_rad'], 0.15)
