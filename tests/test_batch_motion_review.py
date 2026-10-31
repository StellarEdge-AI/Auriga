"""Independent regressions for measured-state checkpoint continuation."""
from copy import deepcopy
import threading
import unittest
from unittest.mock import patch

import numpy as np

from test_batch_motion import batch, jaws, move
from test_joint_replan_runtime import motion_action, periodic_pose
from test_motion_preview_backend import preview_adapter


class BatchMotionReviewTests(unittest.TestCase):
    def test_normal_jaw_checkpoint_repeats_cached_goal_when_measurement_is_blocked(self):
        adapter, _, dispatched = preview_adapter()
        jaws(adapter)
        original = adapter.state

        def measured_state():
            value = original()
            value['arms']['right']['gripper']['position_m'] = [.5]
            return value

        adapter.state = measured_state
        close = motion_action('right')
        close.update(path=dict(kind='hold', waypoints=[]),
            gripper_events=[dict(arm='right', phase='after_motion', goal_normalized=0.)])
        action = batch([move('left', angle=-.04),
            dict(id='look', arm='left', kind='observe', reason='inspect grasp'),
            dict(id='jaw', arm='right', kind='gripper', action=close)])
        plan = adapter.preview_batch(action)
        self.assertEqual(plan['status'], 'planned')
        rows = deepcopy(adapter._batch_plans[plan['plan_id']]['rows'])
        self.assertEqual(len(rows), 2)
        request = dict(action, batch_plan_id=plan['plan_id'])
        with (patch.object(adapter, '_plan_segment', side_effect=AssertionError('must not replan')),
                patch.object(adapter, '_resample_action_plan', side_effect=AssertionError('must not resample'))):
            first = adapter.execute(request, lambda _: None, threading.Event())
            self.assertEqual((first['status'], first['reason']), ('stopped', 'observation_checkpoint'))
            self.assertEqual(first['batch_cursor'], 1)
            second = adapter.execute(request, lambda _: None, threading.Event())
        self.assertEqual(second['status'], 'completed')
        self.assertEqual(second['resume_validation']['status'], 'valid')
        self.assertEqual(second['executed_native_steps'], 1)
        actual = [row['left_arm_joint_state'] + row['left_ee_joint_state'] +
                  row['right_arm_joint_state'] + row['right_ee_joint_state'] for row in dispatched]
        np.testing.assert_array_equal(actual, rows)
        self.assertEqual(adapter.state()['arms']['right']['gripper']['position_m'], [.5])
        self.assertEqual(dispatched[-1]['right_ee_joint_state'], [0.])

    def test_zero_cursor_checkpoint_rechecks_first_step_from_measured_joints(self):
        adapter, _, dispatched = preview_adapter()
        action = batch([dict(id='look', kind='observe', reason='inspect before movement'),
                        move('left', angle=-.05)])
        plan = adapter.preview_batch(action)
        request = dict(action, batch_plan_id=plan['plan_id'])
        first = adapter.execute(request, lambda _: None, threading.Event())
        self.assertEqual((first['status'], first['reason']), ('stopped', 'observation_checkpoint'))
        self.assertEqual(first['batch_cursor'], 0)
        original = adapter.state

        def drifted_state():
            value = original()
            left = value['arms']['left']
            left['q_rad'][0] += .0015
            left['ee_pose'].update(periodic_pose(left['q_rad']))
            return value

        adapter.state = drifted_state
        second = adapter.execute(request, lambda _: None, threading.Event())
        self.assertEqual(second['resume_validation']['status'], 'valid')
        self.assertEqual(second['reason'], 'batch_resume_first_joint_step_exceeds_limit')
        self.assertEqual(second['executed_native_steps'], 0)
        self.assertEqual(dispatched, [])

    def test_during_motion_move_retains_closed_command_when_object_blocks_jaws(self):
        adapter, _, dispatched = preview_adapter()
        jaws(adapter)
        adapter.last_dispatched_goals['left'] = 0.
        original = adapter.state

        def blocked_state():
            value = original()
            value['arms']['left']['gripper']['position_m'] = [.48]
            return value

        adapter.state = blocked_state
        carry = move('carry_and_release', angle=-.2)
        carry['action'].update(gripper_events=[dict(
            arm='left', phase='during_motion', goal_normalized=1., start_fraction=.5, end_fraction=1.)])
        action = batch([carry])
        plan = adapter.preview_batch(action)
        self.assertEqual(plan['status'], 'planned')
        result = adapter.execute(dict(action, batch_plan_id=plan['plan_id']), lambda _: None, threading.Event())
        self.assertEqual((result['status'], result['reason']), ('completed', None))
        jaw_goals = [row['left_ee_joint_state'][0] for row in dispatched]
        self.assertEqual(jaw_goals[0], 0.)
        self.assertEqual(jaw_goals[-1], 1.)

    def test_opening_jaw_advances_across_preceding_move_before_slow_close(self):
        adapter, _, dispatched = preview_adapter()
        jaws(adapter)
        adapter.last_dispatched_goals['left'] = 1.
        original = adapter.state

        def opening_state():
            value = original()
            value['arms']['left']['gripper']['position_m'] = [.998 if adapter.env.take_action_cnt[0] else .9075]
            return value

        adapter.state = opening_state
        close = motion_action('left')
        close.update(path=dict(kind='hold', waypoints=[]), gripper_speed_scale=.5,
            gripper_events=[dict(arm='left', phase='after_motion', goal_normalized=0.)])
        action = batch([move('approach', angle=-.125),
                        dict(id='close', arm='left', kind='gripper', action=close)])
        plan = adapter.preview_batch(action)
        self.assertEqual(plan['status'], 'planned')
        result = adapter.execute(dict(action, batch_plan_id=plan['plan_id']), lambda _: None, threading.Event())
        self.assertEqual((result['status'], result['reason']), ('completed', None))
        self.assertAlmostEqual(dispatched[3]['left_ee_joint_state'][0], 1. - 1. / 12)
        self.assertEqual(dispatched[-1]['left_ee_joint_state'], [0.])

    def test_checkpoint_resume_tolerates_stationary_tracking_lag_and_blocked_jaw(self):
        adapter, _, dispatched = preview_adapter()
        jaws(adapter)
        adapter.last_dispatched_goals['left'] = 0.
        original = adapter.state

        def lagged_state():
            value = original()
            left = value['arms']['left']
            if adapter.env.take_action_cnt[0]:
                left['q_rad'][0] += .003
                left['ee_pose'].update(periodic_pose(left['q_rad']))
            left['gripper']['position_m'] = [.1852]
            return value

        adapter.state = lagged_state
        release = move('release', angle=-.25)
        release['action'].update(gripper_speed_scale=.8, gripper_events=[dict(
            arm='left', phase='during_motion', goal_normalized=1., start_fraction=0., end_fraction=1.)])
        action = batch([move('one', angle=-.125),
                        dict(id='look', kind='observe', reason='check before release'),
                        release])
        plan = adapter.preview_batch(action)
        request = dict(action, batch_plan_id=plan['plan_id'])
        first = adapter.execute(request, lambda _: None, threading.Event())
        self.assertEqual((first['status'], first['reason']), ('stopped', 'observation_checkpoint'))
        second = adapter.execute(request, lambda _: None, threading.Event())
        self.assertEqual((second['status'], second['reason']), ('completed', None))
        self.assertEqual(len(dispatched), plan['planned_native_steps'])

    def test_segment_start_admits_joint_error_within_orientation_bound(self):
        adapter, _, _ = preview_adapter()
        lag = [0.]
        original = adapter.state

        def lagged_state():
            value = original()
            if adapter.env.take_action_cnt[0]:
                value['arms']['left']['q_rad'][5] += lag[0]
            return value

        adapter.state = lagged_state
        close = motion_action('left')
        close.update(path=dict(kind='hold', waypoints=[]),
            gripper_events=[dict(arm='left', phase='after_motion', goal_normalized=0.)])
        action = batch([move('approach', angle=-.125),
                        dict(id='close', arm='left', kind='gripper', action=close)])
        plan = adapter.preview_batch(action)
        lag[0] = .075
        completed = adapter.execute(dict(action, batch_plan_id=plan['plan_id']), lambda _: None, threading.Event())
        self.assertEqual((completed['status'], completed['reason']), ('completed', None))


if __name__ == '__main__':
    unittest.main()
