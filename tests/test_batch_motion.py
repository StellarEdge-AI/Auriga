"""Whole-batch planning and exact execution through the real native CPU loop."""
from copy import deepcopy
import io
import threading
import unittest
from unittest.mock import patch

import numpy as np

from auriga.robot.motion.batch_contract import validate_batch_action
from auriga.robot.motion.batch_motion import consume_batch
from test_motion_preview_backend import preview_adapter
from test_joint_replan_runtime import motion_action


def move(identifier, arm='left', angle=-.125, **values):
    return dict(id=identifier, kind='move', arm=arm, action=motion_action(arm, (angle,)), **values)


def batch(stages, **values):
    own = motion_action()
    return {key: value for key,value in own.items() if key not in ('path', 'gripper_events')} | dict(type='BatchAction', stages=stages, **values)


def jaw(identifier='close', arm='left', goal=0., **values):
    own = motion_action(arm)
    own.update(path=dict(kind='hold', waypoints=[]),
        gripper_events=[dict(arm=arm, phase='after_motion', goal_normalized=goal)])
    return dict(id=identifier, kind='gripper', arm=arm, action=own, **values)


def jaws(adapter):
    robot = adapter.robot.robot_list[0]
    robot.gripper_joints_name = ['jaw']
    robot.gripper_move = dict(base='jaw', sign=1)
    robot.gripper_scale = [0., 1.]
    original = adapter.state
    def state():
        result = original()
        for arm in ('left', 'right'):
            result['arms'][arm]['gripper'] = dict(position_source_kind='measurement',
                position_m=[adapter.last_dispatched_goals[arm]], joint_names=['jaw'])
        return result
    adapter.state = state


