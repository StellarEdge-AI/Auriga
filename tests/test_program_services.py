"""Program broker dependencies are explicit, dynamic and action-owner scoped."""
from dataclasses import FrozenInstanceError
from pathlib import Path
import tempfile
import unittest

from auriga.runtime.harness import Harness
from auriga.runtime.programs.runner import ProgramRunner, ProgramServices
from auriga.core.storage.store import Store
from auriga.core.tool_output import ToolOutput
from auriga.runtime.tools.registry import ToolSpec
from test_harness import FixtureRobot


class ProgramServicesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.store = Store(root / 'run', dict(run_id='services', episode_id='ep',
            env_instance_id='env', env_generation=1))
        self.addCleanup(self.store.close)
        self.harness = Harness(self.store, FixtureRobot(root))
        self.harness.start()
        self.services = self.harness.program_services()
        self.runner = ProgramRunner(self.services)
        self.addCleanup(self.runner.close)

    def test_explicit_port_rejects_runtime_owner_and_exposes_no_robot_or_registry(self):
        with self.assertRaisesRegex(TypeError, 'requires ProgramServices'):
            ProgramRunner(self.harness)
        self.assertIsInstance(self.services, ProgramServices)
        self.assertIs(self.runner.store, self.store)
        for name in ('harness', 'robot', 'registry', 'tool_registry', 'active_context', 'cancel'):
            self.assertFalse(hasattr(self.services, name), name)
        self.assertFalse(hasattr(self.runner, 'harness'))
        with self.assertRaises(FrozenInstanceError):
            self.services.invoke = None

    def test_dynamic_tool_registration_role_effect_and_disable_are_server_bound(self):
        calls = []
        def invoke(context, arguments, correlation):
            calls.append(arguments)
            return ToolOutput(arguments).response()
        definition = dict(type='function', name='late_program_tool', description='Port regression.',
            inputSchema=dict(type='object', properties={'execute': {'type': 'boolean'}},
                required=['execute'], additionalProperties=False))
        self.harness.tool_registry.register(ToolSpec(definition, invoke,
            effect=lambda arguments: 'physical' if arguments['execute'] else 'read',
            output_schemas={'data': definition['inputSchema']}))
        self.runner._validate_tool('late_program_tool')
        self.runner._validate_arguments('late_program_tool', {'execute': False})
        self.assertEqual(self.runner._tool_effect('late_program_tool', {'execute': True}), 'physical')
        self.assertEqual(self.runner._invoke('late_program_tool', {'execute': False}, {}).data, {'execute': False})
        with self.assertRaises(ValueError):
            self.runner._invoke('late_program_tool', {'execute': False, 'unknown': 1}, {})
        self.harness.tool_registry.register(ToolSpec(dict(definition, name='actor_only'), invoke,
            roles=frozenset({'actor'}), output_schemas={'data': definition['inputSchema']}))
        with self.assertRaisesRegex(ValueError, 'program_tool_not_permitted'):
            self.runner._validate_tool('actor_only')
        self.harness.disabled_tools = frozenset({'late_program_tool'})
        with self.assertRaisesRegex(ValueError, 'tool_disabled_by_config'):
            self.runner._invoke('late_program_tool', {'execute': False}, {})
        self.assertEqual(calls, [{'execute': False}])

    def test_observation_snapshot_and_cancellation_never_lend_mutable_state(self):
        original = self.harness.latest['observation_id']
        snapshot = self.services.observation()
        snapshot['observation_id'] = 'changed-by-caller'
        self.assertEqual(self.harness.latest['observation_id'], original)
        self.harness.latest = None
        self.assertIsNone(self.services.observation()['observation_id'])
        self.harness.active_context = {'program_id': 'other-program'}
        self.assertFalse(self.services.cancel_action('my-program'))
        self.assertFalse(self.harness.cancel.is_set())
        self.assertTrue(self.services.action_active('other-program'))
        self.assertTrue(self.services.cancel_action('other-program'))
        self.assertTrue(self.harness.cancel.is_set())
        self.assertTrue(self.harness.accepting)


if __name__ == '__main__':
    unittest.main()
