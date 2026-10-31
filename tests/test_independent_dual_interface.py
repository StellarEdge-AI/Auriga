"""Independent progression is explicit, capability-gated and idempotent.

CPU fixtures exercise public conversion, stored action identity and numeric
boundaries. They do not establish physical collision or manipulation safety.
"""

from copy import deepcopy
import threading
import unittest
from unittest.mock import Mock

import numpy as np

from auriga.core.contracts import validate_action, validate_dual_arm_progression
from auriga.robot.motion.motion_speed import resolve_action_speed
from auriga.robot.backends.robodojo.robodojo import RobotBackend, validate_action as validate_native
from auriga.robot.backends.robodojo.robodojo import validate_joint_prefix, validate_preview_action
import test_dual_arm_interface as paired
from test_dual_arm_runtime import dual_action, dual_adapter, native_rows


def capabilities():
    return dict(paired.capabilities(), dual_arm_progression=dict(
        available=True, modes=['paired', 'independent'], default='paired'))


class IndependentProgressionBoundaryTests(unittest.TestCase):
    def test_omission_preserves_legacy_action_and_requires_no_new_capability(self):
        action = paired.paired_action()
        original = deepcopy(action)
        self.assertEqual(validate_dual_arm_progression(action, paired.capabilities()), 'paired')
        validate_action(action, paired.IDENTITY, 'observed', paired.capabilities())
        self.assertEqual(validate_native(action)[0], 'both')
        self.assertEqual(action, original)
        self.assertNotIn('dual_arm_progression', resolve_action_speed(action))

    def test_explicit_modes_cross_both_numeric_boundaries_without_mutation(self):
        for mode in ('paired', 'independent'):
            with self.subTest(mode=mode):
                action = paired.paired_action(dual_arm_progression=mode)
                original = deepcopy(action)
                self.assertEqual(validate_dual_arm_progression(action, capabilities()), mode)
                validate_action(action, paired.IDENTITY, 'observed', capabilities())
                self.assertEqual(validate_native(action)[0], 'both')
                self.assertEqual(action, original)

    def test_unequal_routes_allow_only_contiguous_prefixes_within_eight_targets(self):
        for shorter in ('left', 'right'):
            action = paired.paired_action(8, dual_arm_progression='independent')
            for point in action['path']['waypoints'][1:]:
                point.pop(shorter)
            validate_action(action, paired.IDENTITY, 'observed', capabilities())
            self.assertEqual(validate_native(action)[0], 'both')
            resumed = deepcopy(action)
            resumed['path']['waypoints'][-1][shorter] = deepcopy(action['path']['waypoints'][0][shorter])
            late_start = deepcopy(action)
            late_start['path']['waypoints'][0].pop(shorter)
            for malformed in (resumed, late_start):
                for boundary in (validate_native,
                        lambda value: validate_action(value, paired.IDENTITY, 'observed', capabilities())):
                    with self.assertRaisesRegex(ValueError, 'dual_arm_progression_requires_dual_arm_motion'):
                        boundary(malformed)

    def test_explicit_paired_and_omitted_mode_dispatch_identical_barrier_rows(self):
        recorded = []
        for selected in ({}, {'dual_arm_progression': 'paired'}):
            adapter, _, rows = dual_adapter()
            result = adapter.execute(dual_action(pairs=((.125, .425), (.55, .55)), **selected),
                                     lambda event: None, threading.Event())
            self.assertEqual((result['status'], result['dual_arm_progression']), ('completed', 'paired'))
            self.assertEqual((result['executed_native_steps'], result['executed_control_steps']), (18, 180))
            recorded.append(native_rows(rows))
        np.testing.assert_array_equal(*recorded)

    def test_unknown_or_wrong_type_mode_is_rejected_at_each_boundary(self):
        for mode in (None, True, False, 0, 1, [], {}, '', 'Independent', 'parallel', 'paired '):
            action = paired.paired_action(dual_arm_progression=mode)
            for boundary in (validate_dual_arm_progression, validate_native,
                             lambda value: validate_action(value, paired.IDENTITY, 'observed', capabilities())):
                with self.subTest(mode=mode, boundary=boundary), self.assertRaisesRegex(
                        ValueError, 'invalid_dual_arm_progression'):
                    boundary(action)

    def test_explicit_mode_requires_advertised_support_including_paired(self):
        bad_support = (None, {}, {'available': False, 'modes': ['paired', 'independent']},
                       {'available': True, 'modes': []})
        for mode in ('paired', 'independent'):
            action = paired.paired_action(dual_arm_progression=mode)
            for support in bad_support:
                caps = dict(capabilities(), dual_arm_progression=support)
                with self.subTest(mode=mode, support=support), self.assertRaisesRegex(
                        ValueError, 'unsupported_capability:dual_arm_progression'):
                    validate_action(action, paired.IDENTITY, 'observed', caps)
            with self.assertRaisesRegex(ValueError, 'unsupported_capability:dual_arm_progression'):
                validate_dual_arm_progression(action, dict(capabilities(), dual_arm=False))
        only_paired = dict(capabilities(), dual_arm_progression=dict(available=True, modes=['paired']))
        validate_dual_arm_progression(paired.paired_action(dual_arm_progression='paired'), only_paired)
        with self.assertRaisesRegex(ValueError, 'unsupported_capability:dual_arm_progression'):
            validate_dual_arm_progression(paired.paired_action(dual_arm_progression='independent'), only_paired)

    def test_flag_rejected_on_single_hold_wait_mixed_and_policy_actions(self):
        for mode in ('paired', 'independent'):
            for kind in ('single', 'hold', 'wait', 'mixed', 'prefix'):
                action = paired.paired_action(dual_arm_progression=mode)
                if kind == 'single':
                    action['path']['waypoints'] = [{'left': p['left']} for p in action['path']['waypoints']]
                elif kind in ('hold', 'wait'):
                    action['path'].update(kind='hold', waypoints=[])
                    action['gripper_events'] = [paired.event('left')]
                    if kind == 'wait':
                        action['hold_native_steps'] = 3
                elif kind == 'mixed':
                    action['path']['waypoints'].append(deepcopy(action['path']['waypoints'][-1]))
                    action['path']['waypoints'][1].pop('right')
                else:
                    action.update(type='JointPrefixAction', actions=[[0.] * 14], policy_provenance={'fixture': True})
                for boundary in (validate_dual_arm_progression, validate_native,
                                 lambda value: validate_action(value, paired.IDENTITY, 'observed', capabilities())):
                    with self.subTest(mode=mode, kind=kind, boundary=boundary), self.assertRaisesRegex(
                            ValueError, 'dual_arm_progression_requires_dual_arm_motion'):
                        boundary(action)
                if kind == 'prefix':
                    with self.assertRaisesRegex(ValueError, 'dual_arm_progression_requires_dual_arm_motion'):
                        validate_joint_prefix(action)

    def test_dual_preview_cannot_reach_backend_rpc(self):
        backend = object.__new__(RobotBackend)
        backend.operation_lock = threading.Lock()
        backend._request = Mock(side_effect=AssertionError('No preview RPC is allowed'))
        for mode in ('paired', 'independent'):
            action = paired.paired_action(1, dual_arm_progression=mode)
            for boundary in (validate_preview_action, backend.preview_motion):
                with self.subTest(mode=mode, boundary=boundary), self.assertRaisesRegex(
                        ValueError, 'dual_arm_motion_preview_unsupported'):
                    boundary(action)
        backend._request.assert_not_called()

    def test_native_capabilities_and_feedback_rejection_do_not_plan_or_step(self):
        adapter, planners, rows = dual_adapter()
        self.assertEqual(adapter.capabilities()['dual_arm_progression'], capabilities()['dual_arm_progression'])
        adapter.motion_execution_mode = 'feedback'
        caps = adapter.capabilities()
        self.assertFalse(caps['dual_arm'])
        self.assertNotIn('dual_arm_progression', caps)
        for mode in ('paired', 'independent'):
            result = adapter.execute(dual_action(dual_arm_progression=mode), lambda event: None, threading.Event())
            self.assertEqual((result['status'], result['reason']), ('rejected', 'feedback_dual_arm_unsupported'))
            self.assertFalse(result['physics_advanced'])
            for key in ('executed_steps', 'executed_native_steps', 'completed_native_steps', 'executed_control_steps'):
                self.assertEqual(result[key], 0)
        self.assertEqual(rows, [])
        self.assertTrue(all(not planner.pose_calls for planner in planners.values()))


