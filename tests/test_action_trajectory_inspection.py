"""Archived automatic feedback remains fully inspectable without robot calls."""
from copy import deepcopy
import json
import threading
import unittest
from unittest.mock import Mock

from auriga.core.config import Config
from auriga.core.contracts import canonical
from auriga.runtime.actions.inspection import inspect_trajectory
from auriga.runtime.actions.trajectory_feedback import build_action_trajectory_feedback
import test_trajectory_feedback as feedback_fixture


class ActionTrajectoryInspectionTests(unittest.TestCase):
    setUp = feedback_fixture.ActionTrajectoryFeedbackTests.setUp
    put = feedback_fixture.ActionTrajectoryFeedbackTests.put
    receipt = feedback_fixture.ActionTrajectoryFeedbackTests.receipt
    rows = staticmethod(feedback_fixture.ActionTrajectoryFeedbackTests.rows)

    def feedback(self, *, plan=None, counts=2, status='completed', complete_sampling=True):
        self.harness.config = Config()
        self.harness.lock = threading.RLock()
        self.harness.robot = Mock()
        self.harness.native_action_count = counts
        receipt = self.receipt(plan if plan is not None else dict(
            actions=self.rows(counts),completed_native_steps=counts),counts=counts)
        receipt['status'] = status
        if status != 'completed':
            receipt['reason'] = 'tracking_error'
        if not complete_sampling:
            receipt.pop('trace_sampling')
        summary,_ = build_action_trajectory_feedback(self.harness,self.action,receipt,{})
        self.assertTrue(summary['available'],summary)
        reference = summary['trajectory_feedback_ref']
        self.original_bytes = self.store.resolve_artifact(reference,('actor',))[1]
        return reference,json.loads(self.original_bytes)

    def inspect(self, reference, **options):
        before = deepcopy(self.harness.latest)
        count = self.harness.native_action_count
        reply = inspect_trajectory(self.harness,dict(trajectory_ref=reference,**options),{})
        self.assertEqual(self.harness.robot.mock_calls,[])
        self.assertEqual(self.harness.native_action_count,count)
        self.assertEqual(self.harness.latest,before)
        return json.loads(reply[0]['text']),reply

    def test_default_measured_path_preserves_exact_joint_jaw_and_time_values(self):
        reference,full = self.feedback()
        result,_ = self.inspect(reference)
        self.assertEqual(result['path_kind'],'measured')
        self.assertEqual(result['available_paths'],['measured','planned'])
        self.assertEqual(result['source_kind'],'measured_action_trajectory')
        self.assertEqual(result['execution_start_observation_id'],'start')
        self.assertFalse(result['current'])
        self.assertTrue(result['historical'])
        self.assertTrue(result['native_boundary_alignment'])
        self.assertNotIn('native_dt_s',result)
        for value,original in zip(result['samples'],full['paths']['measured']['samples']):
            self.assertEqual(value['sim_step'],original['sim_step'])
            for arm in ('left','right'):
                self.assertEqual(value['arms'][arm]['q_rad'],original['arms'][arm]['q_rad'])
                self.assertEqual(value['arms'][arm]['measured_jaw'],original['arms'][arm]['measured_jaw'])
                self.assertNotIn('link6_pose',value['arms'][arm])
        self.assertEqual(result['timing'],full['paths']['measured']['timing'])
        self.assertEqual(self.store.resolve_artifact(reference,('actor',))[1],self.original_bytes)

    def test_incomplete_sampling_exposes_unknown_alignment_without_inventing_timing(self):
        reference,full = self.feedback(complete_sampling=False)
        result,_ = self.inspect(reference)
        self.assertFalse(result['native_boundary_alignment'])
        self.assertEqual(result['available_paths'],['measured'])
        self.assertEqual(result['timing'],full['paths']['measured']['timing'])
        self.assertEqual([sample['sim_step'] for sample in result['samples']],[30,40,50])
        self.assertNotIn('native_dt_s',result)
        self.assertEqual(result['recorded_limitations'],full['summary']['limitations'])
        self.assertTrue(any('Complete native-boundary sampling' in item for item in result['recorded_limitations']))

    def test_paging_returns_every_exact_sample_and_full_geometry_is_opt_in(self):
        reference,full = self.feedback(counts=5)
        start,observed = 0,[]
        while start is not None:
            result,_ = self.inspect(reference,start_native_step=start,max_samples=2,detail='full')
            observed.extend(result['samples'])
            start = result['next_native_step']
        self.assertEqual(observed,full['paths']['measured']['samples'])
        self.assertEqual(result['robot_model'],full['paths']['measured']['robot_model'])
        self.assertEqual(result['T_world_bases'],full['paths']['measured']['T_world_bases'])
        self.assertEqual(self.store.resolve_artifact(reference,('actor',))[1],self.original_bytes)

    def test_planned_path_is_not_measurement_and_preserves_issued_jaw_goals(self):
        reference,full = self.feedback()
        result,_ = self.inspect(reference,path_kind='planned',native_steps=[1,2])
        self.assertEqual(result['source_kind'],'recorded_planned_commands_not_measurement')
        for value,original in zip(result['samples'],full['paths']['planned']['samples'][1:]):
            self.assertEqual(value['execution_state'],original['execution_state'])
            for arm in ('left','right'):
                self.assertEqual(value['arms'][arm]['q_rad'],original['arms'][arm]['q_rad'])
                self.assertEqual(value['arms'][arm]['jaw_goal_normalized'],1.)
                self.assertNotIn('measured_jaw',value['arms'][arm])

    def test_local_unexecuted_path_retains_its_independent_timing(self):
        plan=dict(actions=self.rows(1),completed_native_steps=1,arms={
            'left':dict(segments=[dict(compiled_sim_step=30,waypoint_index=0,
                motion_actions=[row[:7] for row in self.rows(4)],gripper_actions=[],completed_motion_steps=1)]),
            'right':dict(segments=[])})
        reference,full = self.feedback(plan=plan,counts=1,status='failed')
        result,_ = self.inspect(reference,path_kind='local_0',start_native_step=1,max_samples=25)
        self.assertIn('local_0',result['available_paths'])
        self.assertEqual(result['source_kind'],'unexecuted_arm_candidate_not_synchronized')
        self.assertIn('no shared future',result['timing'])
        self.assertEqual(result['remaining_rows'],3)
        self.assertEqual(result['samples'][-1]['execution_state'],'not_recorded_completed')
        self.assertEqual(result['samples'][-1]['arms']['left']['q_rad'],[.4]*6)
        self.assertEqual(set(result['samples'][-1]['arms']),{'left'})

    def test_saved_action_overlay_uses_archived_start_rgb_and_marks_historical(self):
        reference,full = self.feedback()
        result,reply = self.inspect(reference,camera_id='cam_head')
        overlay = result['overlay']
        self.assertEqual(overlay['status'],'available',overlay)
        self.assertEqual(len(reply),2)
        self.assertEqual(overlay['original_rgb_ref'],self.rgb_ref)
        self.assertEqual(overlay['observation_id'],'start')
        self.assertEqual(overlay['execution_start_observation_id'],'start')
        self.assertTrue(overlay['historical'])
        self.assertFalse(overlay['current'])
        self.assertFalse(overlay['physics_advanced'])
        self.assertIn('Historical execution-start RGB',overlay['limitations'])
        self.assertEqual(overlay['panel_width'],640)
        self.assertEqual(self.store.resolve_artifact(reference,('actor',))[1],self.original_bytes)

    def test_unknown_or_missing_path_and_invalid_path_selector_are_rejected(self):
        reference,_ = self.feedback(plan={'segments':[]},counts=0,status='failed')
        for selector in ('planned','local_9','unknown'):
            with self.subTest(selector=selector),self.assertRaisesRegex(ValueError,'path_unavailable:.*available_paths=measured'):
                self.inspect(reference,path_kind=selector)
        for selector in (None,[],3,''):
            with self.subTest(selector=selector),self.assertRaisesRegex(ValueError,'nonempty_string'):
                self.inspect(reference,path_kind=selector)

    def test_actor_scope_identity_binding_source_and_profile_cannot_be_bypassed(self):
        reference,full = self.feedback()
        mutations=[('stale_environment_identity',lambda d:d['identity'].update(episode_id='other')),
            ('stale_environment_identity',lambda d:d['paths']['measured']['identity'].update(episode_id='other')),
            ('binding_mismatch',lambda d:d['paths']['measured'].update(observation_id='other')),
            ('invalid_action_trajectory_path_source',lambda d:d['paths']['measured'].update(source_kind='candidate_command_trajectory_not_measurement')),
            ('privileged',lambda d:d.update(object_poses={'hidden':1}))]
        for reason,mutation in mutations:
            data=deepcopy(full);mutation(data)
            ref=self.put(data,'action_trajectory_feedback')
            with self.subTest(reason=reason),self.assertRaisesRegex(ValueError,reason):
                self.inspect(ref)
        ref=self.put(full,'action_trajectory_feedback',scope='internal')
        with self.assertRaisesRegex(ValueError,'access_scope_denied'):
            self.inspect(ref)
        self.assertEqual(self.harness.robot.mock_calls,[])


if __name__=='__main__':
    unittest.main()
