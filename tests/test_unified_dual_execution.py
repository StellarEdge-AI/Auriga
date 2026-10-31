"""Public batch compilation reaches simultaneous native dispatch for both arms.

The two CPU robot/planner models have independent state and distinct identities.
Only planning and physical following are stand-ins; request conversion, batch
scheduling, speed sampling, admission, tracking and native dispatch are real.
"""
from copy import deepcopy
from pathlib import Path
import threading
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from auriga.robot.motion.batch_contract import validate_batch_action
from auriga.runtime.harness import Harness
from auriga.robot.backends.robodojo.robot_worker import RoboDojoAdapter
from auriga.runtime.actions.unified_actions import compile_request


class _JointLimits:
    def __getitem__(self, index):
        return self

    def cpu(self):
        return self

    def numpy(self):
        return np.array([[-10., 10.]] * 6)


class _Planner:
    def __init__(self, side):
        self.side = side
        self.calls = []
        self.motion_planner = SimpleNamespace()

    def plan_path(self, start, target, origin):
        if origin != self.side:
            raise AssertionError('planner received the other arm base')
        self.calls.append(dict(start=start.tolist(), target=list(target)))
        goal = np.array(start, copy=True)
        goal[0] = target[0]
        return dict(status='Success', position=np.stack([start, goal]))


class _DualRuntime:
    def __init__(self):
        adapter = self.adapter = object.__new__(RoboDojoAdapter)
        adapter.np = np
        adapter.control_hz, adapter.native_step_ticks = 25, 10
        adapter.control_step = adapter.evaluator_actions = 0
        adapter.versions = {}
        # Geometry is intentionally unavailable; this test uses no native assets.
        adapter.root = Path('/nonexistent/unified-dual-cpu-fixture')
        adapter.observation_profile = 'rgb_proprio_calibrated_v1'
        adapter.motion_execution_mode = 'resampled'
        adapter.tracking_guard_enabled = adapter.motion_safety_guard_enabled = True
        adapter.last_dispatched_goals = dict(left=1., right=1.)
        adapter.goals = dict(adapter.last_dispatched_goals)
        adapter.arm_step_limits = {side: np.full(6, .05) for side in ('left', 'right')}
        adapter.arm_joint_limits = {}
        adapter.eef_gripper_dwell_step, adapter.gripper_step_limit = .18, .25
        self.initial = dict(left=[0., .2, 0., 0., 0., 0.], right=[0., -.4, 0., 0., 0., 0.])
        self.measured = deepcopy(self.initial)
        self.jaws = dict(adapter.last_dispatched_goals)
        self.robots = {side: SimpleNamespace(arm_name=side+'_arm', robot_name=side, type='target',
            arm_joints_name=['joint'+str(i) for i in range(6)], arm_joint_indices=list(range(6)),
            gripper_joints_name=['jaw'], gripper_move=dict(base='jaw', sign=1),
            gripper_scale=[0., 1.], entity_origin_pose=side) for side in self.initial}
        self.planners = {side: _Planner(side) for side in self.initial}
        adapter.robot = SimpleNamespace(robot_list=list(self.robots.values()), planner=self.planners,
            robot_key=[SimpleNamespace(data=SimpleNamespace(soft_joint_pos_limits=_JointLimits()))
                for _ in self.initial],
            get_robot_by_arm_name=lambda name: self.robots[name.split('_')[0]],
            get_joint=lambda robot, env_ids: [np.array(self.measured[robot.robot_name])])
        adapter.arm_joint_names = {side: list(robot.arm_joints_name) for side, robot in self.robots.items()}
        adapter.state = self.state
        adapter.observe = lambda: dict(robot_state=self.state(), sim_step=adapter.control_step)
        adapter.env = SimpleNamespace(end_flag=[False], success=[False], take_action_cnt=[0], step_lim=1000, dt=.004,
            take_action=self.take_action)
        self.commands = []
        self.harness = SimpleNamespace(identity=dict(run_id='dual-runtime', episode_id='episode',
            env_instance_id='env', env_generation=1), capabilities=adapter.capabilities(),
            config=SimpleNamespace(features=SimpleNamespace(default_motion_speed_scale=1.)))
        self.harness._action_from_args = MethodType(Harness._action_from_args, self.harness)

    def state(self):
        return dict(arms={side: dict(q_rad=list(q), joint_names=list(self.robots[side].arm_joints_name),
            ee_pose=dict(position_m=[q[0], 0., 1.], quaternion_xyzw=[0., 0., 0., 1.]),
            gripper=dict(position_source_kind='measurement', position_m=[self.jaws[side]]))
            for side, q in self.measured.items()})

    def take_action(self, command):
        self.commands.append(deepcopy(command))
        # A native call receives both targets. The simulated follower updates
        # both measurements together, without a second dispatch or planner.
        for side in self.measured:
            self.measured[side] = list(command[side+'_arm_joint_state'])
            self.jaws[side] = float(command[side+'_ee_joint_state'][0])
        self.adapter.control_step += self.adapter.native_step_ticks
        self.adapter.env.take_action_cnt[0] += 1

    def preview(self, stages):
        request = dict(action_id='dual', observation_id='obs', stages=stages)
        original = deepcopy(request)
        action = compile_request(self.harness, request)
        if request != original:
            raise AssertionError('compilation mutated the public request')
        plan = self.adapter.preview_batch(action)
        if plan['status'] != 'planned':
            raise AssertionError(plan)
        return action, plan

    def execute(self, action, plan):
        return self.adapter.execute(dict(action, batch_plan_id=plan['plan_id']), lambda _: None, threading.Event())

    def simultaneous_rows(self):
        previous = deepcopy(self.initial)
        count = 0
        for command in self.commands:
            current = {side: command[side+'_arm_joint_state'] for side in previous}
            count += all(not np.array_equal(current[side], previous[side]) for side in previous)
            previous = current
        return count


