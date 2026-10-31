"""Geometric counterexamples for shared-timeline proxy diagnostics."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from auriga.robot.motion.batch_geometry import review_batch, validate_proxies, annotate_overlay


def fixture(left=(0.,.6),right=(0.,-.6),right_base=1.):
    joints=[]
    for index in range(1,7):
        joints.append(dict(name='joint'+str(index),type='prismatic' if index==1 else 'fixed',
            parent='base' if index==1 else 'link'+str(index-1),child='link'+str(index),
            T_parent_joint=np.eye(4).tolist(),axis=[1.,0.,0.]))
    model=dict(base_link='base',joints=joints)
    before=dict(arms={arm:dict(joint_names=['joint'+str(i) for i in range(1,7)],q_rad=[0.]*6,
        source_kind='measurement',ee_pose=dict(position_m=[base,0.,0.],quaternion_xyzw=[0.,0.,0.,1.],
            source_kind='measurement',frame_id='env_origin',tcp_config_id='arx-x5-link6-v1'),
        gripper=dict(joint_names=[],position_m=[],position_source_kind='measurement'))
        for arm,base in [('left',0.),('right',right_base)]})
    samples=[dict(native_step=i,arms={arm:dict(q_rad=[values[i],0.,0.,0.,0.,0.],jaw_goal_normalized=1.,phase='move')
        for arm,values in [('left',left),('right',right)]}) for i in range(len(left))]
    trajectory=dict(frame_id='env_origin',tcp_config_id='arx-x5-link6-v1',samples=samples)
    hardware=dict(model=model,spheres={'link6':[dict(center=[0.,0.,0.],radius=.02)]},sphere_buffer_m=0.,
        jaws=dict(gripper_scale=[0.,1.],gripper_move=dict(base='jaw',sign=1),gripper_joints_name=[]))
    return SimpleNamespace(root=Path('/no-task-assets')),before,trajectory,hardware


class BatchGeometryTests(unittest.TestCase):
    def review(self,inputs,proxies):
        adapter,before,trajectory,hardware=inputs
        with (patch('auriga.robot.motion.trajectory_evidence._hardware',return_value=hardware),
              patch('auriga.robot.model.robot_model.collision_mesh_bounds',side_effect=OSError('unavailable'))):
            return review_batch(adapter,before,trajectory,proxies)

    def test_both_moving_arms_cross_while_neither_reaches_peers_start(self):
        result=self.review(fixture(),{})
        self.assertEqual(result['status'],'risks_detected')
        self.assertTrue(any(r['kind']=='inter_arm_proxy_intersection' for r in result['risks']))
        self.assertLess(result['minimum_estimated_clearance_m'],0)
        self.assertIn('Both arms move',result['semantics'])

    def test_obstacle_between_native_endpoints_is_not_missed(self):
        result=self.review(fixture(left=(0.,1.),right=(0.,0.),right_base=2.),dict(obstacles=[
            dict(id='rim',min_m=[.49,-.01,-.01],max_m=[.51,.01,.01],margin_m=0.,evidence='observed rim')]))
        risks=[r for r in result['risks'] if r['obstacle']=='rim']
        self.assertTrue(risks)
        self.assertEqual(risks[0]['native_step'],1)
        self.assertLess(risks[0]['estimated_clearance_m'],0)

    def test_held_extent_moves_rigidly_and_can_hit_when_robot_does_not(self):
        result=self.review(fixture(left=(0.,.4),right=(0.,0.),right_base=2.),dict(
            obstacles=[dict(id='neighbor',min_m=[.68,-.02,-.02],max_m=[.72,.02,.02],margin_m=0.,evidence='visible neighbor')],
            held_objects={'left':dict(min_m=[.29,-.01,-.01],max_m=[.31,.01,.01],margin_m=0.,evidence='estimated held extent')}))
        self.assertTrue(any(r['moving_part']=='left/held_object' and r['obstacle']=='neighbor' for r in result['risks']))
        self.assertTrue(any('release' in text for text in result['unknown']))

    def test_contact_declarations_never_erase_intersection(self):
        proxies=dict(obstacles=[dict(id='target',min_m=[.49,-.01,-.01],max_m=[.51,.01,.01],evidence='visible')],
            contact_entity_id='target',initial_contact=dict(entity_ids=['target'],evidence='initial estimate'))
        result=self.review(fixture(left=(0.,1.),right=(0.,0.),right_base=2.),proxies)
        self.assertTrue(any(r.get('contact_declaration') for r in result['risks']))
        self.assertFalse(result['collision_free_proven'])

    def test_validated_proxies_are_idempotent_and_bad_references_reject(self):
        raw=dict(obstacles=[dict(id='box',min_m=[0,0,0],max_m=[1,1,1],evidence='seen')],
            held_objects={'left':dict(min_m=[0,0,0],max_m=[.1,.1,.1],evidence='seen')})
        checked=validate_proxies(raw)
        self.assertEqual(validate_proxies(checked),checked)
        with self.assertRaisesRegex(ValueError,'contact_entity'):
            validate_proxies(dict(raw,contact_entity_id='missing'))

    def test_overlay_preserves_panels_and_draws_world_and_held_proxies(self):
        camera=dict(T=np.eye(4),K=np.array([[100.,0.,80.],[0.,100.,80.],[0.,0.,1.]]),
            D=np.zeros(5),width=160,height=160)
        trajectory=dict(samples=[dict(native_step=i,arms={'left':dict(link6_pose=dict(position_m=[i*.2,0.,2.],
            rotation_matrix=np.eye(3).tolist()))}) for i in (0,1)],batch_geometry_review=dict(
                obstacles=[dict(id='box',min_m=[-.2,-.2,2.],max_m=[.2,.2,2.2])],
                held_objects={'left':dict(min_m=[-.1,-.1,1.9],max_m=[.1,.1,2.1])}))
        image=np.zeros((160,320,3),dtype=np.uint8)
        metadata=dict(panel_width=160,panel_height=160,panels=[dict(native_step=0),dict(native_step=1)])
        rendered,meta=annotate_overlay(camera,image,metadata,trajectory)
        self.assertEqual(rendered.shape,(160,320,3))
        self.assertTrue(rendered[:140].any())
        self.assertEqual(meta['proxy_overlay']['held_object_arms'],['left'])


if __name__=='__main__': unittest.main()