class IndependentProgressionHarnessTests(unittest.TestCase):
    def setUp(self):
        paired.DualArmHarnessTests.setUp(self)
        self.h.capabilities.update(capabilities())

    def test_shared_waypoint_speeds_project_only_actual_arm_prefix(self):
        action = paired.paired_action(dual_arm_progression='independent',
                                      waypoint_speed_presets=[None, 'slow'], speed_preset='fast')
        action['path']['waypoints'][1].pop('right')
        resolved = resolve_action_speed(action)
        self.assertEqual(resolved['arm_options']['left']['waypoint_speed_scales'], [1.25, .8])
        self.assertEqual(resolved['arm_options']['right']['waypoint_speed_scales'], [1.25])
        validate_action(resolved, paired.IDENTITY, 'observed', capabilities())

    def test_wait_and_policy_reject_flag_before_backend(self):
        with self.assertRaisesRegex(ValueError, 'dual_arm_progression_requires_dual_arm_motion'):
            self.h._action_from_args('robot_wait', {'dual_arm_progression': 'independent'})
        self.h.action_policy = Mock()
        with self.assertRaisesRegex(ValueError, 'dual_arm_progression_requires_dual_arm_motion'):
            self.h.policy_step({'dual_arm_progression': 'independent'}, {})
        self.h.action_policy.infer.assert_not_called()
        self.assertEqual((self.robot.executions, self.h.action_count), (0, 0))


if __name__ == '__main__':
    unittest.main()
