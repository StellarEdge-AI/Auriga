"""Synthetic calibrated wrist pose solving; no simulator/provider/GPU."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np
from scipy.spatial.transform import Rotation

from auriga.core.config import Config
from auriga.core.contracts import canonical
from auriga.runtime.harness import Harness
from auriga.runtime.programs.runner import ProgramRunner
from auriga.runtime.tools.plugins.robotic_view import make_robotic_view_tool, read_robotic_view, _solve_pose, _link6_optical_mount
from auriga.core.storage.store import Store
from auriga.runtime.tools.registry import validator_for
from auriga.runtime.actions.robot_action_schema import ROBOT_ACTION_TOOL


def matrix(xyz=(0.,0.,0.), angles=(0.,0.,0.)):
    value=np.eye(4);value[:3,:3]=Rotation.from_euler('xyz',angles,degrees=True).as_matrix();value[:3,3]=xyz
    return value


class RoboticViewTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(prefix='robotic-view-fixture-');self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name);self.identity=dict(run_id='fixture',episode_id='episode',env_instance_id='env',env_generation=1)
        self.store=Store(self.root/'run',self.identity);self.addCleanup(self.store.close)
        self.robot=SimpleNamespace(observe=Mock(side_effect=AssertionError('no new observation')),
            execute=Mock(side_effect=AssertionError('no physical work')),preview_batch=Mock(side_effect=AssertionError('no native preview')))
        config=Config.from_dict(dict(features=dict(observation_profile='rgb_proprio_calibrated_v1',motion_review=False)))
        self.reader=Mock(side_effect=lambda args,corr:read_robotic_view(self.h,args,corr))
        self.spec=make_robotic_view_tool(self.reader)
        self.h=Harness(self.store,self.robot,config=config,tool_specs=(self.spec,))
        self.h.thread_id='fixture-thread'
        self.h.capabilities=dict(frame_id='env_origin',tcp_config_id='arx-x5-link6-v1')
        self.link=matrix((.2,-.1,.3),(12.,25.,40.))
        self.fixed_first=matrix((.06,.01,0.),(0.,20.,0.))
        self.fixed_second=matrix((.038,-.01,0.),(0.,0.,10.))
        self.fixed=self.fixed_first@self.fixed_second
        self.optical=matrix((.01,.02,.03),(90.,0.,90.))
        self.model=dict(base_link='base',joints=[dict(name='wrist',type='fixed',parent='base',child='link6',T_parent_joint=self.link.tolist()),
            dict(name='hand_to_camera_mount',type='fixed',parent='link6',child='camera_base',T_parent_joint=self.fixed_first.tolist()),
            dict(name='camera_joint',type='fixed',parent='camera_base',child='camera',T_parent_joint=self.fixed_second.tolist())])
        self.calibration=dict(reference_frame_id='env_origin',cameras=dict(wrist=dict(
            arm='left',link_name='camera',T_link_camera=self.optical.tolist(),T_world_base=np.eye(4).tolist(),
            K=[[30.,0.,10.],[0.,30.,10.],[0.,0.,1.]],distortion=[],image_size=[20,20])))
        self.state=dict(sim_step=5,step_domain='control_step',arms=dict(left=dict(
            source_kind='measurement',q_rad=[],joint_names=[],
            ee_pose=dict(source_kind='measurement',frame_id='env_origin',tcp_config_id='arx-x5-link6-v1',
                         position_m=self.link[:3,3].tolist(),quaternion_xyzw=Rotation.from_matrix(self.link[:3,:3]).as_quat().tolist()),
            gripper=dict(joint_names=[],position_m=[],position_source_kind='measurement',command_normalized=0.,command_source_kind='command'))))
        self.sample=dict(T_world_camera=(self.link@self.fixed@self.optical).tolist(),
            T_world_link=(self.link@self.fixed).tolist(),T_world_base=np.eye(4).tolist(),
            source_kind='robot_kinematics',sim_step=5,step_domain='control_step')
        self.seq=0;self.save()
        self.args=dict(observation_id=self.h.latest['observation_id'],camera_id='wrist',
            eye_position_m=[.4,.1,.5],target_position_m=[.1,.2,.2],up_vector=[0.,0.,1.])
        self.addCleanup(self.no_execution)

    def save(self, *, identity=None, profile='rgb_proprio_calibrated_v1', scope='actor', import_scene=True):
        self.seq+=1;oid='obs-'+str(self.seq);frame_id='frame-'+str(self.seq)
        def artifact(value,kind):return self.store.put_artifact(canonical(value).encode(),kind,'application/json',scope)
        sample=dict(self.sample,frame_id=frame_id)
        _,image=cv2.imencode('.png',np.zeros((20,20,3),np.uint8))
        bundle=dict(schema_version='0.1',**(identity or self.identity),world_kind='observed',
            observation_id=oid,observation_profile=profile,sim_step=5,step_domain='control_step',
            clock_domain='env/generation-1',calibration_id='fixture-calibration',robot_model_id='fixture-arm',
            robot_model_ref=artifact(self.model,'robot_model'),robot_state_ref=artifact(self.state,'robot_state'),
            cameras=[dict(camera_id='wrist',frame_id=frame_id,width=20,height=20,sim_step=5,step_domain='control_step',
                calibration_ref=artifact(self.calibration,'calibration'),pose_sample_ref=artifact(sample,'camera_pose'),
                rgb_ref=self.store.put_artifact(image.tobytes(),'rgb','image/png'),pixel_transform='identity')])
        self.store.save('observation',oid,bundle)
        if import_scene:self.h.scene.import_observation(bundle)
        self.h.latest=bundle
        if hasattr(self,'args'):self.args['observation_id']=oid
        return bundle

    def invoke(self,args=None,role='actor'):
        return self.h.invoke('robotic_view',self.args if args is None else args,dict(tool_call_id='fixture-view'),role=role)

    def no_execution(self):
        self.robot.observe.assert_not_called();self.robot.execute.assert_not_called();self.robot.preview_batch.assert_not_called()
        self.assertEqual(self.h.action_count,0)

    def test_real_owner_round_trip_handles_non_link6_mount_and_actor_program(self):
        actor=self.invoke();program=self.invoke(role='program')
        self.assertTrue(actor.success,actor.contract_error);self.assertEqual(actor.kind,'robotic_view');self.assertEqual(actor.data,program.data)
        self.assertEqual(self.spec.effect_for(self.args),'read');self.assertEqual(self.spec.roles,frozenset({'actor','program'}))
        move=actor.data['robot_action_preview']['arguments']['stages'][0]
        desired=matrix();desired[:3,:3]=Rotation.from_quat(move['quaternion_xyzw']).as_matrix();desired[:3,3]=move['position_m']
        camera=desired@self.fixed@self.optical
        np.testing.assert_allclose(camera[:3,3],self.args['eye_position_m'],atol=1e-12)
        ray=np.asarray(self.args['target_position_m'])-camera[:3,3];ray/=np.linalg.norm(ray)
        np.testing.assert_allclose(camera[:3,2],ray,atol=1e-12)
        self.assertGreater(float(np.dot(-camera[:3,1],self.args['up_vector'])),0)
        self.assertAlmostEqual(float(np.linalg.det(camera[:3,:3])),1.)
        # Directly inverting only T_link_camera would leave the 98 mm / 20 deg
        # fixed camera-link transform unaccounted for.
        self.assertGreater(np.linalg.norm((desired@self.optical)[:3,3]-camera[:3,3]),.09)
        validator_for(ROBOT_ACTION_TOOL['inputSchema']).validate(actor.data['robot_action_preview']['arguments'])
        self.assertEqual(actor.data['checks'],dict(ik='not_checked',collision='not_checked',visibility='unknown',retention='unknown'))

    def test_fixed_chain_order_and_identity_mount(self):
        got=_link6_optical_mount(self.model,self.calibration['cameras']['wrist'])
        np.testing.assert_allclose(got,self.fixed@self.optical,atol=1e-14)
        direct=dict(self.calibration['cameras']['wrist'],link_name='link6')
        np.testing.assert_allclose(_link6_optical_mount(self.model,direct),self.optical)

    def test_movable_disconnected_cyclic_and_duplicate_chain_rejected(self):
        for mode in ('movable','missing','cycle','duplicate'):
            with self.subTest(mode=mode):
                model=deepcopy(self.model)
                if mode=='movable':model['joints'][1]['type']='revolute'
                elif mode=='missing':model['joints'][1]['parent']='missing'
                elif mode=='cycle':model['joints'][1]['parent']='camera'
                else:model['joints'].append(deepcopy(model['joints'][1]))
                with self.assertRaises(ValueError):_link6_optical_mount(model,self.calibration['cameras']['wrist'])

    def test_optical_look_at_known_axis_and_up(self):
        link,camera=_solve_pose(np.eye(4),[0.,0.,0.],[1.,0.,0.],[0.,0.,1.])
        np.testing.assert_allclose(camera[:3,:3],[[0.,0.,1.],[-1.,0.,0.],[0.,-1.,0.]])
        np.testing.assert_allclose(link,camera)

    def test_degenerate_and_nonfinite_math_inputs_rejected(self):
        for kwargs in [dict(target=[.4,.1,.5]),dict(up=[0.,0.,0.]),dict(up=[-.3,.1,-.3]),
                       dict(eye=[float('nan'),0.,0.]),dict(target=[float('inf'),0.,0.]),dict(up=[True,0.,1.]),
                       dict(eye=[1e308,0.,0.],target=[-1e308,0.,0.])]:
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError):
                args=dict(eye=self.args['eye_position_m'],target=self.args['target_position_m'],up=self.args['up_vector']);args.update(kwargs)
                _solve_pose(np.eye(4),**args)
        bad=np.eye(4);bad[0,0]=2
        with self.assertRaises(ValueError):_solve_pose(bad,[0.,0.,0.],[1.,0.,0.],[0.,0.,1.])
        bad=np.eye(4);bad[3,0]=.1
        with self.assertRaises(ValueError):_solve_pose(bad,[0.,0.,0.],[1.,0.,0.],[0.,0.,1.])

    def test_invalid_schema_never_reaches_reader(self):
        for args in [dict(self.args,eye_position_m=[0.,0.]),dict(self.args,up_vector=[True,0.,1.]),
                     dict(self.args,camera_id=''),dict(self.args,path='/private/forbidden'),
                     {k:v for k,v in self.args.items() if k!='up_vector'}]:
            with self.subTest(args=args),self.assertRaises(ValueError):self.invoke(args)
        self.reader.assert_not_called()

    def test_stale_missing_latest_and_active_action_rejected(self):
        old=dict(self.args);self.save()
        with self.assertRaisesRegex(ValueError,'current_idle'):self.invoke(old)
        self.h.active_action='active'
        with self.assertRaisesRegex(ValueError,'current_idle'):self.invoke()
        self.h.active_action=None;self.h.latest=None
        with self.assertRaisesRegex(ValueError,'current_idle'):self.invoke()

    def test_cross_identity_is_denied(self):
        self.save(identity=dict(self.identity,episode_id='other'))
        with self.assertRaisesRegex(ValueError,'outside_current_episode'):self.invoke()

    def test_profile_and_disabled_tool_denied_without_reader(self):
        self.h.observation_profile='rgb_proprio_v1'
        with self.assertRaisesRegex(ValueError,'permitted_calibration'):self.invoke()
        self.h.observation_profile='rgb_proprio_calibrated_v1';self.h.disabled_tools=frozenset({'robotic_view'})
        for role in ('actor','program'):
            with self.assertRaisesRegex(ValueError,'tool_disabled'):self.invoke(role=role)
        self.reader.assert_not_called()

    def test_observer_artifact_denied_before_geometry(self):
        self.save(scope='observer',import_scene=False)
        with patch.object(self.h.geometry,'camera',side_effect=AssertionError('must deny before geometry')):
            with self.assertRaises(ValueError):self.invoke()

    def test_unsynchronized_proprio_denied(self):
        self.state['sim_step']=4;self.save()
        with self.assertRaisesRegex(ValueError,'unsynchronized'):self.invoke()

    def test_fixed_camera_or_foreign_ee_frame_denied(self):
        self.calibration['cameras']['wrist']['arm']='center';self.save()
        with self.assertRaises(ValueError):self.invoke()
        self.calibration['cameras']['wrist']['arm']='left';self.state['arms']['left']['ee_pose']['tcp_config_id']='other-tcp';self.save()
        with self.assertRaisesRegex(ValueError,'native_link6'):self.invoke()

    def test_measured_link6_and_camera_mismatch_denied(self):
        self.state['arms']['left']['ee_pose']['position_m'][0]+=.02;self.save()
        with self.assertRaisesRegex(ValueError,'camera_native_link6_mismatch'):self.invoke()

    def test_no_private_provenance_or_fabricated_pose_status(self):
        response=self.invoke();text=canonical(response.data)+canonical(self.h.actor_output.present(response))
        for forbidden in ('sha256','calibration_ref','robot_model_ref',str(self.root),'artifact:'):
            self.assertNotIn(forbidden,text)
        self.assertNotIn('task_success',response.data);self.assertEqual(response.data['status'],'calculated')

    def test_owner_does_not_write_or_read_arbitrary_record(self):
        read=self.store.read
        def guarded(kind,identifier):
            self.assertEqual(kind,'observation');return read(kind,identifier)
        with patch.object(self.store,'read',side_effect=guarded),patch.object(self.store,'save',side_effect=AssertionError('no owner write')),\
             patch.object(self.store,'put_artifact',side_effect=AssertionError('no output artifact')):
            read_robotic_view(self.h,self.args,{})

    def test_currentness_change_during_owner_read_is_rejected(self):
        original=self.h.geometry.camera
        def changed(*args,**kwargs):
            result=original(*args,**kwargs)
            self.h.active_action='concurrent-action'
            return result
        with patch.object(self.h.geometry,'camera',side_effect=changed):
            with self.assertRaisesRegex(ValueError,'current_idle'):self.invoke()

    def test_unknown_stop_gate_is_not_reopened_by_current_observation(self):
        self.h.accepting=False
        self.h.states['unknown-action']=dict(status='status_unknown',stop_confirmation=False)
        before=deepcopy(self.h.states)
        with self.assertRaisesRegex(ValueError,'current_idle'):self.invoke()
        self.assertFalse(self.h.accepting);self.assertEqual(self.h.states,before)

    def test_final_currentness_check_includes_changed_observation_and_stop_gate(self):
        original=self.h.geometry.camera
        for mode in ('observation','stop'):
            self.h.accepting=True;self.save()
            def changed(*args,**kwargs):
                result=original(*args,**kwargs)
                if mode=='observation':self.h.latest=dict(self.h.latest,observation_id='other')
                else:self.h.accepting=False
                return result
            with patch.object(self.h.geometry,'camera',side_effect=changed),self.assertRaisesRegex(ValueError,'current_idle'):
                self.invoke()

    def test_mount_reflection_singular_and_wrong_last_row_rejected(self):
        for index in range(3):
            invalid=np.eye(4)
            if index==0:invalid[0,0]=-1
            elif index==1:invalid[1,1]=0
            else:invalid[3,2]=.1
            cal=dict(self.calibration['cameras']['wrist'],T_link_camera=invalid.tolist())
            with self.assertRaisesRegex(ValueError,'rigid_transform'):_link6_optical_mount(self.model,cal)

    def test_scale_of_up_vector_does_not_change_roll(self):
        args=[self.optical,self.args['eye_position_m'],self.args['target_position_m']]
        first,_=_solve_pose(*args,[0.,0.,1.]);huge,_=_solve_pose(*args,[0.,0.,1e300])
        np.testing.assert_allclose(first,huge,atol=1e-14)

    def test_real_program_broker_receives_same_typed_result(self):
        actor=self.invoke();visible=json.loads(self.h.actor_output.present(actor)['contentItems'][0]['text'])
        # Typed broker parity is independent of the real-kernel isolation suite.
        runner=self.h.programs=ProgramRunner(replace(self.h.program_services(),sandbox_enabled=False));self.addCleanup(runner.close)
        source=('from auriga_client import AurigaClient\n'
            'reply=AurigaClient().call("robotic_view",'+repr(self.args)+')\n'
            'assert reply["success"],reply\nassert reply["kind"]=="robotic_view"\n'
            'assert reply["data"]=='+repr(visible)+'\nprint("pose-only")\n')
        result=runner.run(dict(source=source,timeout_s=10,max_calls=1),
            dict(thread_id=self.h.thread_id,turn_id='fixture-turn',tool_call_id='fixture-program'))
        self.assertEqual(result['status'],'succeeded',result.get('stderr_text'))
        self.assertIn('pose-only',result['stdout_text']);self.assertEqual(result['action_summary']['action_calls'],0)
        self.assertIsNone(result['final_observation_id'])

if __name__=='__main__':unittest.main()
