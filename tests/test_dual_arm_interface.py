"""Paired requests keep both arms atomic across public and numeric boundaries."""

from copy import deepcopy
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


from tool_fixture import install_registry

from auriga.core.config import Config
from auriga.core.contracts import digest, validate_action, validate_action_speed
from auriga.apps.gateway.workbench import Workbench
from auriga.runtime.harness import Harness
from auriga.robot.motion.motion_speed import arm_action, resolve_action_speed
from auriga.robot.backends.robodojo.robodojo import FRAME_ID, TCP_CONFIG_ID, RobotBackend
from auriga.robot.backends.robodojo.robodojo import validate_action as validate_native, validate_preview_action
from auriga.core.storage.store import Store
from test_harness import FixtureRobot


IDENTITY = dict(run_id='dual-interface', episode_id='ep', env_instance_id='env', env_generation=1)


def paired_action(count=2, **selections):
    return dict(schema_version='0.1', **IDENTITY, world_kind='observed', action_id='paired',
        based_on_observation_id='observed', mode='absolute', frame_id=FRAME_ID, tcp_config_id=TCP_CONFIG_ID,
        limits_profile='simulation-slow-v1', timeout_s=120,
        path=dict(kind='backend_free', orientation='backend_free', blend_radius_m=0.,
            position_tolerance_m=.005, waypoints=[{
                arm: dict(position_m=[.1 * (index + 1), lateral, 1.], quaternion_xyzw=[0., 0., 0., 1.])
                for arm, lateral in (('left', -.1), ('right', .1))} for index in range(count)]),
        gripper_events=[], **selections)


def capabilities():
    return dict(frame_id=FRAME_ID, tcp_config_id=TCP_CONFIG_ID, path_kinds=['backend_free', 'hold'],
        max_waypoints=8, dual_arm=True, gripper_after_motion=True,
        gripper_phases=['after_motion', 'during_motion'],
        action_speed=dict(available=True, waypoint_speed_scales=dict(available=True)))


def event(arm, goal=0.):
    return dict(arm=arm, phase='after_motion', goal_normalized=goal)