class BatchMotionTests(unittest.TestCase):
    def execute(self, adapter, action, plan):
        return adapter.execute(dict(action, batch_plan_id=plan['plan_id']), lambda update: None, threading.Event())

    def test_full_multistage_preview_uses_planned_endpoints_without_dispatch(self):
        adapter, planner, dispatched = preview_adapter()
        action = batch([move('first'), move('second', angle=-.25)])
        before = deepcopy(adapter.state())
        plan = adapter.preview_batch(action)
        self.assertEqual(plan['status'], 'planned')
        self.assertEqual(adapter.state(), before)
        self.assertEqual(dispatched, [])
        self.assertEqual(adapter.env.take_action_cnt[0], 0)
        self.assertAlmostEqual(planner.pose_calls[1][0][0], -.125)
        self.assertEqual(plan['planned_native_steps'], 6)
        rows = deepcopy(adapter._batch_plans[plan['plan_id']]['rows'])
        with (patch.object(adapter, '_plan_segment', side_effect=AssertionError('execution must not plan')),
                patch.object(adapter, '_resample_action_plan', side_effect=AssertionError('execution must not resample'))):
            result = self.execute(adapter, action, plan)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['executed_native_steps'], 6)
        for row, actual in zip(rows, dispatched):
            np.testing.assert_array_equal(row[:6], actual['left_arm_joint_state'])
            np.testing.assert_array_equal(row[7:13], actual['right_arm_joint_state'])

    def test_unequal_dual_routes_overlap_without_padding(self):
        adapter, _, dispatched = preview_adapter()
        action = batch([move('l1'), move('l2', angle=-.25), move('r1', 'right', -.375)])
        plan = adapter.preview_batch(action)
        self.assertEqual(plan['status'], 'planned')
        self.assertEqual(plan['planned_native_steps'], 8)
        self.assertEqual(plan['stage_costs']['l2']['start_native_step'], 3)
        self.assertEqual(plan['stage_costs']['r1']['start_native_step'], 0)
        result = self.execute(adapter, action, plan)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(dispatched), 8)

    def test_dependency_delays_peer_and_cycles_reject_before_planning(self):
        adapter, planner, _ = preview_adapter()
        action = batch([move('left'), move('right', 'right', depends_on=['left'])])
        plan = adapter.preview_batch(action)
        self.assertEqual(plan['planned_native_steps'], 6)
        self.assertEqual(plan['stage_costs']['right']['start_native_step'], 3)
        action['stages'][0]['depends_on'] = ['right']
        with self.assertRaisesRegex(ValueError, 'dependency_cycle'):
            adapter.preview_batch(action)
        self.assertEqual(len(planner.pose_calls), 2)

    def test_late_planning_failure_executes_nothing(self):
        adapter, _, dispatched = preview_adapter(fail_pose_at=2)
        plan = adapter.preview_batch(batch([move('one'), move('two', angle=-.25)]))
        self.assertEqual(plan['reason'], 'path_infeasible')
        self.assertEqual(plan['failed_stage_id'], 'two')
        self.assertEqual(dispatched, [])

    def test_checkpoint_stops_peer_midstage_and_resumes_exact_suffix(self):
        adapter, planner, dispatched = preview_adapter()
        action = batch([move('l1'), dict(id='look', arm='left', kind='observe', reason='check grasp'),
            move('l2', angle=-.25), move('r1', 'right', -.5)])
        plan = adapter.preview_batch(action)
        rows = deepcopy(adapter._batch_plans[plan['plan_id']]['rows'])
        first = self.execute(adapter, action, plan)
        self.assertEqual((first['status'], first['reason']), ('stopped', 'observation_checkpoint'))
        self.assertEqual(first['batch_cursor'], 3)
        self.assertIn('r1', first['pending_stage_ids'])
        self.assertEqual(first['stage_results']['r1']['completed_steps'], 3)
        second = self.execute(adapter, action, plan)
        self.assertEqual(second['status'], 'completed')
        self.assertEqual(len(planner.pose_calls), 3)
        self.assertEqual(len(dispatched), len(rows))
        self.assertEqual(first['executed_native_steps']+second['executed_native_steps'], len(rows))

    def test_global_checkpoint_is_barrier_after_both_prior_stages(self):
        adapter, _, _ = preview_adapter()
        action = batch([move('l1'), move('r1', 'right', -.5), dict(id='look', kind='observe', reason='both arrived'),
            move('l2', angle=-.25)])
        plan = adapter.preview_batch(action)
        result = self.execute(adapter, action, plan)
        self.assertEqual(result['batch_cursor'], 10)
        self.assertEqual(set(result['completed_stage_ids']), {'l1', 'r1', 'look'})
        self.assertEqual(self.execute(adapter, action, plan)['status'], 'completed')

    def test_zero_step_checkpoint_revision_rejects_stale_approval(self):
        adapter, planner, dispatched = preview_adapter()
        action = batch([dict(id='look', kind='observe', reason='check before motion'),
            move('reach', angle=-.04)])
        plan = adapter.preview_batch(action)
        approved = dict(action, batch_plan_id=plan['plan_id'],
            batch_expected_cursor=0, batch_expected_revision=0)
        first = adapter.execute(approved, lambda update: None, threading.Event())
        self.assertEqual((first['status'], first['reason']), ('stopped', 'observation_checkpoint'))
        self.assertEqual((first['batch_cursor'], first['batch_revision']), (0, 1))
        self.assertEqual(first['executed_native_steps'], 0)
        self.assertEqual(dispatched, [])
        stale = adapter.execute(approved, lambda update: None, threading.Event())
        self.assertEqual(stale['status'], 'rejected')
        self.assertEqual(stale['reason'], 'batch_plan_revision_changed_since_review')
        self.assertEqual(dispatched, [])
        fresh = adapter.execute(dict(approved, batch_expected_revision=1),
            lambda update: None, threading.Event())
        self.assertEqual(fresh['status'], 'completed')
        self.assertEqual(fresh['batch_revision'], 2)
        self.assertEqual(fresh['executed_native_steps'], plan['planned_native_steps'])
        self.assertEqual(len(planner.pose_calls), 1)

    def test_changed_first_start_rejects_without_replanning(self):
        adapter, planner, dispatched = preview_adapter()
        action = batch([move('one')]); plan = adapter.preview_batch(action)
        adapter.control_step += 1
        result = self.execute(adapter, action, plan)
        self.assertEqual(result['status'], 'rejected')
        self.assertIn('stale_state', result['reason'])
        self.assertEqual(len(planner.pose_calls), 1)
        self.assertEqual(dispatched, [])

    def test_checkpoint_revalidates_unchanged_robot_after_clock_change(self):
        adapter, _, _ = preview_adapter()
        action = batch([move('one'), dict(id='look', kind='observe', reason='check'), move('two', angle=-.25)])
        plan = adapter.preview_batch(action)
        self.execute(adapter, action, plan)
        adapter.control_step += 1
        result = self.execute(adapter, action, plan)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['resume_validation']['status'], 'valid')

    def test_checkpoint_drift_invalidates_suffix(self):
        adapter, _, dispatched = preview_adapter()
        action = batch([move('one'), dict(id='look', kind='observe', reason='check'), move('two', angle=-.25)])
        plan = adapter.preview_batch(action)
        self.execute(adapter, action, plan)
        original = adapter.state
        def drift():
            value = original(); value['arms']['left']['q_rad'][0] += .2
            return value
        adapter.state = drift
        result = self.execute(adapter, action, plan)
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(len(dispatched), 3)

    def test_shared_limit_rejects_complete_batch_including_checkpoint_suffix(self):
        adapter, _, dispatched = preview_adapter()
        stages = [move('first', angle=-3), dict(id='look', kind='observe', reason='check')]
        stages += [move('m'+str(i), angle=(3 if i%2 == 0 else -3)) for i in range(3)]
        plan = adapter.preview_batch(batch(stages))
        self.assertEqual(plan['reason'], 'motion_duration_limit')
        self.assertEqual(dispatched, [])

    def test_slow_pure_jaw_and_during_motion_jaw_compile_and_execute(self):
        adapter, _, dispatched = preview_adapter(); jaws(adapter)
        close = motion_action()
        close.update(path=dict(kind='hold', waypoints=[]), gripper_speed_scale=.8,
            gripper_events=[dict(arm='left', phase='after_motion', goal_normalized=.5)])
        movement = move('reach', angle=-.3)
        movement['action'].update(gripper_speed_scale=.8, gripper_events=[dict(arm='left', phase='during_motion',
            goal_normalized=0., start_fraction=.2, end_fraction=1.)])
        action = batch([dict(id='close', arm='left', kind='gripper', action=close), movement])
        plan = adapter.preview_batch(action)
        self.assertEqual(plan['status'], 'planned')
        self.assertEqual(plan['checkpoints'], [])
        result = self.execute(adapter, action, plan)
        self.assertEqual(result['status'], 'completed')
        values = [1.]+[row['left_ee_joint_state'][0] for row in dispatched]
        self.assertLessEqual(max(abs(b-a) for a,b in zip(values,values[1:])), .18*.8+1e-12)
        self.assertEqual(values[-1], 0.)

    def test_jaw_change_pauses_dependent_motion_then_resumes_exact_rows_once(self):
        for arm in ('left', 'right'):
            for initial, goal in ((1., 0.), (0., 1.)):
                with self.subTest(arm=arm, goal=goal):
                    adapter, planner, dispatched = preview_adapter(); jaws(adapter)
                    adapter.last_dispatched_goals[arm] = initial
                    action = batch([jaw(arm=arm, goal=goal, checkpoint_after_gripper=True), move('carry', arm)])
                    plan = adapter.preview_batch(action)
                    rows = deepcopy(adapter._batch_plans[plan['plan_id']]['rows'])
                    boundary = plan['stage_costs']['close']['end_native_step']
                    self.assertEqual(plan['checkpoints'][0]['cursor'], boundary)
                    self.assertEqual(dispatched, [])
                    with (patch.object(adapter, '_plan_segment', side_effect=AssertionError('must not replan')),
                            patch.object(adapter, '_resample_action_plan', side_effect=AssertionError('must not resample'))):
                        first = self.execute(adapter, action, plan)
                        self.assertEqual((first['status'], first['reason']), ('stopped', 'observation_checkpoint'))
                        self.assertEqual(first['stage_results']['carry']['completed_steps'], 0)
                        self.assertIsNotNone(first['terminal_observation'])
                        self.assertEqual(first['checkpoint_stage_ids'], ['close'])
                        second = self.execute(adapter, action, plan)
                    self.assertEqual(second['status'], 'completed')
                    self.assertEqual(first['executed_native_steps'] + second['executed_native_steps'], len(rows))
                    self.assertEqual(second['batch_revision'], 2)
                    actual = [row['left_arm_joint_state'] + row['left_ee_joint_state'] +
                        row['right_arm_joint_state'] + row['right_ee_joint_state'] for row in dispatched]
                    np.testing.assert_array_equal(actual, rows)
                    self.assertEqual(len(planner.pose_calls), 1)

    def test_synchronized_jaw_change_stops_only_at_end_of_its_move_stage(self):
        adapter, _, dispatched = preview_adapter(); jaws(adapter)
        closing = move('close_while_moving', angle=-.3, checkpoint_after_gripper=True)
        closing['action']['gripper_events'] = [dict(arm='left', phase='during_motion',
            goal_normalized=0., start_fraction=0., end_fraction=.5)]
        action = batch([closing, move('carry', angle=-.4)])
        plan = adapter.preview_batch(action)
        first = self.execute(adapter, action, plan)
        self.assertEqual(first['batch_cursor'], plan['stage_costs']['close_while_moving']['end_native_step'])
        self.assertEqual(first['checkpoint_stage_ids'], ['close_while_moving'])
        self.assertEqual(first['stage_results']['carry']['completed_steps'], 0)
        self.assertEqual(self.execute(adapter, action, plan)['status'], 'completed')
        self.assertEqual(len(dispatched), plan['planned_native_steps'])

    def test_jaw_checkpoint_pauses_independent_peer_midstage_without_serializing_routes(self):
        adapter, _, dispatched = preview_adapter(); jaws(adapter)
        action = batch([jaw(checkpoint_after_gripper=True), move('carry'), move('peer', 'right', -.5)])
        plan = adapter.preview_batch(action)
        self.assertEqual(plan['stage_costs']['peer']['start_native_step'], 0)
        first = self.execute(adapter, action, plan)
        self.assertEqual(first['stage_results']['peer']['status'], 'paused')
        self.assertGreater(first['stage_results']['peer']['completed_steps'], 0)
        self.assertLess(first['batch_cursor'], plan['stage_costs']['peer']['end_native_step'])
        self.assertEqual(self.execute(adapter, action, plan)['status'], 'completed')
        self.assertEqual(len(dispatched), plan['planned_native_steps'])

    def test_jaw_checkpoint_respects_cross_arm_dependency_and_merges_explicit_marker(self):
        for explicit in (False, True):
            with self.subTest(explicit=explicit):
                adapter, _, _ = preview_adapter(); jaws(adapter)
                stages = [jaw(checkpoint_after_gripper=True)]
                if explicit:
                    stages.append(dict(id='inspect', kind='observe', arm='left', reason='check object'))
                stages.append(move('peer', 'right', depends_on=['close']))
                action = batch(stages)
                plan = adapter.preview_batch(action)
                self.assertEqual(len(plan['checkpoints']), 1)
                first = self.execute(adapter, action, plan)
                self.assertEqual(first['stage_results']['peer']['completed_steps'], 0)
                self.assertEqual(set(first['checkpoint_stage_ids']), {'inspect'} if explicit else {'close'})
                self.assertEqual(self.execute(adapter, action, plan)['status'], 'completed')

    def test_jaw_checkpoint_is_not_duplicated_by_explicit_same_arm_observe(self):
        adapter, _, _ = preview_adapter(); jaws(adapter)
        action = batch([jaw(checkpoint_after_gripper=True), dict(id='inspect', arm='left', kind='observe', reason='check object'),
            move('carry')])
        plan = adapter.preview_batch(action)
        self.assertEqual(len(plan['checkpoints']), 1)
        self.assertEqual(plan['checkpoints'][0]['stage_ids'], ['inspect'])
        self.assertNotIn('close', plan['checkpoints'][0]['reasons'])
        first = self.execute(adapter, action, plan)
        self.assertEqual(first['checkpoint_stage_ids'], ['inspect'])
        self.assertEqual(first['stage_results']['carry']['completed_steps'], 0)
        self.assertEqual(self.execute(adapter, action, plan)['status'], 'completed')

    def test_adjacent_jaw_changes_only_checkpoint_at_physical_boundary(self):
        adapter, _, _ = preview_adapter(); jaws(adapter)
        adapter.last_dispatched_goals['left'] = 1.
        action = batch([jaw(identifier='open', goal=.5), jaw(identifier='close', goal=0.), move('carry')],
            checkpoint_after_gripper=True)
        plan = adapter.preview_batch(action)
        self.assertEqual(len(plan['checkpoints']), 1)
        self.assertEqual(plan['checkpoints'][0]['stage_ids'], ['close'])
        self.assertEqual(self.execute(adapter, action, plan)['checkpoint_stage_ids'], ['close'])
        self.assertEqual(self.execute(adapter, action, plan)['status'], 'completed')

    def test_no_extra_checkpoint_for_default_final_jaw_unchanged_goal_independent_peer_or_stage_override(self):
        for stages, batch_flag in (
                ([jaw(), move('carry')], False),
                ([jaw()], True),
                ([jaw(goal=1.), move('carry')], True),
                ([jaw(), move('peer', 'right', -.5)], True),
                ([jaw(checkpoint_after_gripper=False), move('carry')], True)):
            with self.subTest(stages=stages, batch_flag=batch_flag):
                adapter, _, _ = preview_adapter(); jaws(adapter)
                action = batch(stages, **({'checkpoint_after_gripper': True} if batch_flag else {}))
                plan = adapter.preview_batch(action)
                self.assertEqual(plan['checkpoints'], [])
                self.assertEqual(self.execute(adapter, action, plan)['status'], 'completed')

    def test_multi_jaw_window_merges_into_single_checkpoint_before_shared_move(self):
        adapter, _, _ = preview_adapter(); jaws(adapter)
        adapter.last_dispatched_goals['left'] = 1.
        adapter.last_dispatched_goals['right'] = 1.
        action = batch([
            jaw(identifier='close_left', arm='left', goal=0.),
            jaw(identifier='close_right', arm='right', goal=.5),
            move('carry_left', arm='left', depends_on=['close_left', 'close_right']),
        ], checkpoint_after_gripper=True)
        plan = adapter.preview_batch(action)
        self.assertEqual(len(plan['checkpoints']), 1)
        self.assertEqual(set(plan['checkpoints'][0]['stage_ids']), {'close_left', 'close_right'})
        first = self.execute(adapter, action, plan)
        self.assertEqual((first['status'], first['reason']), ('stopped', 'observation_checkpoint'))
        self.assertEqual(first['stage_results']['carry_left']['completed_steps'], 0)
        self.assertNotIn('likely empty grasp', first['checkpoint_reasons']['close_left'])
        self.assertEqual(first['stage_results']['close_left']['grip_facts']['measured_aperture_m'], 0.0)
        self.assertEqual(self.execute(adapter, action, plan)['status'], 'completed')

    def test_batch_level_jaw_checkpoint_skips_opening_and_pauses_only_on_closing(self):
        adapter, _, _ = preview_adapter(); jaws(adapter)
        adapter.last_dispatched_goals['left'] = 0.2
        action = batch([
            jaw(identifier='open_left', arm='left', goal=1.0),
            move('reach', arm='left'),
            jaw(identifier='close_left', arm='left', goal=0.0),
            move('lift', arm='left', angle=-.25),
        ], checkpoint_after_gripper=True)
        plan = adapter.preview_batch(action)
        self.assertEqual(len(plan['checkpoints']), 1)
        self.assertEqual(plan['checkpoints'][0]['stage_ids'], ['close_left'])

    def test_jaw_checkpoint_keeps_resume_budget_and_drift_gates(self):
        for failure in ('budget', 'drift', 'obs_mismatch'):
            with self.subTest(failure=failure):
                adapter, _, dispatched = preview_adapter(); jaws(adapter)
                action = batch([jaw(checkpoint_after_gripper=True), move('carry')])
                plan = adapter.preview_batch(action)
                first = self.execute(adapter, action, plan)
                count = len(dispatched)
                self.assertEqual(first['reason'], 'observation_checkpoint')
                if failure == 'budget':
                    adapter.native_action_budget_limit = count
                elif failure == 'obs_mismatch':
                    adapter._batch_plans[plan['plan_id']]['expected_observation_id'] = 'obs-checkpoint'
                else:
                    original = adapter.state
                    def drift():
                        state = original(); state['arms']['left']['q_rad'][0] += .2
                        return state
                    adapter.state = drift
                second = self.execute(adapter, action, plan)
                self.assertEqual(second['reason'], 'batch_remaining_native_budget_insufficient' if failure == 'budget'
                    else 'batch_plan_stale_state_requires_new_preview')
                self.assertEqual(len(dispatched), count)

    def test_fault_during_jaw_keeps_unknown_and_never_reaches_dependent_motion(self):
        adapter, _, dispatched = preview_adapter(); jaws(adapter)
        action = batch([jaw(), move('carry')])
        plan = adapter.preview_batch(action)
        native = adapter.env.take_action
        def fault(command):
            native(command)
            raise RuntimeError('after partial jaw dispatch')
        adapter.env.take_action = fault
        with patch('sys.stderr', io.StringIO()):
            result = self.execute(adapter, action, plan)
        self.assertEqual(result['status'], 'unknown')
        self.assertTrue(result['stage_results']['close']['execution_uncertain'])
        self.assertEqual(result['stage_results']['carry']['completed_steps'], 0)
        self.assertEqual(self.execute(adapter, action, plan)['status'], 'rejected')
        self.assertEqual(len(dispatched), 1)

    def test_cancel_retains_executed_prefix_and_consumes_plan(self):
        adapter, _, dispatched = preview_adapter()
        action=batch([move('long',angle=-.5)]); plan=adapter.preview_batch(action)
        cancel=threading.Event()
        def progress(update):
            if update.get('executed_steps') == 1:
                cancel.set()
        result=adapter.execute(dict(action,batch_plan_id=plan['plan_id']),progress,cancel)
        self.assertEqual(result['reason'],'cancelled')
        self.assertEqual(result['executed_native_steps'],1)
        self.assertEqual(result['batch_cursor'],1)
        self.assertEqual(self.execute(adapter,action,plan)['status'],'rejected')

    def test_cached_commands_cannot_change(self):
        adapter, _, dispatched=preview_adapter()
        action=batch([move('one')]); plan=adapter.preview_batch(action)
        action['stages'][0]['action']['speed_scale']=.8
        result=self.execute(adapter,action,plan)
        self.assertEqual(result['reason'],'batch_plan_commands_changed')
        self.assertEqual(dispatched,[])

    def test_resuming_middle_of_peer_stage_readmits_whole_suffix_budget(self):
        adapter, _, dispatched=preview_adapter()
        adapter.native_action_budget_limit=100
        action=batch([move('one'),dict(id='look',kind='observe',arm='left',reason='check'),move('peer','right',-.5)])
        plan=adapter.preview_batch(action)
        self.execute(adapter,action,plan)
        adapter.native_action_budget_limit=4
        result=self.execute(adapter,action,plan)
        self.assertEqual(result['reason'],'batch_remaining_native_budget_insufficient')
        self.assertEqual(result['executed_native_steps'],0)
        self.assertEqual(len(dispatched),3)

    def test_resume_measured_first_step_is_checked_even_with_valid_pose_drift(self):
        adapter, _, dispatched=preview_adapter()
        action=batch([move('one'),dict(id='look',kind='observe',arm='left',reason='check'),move('peer','right',-.5)])
        plan=adapter.preview_batch(action)
        self.execute(adapter,action,plan)
        old=adapter.state
        def shifted():
            value=old(); value['arms']['right']['q_rad'][0]+=.001
            return value
        adapter.state=shifted
        result=self.execute(adapter,action,plan)
        self.assertEqual(result['reason'],'batch_resume_first_joint_step_exceeds_limit')
        self.assertEqual(result['executed_native_steps'],0)
        self.assertEqual(len(dispatched),3)

    def test_faulting_native_call_retains_billed_vs_completed_uncertainty(self):
        adapter, _, dispatched=preview_adapter()
        action=batch([move('one')]); plan=adapter.preview_batch(action)
        native=adapter.env.take_action
        def fault(command):
            native(command)
            raise RuntimeError('after partial native dispatch')
        adapter.env.take_action=fault
        with patch('sys.stderr',io.StringIO()):
            result=self.execute(adapter,action,plan)
        self.assertEqual(result['status'],'unknown')
        self.assertEqual(result['executed_native_steps'],1)
        self.assertEqual(result['completed_native_steps'],0)
        self.assertEqual(result['batch_cursor'],0)
        self.assertTrue(result['stage_results']['one']['execution_uncertain'])
        self.assertEqual(len(dispatched),1)


if __name__ == '__main__':
    unittest.main()
