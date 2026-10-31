"""Automatic action feedback is actor-only and cannot execute a robot action."""
import base64
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

from auriga.core.contracts import canonical
from auriga.core.storage.images import image_settings
from auriga.core.storage.store import Store
from auriga.runtime.actions.trajectory_feedback import build_action_trajectory_feedback
from test_trajectory_evidence import public_fixture


class ActionTrajectoryFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.identity = dict(run_id='run', episode_id='episode', env_instance_id='environment', env_generation=1)
        self.store = Store(Path(self.temporary.name), self.identity)
        self.addCleanup(self.store.close)
        _, self.before, _, self.action, hardware = public_fixture(.5)
        self.action.update(self.identity, action_id='action')
        self.before.update(sim_step=30, step_domain='adapter_control_step')
        self.harness = SimpleNamespace(store=self.store, identity=self.identity, latest={'observation_id':'terminal'},
            observation_profile='rgb_proprio_calibrated_v1', image_settings=image_settings())
        self.model_ref = self.put(hardware['model'], 'robot_model')
        camera = np.eye(4); camera[2,3] = -2
        calibration = dict(reference_frame_id='env_origin', cameras={'cam_head':dict(image_size=[640,480],
            K=[[500.,0,320.],[0,500.,240.],[0,0,1.]], distortion=[0.]*5,T_world_camera=camera.tolist())})
        calibration_ref = self.put(calibration, 'calibration')
        buffer = io.BytesIO(); Image.new('RGB',(640,480),(80,120,150)).save(buffer,format='PNG')
        self.rgb_ref = self.store.put_artifact(buffer.getvalue(),'rgb','image/png')
        self.observation = dict(self.identity, observation_id='start', world_kind='observed', sim_step=30,
            step_domain='adapter_control_step', robot_model_ref=self.model_ref,
            robot_state_ref=self.put(self.before,'robot_state'), cameras=[dict(camera_id='cam_head',
                frame_id='head', width=640,height=480,sim_step=30,step_domain='adapter_control_step',
                calibration_ref=calibration_ref, rgb_ref=self.rgb_ref,pixel_transform='identity',pose_sample_ref='FORBIDDEN')])
        self.store.save('observation','start',self.observation)

    def put(self, value, kind, scope='actor'):
        return self.store.put_artifact(canonical(value).encode(),kind,'application/json',scope)

    def receipt(self, plan, counts=2, measured_error=0.):
        actual=[]
        for index in range(counts+1):
            state=deepcopy(self.before); state['sim_step']=30+index*10
            for arm in ('left','right'):
                state['arms'][arm]['q_rad']=[index*.1+(measured_error if index else 0.)]*6
            actual.append(dict(sim_step=state['sim_step'],state=state))
        actual.append(deepcopy(actual[-1]))
        return dict(status='completed', executed_steps=counts, completed_native_steps=counts,
            execution_start_observation_id='start', result_observation_id='terminal',
            trace_sampling=dict(actual_every_native_actions=1, actual_boundary_samples=True),
            planned_trace_ref=self.put(plan,'planned_trace'), actual_trace_ref=self.put(actual,'actual_trace'),
            before_state_ref=self.put(self.before,'before_state'), dispatched_trace_ref=self.put({'samples':[]},'dispatched_trace'))

    @staticmethod
    def rows(count):
        return [[i*.1]*6+[1.]+[i*.1]*6+[1.] for i in range(1,count+1)]

    def test_diagnostic_paired_trace_original_resolution_images_exact_q_and_no_backend(self):
        receipt=self.receipt(dict(actions=self.rows(2),completed_native_steps=2), measured_error=.01)
        original=deepcopy(receipt)
        result,images=build_action_trajectory_feedback(self.harness,self.action,receipt,{},diagnostic=True)
        self.assertEqual(receipt,original)
        self.assertTrue(result['available'],result)
        self.assertEqual(len(images),2)
        self.assertEqual(result['measured']['total_samples'],3)
        self.assertEqual(result['planned']['samples'][-1]['arms']['right']['q_rad'],[.2]*6)
        self.assertAlmostEqual(result['measured_command_error']['max_abs_joint_error_rad'],.01)
        self.assertEqual(result['joint_names'],{arm:self.before['arms'][arm]['joint_names'] for arm in ('left','right')})
        self.assertEqual(result['q_unit'],'rad')
        self.assertNotIn('joint_names',result['measured'])
        self.assertNotIn('geometry',result['planned'])
        self.assertNotIn('measured_jaw',result['measured']['samples'][0]['arms']['left'])
        self.assertEqual(result['final_measured_jaws']['arms']['left']['position_m'],[.044,.044])
        full=json.loads(self.store.resolve_artifact(result['trajectory_feedback_ref'],('actor',))[1])
        for content,metadata in zip(images,full['summary']['overlays'].values()):
            with Image.open(io.BytesIO(base64.b64decode(content['imageUrl'].split(',',1)[1]))) as image:
                self.assertEqual(image.size,(1920,480))
            self.assertEqual(metadata['original_rgb_ref'],self.rgb_ref)
            self.assertEqual(metadata['observation_id'],'start')
            self.assertFalse(metadata['current'])
            self.assertFalse(metadata['physics_advanced'])
            self.assertEqual(metadata['path_sample_counts'],{'left':3,'right':3})
        self.assertEqual(full['paths']['measured']['samples'][-1]['sim_step'],50)
        self.assertEqual(result['overlays']['measured']['sample_indices'],[0,1,2])
        self.assertEqual(result['overlay_camera']['panel_width'],640)
        self.assertNotIn('panels',result['overlays']['measured'])
        self.assertNotIn('FORBIDDEN',canonical(full))

    def test_routine_completed_execution_has_no_overlay_render_or_q_delivery_and_preserves_exact_paths(self):
        receipt=self.receipt(dict(actions=self.rows(2),completed_native_steps=2), measured_error=.01)
        receipt.update(position_error_m=.001,orientation_error_rad=.02)
        original=deepcopy(receipt)
        with patch('auriga.runtime.actions.inspection._trajectory_image',side_effect=AssertionError('routine rendering')) as render:
            result,images=build_action_trajectory_feedback(self.harness,self.action,receipt,{})
        render.assert_not_called()
        self.assertEqual(images,[])
        self.assertEqual(receipt,original)
        self.assertEqual(result['delivery'],'routine_execution_summary')
        self.assertEqual(result['execution_status'],'completed')
        from auriga.core.presentation.actor_payload import lean_action_result
        # Routine feedback accompanies the enclosing receipt view;
        # removing its duplicate echo must leave exact endpoint evidence there.
        direct = lean_action_result(dict(request=self.action, receipt=receipt))['receipt']
        self.assertEqual(direct['position_error_m'], receipt['position_error_m'])
        self.assertEqual(direct['orientation_error_rad'], receipt['orientation_error_rad'])
        self.assertEqual(direct['completed_native_steps'], receipt['completed_native_steps'])
        self.assertAlmostEqual(result['measured_command_error']['max_abs_joint_error_rad'],.01)
        self.assertNotIn('q_rad',canonical(result))
        self.assertNotIn('samples',result)
        self.assertNotIn('measured',result)
        self.assertIn('task success are not assessed',result['semantics'])
        metadata,raw=self.store.resolve_artifact(result['trajectory_feedback_ref'],('actor',))
        self.assertEqual(metadata['kind'],'action_trajectory_feedback')
        full=json.loads(raw)
        self.assertEqual(full['source_refs']['planned_trace_ref'],receipt['planned_trace_ref'])
        self.assertEqual(full['paths']['planned']['samples'][-1]['arms']['right']['q_rad'],[.2]*6)
        self.assertEqual(full['paths']['measured']['samples'][-1]['arms']['left']['q_rad'],[.2+.01]*6)
        self.assertIn('link6_pose',full['paths']['measured']['samples'][-1]['arms']['left'])
        self.assertEqual(full['summary']['overlays'],{})

    def test_uncertain_or_exceptional_completed_and_unknown_execution_retain_diagnostics(self):
        cases=[dict(status='unknown'),dict(status='stopped'),dict(reason='tracking_error'),
            dict(execution_uncertain=True),dict(stop_confirmed=False),dict(stop_confirmation=dict(confirmed=False)),
            dict(gripper_execution=dict(left=dict(execution_uncertain=True))),
            dict(result_observation_id=None),dict(observation_unavailable_reason='camera_unavailable')]
        for updates in cases:
            with self.subTest(updates=updates):
                receipt=self.receipt(dict(actions=self.rows(2),completed_native_steps=2))
                receipt.update(updates)
                result,images=build_action_trajectory_feedback(self.harness,self.action,receipt,{})
                self.assertEqual(len(images),2)
                self.assertIn('q_rad',canonical(result))
                self.assertNotIn('delivery',result)

    def test_independent_failure_keeps_unsynchronized_unexecuted_local_arm_path(self):
        plan=dict(actions=self.rows(1),completed_native_steps=1,row_contexts=[{
            'left':{'phase':'motion'},'right':{'phase':'gripper'}}], arms={
            'left':dict(segments=[dict(compiled_sim_step=30,waypoint_index=0,
                motion_actions=[row[:7] for row in self.rows(4)],gripper_actions=[],completed_motion_steps=1)]),
            'right':dict(segments=[])})
        receipt=self.receipt(plan,counts=1);receipt.update(status='failed',reason='tracking_error')
        result,images=build_action_trajectory_feedback(self.harness,self.action,receipt,{})
        self.assertTrue(result['available'],result)
        self.assertEqual(len(images),3)
        local=result['unexecuted_candidates'][0]
        self.assertEqual(local['remaining_rows'],3)
        self.assertEqual(local['completed_rows'],1)
        self.assertIn('no shared future',local['timing'])
        self.assertEqual(set(local['samples'][-1]['arms']),{'left'})
        self.assertEqual(local['samples'][-1]['arms']['left']['q_rad'],[.4]*6)
        self.assertEqual(result['planned']['total_samples'],2)

    def test_single_arm_compiled_plan_and_measured_other_arm_hold(self):
        plan=dict(arm='left',joint_names=self.before['arms']['left']['joint_names'],
            q_rad=[[.1]*6,[.2]*6,[.3]*6],planned_motion_steps=3,planned_gripper_steps=2,completed_native_steps=1)
        result,_=build_action_trajectory_feedback(self.harness,self.action,self.receipt(plan,counts=1),{})
        self.assertTrue(result['available'])
        self.assertEqual(result['planned']['samples'][-1]['arms']['right']['q_rad'],[0.]*6)
        self.assertEqual(result['planned']['samples'][-1]['execution_state'],'not_recorded_completed')
        self.assertTrue(any('jaw rows' in item for item in result['limitations']))

    def test_zero_step_rejection_reports_missing_plan_and_available_measured_start(self):
        receipt=self.receipt({'segments':[]},counts=0);receipt.update(status='failed',reason='motion_safety_rejected')
        result,images=build_action_trajectory_feedback(self.harness,self.action,receipt,{})
        self.assertTrue(result['available'])
        self.assertFalse(result['planned']['available'])
        self.assertEqual(result['measured']['total_samples'],1)
        self.assertEqual(result['unexecuted_candidates'],[])
        self.assertEqual(len(images),1)

    def test_rejected_solver_endpoint_remains_endpoint_without_fabricated_path(self):
        receipt=self.receipt({'segments':[]},counts=0)
        receipt['failure_feedback']=dict(arm='left',candidates=[dict(stage='ik',
            joint_names=self.before['arms']['left']['joint_names'],endpoint_q_rad=[.42]*6,solver_success=False)])
        result,images=build_action_trajectory_feedback(self.harness,self.action,receipt,{})
        candidate=result['unexecuted_candidates'][0]
        self.assertEqual(candidate['total_samples'],1)
        self.assertEqual(candidate['samples'][0]['execution_state'],'not_executed')
        self.assertIn('endpoint only',candidate['timing'])
        self.assertEqual(len(images),2)

    def test_scope_identity_and_missing_calibration_fail_locally_without_receipt_mutation(self):
        receipt=self.receipt(dict(actions=self.rows(2),completed_native_steps=2))
        receipt['before_state_ref']=self.put(self.before,'before_state','internal')
        original=deepcopy(receipt)
        result,images=build_action_trajectory_feedback(self.harness,self.action,receipt,{})
        self.assertFalse(result['available']);self.assertIn('access_scope_denied',result['detail'])
        self.assertEqual(images,[]);self.assertEqual(receipt,original)
        result,_=build_action_trajectory_feedback(self.harness,{**self.action,'episode_id':'stale'},receipt,{})
        self.assertEqual(result['detail'],'stale_environment_identity')
        self.observation['robot_model_ref']=None
        # Immutable observation gets a different ID for the missing-model case.
        self.observation['observation_id']='missing'
        self.store.save('observation','missing',self.observation)
        receipt=self.receipt(dict(actions=self.rows(2),completed_native_steps=2))
        receipt['execution_start_observation_id']='missing'
        result,images=build_action_trajectory_feedback(self.harness,self.action,receipt,{})
        self.assertTrue(result['available'])
        self.assertEqual(result['geometry']['status'],'unavailable')
        self.assertEqual(result['measured']['samples'][-1]['arms']['left']['q_rad'],[.2]*6)
        self.assertEqual(images,[])

    def test_unknown_sampling_does_not_invent_plan_measurement_alignment(self):
        receipt=self.receipt(dict(actions=self.rows(2),completed_native_steps=2))
        receipt.pop('trace_sampling')
        result,images=build_action_trajectory_feedback(self.harness,self.action,receipt,{})
        self.assertTrue(result['available'])
        self.assertFalse(result['native_boundary_alignment'])
        self.assertFalse(result['planned']['available'])
        self.assertNotIn('measured_command_error',result)

    def test_direct_measurements_and_derivatives_use_irregular_physics_timestamps(self):
        for state in self.before['arms'].values():
            state['qdot_rad_s'] = [1.] * 6
            state['gripper'].update(goal_normalized=.25, goal_source_kind='command',
                                    command_normalized=.5, command_source_kind='command')
        observation = deepcopy(self.observation)
        observation['observation_id'] = 'timed'
        observation['cameras'][0]['control_dt_s'] = .004
        self.store.save('observation', 'timed', observation)
        receipt = self.receipt(dict(actions=self.rows(2), completed_native_steps=2))
        actual = []
        for index, (step, velocity) in enumerate(((30, 1.), (40, 3.), (44, 4.))):
            state = deepcopy(self.before)
            state['sim_step'] = step
            for arm in ('left', 'right'):
                state['arms'][arm]['qdot_rad_s'] = [velocity] * 6
                state['arms'][arm]['ee_pose']['position_m'][0] += .004 * index
                angle = .02 * index
                state['arms'][arm]['ee_pose']['quaternion_xyzw'] = [0., 0., np.sin(angle / 2), np.cos(angle / 2)]
                state['arms'][arm]['gripper']['position_m'] = [.044 - .001 * index] * 2
            actual.append(dict(sim_step=step, state=state))
        receipt.update(execution_start_observation_id='timed', actual_trace_ref=self.put(actual, 'actual_trace'))
        result, _ = build_action_trajectory_feedback(self.harness, self.action, receipt, {}, diagnostic=True)
        self.assertTrue(result['available'], result)
        full = json.loads(self.store.resolve_artifact(result['trajectory_feedback_ref'], ('actor',))[1])
        measured = full['paths']['measured']
        self.assertEqual(measured['measurement_timing']['physics_dt_s'], .004)
        samples = measured['samples']
        self.assertEqual([item['elapsed_time_s'] for item in samples], [0., .04, .056])
        final = samples[-1]['arms']['left']
        self.assertEqual(final['qdot_rad_s'], [4.] * 6)
        self.assertEqual(final['measured_ee_pose'], actual[-1]['state']['arms']['left']['ee_pose'])
        self.assertNotEqual(final['measured_ee_pose']['position_m'], final['link6_pose']['position_m'])
        self.assertEqual(final['jaw_goal_normalized'], .25)
        self.assertEqual(final['jaw_command_normalized'], .5)
        derived = final['derived_motion']
        self.assertEqual(derived['interval_sim_steps'], [40, 44])
        self.assertEqual(derived['interval_s'], .016)
        np.testing.assert_allclose(derived['joint_acceleration_rad_s2'], [62.5] * 6)
        np.testing.assert_allclose(derived['ee_linear_velocity_m_s'], [.25, 0., 0.])
        np.testing.assert_allclose(derived['ee_angular_velocity_rad_s'], [0., 0., 1.25])
        np.testing.assert_allclose(derived['ee_linear_acceleration_m_s2'], [(.25 - .1) / .028, 0., 0.])
        np.testing.assert_allclose(derived['jaw_velocity_m_s'], [-.0625] * 2)

    def test_missing_clock_keeps_measured_velocity_and_pose_without_invented_derivatives(self):
        for state in self.before['arms'].values():
            state['qdot_rad_s'] = [.125] * 6
        result, _ = build_action_trajectory_feedback(self.harness, self.action,
            self.receipt(dict(actions=self.rows(2), completed_native_steps=2)), {}, diagnostic=True)
        full = json.loads(self.store.resolve_artifact(result['trajectory_feedback_ref'], ('actor',))[1])
        measured = full['paths']['measured']
        self.assertEqual(measured['measurement_timing']['status'], 'unavailable')
        for sample in measured['samples']:
            self.assertNotIn('elapsed_time_s', sample)
            self.assertEqual(sample['arms']['left']['qdot_rad_s'], [.125] * 6)
            self.assertIn('measured_ee_pose', sample['arms']['left'])
            self.assertNotIn('derived_motion', sample['arms']['left'])


if __name__=='__main__':
    unittest.main()