class DualArmSpeedTests(unittest.TestCase):
    def test_each_arm_resolves_presets_waypoints_and_jaws_independently(self):
        action = paired_action(3, arm_options={
            'left': dict(speed_preset='normal', waypoint_speed_presets=['faster', None, 'slow'],
                         gripper_speed_preset='slow'),
            'right': dict(speed_scale=.6, waypoint_speed_scales=[None, 1.25, .25],
                          gripper_speed_scale=.5)})
        action['gripper_events'] = [event('left'), event('right', 1.)]
        original = deepcopy(action)
        result = resolve_action_speed(action, 1.5)
        self.assertEqual(result['arm_options'], {
            'left': dict(speed_scale=1., waypoint_speed_scales=[1.5, 1., .8], gripper_speed_scale=.8),
            'right': dict(speed_scale=.6, waypoint_speed_scales=[.6, 1.25, .25], gripper_speed_scale=.5)})
        self.assertEqual(action, original)
        self.assertEqual(resolve_action_speed(result, .25), result)
        validate_action_speed(result, capabilities())
        self.assertEqual(validate_native(result), ('both', result['path']['waypoints'][-1]))
        result['arm_options']['right']['waypoint_speed_scales'][0] = 2.
        self.assertEqual(action, original)

    def test_shared_defaults_and_per_arm_overrides_preserve_null_inheritance(self):
        action = paired_action(speed_preset='fast', gripper_speed_preset='slow',
            waypoint_speed_presets=[None, 'slow'], arm_options={'right': dict(speed_preset='normal')})
        action['gripper_events'] = [event('left')]
        result = resolve_action_speed(action, 1.5)
        self.assertEqual(result['arm_options']['left'], dict(
            speed_scale=1.25, waypoint_speed_scales=[1.25, .8], gripper_speed_scale=.8))
        self.assertEqual(result['arm_options']['right'], dict(
            speed_scale=1., waypoint_speed_scales=[1., .8]))
        plain = resolve_action_speed(paired_action(), 1.5)
        self.assertEqual(plain['arm_options'], {'left': {'speed_scale': 1.5}, 'right': {'speed_scale': 1.5}})
        single = arm_action(result, 'right')
        self.assertNotIn('arm_options', single)
        self.assertNotIn('gripper_speed_scale', single)
        self.assertEqual(single['gripper_events'], [])
        self.assertTrue(all(set(point) == {'right'} for point in single['path']['waypoints']))

    def test_numeric_and_preset_options_share_canonical_content(self):
        presets = paired_action(arm_options={'left': dict(speed_preset='slow'),
            'right': dict(speed_preset='fast', waypoint_speed_presets=[None, 'slow'])})
        numeric = paired_action(arm_options={'left': dict(speed_scale=.8),
            'right': dict(speed_scale=1.25, waypoint_speed_scales=[1.25, .8])})
        self.assertEqual(digest(resolve_action_speed(presets)), digest(resolve_action_speed(numeric)))

    def test_per_arm_conflicts_unknown_fields_and_invalid_numbers_fail_closed(self):
        invalid = [None, [], {'middle': {}}, {'left': None}, {'left': {'timeout_s': 20}},
            {'left': {'gripper_events': []}}, {'left': {'speed_preset': 'slow', 'speed_scale': .8}},
            {'right': {'waypoint_speed_presets': ['slow', None], 'waypoint_speed_scales': [.8, 1.]}},
            {'right': {'gripper_speed_preset': 'slow', 'gripper_speed_scale': .8}}]
        for field, values in (
                ('speed_scale', [True, float('nan'), float('inf'), .24, 2.01]),
                ('gripper_speed_scale', [False, float('nan'), .24, 1.01]),
                ('waypoint_speed_scales', [[1., True], [1., float('nan')], [], [1.]])):
            invalid.extend({'right': {field: value}} for value in values)
        for selection in invalid:
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                action = paired_action(arm_options=selection)
                action['gripper_events'] = [event('left'), event('right')]
                resolve_action_speed(action)
        for speed in ({'gripper_speed_scale': .8}, {'gripper_speed_preset': 'slow'}):
            with self.subTest(speed=speed), self.assertRaises(ValueError):
                action = paired_action(arm_options={'right': speed})
                action['gripper_events'] = [event('left')]
                resolve_action_speed(action)


