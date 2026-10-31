"""CPU-only epipolar math and formal RGB/typed boundary regressions."""
from copy import deepcopy
from dataclasses import replace
import hashlib
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
from auriga.runtime.tools.plugins.stereo_guide import _clip_line, _epipolar_line, make_stereo_guide_tool, read_stereo_guide
from auriga.core.storage.store import Store


def camera(xyz=(0., 0., 0.), angles=(0., 0., 0.), k=None):
    pose=np.eye(4);pose[:3,:3]=Rotation.from_euler('xyz',angles).as_matrix();pose[:3,3]=xyz
    return dict(T=pose,K=np.array(k if k is not None else [[90.,0.,49.],[0.,110.,39.],[0.,0.,1.]]),
                D=np.zeros(5),width=100,height=80)


def projected(cam, point):
    # Independent homogeneous projection; no production epipolar helper.
    p=np.linalg.inv(cam['T'])@np.r_[point,1.]
    uv=cam['K']@p[:3]
    return uv[:2]/uv[2]


class StereoGuideMathTests(unittest.TestCase):
    def test_horizontal_known_truth_and_reverse_order(self):
        first=camera();second=camera((.2,0.,0.))
        for a,b in ((first,second),(second,first)):
            line,ends=_epipolar_line(a,b,[49.,30.])
            np.testing.assert_allclose(line,[0.,1.,-30.],atol=1e-12)
            self.assertEqual(ends,[[0.,30.],[99.,30.]])

    def test_nonparallel_cameras_asymmetric_k_pointcloud_truth(self):
        first=camera((.1,.2,-.1),(.03,.08,-.05))
        second=camera((.4,.14,.02),(-.07,.02,.04),[[120.,2.,45.],[0.,85.,36.],[0.,0.,1.]])
        for point in ([.3,.2,2.],[.1,.4,3.],[-.1,-.2,4.]):
            for a,b in ((first,second),(second,first)):
                pixel=projected(a,point);matched=projected(b,point)
                line,ends=_epipolar_line(a,b,pixel.tolist())
                self.assertLess(abs(np.dot(line,np.r_[matched,1.])),1e-10)
                self.assertAlmostEqual(np.linalg.norm(line[:2]),1.)
                for end in ends:self.assertLess(abs(np.dot(line,np.r_[end,1.])),1e-10)
                self.assertGreater(abs(np.dot(line,np.r_[matched+np.array(line[:2])*5.,1.])),4.99)

    def test_vertical_diagonal_corner_clipping(self):
        self.assertEqual(_clip_line([1.,0.,-20.],100,80),[[20.,0.],[20.,79.]])
        ends=_clip_line([1.,-1.,0.],100,80)
        np.testing.assert_allclose(ends,[[0.,0.],[79.,79.]])
        for line in ([1.,1.,0.],[0.,1.,1.],[1.,0.,-100.]):
            with self.assertRaisesRegex(ValueError,'outside_target'):_clip_line(line,100,80)

    def test_baseline_and_epipole_degeneracy_rejected(self):
        with self.assertRaisesRegex(ValueError,'zero_baseline'):_epipolar_line(camera(),camera(),[49.,39.])
        with self.assertRaisesRegex(ValueError,'zero_baseline'):_epipolar_line(camera(),camera((1e-7,0.,0.)),[49.,39.])
        with self.assertRaisesRegex(ValueError,'epipole'):_epipolar_line(camera(),camera((0.,0.,.2)),[49.,39.])

    def test_no_image_intersection_rejected(self):
        target=camera((.2,0.,0.),k=[[90.,0.,49.],[0.,110.,200.],[0.,0.,1.]])
        with self.assertRaisesRegex(ValueError,'outside_target'):_epipolar_line(camera(),target,[49.,39.])

    def test_nonzero_nonfinite_distortion_and_invalid_pixel_rejected(self):
        for distortion in ([1e-20,0.,0.,0.,0.],[float('nan')],[[0.]]):
            with self.subTest(distortion=distortion),self.assertRaises(ValueError):
                _epipolar_line(dict(camera(),D=distortion),camera((.2,0.,0.)),[49.,39.])
        for pixel in ([float('inf'),2.],[float('nan'),2.],[True,2.],[-1.,2.],[100.,2.],[1.,80.],[1.,2.,3.]):
            with self.subTest(pixel=pixel),self.assertRaises(ValueError):_epipolar_line(camera(),camera((.2,0.,0.)),pixel)

    def test_invalid_transform_and_intrinsics_rejected(self):
        for transform in (np.diag([1.,1.,-1.,1.]),np.diag([2.,1.,1.,1.])):
            with self.assertRaises(ValueError):_epipolar_line(dict(camera(),T=transform),camera((.2,0.,0.)),[49.,39.])
        for k in ([[0.,0.,49.],[0.,110.,39.],[0.,0.,1.]],[[90.,90.,49.],[90.,90.,39.],[0.,0.,1.]]):
            with self.assertRaises(ValueError):_epipolar_line(dict(camera(),K=k),camera((.2,0.,0.)),[49.,39.])


class StereoGuideOwnerTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(prefix='stereo-guide-fixture-');self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name);self.identity=dict(run_id='fixture',episode_id='episode',env_instance_id='env',env_generation=1)
        self.store=Store(self.root/'run',self.identity);self.addCleanup(self.store.close)
        self.robot=SimpleNamespace(observe=Mock(side_effect=AssertionError('no new observation')),
            execute=Mock(side_effect=AssertionError('no action')),preview_batch=Mock(side_effect=AssertionError('no preview')))
        self.reader=Mock(side_effect=lambda args,corr:read_stereo_guide(self.h,args,corr))
        self.spec=make_stereo_guide_tool(self.reader)
        config=Config.from_dict(dict(features=dict(observation_profile='rgb_proprio_calibrated_v1',motion_review=False)))
        self.h=Harness(self.store,self.robot,config=config,tool_specs=(self.spec,));self.h.thread_id='fixture-thread'
        self.cameras=[camera(),camera((.2,0.,0.))];self.seq=0;self.save()
        self.args=dict(observation_id=self.h.latest['observation_id'],source_camera_id='source',target_camera_id='target',source_pixel=[49.,30.])
        self.addCleanup(self.no_execution)

    def save(self, *, identity=None, profile='rgb_proprio_calibrated_v1', rgb_scope='actor', rgb_kind='rgb', calibration_scope='actor',
             step=5, state_step=5, world_kind='observed', corrupt_rgb=False, frame_reference=None,
             distortion_model='pinhole_no_distortion', import_scene=True):
        self.seq+=1;oid='obs-'+str(self.seq)
        def artifact(value,kind,scope='actor'):return self.store.put_artifact(canonical(value).encode(),kind,'application/json',scope)
        state=dict(sim_step=state_step,step_domain='control_step',arms={})
        frames=[]
        for index,(name,cam) in enumerate(zip(('source','target'),self.cameras)):
            cal=dict(reference_frame_id=frame_reference if index==1 and frame_reference else 'env_origin',
                K=cam['K'].tolist(),distortion=cam['D'].tolist(),distortion_model=distortion_model,
                image_size=[100,80],T_world_camera=cam['T'].tolist())
            # Nonuniform RGB makes an unchanged original independently detectable.
            pixels=np.zeros((80,100,3),np.uint8);pixels[:,:,0]=np.arange(100,dtype=np.uint8);pixels[:,:,1]=index*90
            _,image=cv2.imencode('.png',pixels)
            frames.append(dict(camera_id=name,frame_id='frame-'+name+'-'+str(self.seq),width=100,height=80,
                sim_step=step,step_domain='control_step',calibration_ref=artifact(cal,'calibration',calibration_scope),
                rgb_ref=self.store.put_artifact(b'corrupt' if corrupt_rgb else image.tobytes(),rgb_kind,'image/png',rgb_scope),pixel_transform='identity'))
        bundle=dict(schema_version='0.1',**(identity or self.identity),world_kind=world_kind,observation_id=oid,
            observation_profile=profile,sim_step=step,step_domain='control_step',clock_domain='env/generation-1',
            calibration_id='fixture-calibration',robot_state_ref=artifact(state,'robot_state'),cameras=frames)
        self.store.save('observation',oid,bundle)
        if import_scene:self.h.scene.import_observation(bundle)
        self.h.latest=bundle
        if hasattr(self,'args'):self.args['observation_id']=oid
        return bundle

    def invoke(self,args=None,role='actor'):
        return self.h.invoke('stereo_guide',self.args if args is None else args,dict(tool_call_id='fixture-guide'),role=role)

    def no_execution(self):
        self.robot.observe.assert_not_called();self.robot.execute.assert_not_called();self.robot.preview_batch.assert_not_called()
        self.assertEqual(self.h.action_count,0)

    def test_actor_and_program_typed_result_and_new_overlays_preserve_originals(self):
        refs=[f['rgb_ref'] for f in self.h.latest['cameras']]
        before=[hashlib.sha256(self.store.resolve_artifact(ref)[1]).hexdigest() for ref in refs]
        actor=self.invoke();program=self.invoke(role='program')
        self.assertTrue(actor.success,actor.contract_error);self.assertEqual(actor.kind,'stereo_guide')
        # Content-addressed derived artifacts give identical typed results.
        self.assertEqual(actor.data,program.data);self.assertEqual(actor.data['status'],'guided')
        self.assertEqual(self.spec.effect_for(self.args),'read')
        np.testing.assert_allclose(actor.data['target_line'],[0.,1.,-30.],atol=1e-10)
        self.assertEqual(len(actor.content),3)
        for key in ('source_overlay_ref','target_overlay_ref'):
            info,blob=self.store.resolve_artifact(actor.data[key],allowed_scopes=('actor',))
            self.assertEqual(info['kind'],'geometry_overlay');self.assertNotIn(actor.data[key],refs)
            self.assertEqual(cv2.imdecode(np.frombuffer(blob,np.uint8),1).shape,(80,100,3))
        self.assertEqual(before,[hashlib.sha256(self.store.resolve_artifact(ref)[1]).hexdigest() for ref in refs])
        visible=json.loads(self.h.actor_output.present(actor)['contentItems'][0]['text'])
        self.assertNotIn('sha256',json.dumps(visible));self.assertNotIn(str(self.root),json.dumps(visible))
        self.assertNotEqual(visible['source_overlay_ref'],actor.data['source_overlay_ref'])

    def test_stale_active_and_stop_gates_reject_without_derived_writes(self):
        old=dict(self.args);self.save()
        for args,active,accepting in ((old,None,True),(self.args,'active',True),(self.args,None,False)):
            self.h.active_action=active;self.h.accepting=accepting
            with patch.object(self.store,'put_artifact',side_effect=AssertionError('no writes')):
                with self.assertRaisesRegex(ValueError,'current_idle'):self.invoke(args)

    def test_final_currentness_race_rejects_before_derived_write(self):
        original=self.h.geometry.camera
        def changed(*args,**kwargs):
            result=original(*args,**kwargs);self.h.accepting=False;return result
        with patch.object(self.h.geometry,'camera',side_effect=changed),patch.object(self.store,'put_artifact',side_effect=AssertionError('no writes')):
            with self.assertRaisesRegex(ValueError,'current_idle'):self.invoke()

    def test_scope_profile_identity_kind_and_sync_rejections(self):
        variants=[dict(rgb_scope='observer',import_scene=False),dict(calibration_scope='observer',import_scene=False),dict(rgb_kind='geometry_overlay'),
                  dict(profile='rgb_proprio_v1',import_scene=False),dict(identity=dict(self.identity,episode_id='other')),
                  dict(state_step=4),dict(frame_reference='other'),dict(corrupt_rgb=True)]
        for variant in variants:
            with self.subTest(variant=variant):
                self.save(**variant)
                with patch.object(self.store,'put_artifact',side_effect=AssertionError('no writes')):
                    with self.assertRaises(ValueError):self.invoke()

    def test_same_unknown_camera_and_bad_arguments_rejected(self):
        variants=[dict(target_camera_id='source'),dict(source_camera_id='unknown'),dict(source_pixel=[1.,2.,3.]),
                  dict(source_pixel=[float('nan'),2.]),dict(source_camera_id=''),dict(path='/private/no')]
        for variant in variants:
            with self.subTest(variant=variant),self.assertRaises(ValueError):self.invoke(dict(self.args,**variant))

    def test_nonzero_distortion_fails_without_images(self):
        self.cameras[1]['D'][0]=.001;self.save()
        with patch.object(self.store,'put_artifact',side_effect=AssertionError('no writes')):
            with self.assertRaisesRegex(ValueError,'zero_distortion'):self.invoke()

    def test_nonpinhole_model_with_zero_coefficients_is_rejected(self):
        self.save(distortion_model='fisheye')
        with patch.object(self.store,'put_artifact',side_effect=AssertionError('no writes')):
            with self.assertRaisesRegex(ValueError,'zero_distortion'):self.invoke()

    def test_real_program_broker_receives_typed_images_and_no_action(self):
        actor=self.invoke();visible=json.loads(self.h.actor_output.present(actor)['contentItems'][0]['text'])
        # Typed broker parity is independent of the real-kernel isolation suite.
        runner=self.h.programs=ProgramRunner(replace(self.h.program_services(),sandbox_enabled=False));self.addCleanup(runner.close)
        source=('from auriga_client import AurigaClient\n'
            'reply=AurigaClient().call("stereo_guide",'+repr(self.args)+')\n'
            'assert reply["success"],reply\nassert reply["kind"]=="stereo_guide"\n'
            'assert reply["data"]=='+repr(visible)+'\nprint("guide-only")\n')
        result=runner.run(dict(source=source,timeout_s=10,max_calls=1),
            dict(thread_id=self.h.thread_id,turn_id='fixture-turn',tool_call_id='fixture-program'))
        self.assertEqual(result['status'],'succeeded',result.get('stderr_text'))
        self.assertIn('guide-only',result['stdout_text']);self.assertEqual(result['action_summary']['action_calls'],0)
        self.assertIsNone(result['final_observation_id'])


if __name__=='__main__':unittest.main()
