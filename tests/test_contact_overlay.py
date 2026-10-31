"""Contact overlays use calibrated measured geometry, without object inference."""

from copy import deepcopy
import base64
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import cv2
import numpy as np

from auriga.perception.overlays.contact_overlay import render_contact_overlay
from auriga.core.presentation.actor_output import ActorOutput
from auriga.core.presentation.actor_presentation import ActorPresentation
from auriga.core.storage.images import image_settings
from auriga.runtime.actions.inspection import contact_overlay
from auriga.core.storage.store import Store
from auriga.core.tool_output import ToolResponse, wire_response


def camera_fixture():
    step, domain = 30, 'adapter_control_step'
    observation = dict(world_kind='observed', observation_id='observed', sim_step=step, step_domain=domain)
    frame = dict(camera_id='head', frame_id='head-frame', sim_step=step, step_domain=domain,
                 rgb_ref='original-actor-rgb', pixel_transform='identity')
    state = dict(sim_step=step, step_domain=domain, arms={})
    for side, x in (('left', .1), ('right', -.4)):
        state['arms'][side] = dict(ee_pose=dict(position_m=[x, -.05, 2.], quaternion_xyzw=[0.,0.,0.,1.],
            source_kind='measurement', frame_id='env_origin', tcp_config_id='arx-x5-link6-v1'))
    camera = dict(observation=observation, frame=frame, state=state, width=640, height=480,
                  K=np.array([[500.,0,320.],[0,500.,240.],[0,0,1.]]), D=np.zeros(5), T=np.eye(4),
                  reference_frame_id='env_origin', pose_source='fixed_calibration')
    image = np.zeros((480,640,3), dtype=np.uint8)
    image[:,:,0] = np.arange(640, dtype=np.uint16)[None,:] % 256
    return camera, image