class DualArmBoundaryTests(unittest.TestCase):
    def checks(self):
        return (lambda action: validate_action(action, IDENTITY, 'observed', capabilities()), validate_native)

    def test_paired_action_and_two_unique_jaws_pass_both_numeric_boundaries(self):
        action = paired_action()
        action['gripper_events'] = [event('left'), event('right', 1.)]
        action['gripper_events'][1].update(phase='during_motion', start_fraction=.2, end_fraction=.8)
        for check in self.checks():
            check(resolve_action_speed(action))
        unsupported = {**capabilities(), 'dual_arm': False}
        with self.assertRaisesRegex(ValueError, 'unsupported_capability:arms'):
            validate_action(action, IDENTITY, 'observed', unsupported)

    def test_mixed_armsets_and_malformed_events_are_rejected_at_both_boundaries(self):
        actions = []
        for first, second in (({'left'}, {'right'}), ({'left', 'right'}, {'left'}),
                              ({'right'}, {'left', 'right'})):
            action = paired_action()
            action['path']['waypoints'] = [{arm: pose for arm, pose in point.items() if arm in arms}
                for point, arms in zip(action['path']['waypoints'], (first, second))]
            actions.append(action)
        for events in (None, {}, [None], ['left'],
                       [event('left'), event('left')], [event('left'), event('right'), event('left')],
                       [event('middle')], [event('right', True)], [event('right', float('nan'))],
                       [dict(event('right'), unexpected=1)], [dict(event('right'), phase='before_motion')]):
            action = paired_action(arm_options={})
            action['gripper_events'] = events
            actions.append(action)
        for index, action in enumerate(actions):
            for check in self.checks():
                with self.subTest(index=index, boundary=check.__name__), self.assertRaises(ValueError):
                    check(action)

    def test_numeric_boundary_rejects_invalid_options_and_jaw_without_own_event(self):
        options = [None, [], {'middle': {}}, {'right': {'timeout_s': 30}},
            {'right': {'speed_preset': 'slow'}}, {'right': {'speed_scale': True}},
            {'right': {'speed_scale': float('nan')}}, {'right': {'waypoint_speed_scales': [1., None]}},
            {'right': {'gripper_speed_scale': .8}}]
        for selection in options:
            action = paired_action(arm_options=selection)
            action['gripper_events'] = [event('left')]
            for check in self.checks():
                with self.subTest(selection=selection, boundary=check.__name__), self.assertRaises(ValueError):
                    check(action)
        for kind in ('single', 'hold', 'prefix'):
            action = paired_action(arm_options={})
            if kind == 'single':
                action['path']['waypoints'] = [{'left': point['left']} for point in action['path']['waypoints']]
            elif kind == 'hold':
                action['path'].update(kind='hold', waypoints=[])
            else:
                action.update(type='JointPrefixAction', actions=[[0.] * 14])
            for check in (*self.checks(), resolve_action_speed):
                with self.subTest(kind=kind, boundary=check.__name__), self.assertRaises(ValueError):
                    check(action)

    def test_malformed_path_and_pose_objects_raise_validation_errors(self):
        for malformed in (None, [], True, 1):
            for location in ('path', 'single_pose', 'paired_pose'):
                action = paired_action(1)
                if location == 'path':
                    action['path'] = malformed
                else:
                    if location == 'single_pose':
                        action['path']['waypoints'][0].pop('left')
                    action['path']['waypoints'][0]['right'] = malformed
                for check in self.checks():
                    with self.subTest(location=location, malformed=malformed, boundary=check.__name__), \
                            self.assertRaises(ValueError):
                        check(action)

    def test_dual_preview_rejected_before_backend_rpc(self):
        action = resolve_action_speed(paired_action(1))
        with self.assertRaisesRegex(ValueError, 'dual_arm_motion_preview_unsupported'):
            validate_preview_action(action)
        backend = object.__new__(RobotBackend)
        backend.operation_lock = threading.Lock()
        backend._request = Mock(side_effect=AssertionError('preview must not reach the worker'))
        with self.assertRaisesRegex(ValueError, 'dual_arm_motion_preview_unsupported'):
            backend.preview_motion(action)
        backend._request.assert_not_called()


class DualArmHarnessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name) / 'store', dict(IDENTITY))
        self.addCleanup(self.store.close)
        self.robot = FixtureRobot(temporary.name)
        observe = self.robot.observe
        self.robot.observe = lambda: dict(observe(), camera_calibration=dict(reference_frame_id='env_origin', cameras={}),
            camera_pose_sample=dict(cameras={}), robot_model=dict(base_link='base', joints=[]))
        caps = {**self.robot.start(), **capabilities()}
        self.robot.start = lambda: caps
        self.h = Harness(self.store, self.robot, config=Config.from_dict(dict(features=dict(
            motion_review=False, default_motion_speed_scale=1.25))))
        self.h.start()

    def test_unified_catalog_preserves_both_arm_schema_across_backend_modes(self):
        workbench = Workbench.__new__(Workbench)
        workbench.pi05_enabled = False
        workbench.visual_review_model = None
        for mode in ('resampled', 'feedback'):
            for supported in (None, True, False):
                with self.subTest(mode=mode, supported=supported):
                    workbench.config = Config.from_dict(dict(features=dict(
                        motion_review=False, motion_execution_mode=mode)))
                    workbench.harness = SimpleNamespace(capabilities=(None if supported is None else
                        {**capabilities(), 'dual_arm': supported}))
                    install_registry(workbench.harness, workbench.config)
                    tools = {tool['name']: tool for tool in workbench.actor_tools()}
                    self.assertEqual('robot_action' in tools, mode == 'resampled')
                    self.assertIn('operation_memory', tools)
                    self.assertNotIn('robot_move_dual_grasp', tools)
                    if mode == 'feedback':
                        continue
                    schema = tools['robot_action']['inputSchema']['properties']
                    self.assertEqual(schema['stages']['items']['properties']['arm']['enum'],
                                     ['left', 'right'])
                    self.assertIn('depends_on', schema['stages']['items']['properties'])


if __name__ == '__main__':
    unittest.main()
