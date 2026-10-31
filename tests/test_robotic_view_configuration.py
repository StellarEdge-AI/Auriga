"""The optional wrist-camera suggestion tool follows existing config, CLI and queue precedence."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from auriga.core.config import Config, ConfigError
from auriga.apps.evaluation.queue import command
from auriga.apps.gateway.workbench import Workbench, _configured_tool_specs
from auriga.apps.gateway.cli import main
from auriga.core.observation_policy import ACTOR_POLICY, ACTOR_PROFILE
from test_harness import FixtureRobot


class RoboticViewConfigurationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def cli(self, *arguments):
        output = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), \
                patch('auriga.core.config.LOCAL_CONFIG_PATH', self.root/'missing.local.json'), \
                patch('sys.argv', ['auriga', '--print-config', *arguments]), \
                patch('auriga.apps.gateway.cli.serve', new_callable=AsyncMock) as serve, \
                contextlib.redirect_stdout(output):
            main()
        serve.assert_not_called()
        return json.loads(output.getvalue())

    def test_default_off_and_strict_boolean_archival_roundtrip(self):
        self.assertFalse(Config.from_dict({}).features.robotic_view)
        for value in (True, False):
            config = Config.from_dict({'features': {'robotic_view': value}})
            self.assertIs(Config.from_dict(config.to_dict()).features.robotic_view, value)
        for value in (0, 1, None, 'true'):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                Config.from_dict({'features': {'robotic_view': value}})

    def test_default_off_preserves_supplied_plugin_tuple_and_constructor_rejects_nonbool(self):
        supplied = (object(),)
        self.assertIs(_configured_tool_specs(Config.from_dict({'features': {'robotic_view': False, 'stereo_guide': False}}), supplied), supplied)
        for value in (0, 1, None, 'true'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'invalid_control_feature_flag'):
                Workbench(self.root/'unused', config=Config.from_dict({}), robotic_view=value)
        self.assertFalse((self.root/'unused').exists())

    def test_cli_explicit_off_overrides_enabled_config(self):
        config = self.root/'config.json'
        config.write_text(json.dumps({'features': {'robotic_view': True}}))
        self.assertFalse(self.cli()['features']['robotic_view'])
        self.assertTrue(self.cli('--robotic-view')['features']['robotic_view'])
        self.assertFalse(self.cli('--config', str(config), '--no-robotic-view')['features']['robotic_view'])

    def test_cli_disabled_tool_validation_uses_enabled_registry(self):
        values = self.cli('--robotic-view', '--disabled-tools', 'robotic_view')
        self.assertEqual(values['features']['disabled_tools'], ['robotic_view'])
        error = io.StringIO()
        with contextlib.redirect_stderr(error), self.assertRaises(SystemExit):
            self.cli('--no-robotic-view', '--disabled-tools', 'robotic_view')
        self.assertIn('unknown_disabled_tools:robotic_view', error.getvalue())

    def test_queue_roundtrips_explicit_on_off_without_changing_omitted_flags(self):
        case = dict(cwd=str(Path(__file__).resolve().parents[1]), run_dir=str(self.root/'run'),
                    task='push_T', seed=0, layout_id=0, title='fixture', tag=[])
        default = command(case, 0)
        self.assertFalse(any('robotic-view' in item for item in default))
        for enabled, flag in ((True, '--robotic-view'), (False, '--no-robotic-view')):
            argv = command({**case, 'robotic_view': enabled}, 0)
            self.assertEqual([item for item in argv if item != flag], default)
            self.assertIs(self.cli(*argv[4:])['features']['robotic_view'], enabled)
        for value in (0, 1, None, 'true'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'Invalid robotic_view'):
                command({**case, 'robotic_view': value}, 0)


class RoboticViewArchiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_resume_and_replay_preserve_frozen_tool_setting_including_old_archives(self):
        for mode in ('resume', 'replay'):
            for archived in (None, False, True):
                with self.subTest(mode=mode, archived=archived), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    saved = Config.from_dict({'features': {'motion_review': False, 'sandbox_enabled': False,
                        'grasp_memory_dir': str(root/'memory'), 'robotic_view': bool(archived)}}).to_dict()
                    if archived is None:
                        del saved['features']['robotic_view']
                    run = root/'run'
                    run.mkdir()
                    manifest = dict(schema_version='0.1', run_id='run', episode_id='ep',
                        env_instance_id='env', env_generation=1, actor_information_policy=ACTOR_POLICY,
                        observation_profile=ACTOR_PROFILE, effective_config=saved)
                    (run/'manifest.json').write_text(json.dumps(manifest))
                    incoming = Config.from_dict({'features': {'robotic_view': not bool(archived)}})
                    workbench = Workbench(run, robot=FixtureRobot(root), config=incoming, **{mode: True})
                    try:
                        self.assertIs(workbench.config.features.robotic_view, bool(archived))
                        for role in ('actor', 'program'):
                            names = {tool['name'] for tool in workbench.harness.tool_registry.catalog(workbench.harness, role=role)}
                            self.assertIs('robotic_view' in names, bool(archived))
                        self.assertEqual(json.loads((run/'manifest.json').read_text()), manifest)
                    finally:
                        await workbench.close()


    async def test_pre_snapshot_legacy_resume_cannot_enable_new_plugins_from_incoming_config(self):
        for mode in ('resume', 'replay'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                run = root/'run'
                run.mkdir()
                manifest = dict(schema_version='0.1', run_id='run', episode_id='ep',
                    env_instance_id='env', env_generation=1, actor_information_policy=ACTOR_POLICY,
                    observation_profile=ACTOR_PROFILE, grasp_memory_dir=str(root/'memory'))
                (run/'manifest.json').write_text(json.dumps(manifest))
                incoming = Config.from_dict({'features': {'robotic_view': True}})
                workbench = Workbench(run, robot=FixtureRobot(root), config=incoming, **{mode: True})
                try:
                    self.assertFalse(workbench.config.features.robotic_view)
                    for role in ('actor', 'program'):
                        names = {tool['name'] for tool in workbench.harness.tool_registry.catalog(workbench.harness, role=role)}
                        self.assertNotIn('robotic_view', names)
                    self.assertEqual(json.loads((run/'manifest.json').read_text()), manifest)
                finally:
                    await workbench.close()


if __name__ == '__main__':
    unittest.main()