def _move(identifier, arm, target, **options):
    return dict(id=identifier, kind='move', arm=arm, position_m=[target, 0., 1.],
        quaternion_xyzw=[0., 0., 0., 1.], **options)


class UnifiedDualExecutionTests(unittest.TestCase):
    def setUp(self):
        self.runtime = _DualRuntime()

    def complete(self, action, plan):
        receipt = self.runtime.execute(action, plan)
        self.assertEqual((receipt['status'], receipt['reason']), ('completed', None))
        self.assertEqual(receipt['executed_native_steps'], len(self.runtime.commands))
        return receipt

    def test_independent_arms_share_rows_through_public_conversion_and_native_dispatch(self):
        runtime = self.runtime
        action, plan = runtime.preview([_move('left', 'left', .125), _move('right', 'right', -.375)])
        self.assertEqual(runtime.commands, [])
        self.assertEqual(validate_batch_action(action), {'left': set(), 'right': set()})
        self.assertEqual([plan['stage_costs'][side]['start_native_step'] for side in ('left', 'right')], [0, 0])
        with (patch.object(runtime.adapter, '_plan_segment', side_effect=AssertionError('execution replanned')),
                patch.object(runtime.adapter, '_resample_action_plan', side_effect=AssertionError('execution resampled'))):
            self.complete(action, plan)
        self.assertEqual(len(runtime.commands), 8)
        self.assertEqual(runtime.simultaneous_rows(), 3)
        first = runtime.commands[0]
        np.testing.assert_allclose(first['left_arm_joint_state'], [.125/3, .2, 0., 0., 0., 0.])
        np.testing.assert_allclose(first['right_arm_joint_state'], [-.375/8, -.4, 0., 0., 0., 0.])

    def test_grouping_all_left_stages_before_right_stages_does_not_serialize_arms(self):
        runtime = self.runtime
        action, plan = runtime.preview([_move('l1', 'left', .125), _move('l2', 'left', .25),
            _move('r1', 'right', -.375), _move('r2', 'right', -.5)])
        self.complete(action, plan)
        self.assertEqual(len(runtime.commands), 11)
        self.assertEqual(runtime.simultaneous_rows(), 6)
        self.assertEqual([call['start'][0] for call in runtime.planners['left'].calls], [0., .125])
        self.assertEqual([call['start'][0] for call in runtime.planners['right'].calls], [0., -.375])
        self.assertEqual([plan['stage_costs'][key]['start_native_step'] for key in ('l1', 'l2', 'r1', 'r2')],
            [0, 3, 0, 8])

    def test_after_motion_jaw_expansion_keeps_peer_motion_running(self):
        runtime = self.runtime
        action, plan = runtime.preview([_move('l1', 'left', .125, gripper_goal_normalized=0.),
            _move('r1', 'right', -.375, gripper_goal_normalized=0.)])
        self.complete(action, plan)
        self.assertEqual(len(runtime.commands), 14)
        self.assertEqual(runtime.simultaneous_rows(), 3)
        self.assertEqual(plan['stage_costs']['l1']['start_native_step'], 3)
        self.assertEqual(plan['stage_costs']['r1']['start_native_step'], 8)
        # Left jaw begins closing while right motion continues toward its target.
        self.assertEqual(runtime.commands[3]['left_ee_joint_state'], [0.])
        self.assertNotEqual(runtime.commands[3]['right_arm_joint_state'], runtime.commands[2]['right_arm_joint_state'])
        self.assertNotEqual(runtime.commands[3]['right_arm_joint_state'][0], -.375)

    def test_explicit_cross_arm_dependency_serializes_the_dependent_stage(self):
        runtime = self.runtime
        action, plan = runtime.preview([_move('left', 'left', .125),
            _move('right', 'right', -.375, depends_on=['left'])])
        self.complete(action, plan)
        self.assertEqual(len(runtime.commands), 11)
        self.assertEqual(runtime.simultaneous_rows(), 0)
        self.assertEqual(plan['stage_costs']['right']['start_native_step'], 3)

    def test_global_observe_between_arms_is_a_cross_arm_barrier(self):
        runtime = self.runtime
        action, plan = runtime.preview([_move('left', 'left', .125),
            dict(id='look', kind='observe', reason='inspect before right'), _move('right', 'right', -.375)])
        first = runtime.execute(action, plan)
        self.assertEqual((first['status'], first['reason'], first['batch_cursor']),
            ('stopped', 'observation_checkpoint', 3))
        self.assertEqual(runtime.measured['right'], runtime.initial['right'])
        second = runtime.execute(action, plan)
        self.assertEqual((second['status'], second['executed_native_steps']), ('completed', 8))
        self.assertEqual(len(runtime.commands), 11)
        self.assertEqual(runtime.simultaneous_rows(), 0)

    def test_arm_observe_stops_both_then_resumes_exact_parallel_suffix(self):
        runtime = self.runtime
        action, plan = runtime.preview([_move('left', 'left', .125),
            dict(id='look', kind='observe', arm='left', reason='inspect left'), _move('right', 'right', -.375)])
        planned_rows = deepcopy(runtime.adapter._batch_plans[plan['plan_id']]['rows'])
        first = runtime.execute(action, plan)
        self.assertEqual((first['status'], first['batch_cursor']), ('stopped', 3))
        self.assertEqual(runtime.simultaneous_rows(), 3)
        self.assertNotEqual(runtime.measured['right'], runtime.initial['right'])
        with (patch.object(runtime.adapter, '_plan_segment', side_effect=AssertionError('resume replanned')),
                patch.object(runtime.adapter, '_resample_action_plan', side_effect=AssertionError('resume resampled'))):
            second = runtime.execute(action, plan)
        self.assertEqual((second['status'], second['executed_native_steps']), ('completed', 5))
        self.assertEqual(len(runtime.commands), 8)
        for command, row in zip(runtime.commands, planned_rows):
            np.testing.assert_array_equal(command['left_arm_joint_state'], row[:6])
            np.testing.assert_array_equal(command['right_arm_joint_state'], row[7:13])
        self.assertEqual(first['batch_revision'] + 1, second['batch_revision'])

    def test_each_arm_speed_changes_its_own_timeline(self):
        runtime = self.runtime
        action, plan = runtime.preview([_move('left', 'left', .125, speed_scale=.5),
            _move('right', 'right', -.375, speed_scale=1.)])
        self.complete(action, plan)
        self.assertEqual(len(runtime.commands), 8)
        self.assertEqual(runtime.simultaneous_rows(), 5)
        self.assertAlmostEqual(runtime.commands[0]['left_arm_joint_state'][0], .025)
        self.assertAlmostEqual(runtime.commands[0]['right_arm_joint_state'][0], -.375/8)

    def test_tracking_guard_tracks_both_moving_arms_instead_of_holding_peer(self):
        action, plan = self.runtime.preview([_move('left', 'left', 1.), _move('right', 'right', -1.)])
        self.complete(action, plan)
        self.assertEqual(len(self.runtime.commands), 20)
        self.assertEqual(self.runtime.simultaneous_rows(), 20)


if __name__ == '__main__':
    unittest.main()
