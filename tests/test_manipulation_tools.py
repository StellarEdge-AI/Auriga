"""Public queue, budget and planning-only feature boundaries."""
from copy import deepcopy
import json
import unittest
from unittest.mock import Mock

from auriga.core.contracts import validate_action, validate_arm_queue_action
from auriga.robot.backends.robodojo.robodojo import validate_action as native_validate, validate_preview_action
import test_dual_arm_interface as base


class ManipulationBoundaryTests(unittest.TestCase):
    setUp = base.DualArmHarnessTests.setUp

    def queue(self):
        self.h.capabilities['arm_queue'] = dict(available=True)
        return dict(schema_version='0.1', **self.h.identity, world_kind='observed', type='ArmQueueAction',
            action_id='q1', based_on_observation_id=self.h.latest['observation_id'], mode='absolute',
            frame_id=self.h.capabilities['frame_id'], tcp_config_id=self.h.capabilities['tcp_config_id'],
            limits_profile='simulation-slow-v1', timeout_s=120., arm_queues={
            'left': [dict(id='approach', kind='move', target=dict(position_m=[.3001,-.1,.9226], quaternion_xyzw=[0,1,0,0]),
                          speed_scale=.8, position_tolerance_m=.005),
                     dict(id='close', kind='gripper', goal_normalized=0., gripper_speed_scale=.8),
                     dict(id='check', kind='observe', reason='Check object follows before transport')],
            'right': []})

    def test_raw_queue_passes_contract_and_native_boundaries_but_not_preview(self):
        action=self.queue()
        validate_action(action,self.h.identity,action['based_on_observation_id'],self.h.capabilities)
        self.assertEqual(native_validate(action),('both',None))
        with self.assertRaisesRegex(ValueError,'preview_unsupported'):
            validate_preview_action(action)

    def test_queue_dependencies_reject_cycles_including_implicit_arm_order(self):
        action=self.queue()
        action['arm_queues']['left'][0]['depends_on']=['close']
        with self.assertRaisesRegex(ValueError,'dependency_cycle'): validate_arm_queue_action(action)
        action['arm_queues']['left'][0]['depends_on']=['missing']
        with self.assertRaisesRegex(ValueError,'unknown_dependency'): validate_arm_queue_action(action)
        action['arm_queues']['left'][0]['depends_on']=[]
        action['arm_queues']['right']=[dict(id='close',kind='observe',reason='duplicate')]
        with self.assertRaisesRegex(ValueError,'unique'): validate_arm_queue_action(action)

    def test_raw_queue_feature_cannot_bypass_disabled_convenience_tool(self):
        action=self.queue()
        self.h.config=self.h.config.with_overrides({'features':{'arm_queue':False}})
        result=self.h.execute(action)
        self.assertEqual(result['receipt']['status'],'rejected')
        self.assertIn('not_enabled',result['receipt']['reason'])
        self.assertEqual(self.robot.executions,0)

    def test_queue_identity_conflict_and_replay_do_not_dispatch_twice(self):
        action=self.queue()
        self.robot.execute=Mock(return_value=dict(status='completed',reason=None,executed_native_steps=2,
            episode_terminal=dict(native_action_count=2,native_step_limit=700,ended=False),
            terminal_observation=self.robot.observe()))
        first=self.h.execute(action)
        replay=self.h.execute(action)
        self.assertEqual(first,replay)
        self.assertEqual(self.robot.execute.call_count,1)
        changed=deepcopy(action); changed['arm_queues']['left'][0]['speed_scale']=1.
        with self.assertRaisesRegex(ValueError,'content_conflict'): self.h.execute(changed)
        self.assertEqual(self.robot.execute.call_count,1)
        self.assertEqual(first['receipt']['action_budget']['native_steps_remaining'],698)

    def test_budget_published_on_observation_and_preserved_for_historical_view(self):
        self.h._native_progress(dict(native_action_count=100,native_step_limit=700,ended=False,score=40))
        old=self.h.observe(); self.assertEqual(old['action_budget']['native_steps_remaining'],600)
        self.h._native_progress(dict(native_action_count=125,native_step_limit=700,ended=False))
        new=self.h.observe()
        self.assertEqual(new['action_budget']['native_steps_remaining'],575)
        historical=json.loads(self.h.observation_content(old,{},numeric_only=True)[0]['text'])
        self.assertEqual(historical['action_budget']['native_steps_remaining'],600)
        self.assertNotIn('score',json.dumps(historical))

    def test_unknown_budget_does_not_invent_limit(self):
        self.assertIsNone(self.h.action_budget()['native_steps_limit'])
        self.assertIsNone(self.h.action_budget()['native_steps_remaining'])

    def test_queue_malformed_speeds_goals_and_mixed_modes_fail_before_native(self):
        action=self.queue()
        for mutate in (lambda a:a.update(path={}),lambda a:a.update(dual_arm_progression='paired'),
                       lambda a:a['arm_queues']['left'][0].update(speed_scale=True),
                       lambda a:a['arm_queues']['left'][1].update(goal_normalized=float('nan'))):
            invalid=deepcopy(action); mutate(invalid)
            with self.assertRaises(ValueError): native_validate(invalid)
