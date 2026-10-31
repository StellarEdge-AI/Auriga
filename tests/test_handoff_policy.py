"""Run-scoped handoff switches must not disable simultaneous arm execution."""
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from tool_fixture import install_registry

from auriga.core.config import Config, ConfigError
from auriga.apps.gateway.workbench import Workbench
from auriga.apps.gateway.cli import main
from auriga.cortex.context.task_notes import handoff_policy
from test_task_notes import fixture_workbench


class HandoffConfigurationTest(unittest.TestCase):
    def test_strategy_switch_preserves_parallelism_and_requires_boolean(self):
        defaults = Config()
        self.assertTrue(defaults.features.airborne_handoff)
        self.assertEqual(defaults.features.handoff_preference,'surface')
        for airborne in (False, True):
            config = Config.from_dict({'features': dict(airborne_handoff=airborne)})
            self.assertTrue(config.features.arm_queue)
            self.assertEqual(config.features.motion_execution_mode, 'resampled')
            self.assertEqual(config.features.handoff_preference,'airborne' if airborne else 'surface')
        for value in (0, 1, None, 'false'):
            with self.assertRaises(ConfigError):
                Config.from_dict({'features': {'airborne_handoff': value}})

    def test_preference_is_separate_from_permission_and_legacy_overrides_keep_strategy(self):
        cases=[(False,'surface','surface_only'),(True,'surface','prefer_surface'),(True,'airborne','prefer_airborne')]
        for allowed,preference,strategy in cases:
            config=Config.from_dict({'features':dict(airborne_handoff=allowed,handoff_preference=preference)})
            policy=handoff_policy(config.features.airborne_handoff,handoff_preference=config.features.handoff_preference)
            self.assertEqual(policy['strategy'],strategy)
        surface=handoff_policy(True,handoff_preference='surface')['text']
        self.assertIn('not a requirement',surface)
        self.assertIn('Airborne handoff remains available',surface)
        self.assertIn('fresh evidence of receiver support',surface)
        self.assertIn('independent progression',surface)
        for preference in ('surface', 'airborne'):
            text=handoff_policy(True,handoff_preference=preference)['text']
            self.assertIn('when the working arm cannot reach it',text)
            self.assertIn('cooperative manipulation of one difficult object',text)
            self.assertIn('with no inherent preference',text)
        self.assertNotIn('with no inherent preference',handoff_policy(False)['text'])
        for value in ('invalid',None,True,[]):
            with self.assertRaises(ConfigError):Config.from_dict({'features':dict(handoff_preference=value)})
        with self.assertRaises(ConfigError):
            Config.from_dict({'features':dict(airborne_handoff=False,handoff_preference='airborne')})
        base=Config()
        self.assertEqual(base.with_overrides({'features':{'airborne_handoff':True}}).features.handoff_preference,'airborne')
        self.assertEqual(base.with_overrides({'features':{'airborne_handoff':False}}).features.handoff_preference,'surface')

    def test_legacy_files_and_cli_override_precedence(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'config.json'
            path.write_text(json.dumps({'features':{'airborne_handoff':True}}))
            old=Config.load(path,environ={})
            self.assertEqual(old.features.handoff_preference,'airborne')
            new=Config.load(path,environ={},overrides={'features':{'handoff_preference':'surface'}})
            self.assertTrue(new.features.airborne_handoff)
            self.assertEqual(new.features.handoff_preference,'surface')
            disabled=Config.load(path,environ={},overrides={'features':{'airborne_handoff':False}})
            self.assertFalse(disabled.features.airborne_handoff)
            self.assertEqual(disabled.features.handoff_preference,'surface')

    def test_cli_roundtrips_each_mode_and_keeps_unified_dual_actions(self):
        for airborne in (False, True):
            flags = [('--' if airborne else '--no-') + 'airborne-handoff']
            with patch('sys.argv', ['auriga', '--benchmark', *flags]), \
                    patch('auriga.apps.gateway.cli.serve', new_callable=AsyncMock) as serve:
                main()
            args = serve.call_args.args[0]
            self.assertEqual(args.airborne_handoff, airborne)
            workbench = Workbench.__new__(Workbench)
            workbench.config = args.resolved_config
            workbench.pi05_enabled = False
            workbench.visual_review_model = None
            workbench.harness = SimpleNamespace(capabilities=dict(dual_arm=True,
                arm_queue=dict(available=True), motion_cost_preview=dict(available=True)))
            install_registry(workbench.harness, workbench.config)
            tools = {tool['name']: tool for tool in workbench.actor_tools()}
            self.assertIn('robot_action', tools)
            self.assertIn('operation_memory', tools)
            self.assertFalse({'robot_move_dual_grasp', 'robot_execute_queue',
                              'robot_preview_cost', 'manipulation_run', 'skill_run'} & tools.keys())
            schema = tools['robot_action']['inputSchema']['properties']
            self.assertEqual(schema['stages']['items']['properties']['arm']['enum'], ['left', 'right'])
            self.assertIn('depends_on', schema['stages']['items']['properties'])
            self.assertTrue({'preview', 'execute', 'inspect'} <= set(schema['mode']['enum']))

    def test_cli_new_default_and_explicit_surface_preference_leave_airborne_available(self):
        for flags in ([],['--airborne-handoff','--handoff-preference','surface']):
            with patch('sys.argv',['auriga','--benchmark',*flags]),patch('auriga.apps.gateway.cli.serve',new_callable=AsyncMock) as serve:
                main()
            config=serve.call_args.args[0].resolved_config
            self.assertTrue(config.features.airborne_handoff)
            self.assertEqual(config.features.handoff_preference,'surface')


class HandoffStartupTest(unittest.IsolatedAsyncioTestCase):
    async def test_new_default_policy_is_injected_and_archived_without_optional_notes(self):
        with tempfile.TemporaryDirectory() as temporary:
            config=Config.from_dict({'features':{'default_task_notes':False}})
            root=Path(temporary)/'run'
            workbench,terminal=fixture_workbench(root,config=config)
            try:
                with patch('auriga.apps.gateway.workbench.Terminal',return_value=terminal):
                    await workbench.start('unused',benchmark=True)
                manifest=json.loads((root/'manifest.json').read_text())
                self.assertEqual(manifest['handoff_policy']['strategy'],'prefer_surface')
                self.assertTrue(manifest['handoff_policy']['airborne_handoff'])
                self.assertIn('not a requirement',workbench.session.start.call_args.args[1])
            finally:await workbench.close()
            resumed,_=fixture_workbench(root,resume=True)
            try:
                self.assertTrue(resumed.config.features.airborne_handoff)
                self.assertEqual(resumed.config.features.handoff_preference,'surface')
            finally:await resumed.close()

    async def test_actual_policy_is_archived_and_sent_even_without_optional_notes(self):
        for airborne in (False, True):
            with tempfile.TemporaryDirectory() as temporary:
                config = Config.from_dict({'features': dict(default_task_notes=False,
                    airborne_handoff=airborne)})
                workbench, terminal = fixture_workbench(Path(temporary) / 'run', config=config)
                try:
                    with patch('auriga.apps.gateway.workbench.Terminal', return_value=terminal):
                        await workbench.start('unused', benchmark=True)
                    policy = handoff_policy(airborne)
                    manifest = json.loads((workbench.root / 'manifest.json').read_text())
                    self.assertEqual(manifest['handoff_policy'], policy)
                    call = workbench.session.start.call_args
                    self.assertIn(policy['text'], call.args[1])
                    self.assertIsNone(manifest['task_notes'])
                finally:
                    await workbench.close()

    async def test_legacy_resume_keeps_airborne_allowed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'run'
            original, terminal = fixture_workbench(root)
            try:
                with patch('auriga.apps.gateway.workbench.Terminal', return_value=terminal):
                    await original.start('unused', benchmark=True)
            finally:
                await original.close()
            path = root / 'manifest.json'
            manifest = json.loads(path.read_text())
            manifest['effective_config']['features'].pop('airborne_handoff')
            manifest['effective_config']['features'].pop('handoff_preference')
            path.write_text(json.dumps(manifest))
            resumed, _ = fixture_workbench(root, resume=True)
            try:
                self.assertTrue(resumed.config.features.airborne_handoff)
                self.assertEqual(resumed.config.features.handoff_preference,'airborne')
            finally:
                await resumed.close()
