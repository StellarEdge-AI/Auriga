"""Evidence uses robot metadata and public measurements, never environment state."""
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import numpy as np
from auriga.robot.model.geometry import fk
from auriga.robot.motion.trajectory_evidence import build_trajectory_evidence, compact_trajectory_evidence, nominal_timeline


def hardware():
    joints=[]
    for i in range(1,7):
        t=np.eye(4);t[0,3]=.1
        joints.append(dict(name=f'joint{i}',type='revolute',parent='base_link' if i==1 else f'link{i-1}',
                           child=f'link{i}',axis=[0,0,1],T_parent_joint=t.tolist()))
    for i in (7,8):
        joints.append(dict(name=f'joint{i}',type='prismatic',parent='link6',child=f'link{i}',
                           axis=[0,1 if i==7 else -1,0],T_parent_joint=np.eye(4).tolist()))
    return dict(model=dict(base_link='base_link',joints=joints,source_sha256='public-fixture'),
        spheres={f'link{i}':[dict(center=[0,0,0],radius=.03)] for i in range(1,7)},sphere_buffer_m=0,
        jaws=dict(gripper_scale=[-.01,.044],gripper_move=dict(base='joint7',sign=1,mimic=['joint8',1.,0.]),
                  gripper_joints_name=['joint7','joint8']),provenance={'kind':'public-hardware-fixture'})


def public_fixture(separation=3.):
    h=hardware();state={'arms':{}}
    for arm,base in [('left',0),('right',separation)]:
        state['arms'][arm]=dict(source_kind='measurement',joint_names=[f'joint{i}' for i in range(1,7)],q_rad=[0.]*6,
            ee_pose=dict(frame_id='env_origin',tcp_config_id='arx-x5-link6-v1',source_kind='measurement',
                         position_m=[base+.6,0,0],quaternion_xyzw=[0,0,0,1]),
            gripper=dict(joint_names=['joint7','joint8'],position_source_kind='measurement',position_m=[.044,.044]))
    adapter=SimpleNamespace(control_hz=25,last_dispatched_goals={'left':1.,'right':1.})
    row=[0.]*6+[1.]+[0.]*6+[1.]
    compiled={a:(np.zeros((2,6)),[list(row),list(row)],[],None) for a in ('left','right')}
    action=dict(frame_id='env_origin',tcp_config_id='arx-x5-link6-v1')
    return adapter,state,compiled,action,h


class TrajectoryEvidenceTests(unittest.TestCase):
    def test_public_fk_bases_and_model_near_approach_have_witness_not_collision_claim(self):
        for separation,expected in [(3.,'no_near_approach_at_sampled_boundaries'),(.5,'model_near_approach')]:
            adapter,state,compiled,action,h=public_fixture(separation)
            # No env, articulation or planner attribute exists on this adapter.
            with patch('auriga.robot.motion.trajectory_evidence._hardware',return_value=h):
                short,full=build_trajectory_evidence(adapter,state,compiled,'paired',action)
            self.assertEqual(short['geometry']['status'],'available')
            self.assertEqual(short['inter_arm_proximity']['status'],expected)
            self.assertTrue(short['inter_arm_proximity']['missing_finger_coverage'])
            self.assertAlmostEqual(full['T_world_bases']['right'][0][3],separation)
            self.assertAlmostEqual(full['samples'][0]['arms']['left']['link_positions_m']['link6'][0],.6)
            self.assertIn('not actual collision',short['inter_arm_proximity']['semantics'])
            self.assertEqual(len(full['samples']),3)

    def test_missing_relative_calibration_returns_unknown_without_fallback(self):
        adapter,state,compiled,action,h=public_fixture()
        state['arms']['right']['ee_pose']['frame_id']='right_local_unregistered'
        with patch('auriga.robot.motion.trajectory_evidence._hardware',return_value=h):
            short,full=build_trajectory_evidence(adapter,state,compiled,'paired',action)
        self.assertEqual(short['geometry']['status'],'unknown')
        self.assertEqual(short['inter_arm_proximity']['status'],'unknown')
        self.assertNotIn('link_positions_m',full['samples'][0]['arms']['left'])
        self.assertNotIn('link6_pose',full['samples'][0]['arms']['left'])
        self.assertEqual(short['anomalies'][0]['kind'],'public_geometry_unknown')

    def test_missing_spheres_preserves_fk_but_proximity_unknown(self):
        adapter,state,compiled,action,h=public_fixture();h['spheres']=None
        with patch('auriga.robot.motion.trajectory_evidence._hardware',return_value=h):
            short,full=build_trajectory_evidence(adapter,state,compiled,'paired',action)
        self.assertEqual(short['geometry']['status'],'available')
        self.assertEqual(short['inter_arm_proximity']['status'],'unknown')
        self.assertIn('link_positions_m',full['samples'][-1]['arms']['left'])
        self.assertEqual(short['anomalies'][0]['kind'],'inter_arm_proximity_unknown')

    def test_compact_evidence_bounds_dense_near_path_and_preserves_exact_artifact(self):
        adapter,state,compiled,action,h=public_fixture(.5)
        for arm in compiled:
            positions,motion,jaws,schedule=compiled[arm]
            compiled[arm]=(positions,motion*40,jaws,schedule)
        with patch('auriga.robot.motion.trajectory_evidence._hardware',return_value=h):
            short,full=build_trajectory_evidence(adapter,state,compiled,'paired',action)
        original=deepcopy(full)
        self.assertEqual(short,compact_trajectory_evidence(full))
        self.assertEqual(full,original)
        self.assertLessEqual(len(short['key_poses']),6)
        self.assertEqual(short['q_unit'],'rad')
        self.assertEqual(short['joint_names'],full['joint_names'])
        self.assertEqual(len(full['samples']),81)
        self.assertEqual(short['inter_arm_proximity']['near_boundary_count'],81)
        self.assertEqual(full['inter_arm_proximity']['near_native_steps'],list(range(81)))
        self.assertEqual(short['anomalies'],[dict(kind='model_near_approach',native_step=0)])
        for forbidden in ('link_positions_m','center_m','radius_m','near_native_steps','samples'):
            self.assertNotIn('"'+forbidden+'"',json.dumps(short))
        for selected in short['key_poses']:
            for arm in ('left','right'):
                self.assertEqual(selected['arms'][arm]['q_rad'],full['samples'][selected['native_step']]['arms'][arm]['q_rad'])
        pose=short['key_poses'][0]['arms']['left']['link6_pose']
        self.assertEqual(pose,dict(position_m=[.6,0.,0.],rotation_matrix=np.eye(3).tolist()))
        self.assertIn('center_m',full['inter_arm_proximity']['witness']['left'])
        self.assertEqual(full['samples'][0]['arms']['left']['q_rad'],state['arms']['left']['q_rad'])


if __name__=='__main__':unittest.main()
