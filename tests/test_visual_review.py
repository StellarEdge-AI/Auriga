import json
import tempfile
import threading

import cv2
import numpy as np
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from auriga.cortex.session.visual_review import VisualReviewer, MODEL
from auriga.core.presentation.actor_presentation import ActorPresentation
from auriga.cortex.context import operation_verification
from auriga.core.storage.store import Store


class VisualReviewTest(unittest.TestCase):
    def test_operation_packet_hides_hashes_but_retains_private_execution_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory, dict(run_id='r', episode_id='e', env_instance_id='i', env_generation=1))
            self.addCleanup(store.close)
            rgb = store.put_artifact(cv2.imencode('.png', np.zeros((20, 20, 3), np.uint8))[1].tobytes(), 'rgb', 'image/png')
            source = dict(rgb_ref=rgb, width=20, height=20)
            fingerprint = 'a' * 64
            action_id = 'action-' + 'b' * 24
            execution = dict(status='completed', blockers=[], actions=[dict(action_id=action_id,
                request_sha256=fingerprint, receipt_sha256=fingerprint,
                requested=dict(policy_provenance=dict(checkpoint_sha256=fingerprint)),
                feedback=dict(status='completed', executed_steps=7))],
                after_robot_state=dict(sha256=fingerprint, robot_state_ref=rgb, state=dict(measured_position=[.1, .2, .3])))
            answer = {name: (spec['enum'][0] if 'enum' in spec else [] if spec['type'] == 'array' else 'Visible evidence')
                      for name, spec in operation_verification.ANSWER_SCHEMA['properties'].items()}
            answer['assessment'] = 'uncertain'
            reviewer = VisualReviewer(SimpleNamespace(model=MODEL))
            with patch('auriga.cortex.session.visual_review.comparison_inputs', return_value=(None, [source, source])), \
                    patch.object(operation_verification, 'execution_inputs', return_value=execution), \
                    patch.object(reviewer, '_request', return_value=dict(model=MODEL, status='completed', answer=answer,
                        thread_id='helper-thread', turn_id='helper-turn', context_policy='fresh', backend='sdk')) as request:
                returned = reviewer.review_operation(store, {}, phase='grasp', target='block ' + fingerprint,
                    expected_result='retained ' + fingerprint, destination='tray ' + fingerprint,
                    action_ids=[action_id], before_observation_id='before', after_observation_id='after', camera_id='head', roi=[1, 1, 4, 4])
            payload = request.call_args.args[0]
            prompt = payload['input'][0]['text']
            for private in (fingerprint, action_id, rgb, 'request_sha256', 'receipt_sha256', 'checkpoint_sha256'):
                self.assertNotIn(private, prompt)
            packet = json.loads(prompt.split('Recorded execution evidence (commands are labeled requested; robot states are measured): ')[1])
            self.assertEqual(packet['actions'][0]['feedback']['executed_steps'], 7)
            self.assertEqual(packet['after_robot_state']['state']['measured_position'], [.1, .2, .3])
            self.assertEqual(ActorPresentation(store).restore(packet)['actions'][0]['action_id'], action_id)
            self.assertEqual(returned['execution'], execution)
            event = store.events()[-1]['payload']
            for key, value in dict(agent_thread_id='helper-thread', agent_turn_id='helper-turn',
                                   context_policy='fresh', backend='sdk').items():
                self.assertNotIn(key, returned)
                self.assertEqual(event[key], value)
            self.assertEqual(event['execution']['actions'][0]['request_sha256'], fingerprint)
            self.assertEqual(event['image_encodings'][0]['source_sha256'], store.artifact(rgb)[0]['sha256'])
            archived = json.loads(store.resolve_artifact(event['request_ref'], ('observer',))[1])
            self.assertEqual(archived, payload)

    def test_only_actor_images_exact_model_and_private_usage(self):
        identity = dict(run_id='r',episode_id='e',env_instance_id='i',env_generation=1)
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory,identity)
            self.addCleanup(store.close)
            rgb = store.put_artifact(cv2.imencode('.png', np.zeros((20, 20, 3), np.uint8))[1].tobytes(),'rgb','image/png')
            sources = [dict(rgb_ref=rgb,width=20,height=20),dict(rgb_ref=rgb,width=20,height=20)]
            for index, name in enumerate(('before','after')):
                state = dict(sim_step=index, step_domain='fixture', arms=dict(left=dict(
                    source_kind='measurement', q_rad=[index * .1], qdot_rad_s=[index * .2], joint_names=['q1'],
                    ee_pose=dict(position_m=[index * .01, 0., 1.], quaternion_xyzw=[0., 0., 0., 1.], source_kind='measurement'),
                    gripper=dict(goal_normalized=0., goal_source_kind='command', position_m=[.02, .02],
                                 joint_names=['jaw1', 'jaw2'], position_source_kind='measurement'))))
                state_ref = store.put_artifact(json.dumps(state).encode(), 'robot_state', 'application/json')
                store.save('observation',name,dict(**identity, observation_id=name,observation_profile='rgb_proprio_calibrated_v1', robot_state_ref=state_ref,
                    sim_step=index,step_domain='fixture',cameras=[dict(camera_id='head',rgb_ref=rgb,width=20,height=20)]))
            client = SimpleNamespace(base_url='http://127.0.0.1:1',_token='test-secret',model=MODEL)
            reviewer = VisualReviewer(client)
            answer = dict(visibility_after='visible',motion='displaced',after_center_px=[10,10],retained_with_gripper='uncertain',evidence='Occluded fingers.')
            result = dict(model=MODEL,status='completed',usage=dict(output_tokens=80),answer=answer,
                          thread_id='helper-thread', turn_id='helper-turn', context_policy='fresh', backend='sdk')
            def response():
                return result
            args=dict(before_observation_id='before',after_observation_id='after',camera_id='head',roi=[1,1,4,4],target='visible cup')
            with patch.object(reviewer, '_request') as request:
                request.side_effect=lambda *a, **kw: response()
                returned=reviewer.review(store,identity,**args)
                self.assertEqual(returned['answer'],answer)
                self.assertNotIn('usage',returned)
                for key, value in dict(agent_thread_id='helper-thread', agent_turn_id='helper-turn',
                                       context_policy='fresh', backend='sdk').items():
                    self.assertNotIn(key, returned)
                    self.assertEqual(store.events()[-1]['payload'][key], value)
                self.assertEqual([source['observation_id'] for source in returned['sources']], ['before','after'])
                payload=request.call_args.args[0]
                self.assertEqual(payload['model'],MODEL)
                self.assertEqual(sum(p['type']=='image' for p in payload['input']),2)
                bound = [json.loads(part['text'].split('\n', 1)[1]) for part in payload['input']
                         if part['type'] == 'text' and part['text'].startswith(('BEFORE\n', 'AFTER\n'))]
                self.assertEqual([item['sim_step'] for item in bound], [0, 1])
                self.assertEqual([item['robot_state']['arms']['left']['q_rad'] for item in bound], [[0.], [.1]])
                self.assertEqual(returned['sources'][1]['observation_state']['robot_state']['arms']['left']['qdot_rad_s'], [.2])
                result['model']='substituted-model'
                with self.assertRaisesRegex(ValueError,'unverified'):
                    reviewer.review(store,identity,**args)

    def test_condition_needs_visible_target_and_failed_request_keeps_unknown_usage(self):
        identity = dict(run_id='r',episode_id='e',env_instance_id='i',env_generation=1)
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory,identity)
            self.addCleanup(store.close)
            rgb = store.put_artifact(cv2.imencode('.png', np.zeros((20, 20, 3), np.uint8))[1].tobytes(),'rgb','image/png')
            sources = [dict(rgb_ref=rgb,width=20,height=20)]*2
            reviewer = VisualReviewer(SimpleNamespace(base_url='http://127.0.0.1:1',_token='test-secret',model=MODEL))
            args=dict(before_observation_id='before',after_observation_id='after',camera_id='head',roi=[1,1,4,4],
                      target='visible cup',condition='released_from_gripper')
            answer = dict(visibility_after='not_visible',motion='uncertain',after_center_px=None,
                          retained_with_gripper='not_visually_supported',condition_assessment='met',evidence='Hidden target.')
            raw = dict(model=MODEL,status='completed',answer=answer)
            with patch('auriga.cortex.session.visual_review.comparison_inputs',return_value=(None,sources)), patch.object(reviewer,'_request',return_value=raw) as request:
                self.assertEqual(reviewer.review(store,identity,**args)['answer']['condition_assessment'],'uncertain')
                self.assertIn('absence of retention evidence alone', request.call_args.args[0]['input'][0]['text'])
                request.side_effect=TimeoutError('fixture')
                with self.assertRaises(TimeoutError):
                    reviewer.review(store,identity,**args)
            failure=store.events()[-1]
            self.assertEqual(failure['type'],'visual.model_review_failed')
            self.assertIsNone(failure['payload']['usage'])
            self.assertTrue(failure['payload']['request_id'])

    def test_cancel_event_is_forwarded_to_official_agent(self):
        from unittest.mock import Mock
        cancel = threading.Event()
        client = SimpleNamespace(model=MODEL, review_evidence=Mock(side_effect=TimeoutError("cancelled")))
        reviewer = VisualReviewer(client)
        with self.assertRaises(TimeoutError):
            reviewer._request(dict(input=[], outputSchema={}), "cancel-fixture", cancel_event=cancel, deadline=0)
        client.review_evidence.assert_called_once_with(input_items=[], output_schema={}, request_id="cancel-fixture", cancel_event=cancel)
