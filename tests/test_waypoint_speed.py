"""Per-waypoint overrides survive public tools, admission, archival and caching."""

from copy import deepcopy
import unittest


from auriga.core.contracts import digest, validate_action_speed
from auriga.robot.motion.motion_speed import resolve_action_speed
from auriga.robot.backends.robodojo.robodojo import validate_action as validate_native
from test_joint_replan_runtime import motion_action
from test_native_wait import wait_action


def motion(count=2, **selections):
    return motion_action(angles=tuple(-.1 * (index + 1) for index in range(count))) | selections


class WaypointSpeedResolutionTests(unittest.TestCase):
    def test_presets_follow_waypoint_order_with_fixed_values_and_null_inheritance(self):
        for base, inherited in (({}, 1.25), ({'speed_preset': 'normal'}, 1.),
                                ({'speed_scale': .8}, .8)):
            action = motion(4, waypoint_speed_presets=['faster', None, 'slow', 'normal'], **base)
            result = resolve_action_speed(action, 1.25)
            self.assertEqual(result['waypoint_speed_scales'], [1.5, inherited, .8, 1.])
            self.assertTrue(all(type(value) is float for value in result['waypoint_speed_scales']))
            self.assertNotIn('waypoint_speed_presets', result)
            validate_native(result)

    def test_numeric_boundaries_and_null_inherit_without_mutating_inputs(self):
        action = motion(4, speed_scale=.75, waypoint_speed_scales=[.25, None, 1, 2])
        original = deepcopy(action)
        result = resolve_action_speed(action, 1.5)
        self.assertEqual(result['waypoint_speed_scales'], [.25, .75, 1., 2.])
        self.assertEqual(action, original)
        result['waypoint_speed_scales'][0] = 1.
        self.assertEqual(action, original)
        canonical = resolve_action_speed(action, 1.5)
        self.assertEqual(resolve_action_speed(canonical, 2.), canonical)
        same = resolve_action_speed(motion(4, speed_scale=.75,
                                          waypoint_speed_scales=[.25, .75, 1., 2.]))
        self.assertEqual(digest(canonical), digest(same))

    def test_omission_preserves_legacy_shape_and_jaw_selection_is_independent(self):
        result = resolve_action_speed(motion(), 1.25)
        self.assertNotIn('waypoint_speed_scales', result)
        self.assertEqual(result['speed_scale'], 1.25)
        action = motion(2, waypoint_speed_presets=['fast', 'slow'], gripper_speed_preset='slow')
        action['gripper_events'] = [dict(arm='left', phase='after_motion', goal_normalized=0.)]
        result = resolve_action_speed(action, 1.5)
        self.assertEqual(result['waypoint_speed_scales'], [1.25, .8])
        self.assertEqual(result['gripper_speed_scale'], .8)

    def test_conflicts_lengths_container_types_and_invalid_entries_fail_closed(self):
        with self.assertRaises(ValueError):
            resolve_action_speed(motion(2, waypoint_speed_presets=['normal', 'normal'],
                                         waypoint_speed_scales=[1., 1.]))
        for field in ('waypoint_speed_presets', 'waypoint_speed_scales'):
            for value in (None, True, 'slow', {}, (), [], [None], [None] * 3):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    resolve_action_speed(motion(**{field: value}))
        for field, invalid in (
                ('waypoint_speed_presets', (True, 1, .6, '', 'SLOW', 'unknown', [], {})),
                ('waypoint_speed_scales', (True, False, 'slow', '1', .24, 2.01, float('nan'), float('inf'), [], {}))):
            for value in invalid:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    resolve_action_speed(motion(**{field: [None, value]}))

    def test_malformed_waypoint_container_is_a_validation_error_at_both_boundaries(self):
        for points in (None, 1, 'invalid', {}, ()):
            action = motion(1, waypoint_speed_scales=[1.])
            action['path']['waypoints'] = points
            for check in (resolve_action_speed, validate_action_speed, validate_native):
                with self.subTest(points=points, boundary=check.__name__), self.assertRaises(ValueError):
                    check(action)

    def test_wait_prefix_and_standalone_jaw_cannot_accept_waypoint_choices(self):
        gripper = wait_action()
        gripper.pop('hold_native_steps')
        gripper['gripper_events'] = [dict(arm='left', phase='after_motion', goal_normalized=0.)]
        for action in (wait_action(1), dict(type='JointPrefixAction', actions=[[0.] * 14]), gripper):
            for field in ('waypoint_speed_presets', 'waypoint_speed_scales'):
                with self.subTest(kind=action.get('type'), field=field), self.assertRaises(ValueError):
                    resolve_action_speed(action | {field: [None]})

    def test_numeric_boundary_requires_resolved_entries_and_explicit_capability(self):
        for invalid in (motion(1, waypoint_speed_presets=['slow']),
                        motion(1, waypoint_speed_scales=[None]),
                        motion(1, waypoint_speed_scales=[True]),
                        motion(1, waypoint_speed_scales=[float('nan')])):
            with self.assertRaises(ValueError):
                validate_native(invalid)
        action = resolve_action_speed(motion(1, waypoint_speed_presets=['normal']))
        for caps in ({}, {'action_speed': {'available': True}},
                     {'action_speed': {'available': True, 'waypoint_speed_scales': {'available': False}}}):
            with self.assertRaises(ValueError):
                validate_action_speed(action, caps)
        validate_action_speed(action, {'action_speed': {'available': True,
                                                       'waypoint_speed_scales': {'available': True}}})
        validate_action_speed(resolve_action_speed(motion(1)), {})


if __name__ == '__main__':
    unittest.main()
