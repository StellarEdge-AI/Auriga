"""Selective delivery never removes archived or explicitly requested evidence."""
import json
import unittest
from unittest.mock import patch, Mock
from PIL import Image

import test_dual_arm_interface as base
from auriga.core.config import Config, cli_overrides
from scripts.benchmark_queue import command
import test_benchmark_queue as queue_fixture


class ImageFeedbackDeliveryTests(unittest.TestCase):
    def setUp(self):
        base.DualArmHarnessTests.setUp(self)
        original = self.robot.observe
        self.paths = {}
        for camera in ('cam_head', 'cam_left_wrist', 'cam_right_wrist'):
            path = self.robot.path.parent / (camera+'.png')
            Image.new('RGB', (160,120), (70,70,70)).save(path)
            self.paths[camera] = path
        def observe():
            raw = original()
            raw['cameras'] = [dict(camera_id=key,png_path=str(path),width=160,height=120)
                              for key,path in self.paths.items()]
            return raw
        self.robot.observe = observe
        self.h.image_settings['feedback_policy'] = 'adaptive'
        self.first = self.h.observe()
        # Geometry validation itself has dedicated tests; this fixture has synthetic calibration.
        self.h.geometry.camera = Mock(return_value={})
        self.action = dict(path=dict(waypoints=[{'left':{}}]),gripper_events=[])
        self.receipt = dict(status='completed',reason=None)

    def deliver(self, bundle=None, **kwargs):
        return self.h.observation_content(bundle or self.h.latest, {},
            feedback_action=self.action,feedback_receipt=self.receipt, **kwargs)

    @staticmethod
    def count(content):
        return sum(item['type']=='inputImage' for item in content)

    @staticmethod
    def image_metadata(response):
        return next(value['image_feedback'] for item in response['contentItems']
                    if item['type'] == 'inputText'
                    for value in [json.loads(item['text'])]
                    if 'image_feedback' in value)

    def test_initial_full_then_all_unchanged_omission_and_explicit_retrieval(self):
        self.assertEqual(self.count(self.h.observation_content(self.first, {})), 3)
        current = self.h.observe()
        content = self.deliver(current)
        self.assertEqual(self.count(content),0)
        metadata = json.loads(content[0]['text'])['image_feedback']
        self.assertEqual(metadata['omitted_camera_ids'],list(self.paths))
        self.assertEqual([item['baseline_observation_id'] for item in metadata['omitted_views']],
                         [self.first['observation_id']]*3)
        self.assertEqual(len(self.store.read('observation',current['observation_id'])['cameras']),3)
        before = self.robot.executions
        response = self.h.dispatch('history_view_observation',dict(observation_id=current['observation_id'],
            camera_id='cam_right_wrist'),{'tool_call_id':'retrieve'})
        self.assertEqual(self.count(response['contentItems']),1)
        self.assertEqual(self.robot.executions,before)
        self.assertEqual(self.h._last_delivered_views['cam_right_wrist']['observation_id'],current['observation_id'])

    def test_program_internal_and_crop_do_not_advance_model_baseline(self):
        self.h.observation_content(self.first,{})
        new = self.h.observe()
        self.assertEqual(self.count(self.h.observation_content(new, {'program_id':'internal'},
            feedback_action=self.action,feedback_receipt=self.receipt)),3)
        self.h.observation_content(new,{},camera_id='cam_right_wrist',roi=[0,0,20,20])
        self.assertEqual(self.h._last_delivered_views['cam_right_wrist']['observation_id'],self.first['observation_id'])
        self.deliver(new)
        self.assertEqual(self.h._last_delivered_views['cam_right_wrist']['observation_id'],self.first['observation_id'])

    def test_changed_view_included_and_failures_or_checkpoints_include_all(self):
        self.h.observation_content(self.first,{})
        Image.new('RGB',(160,120),(100,70,70)).save(self.paths['cam_right_wrist'])
        self.assertEqual(self.count(self.deliver(self.h.observe())),1)
        for status,reason in [('failed','tracking_error'),('stopped','observation_checkpoint'),('unknown',None)]:
            self.receipt=dict(status=status,reason=reason)
            self.assertEqual(self.count(self.deliver(self.h.observe())),3)
        self.receipt=dict(status='completed',reason=None)
        self.action={}
        self.assertEqual(self.count(self.deliver(self.h.observe())),0)
        self.receipt=dict(status='completed',execution_uncertain=True)
        self.assertEqual(self.count(self.deliver(self.h.observe())),3)

    def test_selector_error_preserves_completed_receipt_and_full_rgb(self):
        with patch('auriga.perception.view_selection.select_observation_views',side_effect=ValueError('bad comparison')):
            self.assertEqual(self.count(self.deliver()),3)

    def test_action_uncertainty_is_full_for_top_level_arm_and_jaw_evidence(self):
        self.h.observation_content(self.first, {})
        for updates in (dict(error='observation uncertainty'), dict(execution_uncertain=True),
                        dict(stop_confirmed=False), dict(stop_confirmation=False),
                        dict(stop_confirmation=dict(confirmed=False)),
                        dict(arm_results=dict(left=dict(execution_uncertain=True))),
                        dict(gripper_execution=dict(right=dict(execution_uncertain=True)))):
            with self.subTest(updates=updates):
                self.receipt = dict(status='completed', **updates)
                self.assertEqual(self.count(self.deliver(self.h.observe())), 3)

    def test_default_all_has_no_filter_and_numeric_feedback_has_no_images(self):
        self.assertEqual(Config().images.feedback_policy,'all')
        self.h.observation_content(self.first,{})
        self.h.image_settings['feedback_policy']='all'
        content=self.deliver(self.h.observe())
        self.assertEqual(self.count(content),3)
        self.assertNotIn('image_feedback',json.loads(content[0]['text']))
        self.assertEqual(self.count(self.deliver(numeric_only=True)),0)

    def test_action_dispatch_passes_real_canonical_request_to_selector(self):
        self.h.observation_content(self.first,{})
        original=self.robot.execute
        def execute(*args,**kwargs):
            result=original(*args,**kwargs); result['reason']=None; return result
        self.robot.execute=execute
        self.h.capabilities['bounded_wait']=dict(max_native_steps=50)
        result=self.h.dispatch('robot_wait',dict(observation_id=self.h.latest['observation_id'],
            native_steps=5),{'tool_call_id':'motion'})
        self.assertEqual(self.count(result['contentItems']),0)
        receipt=json.loads(result['contentItems'][0]['text'])['receipt']
        self.assertEqual(receipt['status'],'completed')
        self.assertEqual(self.robot.executions,1)

    def test_config_and_queue_option_are_validated(self):
        cfg=Config().with_overrides(cli_overrides({'image_feedback_policy':'adaptive'}))
        self.assertEqual(cfg.images.feedback_policy,'adaptive')
        fixture=queue_fixture.QueueCheck(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        argv=command(dict(fixture.case,image_feedback_policy='adaptive'),1,config=Config())
        self.assertEqual(argv[argv.index('--image-feedback-policy')+1],'adaptive')
        for invalid in ('none', True, None):
            with self.assertRaises(ValueError):
                command(dict(fixture.case,image_feedback_policy=invalid),1,config=Config())

    def test_compaction_marker_and_new_session_invalidate_previously_seen_views(self):
        self.h.observation_content(self.first,{})
        self.assertEqual(self.count(self.deliver(self.h.observe())),0)
        self.store.set_state('runtime','image_context_epoch','compact-1')
        self.assertEqual(self.count(self.deliver(self.h.observe())),3)
        self.assertEqual(self.count(self.deliver(self.h.observe())),0)
        self.store.set_state('runtime','codex',{'app_server_instance_id':'replacement'})
        self.assertEqual(self.count(self.deliver(self.h.observe())),3)

    def test_failed_actor_projection_restores_full_views(self):
        self.h.thread_id = 'image-projection-failure'
        for method in ('_present_part', 'present'):
            with self.subTest(method=method):
                self.h.observation_content(self.h.latest, {})
                self.assertEqual(self.count(self.deliver(self.h.observe())), 0)
                params = dict(threadId=self.h.thread_id, turnId='turn', callId='failed-' + method,
                              tool='robot_observe', arguments={})
                with patch.object(self.h.actor_output, method,
                                  side_effect=ValueError('synthetic image display failure')):
                    if method == 'present':
                        with self.assertRaisesRegex(ValueError, 'synthetic image display failure'):
                            self.h.handle_tool(params)
                    else:
                        self.assertFalse(self.h.handle_tool(params)['success'])
                marker = self.store.read_state('runtime', 'image_context_epoch')
                self.assertEqual(marker['source'], 'tool_output_failed')
                self.assertEqual(marker['tool_call_id'], params['callId'])
                self.assertEqual(self.count(self.deliver(self.h.observe())), 3)
        self.assertEqual(self.robot.executions, 0)

    def test_invalid_calibration_keeps_inactive_view(self):
        self.h.observation_content(self.first,{})
        self.h.geometry.camera.side_effect=ValueError('missing_calibration')
        self.assertEqual(self.count(self.deliver(self.h.observe())),3)

    def test_compaction_invalidates_before_asynchronous_proxy_callback(self):
        from auriga.cortex.session.model_proxy import ModelRequestProxy
        events=[]
        proxy=ModelRequestProxy('http://127.0.0.1:1',on_event=events.append)
        self.h.model_proxy=proxy
        self.h.observation_content(self.first,{})
        self.assertEqual(self.count(self.deliver(self.h.observe())),0)
        proxy._record('model_call_started',endpoint='/responses/compact',logical_call_id='compact-sync')
        self.assertEqual(events,[])
        self.assertIsNone(self.store.read_state('runtime','image_context_epoch'))
        self.assertEqual(self.count(self.deliver(self.h.observe())),3)

    def test_native_window_compaction_events_restore_all_views_without_compact_endpoint(self):
        from auriga.cortex.session.codex import CodexSession
        session=CodexSession(self.store.root,self.store,lambda value:{})
        self.addCleanup(session.close)
        session.thread_id='actor-thread'
        self.h.observation_content(self.first,{})
        self.assertEqual(self.count(self.deliver(self.h.observe())),0)
        for phase in ('item/started','item/completed'):
            # Actual October experiment event shape; this path calls /responses.
            session._notification(dict(method=phase,params=dict(threadId='actor-thread',
                turnId='actor-turn',item=dict(type='contextCompaction',id='window-compact'))))
            marker=self.store.read_state('runtime','image_context_epoch')
            self.assertEqual(marker['source'],'native_context_compaction')
            self.assertEqual(marker['phase'],phase)
            self.assertEqual(self.count(self.deliver(self.h.observe())),3)
            self.assertEqual(self.count(self.deliver(self.h.observe())),0)

    def test_other_thread_compaction_and_ordinary_items_do_not_reset_actor_views(self):
        from auriga.cortex.session.codex import CodexSession
        session=CodexSession(self.store.root,self.store,lambda value:{})
        self.addCleanup(session.close)
        session.thread_id='actor-thread'
        self.h.observation_content(self.first,{})
        for thread,kind in [('background-thread','contextCompaction'),('actor-thread','reasoning')]:
            session._notification(dict(method='item/started',params=dict(threadId=thread,
                turnId='turn',item=dict(type=kind,id='not-actor-compaction'))))
            self.assertIsNone(self.store.read_state('runtime','image_context_epoch'))
            self.assertEqual(self.count(self.deliver(self.h.observe())),0)