class ContactOverlayTests(unittest.TestCase):
    def test_all_actual_reference_positions_and_pixels_match_x5_calibration(self):
        camera, image = camera_fixture()
        output, result = render_contact_overlay(camera, image, arms=['left'],
            selections={'left': {'contact_region': 'finger_middle'}})
        refs = result['arms']['left']['actual_references']
        for region, offset in (('pad_center', .1501), ('finger_middle', .1226), ('fingertip', .1576)):
            expected = [.1+offset, -.05, 2.]
            np.testing.assert_allclose(refs[region]['position_m'], expected, atol=1e-12)
            point = result['points'][refs[region]['point_index']]
            np.testing.assert_allclose(point['pixel_xy'], [320+250*(.1+offset), 227.5], atol=1e-10)
            self.assertEqual(point['source_kind'], 'derived_from_measured_link6')
            self.assertTrue(point['in_image'])
            self.assertEqual(point['occlusion'], 'not_evaluated')
        np.testing.assert_allclose(result['arms']['left']['actual_selected_position_m'], [.2226,-.05,2.])
        self.assertEqual(result['original_rgb_ref'], 'original-actor-rgb')
        self.assertEqual(result['observation_id'], 'observed')
        self.assertEqual(output.shape, image.shape)
        self.assertTrue(np.any(output != image))
        self.assertFalse(result['physics_advanced'])
        self.assertEqual((result['executed_native_steps'],result['executed_control_steps']), (0,0))
        json.dumps(result, allow_nan=False)

    def test_rotation_and_camera_transform_use_world_axis_and_optical_convention(self):
        camera, image = camera_fixture()
        camera['state']['arms']['left']['ee_pose']['quaternion_xyzw'] = [0.,0.,2**-.5,2**-.5]
        angle = .2
        camera['T'][:3,:3] = [[np.cos(angle),0,np.sin(angle)],[0,1,0],[-np.sin(angle),0,np.cos(angle)]]
        camera['T'][:3,3] = [.05,.02,.1]
        _, result = render_contact_overlay(camera, image, arms=['left'],
                                           selections={'left': {'contact_depth_m': .01}})
        own = result['arms']['left']
        np.testing.assert_allclose(own['actual_selected_position_m'], [.1,.0901,2.], atol=1e-12)
        np.testing.assert_allclose(own['actual_approach_axis_world'], [0,1,0], atol=1e-12)
        local = np.linalg.inv(camera['T']) @ np.r_[own['actual_selected_position_m'],1.]
        expected = camera['K'] @ (local[:3]/local[2])
        np.testing.assert_allclose(result['points'][own['actual_selected_point_index']]['pixel_xy'], expected[:2])
        arrow = result['arrows'][0]
        difference = np.array(result['points'][arrow['end']]['position_m'])-result['points'][arrow['start']]['position_m']
        np.testing.assert_allclose(difference, [0,.06,0], atol=1e-12)

    def test_requested_target_and_approach_are_separate_from_actual_measurements(self):
        camera, image = camera_fixture()
        _, result = render_contact_overlay(camera, image, arms=['right'],
            selections={'right': {'contact_region':'fingertip'}},
            targets={'right': {'position_m':[.3,.1,2.], 'approach_axis_world':[0,-2,0]}})
        own = result['arms']['right']
        np.testing.assert_allclose(own['actual_selected_position_m'], [-.2424,-.05,2.])
        self.assertEqual(own['requested_selected_position_m'], [.3,.1,2.])
        self.assertEqual(own['requested_approach_axis_world'], [0.,-1.,0.])
        point = result['points'][own['requested_point_index']]
        self.assertEqual(point['source_kind'], 'caller_request')
        self.assertIn('REQUESTED', point['label'])
        self.assertEqual(point['marker'], 'cross')
        self.assertIn('not a measured object', own['target_semantics'])
        self.assertIn('does not infer objects', result['limitation'])
        self.assertEqual([arrow['kind'] for arrow in result['arrows']], ['actual_approach','requested_approach'])

    def test_original_image_and_input_records_are_unchanged(self):
        camera, image = camera_fixture()
        before_image, before_camera = image.copy(), deepcopy(camera)
        selection={'left':{'contact_region':'finger_middle'}}
        target={'left':{'position_m':[.2,.1,2.]}}
        before_selection, before_target = deepcopy(selection), deepcopy(target)
        output, _ = render_contact_overlay(camera,image,selections=selection,targets=target)
        np.testing.assert_array_equal(image,before_image)
        self.assertFalse(np.shares_memory(output,image))
        for key in ('state','observation','frame'):
            self.assertEqual(camera[key],before_camera[key])
        for key in ('K','D','T'):
            np.testing.assert_array_equal(camera[key],before_camera[key])
        self.assertEqual(selection,before_selection)
        self.assertEqual(target,before_target)
        np.testing.assert_array_equal(output[:50,:50],image[:50,:50])

    def test_public_hook_returns_decodable_image_and_actor_scoped_provenance(self):
        camera,image=camera_fixture()
        identity=dict(run_id='overlay',episode_id='ep',env_instance_id='env',env_generation=1)
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'store',identity)
            try:
                ok,png=cv2.imencode('.png',image)
                self.assertTrue(ok)
                original=png.tobytes()
                reference=store.put_artifact(original,'rgb','image/png','actor')
                camera['frame']['rgb_ref']=reference
                harness=SimpleNamespace(lock=threading.RLock(),identity=identity,store=store,
                    geometry=SimpleNamespace(camera=Mock(return_value=camera)),
                    observation_profile='rgb_proprio_calibrated_v1',image_settings=image_settings(),
                    latest=dict(observation_id='observed'))
                # No robot/backend exists on this fixture: any motion access fails.
                content=contact_overlay(harness,dict(observation_id='observed',camera_id='head',arms=['left']),{},
                    structured=True)
                response=wire_response(ToolResponse(content))['contentItems']
                metadata=json.loads(response[0]['text'])
                self.assertTrue(metadata['current'])
                self.assertEqual(metadata['original_rgb_ref'],reference)
                self.assertEqual(response[1]['type'],'inputImage')
                uri=response[1]['imageUrl']
                self.assertTrue(uri.startswith('data:image/'))
                decoded=cv2.imdecode(np.frombuffer(base64.b64decode(uri.split(',',1)[1]),np.uint8),cv2.IMREAD_COLOR)
                self.assertEqual(decoded.shape,image.shape)
                self.assertEqual(store.resolve_image(reference,allowed_scopes=('actor',))[1],original)
                store.resolve_image(metadata['overlay_ref'],allowed_scopes=('actor',))
                actor=ActorOutput(store,ActorPresentation(store))
                shown=actor.present(ToolResponse(content))['contentItems']
                shown_metadata=json.loads(shown[0]['text'])
                for field in ('points','arrows','approach_arrow_length_m'):
                    self.assertEqual(shown_metadata[field],metadata[field])
                self.assertEqual(shown[1],response[1])
                detail=json.loads(store.resolve_artifact(actor.restore(shown_metadata['details_ref']))[1])
                self.assertEqual(detail['points'],metadata['points'])
                self.assertEqual(store.records('action_request'),[])
            finally:
                store.close()

    def test_behind_outside_and_zero_depth_are_not_drawn_as_visible(self):
        for position in ([.1,0.,-1.], [100.,0.,1.], [.1,0.,0.]):
            camera,image = camera_fixture()
            camera['state']['arms']['left']['ee_pose']['position_m']=position
            with self.subTest(position=position):
                _, result=render_contact_overlay(camera,image,arms=['left'])
                self.assertTrue(all(not point['in_image'] for point in result['points']))
                self.assertTrue(all(not arrow['drawn'] for arrow in result['arrows']))
                json.dumps(result,allow_nan=False)

    def test_internal_unsynchronized_predicted_or_wrong_tcp_data_is_rejected(self):
        mutations = [lambda c: c['observation'].update(world_kind='internal'),
            lambda c:c['state'].update(sim_step=29), lambda c:c['frame'].update(sim_step=29),
            lambda c:c['frame'].update(pixel_transform='cropped'),
            lambda c:c['state']['arms']['left']['ee_pose'].update(source_kind='prediction'),
            lambda c:c['state']['arms']['left']['ee_pose'].update(tcp_config_id='other_tcp'),
            lambda c:c['state']['arms']['left']['ee_pose'].update(frame_id='other_frame'),
            lambda c:c['state']['arms']['left']['ee_pose'].update(quaternion_xyzw=[0,0,0,2])]
        for index, mutate in enumerate(mutations):
            camera,image=camera_fixture()
            mutate(camera)
            with self.subTest(index=index),self.assertRaises(ValueError):
                render_contact_overlay(camera,image,arms=['left'])

    def test_bad_dimensions_arms_selections_and_targets_fail_explicitly(self):
        camera,image=camera_fixture()
        for args in (dict(arms=[]),dict(arms=['left','left']),dict(arms=['other']),
                     dict(selections={'left':{'contact_region':'middle'}}),
                     dict(selections={'left':{'contact_depth_m':True}}),
                     dict(selections={'left':{'contact_depth_m':.1}}),
                     dict(selections={'left':{'unexpected':1}}),
                     dict(targets={'left':{'position_m':[True,0,1]}}),
                     dict(targets={'left':{'position_m':[float('nan'),0,1]}}),
                     dict(targets={'left':{'position_m':[0,0,1],'approach_axis_world':[0,0,0]}}),
                     dict(targets={'left':{'position_m':[0,0,1],'angles_deg':[0,0,0]}})):
            with self.subTest(args=args),self.assertRaises(ValueError):
                render_contact_overlay(camera,image,**args)
        with self.assertRaisesRegex(ValueError,'size_mismatch'):
            render_contact_overlay(camera,image[:240])


if __name__ == '__main__':
    unittest.main()
