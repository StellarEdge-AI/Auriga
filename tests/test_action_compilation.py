"""Backend compiler selection through Harness and the public batch builder."""
from copy import deepcopy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

from auriga.robot.motion import action_compilation, arx_actions
from auriga.runtime.harness import Harness
from auriga.runtime.actions.unified_actions import compile_request
from auriga.runtime.actions.unified_actions import dispatch
from auriga.core.storage.store import Store
from test_harness import FixtureRobot


class CustomRobot(FixtureRobot):
    def start(self):
        return dict(super().start(), robot_model_id='custom', frame_id='custom-world',
            tcp_config_id='custom-tcp', limits_profile='custom-limits', path_kinds=['backend_free', 'hold'])

    @staticmethod
    def compile_action(kind, args, identity, capabilities):
        if kind != 'move':
            raise ValueError('custom_fixture_requires_link_pose')
        return dict(schema_version='0.1', **identity, world_kind='observed',
            action_id=args.get('action_id', 'custom-leaf'), based_on_observation_id=args['observation_id'],
            frame_id=capabilities['frame_id'], tcp_config_id=capabilities['tcp_config_id'],
            mode='absolute', limits_profile=capabilities['limits_profile'], timeout_s=args['timeout_s'],
            path=dict(kind='backend_free', waypoints=[{args['arm']: dict(position_m=args['position_m'],
                quaternion_xyzw=args['quaternion_xyzw'])}]), gripper_events=[])

    def validate_action(self, action):
        self.validations = getattr(self, 'validations', 0) + 1
        if action['tcp_config_id'] != 'custom-tcp':
            raise ValueError('custom_fixture_wrong_tcp')

    def preview_batch(self, action):
        self.previews = getattr(self, 'previews', 0) + 1
        return dict(status='planned', plan_id='custom-native', planned_native_steps=1,
            trajectory_data=dict(samples=[]))

    def execute(self, action, progress_callback=None, cancel_flag=None):
        self.executions += 1
        return dict(status='completed', executed_native_steps=1, completed_native_steps=1,
            batch_status='completed', batch_cursor=len(action['stages']),
            completed_stage_ids=[stage['id'] for stage in action['stages']], pending_stage_ids=[],
            stage_results={}, terminal_observation=self.observe())


