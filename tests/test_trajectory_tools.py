"""Candidate commands stay bounded, observation-bound and deployable."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch
from pathlib import Path
import tempfile
from auriga.core.storage.store import Store
from auriga.core.contracts import canonical

import numpy as np
from auriga.runtime.actions.inspection import inspect_trajectory
from auriga.perception.overlays.trajectory_overlay import deployable_camera, render_trajectory_overlay
from test_contact_overlay import camera_fixture
from test_manipulation_tools import ManipulationBoundaryTests


def path_fixture():
    return dict(source_kind='candidate_command_trajectory_not_measurement', frame_id='env_origin',
        tcp_config_id='arx-x5-link6-v1', joint_names={a:['joint1'] for a in ('left','right')},native_dt_s=.05,
        robot_model=dict(joints=[dict(parent='base',child='link6')]), samples=[
            dict(native_step=i, arms={a:dict(q_rad=[i*.01]*6,phase='motion',
                link_positions_m={'base':[x,0,2], 'link6':[x+i*.03, .1, 2]})
                for a,x in [('left',-.1),('right',.1)]}) for i in range(4)])


class TrajectoryOverlayTests(unittest.TestCase):
    def test_synced_panels_preserve_original_rgb_and_label_candidate(self):
        camera, image = camera_fixture(); before=image.copy()
        output, metadata = render_trajectory_overlay(camera,image,path_fixture())
        self.assertEqual(output.shape,(480,1920,3))
        self.assertEqual([p['native_step'] for p in metadata['panels']],[0,2,3])
        self.assertEqual(set(metadata['panels'][1]['arms']),{'left','right'})
        np.testing.assert_array_equal(image,before)
        self.assertFalse(metadata['physics_advanced'])
        self.assertIn('Unexecuted',metadata['limitations'])

    def test_privileged_camera_frame_mismatch_and_unbounded_samples_reject(self):
        camera,image=camera_fixture(); path=path_fixture()
        for change,reason in [({'pose_source':'simulator_camera_transform'},'deployable'),
                              ({'reference_frame_id':'unknown'},'frame_mismatch')]:
            with self.assertRaisesRegex(ValueError,reason):render_trajectory_overlay({**camera,**change},image,path)
        with self.assertRaisesRegex(ValueError,'one_to_three'):
            render_trajectory_overlay(camera,image,path,native_steps=[0,1,2,3])

    def test_fixed_camera_uses_only_static_calibration_never_dynamic_pose_sample(self):
        camera,image=camera_fixture(); observation=deepcopy(camera['observation'])
        observation['cameras']=[dict(camera['frame'],width=640,height=480,calibration_ref='cal',pose_sample_ref='FORBIDDEN')]
        config=dict(reference_frame_id='env_origin',cameras={'head':dict(K=camera['K'].tolist(),distortion=[0.]*5,
            image_size=[640,480],T_world_camera=np.eye(4).tolist(),T_world_base='FORBIDDEN')})
        temporary=tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        identity=dict(run_id='run',episode_id='ep',env_instance_id='env',env_generation=1)
        store=Store(Path(temporary.name),identity); self.addCleanup(store.close)
        observation.update(identity)
        reference=store.put_artifact(json.dumps(config).encode(),'calibration','application/json')
        observation['cameras'][0]['calibration_ref']=reference
        store.save('observation','observed',observation)
        actual=deployable_camera(store,identity,'observed','head',path_fixture())
        np.testing.assert_array_equal(actual['T'],np.eye(4));self.assertEqual(actual['pose_source'],'fixed_calibration')
        config.pop('reference_frame_id')
        reference=store.put_artifact(json.dumps(config).encode(),'calibration','application/json')
        observation['cameras'][0]['calibration_ref']=reference
        store.save('observation','missing',dict(observation,observation_id='missing'))
        with self.assertRaisesRegex(ValueError,'missing_calibrated_frame'):
            deployable_camera(store,identity,'missing','head',path_fixture())

    def test_wrist_camera_uses_measured_fk_and_never_pose_sample_or_calibration_base(self):
        camera,image=camera_fixture(); observation=deepcopy(camera['observation'])
        identity=dict(run_id='run',episode_id='ep',env_instance_id='env',env_generation=1)
        temporary=tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        store=Store(Path(temporary.name),identity); self.addCleanup(store.close)
        observation.update(identity)
        config=dict(reference_frame_id='env_origin',cameras={'wrist':dict(K=camera['K'].tolist(),distortion=[0.]*5,
            image_size=[640,480],arm='left',link_name='link6',T_link_camera=np.eye(4).tolist(),T_world_base='FORBIDDEN')})
        state=dict(sim_step=observation['sim_step'],step_domain=observation['step_domain'],arms={'left':dict(
            source_kind='measurement',joint_names=['j'+str(i) for i in range(6)],q_rad=[0.]*6,
            gripper=dict(position_source_kind='measurement',joint_names=[],position_m=[]))})
        observation['robot_state_ref']=store.put_artifact(json.dumps(state).encode(),'robot_state','application/json')
        cal=store.put_artifact(json.dumps(config).encode(),'calibration','application/json')
        observation['cameras']=[dict(camera['frame'],camera_id='wrist',width=640,height=480,calibration_ref=cal,pose_sample_ref='FORBIDDEN')]
        store.save('observation','observed',observation)
        path=path_fixture();base=np.eye(4);base[2,3]=1.
        path['T_world_bases']={'left':base.tolist()}
        path['robot_model']=dict(base_link='base',joints=[dict(name='fixed',type='fixed',parent='base',child='link6',T_parent_joint=np.eye(4).tolist(),axis=[1,0,0])])
        actual=deployable_camera(store,identity,'observed','wrist',path)
        np.testing.assert_array_equal(actual['T'],base)
        self.assertEqual(actual['pose_source'],'robot_kinematics')


class TrajectoryBoundaryTests(unittest.TestCase):
    setUp=ManipulationBoundaryTests.setUp

    def preview(self, data=None):
        data=dict(data or path_fixture(),identity=dict(self.h.identity),
                  observation_id=self.h.latest['observation_id'],label='candidate')
        return dict(trajectory_ref=self.h.store.put_artifact(canonical(data).encode(),'candidate_trajectory','application/json'))

    def test_large_data_stored_and_exact_bounded_samples_retrievable_without_actions(self):
        result=self.preview()
        args=dict(trajectory_ref=result['trajectory_ref'],native_steps=[1,3])
        info=json.loads(inspect_trajectory(self.h,args,{})[0]['text'])
        self.assertEqual(info['samples'][0]['arms']['left']['q_rad'],[.01]*6)
        self.assertNotIn('link_positions_m',info['samples'][0]['arms']['left'])
        self.assertTrue(info['current']);self.assertEqual(self.robot.executions,0)
        with self.assertRaisesRegex(ValueError,'one_to_twenty_five'):
            inspect_trajectory(self.h,{**args,'native_steps':[True]}, {})
        self.h.identity['episode_id']='new'
        with self.assertRaisesRegex(ValueError,'stale_environment_identity'):inspect_trajectory(self.h,args,{})

    def test_pagination_retrieves_every_exact_joint_and_jaw_command(self):
        data=path_fixture()
        data['samples']=[dict(native_step=i,arms={arm:dict(q_rad=[i*.12345678901234567+j*.00000000012345 for j in range(6)],
            jaw_goal_normalized=i/63.,phase='motion',link_positions_m={'link6':[i*.03,0.,2.]})
            for arm in ('left','right')}) for i in range(63)]
        preview=self.preview(data)
        ref=preview['trajectory_ref']
        collected=[]; start=0; sizes=[]
        while start is not None:
            args=dict(trajectory_ref=ref,start_native_step=start,max_samples=25)
            info=json.loads(inspect_trajectory(self.h,args,{})[0]['text'])
            self.assertEqual(info['selection_mode'],'page')
            self.assertEqual(info['total_samples'],63)
            collected.extend(info['samples']);sizes.append(len(info['samples']))
            start=info['next_native_step']
        expected=[dict(native_step=s['native_step'],arms={a:{k:v for k,v in c.items() if k!='link_positions_m'}
                  for a,c in s['arms'].items()}) for s in data['samples']]
        self.assertEqual(collected,expected)
        self.assertEqual(sizes,[25,25,13])
        _,raw=self.h.store.resolve_artifact(ref,allowed_scopes=('actor',))
        self.assertEqual(json.loads(raw)['samples'],data['samples'])
        self.assertEqual(self.robot.executions,0)

    def test_default_key_samples_and_explicit_geometry(self):
        data=path_fixture();data.update(geometry=dict(status='available'),
            inter_arm_proximity=dict(status='unknown',reason='missing_spheres'),hardware_provenance={'kind':'public'})
        preview=self.preview(data);args=dict(trajectory_ref=preview['trajectory_ref'])
        short=json.loads(inspect_trajectory(self.h,args,{})[0]['text'])
        self.assertEqual([s['native_step'] for s in short['samples']],[0,2,3])
        self.assertEqual(short['selection_mode'],'key_samples')
        self.assertNotIn('robot_model',short)
        full=json.loads(inspect_trajectory(self.h,{**args,'include_geometry':True}, {})[0]['text'])
        self.assertEqual(full['samples'],[data['samples'][i] for i in [0,2,3]])
        self.assertEqual(full['robot_model'],data['robot_model'])
        self.assertEqual(full['inter_arm_proximity'],data['inter_arm_proximity'])
        self.assertEqual(full,json.loads(inspect_trajectory(self.h,{**args,'detail':'full'}, {})[0]['text']))

    def test_selector_validation_and_pagination_defaults(self):
        result=self.preview();args=dict(trajectory_ref=result['trajectory_ref'])
        invalid=[dict(native_steps=[[]]),dict(native_steps=[0,0]),dict(native_steps=[]),dict(native_steps=None),
            dict(native_steps=[4]),dict(native_steps=[0],max_samples=1),dict(native_steps=[0],start_native_step=0),
            dict(start_native_step=True),dict(start_native_step=-1),dict(start_native_step=4),dict(start_native_step=0.5),
            dict(max_samples=True),dict(max_samples=0),dict(max_samples=26),dict(max_samples=1.5),
            dict(include_geometry='true'),dict(detail='bad'),dict(detail='full',include_geometry=False),
            dict(detail='summary',include_geometry=True)]
        for extra in invalid:
            with self.subTest(extra=extra),self.assertRaises(ValueError):inspect_trajectory(self.h,{**args,**extra},{})
        first=json.loads(inspect_trajectory(self.h,{**args,'max_samples':2},{})[0]['text'])
        self.assertEqual([s['native_step'] for s in first['samples']],[0,1])
        self.assertEqual(first['next_native_step'],2)
        last=json.loads(inspect_trajectory(self.h,{**args,'start_native_step':2},{})[0]['text'])
        self.assertEqual([s['native_step'] for s in last['samples']],[2,3])
        self.assertIsNone(last['next_native_step'])

    def test_overlay_contract_remains_independent_of_geometry_output(self):
        result=self.preview();args=dict(trajectory_ref=result['trajectory_ref'],native_steps=[1,3],camera_id='head')
        image=dict(type='inputImage',imageUrl='data:image/jpeg;base64,test')
        with patch('auriga.runtime.actions.inspection._trajectory_image',return_value=(dict(status='available'),[image])) as render:
            content=inspect_trajectory(self.h,args,{})
        self.assertEqual(content[1],image)
        self.assertEqual(render.call_args.args[-1],[1,3])
        self.assertIn('link_positions_m',render.call_args.args[1]['samples'][0]['arms']['left'])
        with self.assertRaisesRegex(ValueError,'one_to_three'):
            inspect_trajectory(self.h,{**args,'native_steps':[0,1,2,3]}, {})

    def test_nonactor_and_wrong_kind_artifacts_reject(self):
        data=path_fixture();data.update(identity=self.h.identity)
        for kind,scope,reason in [('candidate_trajectory','internal','access_scope_denied'),
                                  ('robot_state','actor','not_a_candidate_trajectory')]:
            ref=self.h.store.put_artifact(canonical(data).encode(),kind,'application/json',access_scope=scope)
            with self.subTest(kind=kind,scope=scope),self.assertRaisesRegex(ValueError,reason):
                inspect_trajectory(self.h,dict(trajectory_ref=ref),{})

    def test_old_preview_is_readable_but_not_overlaid_as_current(self):
        result=self.preview(); self.h.latest={**self.h.latest,'observation_id':'new'}
        info=json.loads(inspect_trajectory(self.h,dict(trajectory_ref=result['trajectory_ref'],camera_id='head'),{})[0]['text'])
        self.assertFalse(info['current']);self.assertEqual(info['overlay']['status'],'unavailable')
        self.assertIn('requires_current_preview',info['overlay']['reason'])

    def test_action_path_selector_cannot_change_candidate_preview_semantics(self):
        result=self.preview()
        with self.assertRaisesRegex(ValueError,'path_kind_requires_action_feedback'):
            inspect_trajectory(self.h,dict(trajectory_ref=result['trajectory_ref'],path_kind='measured'),{})
        self.assertEqual(self.robot.executions,0)

    def test_disabled_trajectory_and_privileged_artifacts_cannot_leak(self):
        result=self.preview()
        data=path_fixture(); data.update(identity=self.h.identity,object_poses={'hidden':1})
        ref=self.h.store.put_artifact(json.dumps(data).encode(),'candidate_trajectory','application/json')
        with self.assertRaisesRegex(ValueError,'privileged'):
            inspect_trajectory(self.h,dict(trajectory_ref=ref),{})
        self.h.config=self.h.config.with_overrides({'features':{'trajectory_preview':False}})
        with self.assertRaisesRegex(ValueError,'disabled'):inspect_trajectory(self.h,dict(trajectory_ref=result['trajectory_ref']),{})
