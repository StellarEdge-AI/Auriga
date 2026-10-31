"""Stage labels carry action identity, never permission to publish hidden state."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from auriga.core.config import Config
from auriga.core.contracts import canonical
from auriga.runtime.harness import Harness
from auriga.apps.gateway.workbench import Workbench
from auriga.core.observation_policy import (ACTOR_PROFILE, LEGACY_ACTOR_PROFILE,
    DEPTH_ACTOR_PROFILE, reject_privileged)
from auriga.cortex.context.operation_verification import execution_inputs
from auriga.runtime.programs.runner import ProgramRunner
from auriga.core.storage.store import Store
from test_harness import FixtureRobot
from test_joint_replan_runtime import periodic_pose
from test_motion_preview_backend import preview_adapter
import test_runtime_lifecycle


class StageLabelPolicyTests(unittest.TestCase):
    def receipt(self):
        return dict(status='stopped', reason='observation_checkpoint', executed_native_steps=1,
            stage_results={'depth': dict(kind='observe', status='completed', completed_steps=0)},
            checkpoint_stage_ids=['depth'], checkpoint_reasons={'depth': 'Inspect visible insertion'})

    def test_only_bound_top_level_labels_are_exempt_and_values_remain_checked(self):
        value = self.receipt()
        original = deepcopy(value)
        for profile in (ACTOR_PROFILE, LEGACY_ACTOR_PROFILE, DEPTH_ACTOR_PROFILE):
            with self.subTest(profile=profile):
                reject_privileged(value, profile, stage_ids=['depth'])
                reject_privileged({'stages': value['stage_results']}, profile, stage_ids=['depth'])
                reject_privileged({
                    'stage_costs': {'depth': dict(arm='left', kind='observe', start_native_step=1, end_native_step=1, native_steps=0)},
                    'checkpoints': [dict(cursor=1, stage_ids=['depth'], reasons={'depth': 'Inspect visible insertion'})],
                }, profile, stage_ids=['depth'])
                with self.assertRaisesRegex(ValueError, 'privileged_data'):
                    reject_privileged(value, profile)
                with self.assertRaisesRegex(ValueError, 'does_not_match'):
                    reject_privileged(value, profile, stage_ids=['reach'])
                for hidden in ({'depth': [[1.2]]}, {'evaluator_state': {'score': 100}},
                               {'stage_results': {'depth': {'kind': 'observe'}}}):
                    bad = deepcopy(value)
                    bad['stage_results']['depth'].update(hidden)
                    with self.subTest(hidden=hidden), self.assertRaisesRegex(ValueError, 'privileged_data'):
                        reject_privileged(bad, profile, stage_ids=['depth'])
                with self.assertRaisesRegex(ValueError, 'invalid_action_stage_map_value'):
                    reject_privileged({'stage_results': {'depth': [[1.2]]}}, profile, stage_ids=['depth'])
                with self.assertRaisesRegex(ValueError, 'privileged_data'):
                    reject_privileged({'extra': value}, profile, stage_ids=['depth'])
        self.assertEqual(value, original)


class StageLabelExecutionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name) / 'run', dict(run_id='labels', episode_id='ep',
            env_instance_id='env', env_generation=1))
        self.addCleanup(self.store.close)
        self.adapter, _, self.dispatched = preview_adapter()
        robot = FixtureRobot(temporary.name)
        observation = robot.observe
        robot.observe = lambda: dict(observation(), robot_state=self.adapter.state(),
            sim_step=self.adapter.control_step, camera_calibration=dict(reference_frame_id='env_origin', cameras={}),
            camera_pose_sample=dict(cameras={}), robot_model=self.adapter.robot_model)
        self.adapter.observe = robot.observe
        robot.start = lambda: dict(self.adapter.capabilities(), observer_video=False)
        robot.preview_batch = self.adapter.preview_batch
        robot.execute = lambda action, progress_callback, cancel_flag: self.adapter.execute(action, progress_callback, cancel_flag)
        self.h = Harness(self.store, robot, config=Config.from_dict(dict(features=dict(
            motion_review=False, completion_guidance='none'))))
        self.h.start()
        self.h.thread_id = 'thread'
        self.args = dict(mode='direct', observation_id=self.h.latest['observation_id'], action_id='label-action', timeout_s=10,
            stages=[dict(id='reach', kind='move', arm='left', **periodic_pose([-.025])),
                    dict(id='depth', kind='observe', arm='left', reason='Inspect visible insertion')])

    def check_checkpoint(self):
        receipt = self.store.read('action_receipt', 'label-action')
        self.assertEqual((receipt['status'], receipt['reason']), ('stopped', 'observation_checkpoint'))
        self.assertEqual(receipt['stage_results']['depth']['status'], 'completed')
        self.assertEqual(receipt['executed_native_steps'], 1)
        self.assertEqual(len(self.dispatched), 1)
        self.assertEqual(self.h.native_action_count, 1)
        self.assertTrue(self.h.accepting)
        self.assertIsNotNone(self.h.latest)
        return receipt

    def test_actor_checkpoint_replay_and_archived_artifact(self):
        params = dict(threadId='thread', turnId='turn', callId='depth-check', tool='robot_action', arguments=self.args)
        response = self.h.handle_tool(params)
        self.assertTrue(response['success'])
        presented = json.loads(response['contentItems'][0]['text'])
        self.assertEqual(presented['stages']['depth']['status'], 'completed')
        self.assertEqual(presented['receipt']['executed_native_steps'], 1)
        receipt = self.check_checkpoint()
        action = self.store.read('action_request', 'label-action')
        execution = execution_inputs(self.store, self.h.identity, action_ids=['label-action'],
            before_observation_id=action['based_on_observation_id'],
            after_observation_id=receipt['result_observation_id'])
        self.assertEqual(execution['actions'][0]['feedback']['executed_native_steps'], 1)
        self.assertEqual(execution['status'], 'incomplete')
        self.assertEqual(self.h.execute(action)['receipt'], receipt)
        self.assertEqual(len(self.dispatched), 1)
        runner = ProgramRunner(self.h.program_services())
        self.addCleanup(runner.close)
        entry = dict(cancel=threading.Event(), deadline=time.monotonic()+30, call_lock=threading.RLock())
        result = dict(request=action, receipt=receipt, action_id='label-action')
        reference = presented['details_ref']
        self.assertIn('data_base64', runner.dispatch(entry, {'artifact_ref': reference}))
        evidence = dict(action_id='label-action', **receipt)
        reference = self.store.put_artifact(canonical([evidence]).encode(), 'program_action_evidence', 'application/json')
        self.assertIn('data_base64', runner.dispatch(entry, {'artifact_ref': reference}))
        for bad in (dict(result, request=dict(action, stages=[dict(id='evaluator_state')])),
                    dict(result, receipt=dict(receipt, stage_results={'depth': {'depth': [1.2]}}))):
            reference = self.store.put_artifact(canonical(bad).encode(), 'unified_action_result', 'application/json')
            with self.assertRaises(ValueError):
                runner.dispatch(entry, {'artifact_ref': reference})
        reference = self.store.put_artifact(canonical(receipt).encode(), 'computed', 'application/json')
        with self.assertRaisesRegex(ValueError, 'privileged_data'):
            runner.dispatch(entry, {'artifact_ref': reference})

    def test_program_checkpoint_keeps_typed_step_evidence(self):
        runner = self.h.programs = ProgramRunner(self.h.program_services())
        self.addCleanup(runner.close)
        source = ('from auriga_client import AurigaClient\n'
                  'client = AurigaClient()\n'
                  'reply = client.call("robot_action", ' + repr(self.args) + ')\n'
                  'assert reply["success"], reply\n')
        job = runner.run(dict(source=source, timeout_s=30, allow_direct_actions=True),
            dict(thread_id='thread', turn_id='turn', tool_call_id='depth-program'))
        self.assertEqual(job['status'], 'succeeded', job.get('stderr_text'))
        evidence = job['action_summary']['recent_actions'][0]
        self.assertEqual((evidence['status'], evidence['executed_native_steps']), ('stopped', 1))
        self.assertEqual(evidence['stage_results']['depth']['status'], 'completed')
        self.check_checkpoint()

    def test_metadata_named_checkpoint_keeps_actor_and_archived_stage_evidence(self):
        for label in ('hash', 'sha256'):
            with self.subTest(label=label):
                args = deepcopy(self.args)
                args.update(action_id='label-' + label, observation_id=self.h.latest['observation_id'])
                args['stages'][1]['id'] = label
                response = self.h.handle_tool(dict(threadId='thread', turnId='turn', callId=label,
                                                   tool='robot_action', arguments=args))
                self.assertTrue(response['success'])
                shown = json.loads(response['contentItems'][0]['text'])
                receipt = self.store.read('action_receipt', args['action_id'])
                self.assertEqual(shown['stages'][label]['status'], 'completed')
                self.assertEqual(receipt['stage_results'][label]['status'], 'completed')
                self.assertEqual(shown['receipt']['executed_native_steps'], 1)


class UnsafeAcknowledgementTests(unittest.TestCase):
    setUp = test_runtime_lifecycle.RuntimeLifecycleTests.setUp
    action = test_runtime_lifecycle.RuntimeLifecycleTests.action

    def test_bad_payload_keeps_execution_facts_without_leaking_or_replaying(self):
        execute = self.robot.execute
        for status in ('completed', 'partial', 'stopped', 'unknown', 'not_a_receipt_status'):
            with self.subTest(status=status):
                expected_status = 'unknown' if status == 'not_a_receipt_status' else status
                self.harness.accepting = True
                initial = self.harness.observe()['observation_id']
                action = self.action('unsafe-' + status, initial)
                def unsafe(*args, **kwargs):
                    result = execute(*args, **kwargs)
                    result.update(status=status, executed_native_steps=1, completed_native_steps=1,
                        executed_control_steps=10, stop_confirmation=dict(clock='paused',
                            actuator_target='last_native_command_retained', settling_steps=0),
                        episode_terminal=dict(ended=False, native_action_count=self.robot.executions, native_step_limit=20),
                        extra={'evaluator_state': {'secret': 'DO_NOT_PUBLISH'}})
                    return result
                with patch.object(self.robot, 'execute', side_effect=unsafe):
                    result = self.harness.execute(action)
                receipt = result['receipt']
                self.assertEqual(receipt['status'], expected_status)
                self.assertEqual((receipt['executed_native_steps'], receipt['completed_native_steps']), (1, 1))
                self.assertEqual(receipt['executed_control_steps'], 10)
                self.assertEqual(receipt['stop_confirmation']['clock'], 'paused')
                self.assertFalse(receipt['post_execution_error']['action_replay_permitted'])
                self.assertNotIn('DO_NOT_PUBLISH', canonical(result))
                self.assertTrue(set(receipt).isdisjoint({'extra', 'planned_trace', 'actual_trace', 'before_state'}))
                self.assertIsNone(self.harness.latest)
                before = self.robot.executions
                self.assertEqual(self.harness.execute(action), result)
                self.assertEqual(self.robot.executions, before)
                self.assertEqual(self.harness.native_action_count, before)
                self.assertEqual(self.store.read('action_receipt', action['action_id']), receipt)
                self.harness.observe()
                self.assertEqual(self.harness.accepting, expected_status != 'unknown')

    def test_non_json_payload_and_hidden_stop_fields_cannot_escape_safe_ack(self):
        execute = self.robot.execute
        def unsafe(*args, **kwargs):
            result = execute(*args, **kwargs)
            result.update(executed_native_steps=1, completed_native_steps=True,
                executed_control_steps=-1, stop_confirmation={'evaluator_state': 'DO_NOT_PUBLISH'}, extra=object())
            self.harness.request_stop()
            return result
        with patch.object(self.robot, 'execute', side_effect=unsafe):
            result = self.harness.execute(self.action('bad-values'))
        receipt = result['receipt']
        self.assertEqual(receipt['status'], 'completed')
        self.assertEqual(receipt['executed_native_steps'], 1)
        self.assertNotIn('completed_native_steps', receipt)
        self.assertNotIn('executed_control_steps', receipt)
        self.assertNotIn('stop_confirmation', receipt)
        self.assertNotIn('DO_NOT_PUBLISH', canonical(result))
        self.harness.observe()
        self.assertFalse(self.harness.accepting)

    def test_serialization_failure_keeps_counts_and_rejects_malformed_stop_values(self):
        execute = self.robot.execute
        def unsafe(*args, **kwargs):
            result = execute(*args, **kwargs)
            result.update(executed_native_steps=1, extra=object(),
                stop_confirmation=dict(clock='paused', actuator_target='last_native_command_retained',
                                       settling_steps=True))
            return result
        with patch.object(self.robot, 'execute', side_effect=unsafe):
            result = self.harness.execute(self.action('not-json'))
        receipt = result['receipt']
        self.assertEqual(receipt['status'], 'completed')
        self.assertEqual(receipt['executed_native_steps'], 1)
        self.assertNotIn('stop_confirmation', receipt)
        self.assertEqual(receipt['post_execution_error']['error_type'], 'TypeError')
        self.assertEqual(receipt['post_execution_error']['message'], 'display_not_json')
        self.assertIsNone(self.harness.latest)
        self.assertEqual(self.harness.execute(self.action('not-json')), result)
        self.assertEqual(self.robot.executions, 1)

    def test_negative_execution_acknowledgements_survive_fallback_and_fresh_observe(self):
        execute = self.robot.execute
        signals = [dict(execution_uncertain=True), dict(stop_confirmed=False),
            dict(stop_confirmation=False), dict(stop_confirmation=dict(confirmed=False)),
            dict(arm_results=dict(left=dict(execution_uncertain=True))),
            dict(stage_results=dict(reach=dict(execution_uncertain=True)))]
        for index, signal in enumerate(signals):
            for unsafe_payload in (False, True):
                with self.subTest(signal=signal, unsafe_payload=unsafe_payload):
                    self.harness.accepting = True
                    initial = self.harness.observe()['observation_id']
                    action = self.action('unconfirmed-' + str(index) + '-' + str(unsafe_payload), initial)
                    def uncertain(*args, **kwargs):
                        result = execute(*args, **kwargs)
                        result.update(status='stopped', executed_native_steps=1, **signal)
                        if unsafe_payload:
                            result['extra'] = {'evaluator_state': 'DO_NOT_PUBLISH'}
                        return result
                    with patch.object(self.robot, 'execute', side_effect=uncertain):
                        result = self.harness.execute(action)
                    receipt = result['receipt']
                    self.assertEqual((receipt['status'], receipt['executed_native_steps']), ('stopped', 1))
                    for key, value in signal.items():
                        if unsafe_payload and key in ('arm_results', 'stage_results'):
                            self.assertTrue(receipt['execution_uncertain'])
                        else:
                            self.assertEqual(receipt[key], value)
                    self.assertNotIn('DO_NOT_PUBLISH', canonical(receipt))
                    fresh = self.harness.observe()
                    self.assertFalse(self.harness.accepting)
                    executions = self.robot.executions
                    self.assertEqual(self.harness.execute(action), result)
                    refused = self.harness.execute(self.action(action['action_id'] + '-after', fresh['observation_id']))
                    self.assertEqual(refused['receipt']['reason'], 'run_stopping_or_reconciliation_required')
                    self.assertEqual(self.robot.executions, executions)
                    workbench = object.__new__(Workbench)
                    workbench.harness, workbench.store, workbench.identity = self.harness, self.store, self.identity
                    self.assertTrue(workbench.current_unknown_actions())
                    restored = Harness(self.store, self.robot, config=self.harness.config)
                    self.assertFalse(restored.accepting)

    def test_confirmed_checkpoint_allows_next_action_after_fresh_observe(self):
        execute = self.robot.execute
        def checkpoint(*args, **kwargs):
            result = execute(*args, **kwargs)
            result.update(status='stopped', executed_native_steps=1,
                stop_confirmation=dict(clock='paused', actuator_target='last_native_command_retained', settling_steps=0),
                extra={'evaluator_state': 'DO_NOT_PUBLISH'})
            return result
        with patch.object(self.robot, 'execute', side_effect=checkpoint):
            result = self.harness.execute(self.action('confirmed-checkpoint'))
        self.assertEqual(result['receipt']['status'], 'stopped')
        fresh = self.harness.observe()
        self.assertTrue(self.harness.accepting)
        next_result = self.harness.execute(self.action('after-confirmed', fresh['observation_id']))
        self.assertEqual(next_result['receipt']['status'], 'completed')
        self.assertEqual(self.robot.executions, 2)