class ActionCompilationTests(unittest.TestCase):
    def setUp(self):
        self.harness = object.__new__(Harness)
        self.harness.identity = dict(run_id='run', episode_id='ep', env_instance_id='env', env_generation=1)
        self.harness.capabilities = dict(frame_id='env_origin', tcp_config_id='arx-x5-link6-v1')
        self.harness.robot = SimpleNamespace()
        self.harness.config = SimpleNamespace(features=SimpleNamespace(default_motion_speed_scale=1.))
        self.pose = dict(arm='left', grasp_position_m=[.2, -.1, .8], angles_deg=[0, 0, 0])

    def test_non_arx_backend_owns_wait_and_grasp_compilation_paths(self):
        self.harness.capabilities = dict(frame_id='custom-world', tcp_config_id='custom-tcp', robot_model_id='custom')
        compiled = {'custom': 'compiled'}
        self.harness.robot = SimpleNamespace(compile_action=Mock(return_value=compiled))
        args = {'observation_id': 'obs'}
        with patch('auriga.robot.motion.arx_actions.compile_action', side_effect=AssertionError('ARX must not run')):
            self.assertIs(self.harness._action_from_args('robot_wait', args), compiled)
            self.assertIs(action_compilation.compile_action(self.harness.robot, 'grasp', args,
                self.harness.identity, self.harness.capabilities), compiled)
        self.harness.robot.compile_action.assert_has_calls([
            call('wait', args, self.harness.identity, self.harness.capabilities),
            call('grasp', args, self.harness.identity, self.harness.capabilities)])
        self.assertEqual(self.harness.robot.compile_action.call_count, 2)

    def test_unknown_or_conflicting_calibration_requires_explicit_compiler(self):
        for caps in ({}, {'robot_model_id': 'arx-x5'}, {'tcp_config_id': 'custom'},
                dict(self.harness.capabilities, robot_model_id='custom')):
            self.harness.capabilities = caps
            with self.subTest(capabilities=caps):
                with self.assertRaisesRegex(ValueError, '^action_compiler_unavailable:compile_action$'):
                    self.harness._action_from_args('robot_wait', {})

    def test_declared_legacy_x5_and_native_hooks_preserve_requests_and_errors(self):
        from auriga.robot.backends.robodojo.robodojo import RobotBackend
        single_args = dict(self.pose, observation_id='obs', action_id='single', contact_region='finger_middle')
        original = deepcopy(single_args)
        for robot in (SimpleNamespace(), object.__new__(RobotBackend)):
            self.harness.robot = robot
            self.assertEqual(action_compilation.compile_action(robot, 'grasp', single_args,
                    self.harness.identity, self.harness.capabilities),
                arx_actions.compile_action('grasp', single_args, self.harness.identity, self.harness.capabilities))
            with self.assertRaisesRegex(ValueError, '^dual_arm_progression_requires_dual_arm_motion$'):
                self.harness._action_from_args('robot_wait', {'dual_arm_progression': 'independent'})
        self.assertEqual(single_args, original)

    def test_batch_uses_backend_compiler_for_motion_and_split_jaw(self):
        self.harness.robot.compile_action = Mock(side_effect=arx_actions.compile_action)
        request = dict(observation_id='obs', action_id='batch', stages=[dict(self.pose,
            id='reach', kind='move', gripper_goal_normalized=0.)])
        original = deepcopy(request)
        result = compile_request(self.harness, request)
        self.assertEqual([call.args[0] for call in self.harness.robot.compile_action.call_args_list], ['grasp', 'gripper'])
        self.assertEqual([stage['id'] for stage in result['stages']], ['reach/motion', 'reach'])
        self.assertEqual(request, original)

    def custom_harness(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = Store(Path(temporary.name) / 'store', self.harness.identity)
        self.addCleanup(store.close)
        robot = CustomRobot(temporary.name)
        harness = Harness(store, robot)
        harness.start()
        request = dict(observation_id=harness.latest['observation_id'], stages=[dict(id='reach',
            kind='move', arm='left', position_m=[.2, 0., 1.], quaternion_xyzw=[0., 0., 0., 1.])])
        return harness, robot, request

    def test_non_arx_public_preview_and_cached_execution_use_same_validation(self):
        harness, robot, request = self.custom_harness()
        with patch('auriga.runtime.actions.inspection._trajectory_image', return_value=({'status': 'unavailable'}, [])):
            preview = dispatch(harness, request, {'tool_call_id': 'preview'})
            self.assertEqual(preview.data['status'], 'planned')
            harness.publish_observation(robot.observe())
            result = dispatch(harness, dict(mode='execute', plan_id=preview.data['plan_id']),
                {'tool_call_id': 'execute'})
        self.assertEqual(result.data['receipt']['status'], 'completed')
        self.assertEqual((robot.previews, robot.executions), (1, 1))
        self.assertEqual(robot.validations, 3)

    def test_non_arx_batch_rejects_invalid_children_and_missing_native_validator(self):
        from auriga.core.contracts import validate_action
        harness, robot, request = self.custom_harness()
        compiled = compile_request(harness, dict(request, action_id='batch'))
        for field, value, reason in (
                ('based_on_observation_id', 'stale', 'stale_observation'),
                ('env_generation', 999, 'stale_environment_identity'),
                ('type', 'BatchAction', 'batch_stage_requires_ee_action')):
            action = deepcopy(compiled)
            action['stages'][0]['action'][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, reason):
                validate_action(action, harness.identity, harness.latest['observation_id'], harness.capabilities)
        action = deepcopy(compiled)
        action['stages'][0]['action']['path']['waypoints'][0]['left']['position_m'][0] = float('nan')
        with self.assertRaisesRegex(ValueError, 'position_m'):
            validate_action(action, harness.identity, harness.latest['observation_id'], harness.capabilities)
        robot.validate_action = None
        with self.assertRaisesRegex(ValueError, 'action_validator_unavailable'):
            dispatch(harness, request, {})
        rejected = harness.execute(compiled)['receipt']
        self.assertEqual(rejected['status'], 'rejected')
        self.assertIn('action_validator_unavailable', rejected['reason'])
        self.assertEqual(robot.executions, 0)
        self.assertFalse(hasattr(robot, 'previews'))


if __name__ == '__main__':
    unittest.main()
