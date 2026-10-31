"""Offline native request lifecycle and asynchronous RPC reader checks. No model/GPU."""
import concurrent.futures
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from openai_codex.client import CodexConfig
from openai_codex.errors import InvalidParamsError, TransportClosedError
from openai_codex.generated.v2_all import MessageResponseItem
from jsonschema import validate

from auriga.cortex.session.codex import (CodexSession, RecordedClient, MODEL_COMPLETION_COUNTING,
                            DYNAMIC_TOOL_NAMESPACE, _native_dynamic_tools)
from auriga.core.config import Config
from auriga.core.storage.store import Store
from auriga.cortex.context.task_planning import TaskPlanning


class NativeRequestsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.identity = dict(run_id='synthetic-requests',episode_id='episode',env_instance_id='environment',env_generation=1)
        self.store = Store(self.root, self.identity)
        config = Config.from_dict({'model': {'codex_home': str(self.root / 'fixture-config')}})
        self.session = CodexSession(self.root,self.store,lambda value: {},config=config)
        self.session.thread_id,self.session.turn_id,self.session.state = 'thread-owned','turn-owned','running'
        self.workers = concurrent.futures.ThreadPoolExecutor(max_workers=4)

    def tearDown(self):
        self.session.close()
        self.workers.shutdown(wait=True,cancel_futures=True)
        self.store.close()
        self.temporary.cleanup()

    def message(self,identifier=1,method='item/commandExecution/requestApproval',**params):
        return dict(id=identifier,method=method,params=dict(threadId='thread-owned',turnId='turn-owned',itemId='item-owned',**params))

    def test_decision_context_uses_current_revision_without_changing_history_or_evidence(self):
        from auriga.cortex.context.task_planning import DECISION_BOUNDARY_INSTRUCTIONS
        planning = self.session.task_planning = TaskPlanning(self.store)
        observation = dict(self.identity, observation_id='obs_1')
        attention = [dict(concern='Retention is uncertain.',
                          check='Check co-motion in useful transport; revise the grasp if absent.',
                          when='Before depending on retention for placement.')]
        ledger = [dict(id='retention', expected='The object remains aligned with the fingers after lift.',
                       status='pending', basis='unknown',
                       next_check='Compare the next task-progress frame with the fingers.')]
        planning.call(dict(plan='Task understanding: unknown retention. Approach: transport then place.',
                           attention=attention, ledger=ledger), self.identity, observation, {})
        request = dict(input=[dict(type='function_call_output', call_id='native-call',
                                  output='{"receipt":{"status":"partial","completed":2}}'),
                              dict(type='reasoning', encrypted_content='opaque-reasoning', summary=[])])
        original = deepcopy(request)
        identity = dict(source='actor', native_thread_id=self.session.thread_id, endpoint='/responses')
        compact_identity = dict(identity, endpoint='/responses/compact')
        normal_first = self.session._task_decision_context(request, identity)
        self.assertIs(normal_first, request)
        self.assertEqual(request, original)
        extended_request = dict(input=request['input'] + [dict(role='user', content='Next step')])
        normal_second = self.session._task_decision_context(extended_request, identity)
        self.assertEqual(normal_second['input'][:len(normal_first['input'])], normal_first['input'])

        first = self.session._task_decision_context(request, compact_identity)
        self.assertEqual(request, original)
        self.assertEqual(first['input'][:-1], original['input'])
        self.assertEqual(first['input'][-1]['role'], 'assistant')
        validate(first['input'][-1], MessageResponseItem.model_json_schema())
        self.assertEqual(first['input'][-1]['content'][0]['text'].count(DECISION_BOUNDARY_INSTRUCTIONS), 1)
        snapshot = json.loads(first['input'][-1]['content'][0]['text'].split('\n')[-1])
        self.assertEqual(snapshot['revision'], 1)
        self.assertEqual(snapshot['attention'], attention)
        self.assertEqual(snapshot['ledger'], ledger)
        self.assertEqual(snapshot['plan_observation_id'], 'obs_1')
        self.assertEqual(snapshot['attention_observation_id'], 'obs_1')
        self.assertNotIn('based_on_observation_id', snapshot)
        self.assertNotIn('identity', snapshot)

        planning.call(dict(operation='update', attention=[]), self.identity,
                      dict(observation, observation_id='obs_2'), {})
        current = self.session._task_decision_context(request, compact_identity)
        snapshot = json.loads(current['input'][-1]['content'][0]['text'].split('\n')[-1])
        self.assertEqual(current['input'][-1]['content'][0]['text'].count(DECISION_BOUNDARY_INSTRUCTIONS), 1)
        self.assertEqual(snapshot['revision'], 2)
        self.assertEqual(snapshot['attention'], [])
        self.assertEqual(snapshot['ledger'], ledger)
        self.assertEqual(snapshot['plan_observation_id'], 'obs_1')
        self.assertEqual(snapshot['attention_observation_id'], 'obs_2')
        self.assertEqual(len(current['input']), len(original['input']) + 1)
        self.assertNotIn('Retention is uncertain.', current['input'][-1]['content'][0]['text'])
        recovered = self.session._task_decision_context(dict(input=[]), compact_identity)
        self.assertEqual(recovered['input'][:-1], [])
        self.assertIn('"revision": 2', recovered['input'][-1]['content'][0]['text'])
        self.assertEqual(recovered['input'][-1]['content'][0]['text'].count(DECISION_BOUNDARY_INSTRUCTIONS), 1)
        self.assertIn('"attention": []', recovered['input'][-1]['content'][0]['text'])
        self.assertEqual(self.store.records('action'), [])

    def test_decision_context_reinjects_after_new_world_evidence(self):
        planning = self.session.task_planning = TaskPlanning(self.store)
        observation = dict(self.identity, observation_id='obs_1')
        planning.call(dict(plan='Move and verify the target.', ledger=[]), self.identity, observation, {})
        identity = dict(source='actor', native_thread_id=self.session.thread_id, endpoint='/responses/compact')
        request = dict(input=[dict(role='user', content='Continue')])
        self.assertIs(self.session._task_decision_context(request, dict(identity, endpoint='/responses')), request)
        first = self.session._task_decision_context(request, identity)
        second = self.session._task_decision_context(request, identity)
        self.assertIn('"revision": 1', first['input'][-1]['content'][0]['text'])
        self.assertIn('"revision": 1', second['input'][-1]['content'][0]['text'])
        self.store.event('observation.available', {'observation_id': 'obs_2'},
                         dict(observation_id='obs_2'))
        third = self.session._task_decision_context(request, identity)
        third_snapshot = json.loads(third['input'][-1]['content'][0]['text'].split('\n')[-1])
        self.assertEqual(third_snapshot['revision'], 1)
        self.assertEqual(third_snapshot['plan'], 'Move and verify the target.')
        self.assertEqual(third_snapshot['attention'], [])
        self.assertEqual(third_snapshot['ledger'], [])

    def test_native_context_compaction_keeps_next_actor_snapshot_complete(self):
        planning = self.session.task_planning = TaskPlanning(self.store)
        planning.call(dict(plan='Move and verify the target.', ledger=[]), self.identity,
                      dict(self.identity, observation_id='obs_1'), {})
        identity = dict(source='actor', native_thread_id=self.session.thread_id, endpoint='/responses/compact')
        self.session._task_decision_context(dict(input=[]), identity)
        self.session._notification(self.message(method='item/started',
            item={'id': 'compaction-1', 'type': 'contextCompaction'}))
        normal = dict(input=[])
        self.assertIs(self.session._task_decision_context(normal, dict(identity, endpoint='/responses')), normal)
        projected = self.session._task_decision_context(dict(input=[]), identity)
        text = projected['input'][-1]['content'][0]['text']
        self.assertIn('"plan": "Move and verify the target."', text)
        self.assertNotIn('snapshot is unchanged', text)

    def test_decision_reminders_are_deduplicated_without_removing_normal_messages(self):
        planning = self.session.task_planning = TaskPlanning(self.store)
        planning.call(dict(plan='Move and verify the target.', ledger=[]), self.identity,
                      dict(self.identity, observation_id='obs_1'), {})
        identity = dict(source='actor', native_thread_id=self.session.thread_id, endpoint='/responses/compact')
        request = dict(input=[dict(role='user', content='Continue')])
        full = self.session._task_decision_context(request, identity)
        reminder = full['input'][-1]
        ordinary = dict(role='assistant', content=[dict(type='output_text',
            text='Current task decision boundary is mentioned by the user, not Harness generated.')])
        ordinary_exact = dict(type="message", role="assistant", content=[dict(
            type="output_text", text='Current task decision boundary\nThis is ordinary assistant content.')])
        duplicated = dict(input=full['input'] + [reminder, reminder, ordinary, ordinary_exact])
        projected = self.session._task_decision_context(duplicated, identity)
        generated = [item for item in projected['input']
                     if item.get('type') == 'message' and item.get('role') == 'assistant'
                     and item.get('content') and len(item['content']) == 1
                     and item['content'][0].get('type') == 'output_text'
                     and item['content'][0].get('text', '').startswith(
                         'Current task decision boundary\nThe saved plan and attention')]
        self.assertEqual(len(generated), 1)
        self.assertIn('mentioned by the user', json.dumps(projected['input']))
        self.assertIn('ordinary assistant content', json.dumps(projected['input']))

        old = dict(type="message", role="assistant", content=[dict(
            type="output_text", text="Current task decision context\nThe saved plan and attention was archived.")])
        old_projected = self.session._task_decision_context(dict(input=[old]), identity)
        self.assertEqual(sum(item.get('type') == 'message' and item.get('role') == 'assistant'
                             and item.get('content', [{}])[0].get('text', '').startswith(
                                 ('Current task decision boundary\n', 'Current task decision context\n'))
                             for item in old_projected['input']), 1)

    def test_decision_context_does_not_duplicate_action_checkpoint(self):
        planning = self.session.task_planning = TaskPlanning(self.store)
        observation = dict(self.identity, observation_id="obs_1")
        planning.call(dict(plan="Transfer and verify.", ledger=[dict(
            id="retention", expected="The object moves with the fingers.", status="contradicted",
            basis="RGB shows no co-motion.", next_check="Change the contact or route before relying on retention.")]),
            self.identity, observation, {})
        action = dict(schema_version="0.1", **self.identity, world_kind="observed", type="BatchAction",
            action_id="internal-a1", based_on_observation_id="obs_1", stages=[dict(
                id="move", arm="left", kind="move", action=dict(type="move", arm="left",
                    path=dict(contact_region="pad_center", waypoints=[[0, 0, 0]])))])
        for action_id in ("internal-a1", "internal-a2"):
            action = dict(action, action_id=action_id)
            self.store.event("action.requested", {"request": action}, {"action_id": action_id})
            self.store.event("action.failed", {"status": "failed", "reason": "path_infeasible"}, {"action_id": action_id})
        request = dict(input=[dict(role="user", content="Continue")])
        projected = self.session._task_decision_context(request, dict(source="actor", native_thread_id=self.session.thread_id,
                                                                       endpoint="/responses/compact", **self.identity))
        text = projected["input"][-1]["content"][0]["text"]
        self.assertNotIn("Adaptive action checkpoint", text)
        self.assertIn("Change the contact", text)  # The actor's ledger remains complete.
        self.assertNotIn("internal-a1", text)
        self.assertNotIn("sha256", text)

    def test_decision_context_restores_saved_plan_and_does_not_leak_across_episodes(self):
        observation = dict(self.identity, observation_id='obs_1')
        TaskPlanning(self.store).call(dict(plan='Keep all meaningful details. ' + 'useful detail; ' * 800 + 'tail evidence', ledger=[]),
                                     self.identity, observation, {})
        self.session.task_planning = TaskPlanning(self.store)
        identity = dict(source='actor', native_thread_id=self.session.thread_id, endpoint='/responses/compact')
        projected = self.session._task_decision_context(dict(input='Continue the task.'), identity)
        self.assertEqual(projected['input'][0], dict(role='user', content='Continue the task.'))
        self.assertIn('tail evidence', projected['input'][-1]['content'][0]['text'])
        self.identity['env_generation'] += 1
        projected = self.session._task_decision_context(dict(input=[]), identity)
        self.assertIn('No plan is stored for this episode', projected['input'][-1]['content'][0]['text'])
        self.assertNotIn('tail evidence', projected['input'][-1]['content'][0]['text'])

    def test_decision_context_excludes_helpers_unknown_threads_and_schema_projection(self):
        self.session.task_planning = mock.Mock()
        request = dict(input='Selected visual evidence only.')
        for identity in (dict(source='actor', native_thread_id=self.session.thread_id, endpoint='/responses'),
                         dict(source='actor', native_thread_id=self.session.thread_id),
                         dict(source='sol_visual_review', native_thread_id=self.session.thread_id, endpoint='/responses/compact'),
                         dict(source='actor', native_thread_id='other-thread', endpoint='/responses/compact'),
                         dict(source='actor', endpoint='/responses/compact'), {}, dict(native_thread_id=self.session.thread_id)):
            with self.subTest(identity=identity):
                self.assertIs(self.session._task_decision_context(request, identity), request)
        self.session._present_model_request(dict(tools=[]))
        self.session.task_planning.decision_context.assert_not_called()

    def test_native_start_and_turn_project_text_without_changing_tool_contract(self):
        checksum = 'f' * 64
        tools = [dict(type='function', name='fixture', description='build ' + checksum,
                      inputSchema=dict(type='object', properties={'sha256': dict(type='string')},
                                       required=['sha256'], additionalProperties=False)),
                 dict(type='function', name='fixture_deferred', description='Deferred fixture.',
                      inputSchema={'type': 'object'}, deferLoading=True)]
        original = deepcopy(tools)
        self.session.private.mkdir()
        self.session.socket_path = self.root / 'fixture.sock'
        self.session.socket_path.touch()
        self.session.sandbox = mock.Mock(host_socket_path='/proc/fixture/fd/pinned')
        self.session.sandbox.repair_socket_alias.return_value = True
        self.session.command = ['fixture-only']
        self.session.env = {}
        self.session.cli_executable = Path('/fixture/codex')
        client = mock.MagicMock()
        client.initialize.return_value.model_dump.return_value = {}
        client.thread_start.return_value = SimpleNamespace(thread=SimpleNamespace(id='fixture-thread'))
        client.turn_start.return_value = SimpleNamespace(turn=SimpleNamespace(id='fixture-turn'))
        process = SimpleNamespace(pid=1, poll=lambda: 0)
        self.session.model_proxy = mock.Mock()
        with mock.patch.object(self.session, '_prepare', return_value='fixture'), \
                mock.patch.object(self.session, '_stop_previous_owned_processes'), \
                mock.patch('auriga.cortex.session.codex.subprocess.Popen', return_value=process), \
                mock.patch('auriga.cortex.session.codex.RecordedClient', return_value=client) as factory:
            self.session.start(tools, 'instructions sha256=' + checksum)
        self.session.model_proxy.register_thread.assert_called_once_with('fixture-thread', 'actor', 'fixture-thread')
        self.session.sandbox.repair_socket_alias.assert_called_once_with(self.session.socket_path)
        self.assertEqual(factory.call_args.args[0].launch_args_override[-1], '/proc/fixture/fd/pinned')
        parameters = client.thread_start.call_args.args[0]
        self.assertNotIn('baseInstructions', parameters)
        self.assertNotIn(checksum, parameters['developerInstructions'])
        self.assertNotIn(checksum, parameters['dynamicTools'][0]['description'])
        self.assertEqual(parameters['dynamicTools'][0]['inputSchema'], original[0]['inputSchema'])
        self.assertEqual(parameters['dynamicTools'][1]['name'], DYNAMIC_TOOL_NAMESPACE)
        self.assertEqual(parameters['dynamicTools'][1]['tools'], [original[1]])
        self.assertEqual(tools, original)
        tool_callback = factory.call_args.args[2]
        self.session.tool_handler = mock.Mock(return_value={})
        for params in (dict(tool='fixture_deferred'),
                       dict(tool='fixture_deferred', namespace='other'),
                       dict(tool='fixture', namespace=DYNAMIC_TOOL_NAMESPACE),
                       dict(tool='not_registered', namespace=DYNAMIC_TOOL_NAMESPACE)):
            with self.subTest(params=params), self.assertRaisesRegex(ValueError, 'native_tool_call_namespace_mismatch'):
                tool_callback(params)
        self.session.tool_handler.assert_not_called()
        for params in (dict(tool='fixture'), dict(tool='fixture_deferred', namespace=DYNAMIC_TOOL_NAMESPACE)):
            tool_callback(params)
            self.session.tool_handler.assert_called_with(params)
        self.session.run_turn('continuation hash: ' + checksum)
        text = client.turn_start.call_args.args[1][0]['text']
        self.assertNotIn(checksum, text)
        self.assertIn('continuation', text)

    def test_failed_terminal_counting_change_rejects_old_resume_ledger(self):
        config = self.root/'fixture-config'
        config.mkdir()
        (config/'config.toml').write_text('model_provider = "fixture"\n[model_providers.fixture]\nname = "fixture"\nbase_url = "http://127.0.0.1:1"\n')
        previous = dict(completed_calls=5, http_attempt_count=7,
                        counting='authoritative_terminal_actor_helper_compact_responses')
        self.store.set_state('runtime', 'model_requests', previous)
        with mock.patch('auriga.cortex.session.codex.ModelRequestProxy', side_effect=RuntimeError('proxy-construction-reached')) as constructor:
            with self.assertRaisesRegex(ValueError, 'resume_model_budget_semantics_changed'):
                self.session._prepare()
            constructor.assert_not_called()
            self.store.set_state('runtime', 'model_requests', {**previous, 'counting':MODEL_COMPLETION_COUNTING})
            with self.assertRaisesRegex(RuntimeError, 'proxy-construction-reached'):
                self.session._prepare()
            self.assertEqual(constructor.call_args.kwargs['max_requests'], 175)
            self.assertIsNone(constructor.call_args.kwargs['request_timeout_s'])
            self.assertEqual(constructor.call_args.kwargs['max_retries'], 12)
            # A ledger without a recorded recovery policy resumes without automatic recovery.
            self.assertIsNone(constructor.call_args.kwargs['transport_recovery'])
            self.session.model_proxy = SimpleNamespace(request_count=1, attempt_count=2, max_retries=12,
                                                       transport_blocked=False, blocked_error=None,
                                                       transport_recovery_summary=dict(policy=None, rounds=0))
            try:
                constructor.call_args.kwargs['on_event']({'kind':'fixture'})
                state = self.store.read_state('runtime', 'model_requests')
                self.assertEqual(state['counting'], MODEL_COMPLETION_COUNTING)
                self.assertEqual(state['completed_calls'], 6)
                self.assertEqual(state['http_attempt_count'], 9)
                self.assertIsNone(state['timeout_s'])
                self.assertEqual(state['max_retries'], 12)
                self.assertEqual(state['retry_policy'], 'bounded_harness_retry_after_explicit_retryable_provider_error')
                self.assertEqual(state['transport_recovery'], dict(policy=None, rounds=0))
            finally:
                self.session.model_proxy = None

    def wait_pending(self,message):
        identifier = self.session._request_id(message)
        deadline = time.monotonic()+2
        while identifier not in self.session.pending and time.monotonic()<deadline:
            time.sleep(.005)
        self.assertIn(identifier,self.session.pending)
        return identifier

    def issue(self,message):
        def handle():
            response = self.session._server_request(message)
            self.session._server_request(message,sent=True)
            return response
        future = self.workers.submit(handle)
        return future,self.wait_pending(message)

    def test_unattended_approval_preserves_exact_rpc_identity_and_rejects_late_reply(self):
        message = self.message(identifier=0)
        future,identifier = self.issue(message)
        self.assertEqual(future.result(timeout=2),{'decision':'acceptForSession'})
        result = self.store.read_state('native_request',identifier)
        self.assertEqual(result['rpc_id'],0)
        self.assertEqual(result['status'],'resolved')
        self.assertEqual(result['response_policy'],'unattended')
        for response in ({'decision':'decline'}, {'decision':'accept'}):
            with self.assertRaisesRegex(ValueError,'expired_or_not_owned'):
                self.session.reply(identifier,response)

    def test_unattended_questions_and_all_permission_responses_are_immediate_and_valid(self):
        for method, expected in [
            ('item/tool/requestUserInput', {'answers':{}}),
            ('item/commandExecution/requestApproval', {'decision':'acceptForSession'}),
            ('item/fileChange/requestApproval', {'decision':'acceptForSession'}),
            ('item/permissions/requestApproval', {'permissions':{},'scope':'session','strictAutoReview':False}),
        ]:
            with self.subTest(method=method):
                message = self.message(identifier=method, method=method,
                    questions=[{'id':'choice','question':'Synthetic choice'}])
                future,identifier = self.issue(message)
                response = future.result(timeout=.5)
                self.assertEqual(response, expected)
                validate(response, self.session.response_schemas[method])
                record = self.store.read_state('native_request',identifier)
                self.assertEqual(record['status'], 'resolved')
                self.assertEqual(record['response_policy'], 'unattended')
                self.assertIn('unattended', record['reason'])
                with self.assertRaisesRegex(ValueError,'expired_or_not_owned'):
                    self.session.reply(identifier, response)

    def test_permissions_grant_complete_requested_profile_without_mutating_native_input(self):
        permissions = dict(network=dict(enabled=True), fileSystem=dict(
            read=['/tmp/requested-read'], write=['/tmp/requested-write'], globScanMaxDepth=3,
            entries=[dict(access='write', path=dict(type='glob_pattern', pattern='/tmp/requested-*')),
                     dict(access='read', path=dict(type='special', value=dict(kind='project_roots'))),
                     dict(access='deny', path=dict(type='path', path='/tmp/explicitly-denied'))]))
        message = self.message(identifier='full-profile', method='item/permissions/requestApproval', permissions=permissions)
        original = deepcopy(message)
        client = RecordedClient(CodexConfig(cwd=str(self.root)), self.store, lambda params: {}, lambda message: None)
        try:
            for owner in ('session', 'helper'):
                with self.subTest(owner=owner):
                    response = (self.session._server_request(message) if owner == 'session'
                                else client._approval(message['method'], message['params']))
                    validate(response, self.session.response_schemas[message['method']])
                    self.assertEqual(response, dict(permissions=permissions, scope='session', strictAutoReview=False))
                    self.assertIsNot(response['permissions'], permissions)
                    self.assertIsNot(response['permissions']['fileSystem']['entries'], permissions['fileSystem']['entries'])
                    response['permissions']['fileSystem']['write'].append('/tmp/response-only')
                    self.assertEqual(message, original)
            record = self.store.read_state('native_request', self.session._request_id(message))
            self.assertEqual(record['reason'], 'unattended_permission_granted')
            self.assertEqual(record['params'], original['params'])
        finally:
            client.server_pool.shutdown(wait=True)

    def test_isolated_helper_approval_fallback_uses_session_grants_and_empty_answers(self):
        client = RecordedClient(CodexConfig(cwd=str(self.root)), self.store, lambda params: {}, lambda message: None)
        try:
            for method, expected in [
                ('item/tool/requestUserInput', {'answers': {}}),
                ('item/commandExecution/requestApproval', {'decision': 'acceptForSession'}),
                ('item/fileChange/requestApproval', {'decision': 'acceptForSession'}),
                ('item/permissions/requestApproval', {'permissions': {'network': None, 'fileSystem': None},
                    'scope': 'session', 'strictAutoReview': False}),
            ]:
                with self.subTest(method=method):
                    message = self.message(method=method, permissions={'network': None, 'fileSystem': None})
                    original = deepcopy(message)
                    response = client._approval(method, message['params'])
                    validate(response, self.session.response_schemas[method])
                    self.assertEqual(response, expected)
                    self.assertEqual(message, original)
            self.assertEqual(client._approval('item/future/requestApproval', {}), {'decision': 'decline'})
        finally:
            client.server_pool.shutdown(wait=True)

    def test_permission_grants_reject_mismatched_identity_and_resolved_requests(self):
        for method in ('item/commandExecution/requestApproval', 'item/fileChange/requestApproval',
                       'item/permissions/requestApproval'):
            for field in ('threadId', 'turnId'):
                with self.subTest(method=method, field=field):
                    message = self.message(identifier=method + field, method=method, permissions={'network': {'enabled': True}})
                    message['params'][field] = 'not-owned'
                    response = self.session._server_request(message)
                    validate(response, self.session.response_schemas[method])
                    self.assertEqual(response, ({'permissions': {}, 'scope': 'turn'}
                        if method == 'item/permissions/requestApproval' else {'decision': 'decline'}))
                    self.assertNotIn(self.session._request_id(message), self.session.pending)
            message = self.message(identifier=method + '-resolved', method=method, permissions={'network': {'enabled': True}})
            self.session._resolve_request(dict(threadId='thread-owned', requestId=message['id']))
            self.assertIsNone(self.session._server_request(message))
            self.assertIsNone(self.session._server_request(message, before_send=True))
            self.assertEqual(self.store.read_state('native_request', self.session._request_id(message))['status'], 'expired')

    def test_disconnect_expires_unsent_automatic_response_and_rejects_late_reply(self):
        message = self.message()
        self.assertEqual(self.session._server_request(message), {'decision':'acceptForSession'})
        identifier = self.session._request_id(message)
        self.session._disconnected(RuntimeError('synthetic lost transport'))
        self.assertEqual(self.session.state,'disconnected')
        self.assertEqual(self.store.read_state('native_request',identifier)['status'],'expired')
        with self.assertRaisesRegex(ValueError,'expired'):
            self.session.reply(identifier,{'decision':'accept'})
        lost = next(event for event in self.store.events() if event['type']=='codex.connection_lost')
        self.assertFalse(lost['payload']['history_complete'])

    def test_unknown_native_request_is_recorded_without_pending_input(self):
        message = self.message(identifier=10,method='future/unsupportedNativeRequest')
        self.session._server_request(message)
        event = self.store.events()[-1]
        self.assertEqual(event['type'],'codex.unsupported_request')
        self.assertEqual(event['payload']['rpc_id'],10)
        self.assertEqual(self.session.requests(),[])

    def test_off_thread_request_cannot_become_writable(self):
        message = self.message(identifier=11)
        message['params']['threadId'] = 'other-thread'
        future = self.workers.submit(self.session._server_request,message)
        try:
            future.result(timeout=.5)
        except ValueError:
            pass
        except concurrent.futures.TimeoutError:
            self.fail('off-thread native request entered the writable pending queue')
        self.assertFalse(any(request['status']=='pending' for request in self.session.requests()))

    def test_server_resolution_uses_exact_rpc_and_thread_and_cannot_be_resurrected(self):
        message = self.message(identifier=0)
        self.session._server_request(message)
        identifier = self.session._request_id(message)
        for thread, rpc_id in [('other-thread', 0), ('thread-owned', '0')]:
            self.session._notification(dict(method='serverRequest/resolved',
                params=dict(threadId=thread, requestId=rpc_id)))
            self.assertEqual(self.store.read_state('native_request',identifier)['status'], 'responding')
        self.session._notification(dict(method='serverRequest/resolved',
            params=dict(threadId='thread-owned', requestId=0)))
        self.assertTrue(self.session.pending[identifier]['event'].is_set())
        self.assertTrue(self.session.pending[identifier]['sent'].is_set())
        self.session._server_request(message, sent=True)
        self.assertEqual(self.store.read_state('native_request',identifier)['status'], 'expired')
        with self.assertRaisesRegex(ValueError, 'expired_or_not_owned'):
            self.session.reply(identifier, {'decision':'accept'})

    def test_server_resolution_before_worker_admission_skips_reply_and_does_not_cross_connections(self):
        message = self.message(identifier='before-worker')
        self.session._notification(dict(method='serverRequest/resolved',
            params=dict(threadId='thread-owned', requestId=message['id'])))
        client = RecordedClient(CodexConfig(cwd=str(self.root)), self.store, lambda params: {},
            lambda message: None, self.session._server_request)
        client._write_message = mock.Mock()
        try:
            client._reply_request(message)
            client._write_message.assert_not_called()
            record = self.store.read_state('native_request', self.session._request_id(message))
            self.assertEqual(record['status'], 'expired')
            self.assertEqual(record['reason'], 'server_request_resolved')
            self.session.native_connection_id = 'different-connection'
            client._reply_request(message)
            client._write_message.assert_called_once_with(dict(id=message['id'], result={'decision':'acceptForSession'}))
        finally:
            client.server_pool.shutdown(wait=True)

    def test_resolution_during_response_preparation_skips_write(self):
        message = self.message(identifier='during-preparation')
        def handle(value, **kwargs):
            response = self.session._server_request(value, **kwargs)
            if not kwargs:
                self.session._notification(dict(method='serverRequest/resolved',
                    params=dict(threadId='thread-owned', requestId=value['id'])))
            return response
        client = RecordedClient(CodexConfig(cwd=str(self.root)), self.store, lambda params: {},
            lambda message: None, handle)
        client._write_message = mock.Mock()
        try:
            client._reply_request(message)
            client._write_message.assert_not_called()
            record = self.store.read_state('native_request', self.session._request_id(message))
            self.assertEqual(record['status'], 'expired')
        finally:
            client.server_pool.shutdown(wait=True)

    def test_response_preflight_preserves_unsupported_and_off_thread_replies(self):
        client = RecordedClient(CodexConfig(cwd=str(self.root)), self.store, lambda params: {},
            lambda message: None, self.session._server_request)
        client._write_message = mock.Mock()
        unsupported = self.message(identifier='unsupported', method='fixture/unsupported')
        mismatch = self.message(identifier='mismatch')
        mismatch['params']['threadId'] = 'other-thread'
        try:
            client._reply_request(unsupported)
            client._reply_request(mismatch)
            self.assertEqual(client._write_message.call_args_list, [
                mock.call(dict(id='unsupported', result={})),
                mock.call(dict(id='mismatch', result={'decision':'decline'})),
            ])
            self.assertEqual(self.session.requests(), [])
        finally:
            client.server_pool.shutdown(wait=True)

    def test_old_connection_callbacks_cannot_close_or_resolve_new_connection(self):
        self.session.private.mkdir()
        self.session.socket_path = self.root/'fixture.sock'
        self.session.socket_path.touch()
        self.session.sandbox = mock.Mock(host_socket_path='/proc/fixture/fd/pinned')
        self.session.sandbox.repair_socket_alias.return_value = True
        self.session.command = ['fixture-only']
        self.session.env = {}
        self.session.cli_executable = Path('/fixture/codex')
        callbacks = []
        tool_calls = []
        retired = []
        self.session.tool_handler = lambda params: tool_calls.append(params) or {'success': True, 'contentItems': []}
        def client(*args, **kwargs):
            callbacks.append(args[2:6])
            self.assertFalse(kwargs['route_notifications'])
            return SimpleNamespace(start=lambda: None, _proc=None,
                close=lambda: retired.append(('transport', self.session.native_connection_id)),
                server_pool=SimpleNamespace(shutdown=lambda **options:
                    retired.append(('workers', self.session.native_connection_id, options))),
                initialize=lambda: SimpleNamespace(model_dump=lambda **kwargs: {}),
                thread_start=lambda params: SimpleNamespace(thread=SimpleNamespace(id='same-thread')))
        process = SimpleNamespace(pid=1, poll=lambda: 0)
        try:
            with mock.patch.object(self.session, '_prepare', return_value='fixture'), \
                    mock.patch.object(self.session, '_stop_previous_owned_processes'), \
                    mock.patch('auriga.cortex.session.codex.subprocess.Popen', return_value=process), \
                    mock.patch('auriga.cortex.session.codex.RecordedClient', side_effect=client):
                self.session.start([], 'fixture instructions')
                original_connection = self.session.native_connection_id
                self.session.log.close()
                self.session.start([], 'fixture instructions')
            self.assertEqual(retired, [('transport', original_connection),
                ('workers', original_connection, {'wait': True, 'cancel_futures': True})],
                'retire transport and drain workers before connection rotation')
            self.assertNotEqual(self.session.native_connection_id, original_connection)
            message = self.message(identifier=0)
            message['params']['threadId'] = 'same-thread'
            callbacks[1][2](message)
            identifier = self.session._request_id(message)
            resolved = dict(method='serverRequest/resolved', params=dict(threadId='same-thread', requestId=0))
            callbacks[0][1](resolved)
            self.assertIsNone(callbacks[0][2](message))
            callbacks[0][3](RuntimeError('old connection ended'))
            self.assertEqual(self.session.state, 'idle')
            self.assertEqual(self.store.read_state('native_request', identifier)['status'], 'responding')
            callbacks[1][1](resolved)
            self.assertEqual(self.store.read_state('native_request', identifier)['status'], 'expired')
            with self.assertRaisesRegex(ValueError, 'native_tool_call_stale_connection'):
                callbacks[0][0](message['params'])
            self.assertEqual(tool_calls, [])
            self.assertTrue(callbacks[1][0](message['params'])['success'])
            self.assertEqual(tool_calls, [message['params']])
            for state in ('disconnected', 'closed'):
                self.session.state = state
                with self.assertRaisesRegex(ValueError, 'native_tool_call_stale_connection'):
                    callbacks[1][0](message['params'])
        finally:
            self.session.client = None

    def test_explicit_turn_rejection_restores_state_and_allows_next_prompt(self):
        self.session.state = 'completed'
        original = self.session.turn_result = {'status':'completed'}
        self.session.turn_done.set()
        rejection = InvalidParamsError(-32602, 'fixture rejected request')
        accepted = SimpleNamespace(turn=SimpleNamespace(id='new-turn'))
        self.session.client = SimpleNamespace(turn_start=mock.Mock(side_effect=[rejection, accepted]))
        try:
            with self.assertRaises(InvalidParamsError):
                self.session.run_turn('rejected prompt')
            self.assertEqual(self.session.state, 'completed')
            self.assertEqual(self.session.turn_id, 'turn-owned')
            self.assertEqual(self.session.turn_result, original)
            self.assertTrue(self.session.turn_done.is_set())
            self.assertEqual(self.session.run_turn('accepted prompt'), 'new-turn')
            self.assertEqual(self.session.state, 'running')
        finally:
            self.session.client = None

    def test_turn_transport_uncertainty_stops_admission_and_rejects_retry(self):
        self.session.state = 'idle'
        self.session.on_disconnect = mock.Mock()
        self.session.client = SimpleNamespace(turn_start=mock.Mock(side_effect=TransportClosedError('fixture lost acknowledgement')))
        try:
            with self.assertRaises(TransportClosedError):
                self.session.run_turn('uncertain prompt')
            self.assertEqual(self.session.state, 'disconnected')
            self.assertTrue(self.session.turn_done.is_set())
            self.session.on_disconnect.assert_called_once_with()
            with self.assertRaisesRegex(ValueError, 'session_not_connected'):
                self.session.run_turn('must not retry')
            self.assertEqual(self.session.client.turn_start.call_count, 1)
        finally:
            self.session.client = None

    def test_rejected_start_does_not_overwrite_later_native_turn_event(self):
        self.session.state = 'idle'
        def start(*args):
            self.session._notification(dict(method='turn/started',
                params=dict(threadId='thread-owned', turn=dict(id='actually-started', status='inProgress'))))
            raise InvalidParamsError(-32602, 'late fixture rejection')
        self.session.client = SimpleNamespace(turn_start=start)
        try:
            with self.assertRaises(InvalidParamsError):
                self.session.run_turn('fixture prompt')
            self.assertEqual(self.session.state, 'running')
            self.assertEqual(self.session.turn_id, 'actually-started')
            self.assertFalse(self.session.turn_done.is_set())
        finally:
            self.session.client = None

    def test_model_relay_rejects_server_side_search_and_remote_tools(self):
        for kind in ("web_search", "web_search_preview", "mcp", "computer_use_preview", "code_interpreter"):
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, "sandbox_remote_model_tool_disabled"):
                self.session._present_model_request({"tools": [{"type": kind}]})
        for tool in ({"type": "namespace", "name": "remote", "tools": [{"type": "web_search"}]},
                     {"type": "shell", "environment": {"type": "container_auto"}}):
            for request in ({"tools": [tool]}, {"input": [{"type": "additional_tools", "tools": [tool]}]}):
                with self.subTest(request=request), self.assertRaisesRegex(ValueError, "sandbox_remote_model_tool_disabled"):
                    self.session._present_model_request(request)
        local = {"tools": [{"type": "shell", "environment": {"type": "local"}}]}
        self.assertEqual(self.session._present_model_request(local), local)
        for item in ({"type": "input_image", "image_url": "https://example.org/private.png"},
                     {"type": "input_file", "file_url": "http://127.0.0.1/private.txt"}):
            with self.subTest(item=item), self.assertRaisesRegex(ValueError, "sandbox_remote_media_url_disabled"):
                self.session._present_model_request({"input": [{"role": "user", "content": [item]}]})

    def test_native_catalog_groups_only_explicit_deferred_tools_without_mutation(self):
        tools = [dict(type='function', name=name, description=name, inputSchema={'type': 'object'}, **flags)
                 for name, flags in [('eager', {}), ('deferred', {'deferLoading': True}),
                                     ('explicit_eager', {'deferLoading': False})]]
        original = deepcopy(tools)
        catalog = _native_dynamic_tools(tools)
        self.assertEqual([tool['name'] for tool in catalog], ['eager', 'explicit_eager', DYNAMIC_TOOL_NAMESPACE])
        self.assertEqual(catalog[-1]['tools'], [original[1]])
        catalog[-1]['tools'][0]['inputSchema']['type'] = 'string'
        self.assertEqual(tools, original)
        self.assertEqual(_native_dynamic_tools([]), [])

    def test_search_history_checks_execution_and_registered_namespace_before_projection(self):
        self.session.deferred_tools = frozenset({'fixture_read'})
        checksum = 'a' * 64
        namespace = dict(type='namespace', name=DYNAMIC_TOOL_NAMESPACE, tools=[dict(type='function',
            name='fixture_read', description='Fixture value hash=' + checksum,
            parameters={'type': 'object', 'properties': {'value': {'type': 'integer'}}})])
        output = dict(type='tool_search_output', call_id='search-call', execution='client',
                      status='completed', tools=[namespace])
        request = dict(input=[dict(type='additional_tools', tools=[dict(type='tool_search', execution='client')]),
                             dict(type='tool_search_call', execution='client', call_id='search-call',
                                  arguments={'query': 'fixture_read hash=' + checksum, 'limit': 1}), output])
        projected = self.session._present_model_request(request)
        self.assertNotIn(checksum, json.dumps(projected))
        self.assertEqual(projected['input'][1]['arguments']['query'].strip(), 'fixture_read')
        self.assertEqual(projected['input'][1]['arguments']['limit'], 1)
        self.assertEqual(projected['input'][-1]['tools'][0]['tools'][0]['parameters'],
                         namespace['tools'][0]['parameters'])
        self.assertEqual(projected['input'][-1]['call_id'], 'search-call')
        for execution in (None, 'server', 'remote'):
            for item in (dict(type='tool_search', execution=execution),
                         dict(output, execution=execution),
                         dict(type='tool_search_call', execution=execution, arguments={})):
                candidate = {'tools': [item]} if item['type'] == 'tool_search' else {'input': [item]}
                with self.subTest(item=item), self.assertRaisesRegex(ValueError, 'sandbox_remote_model_tool_disabled'):
                    self.session._present_model_request(candidate)
        for found in (dict(namespace, name='unknown'),
                      dict(namespace, tools=[dict(type='function', name='not_registered')]),
                      dict(type='function', name='fixture_read'),
                      dict(namespace, tools=[dict(type='web_search')]),
                      dict(namespace, tools=[dict(type='namespace', name=DYNAMIC_TOOL_NAMESPACE,
                                                  tools=namespace['tools'])])):
            with self.subTest(found=found), self.assertRaises(ValueError):
                self.session._present_model_request({'input': [dict(output, tools=[found])]})

    def test_native_command_output_delta_accepts_initial_null_output(self):
        self.session.thread_id = "null-output-thread"
        self.session._notification({"method": "item/started", "params": {
            "threadId": self.session.thread_id, "turnId": "turn", "item": {
                "id": "command", "type": "commandExecution", "aggregatedOutput": None}}})
        self.session._notification({"method": "item/commandExecution/outputDelta", "params": {
            "threadId": self.session.thread_id, "turnId": "turn", "itemId": "command", "delta": "pip output"}})
        self.assertEqual(self.store.read_state("codex_item", "command")["aggregatedOutput"], "pip output")

    def test_native_wire_projection_filters_recursive_metadata_and_keeps_media(self):
        reference = self.store.put_artifact(b'fixture', 'fixture', 'text/plain')
        text = json.dumps(dict(hash='a'*64, source_url='https://example.test/source', version='b'*64,
            nested=dict(source_url='https://example.test/nested', hash='c'*64, digest='d'*64, measured_value=.25),
            reference=reference))
        image = dict(type='inputImage', imageUrl='data:image/png;base64,AAAA')
        audio = dict(type='inputAudio', audioUrl='data:audio/wav;base64,AAAA')
        client = RecordedClient(CodexConfig(cwd=str(self.root)), self.store,
            lambda params: dict(success=True, contentItems=[dict(type='inputText', text=text), image, audio]),
            lambda message: None)
        try:
            result = client._approval('item/tool/call', {})
            alias = self.store.states('actor_reference')[0]['alias']
            self.assertEqual(json.loads(result['contentItems'][0]['text']),
                             dict(nested=dict(measured_value=.25), reference=alias))
            self.assertEqual(result['contentItems'][1:], [image, audio])
        finally:
            client.server_pool.shutdown(wait=True)

    def test_native_actor_reply_preserves_recorded_action_after_invalid_display(self):
        from auriga.robot.motion.arx_actions import compile_action
        from auriga.runtime.harness import Harness
        from auriga.core.tool_output import ToolResponse
        from auriga.runtime.tools.builtins import output_schemas
        from auriga.runtime.tools.registry import ToolSpec
        from test_harness import FixtureRobot
        robot = FixtureRobot(self.root)
        harness = Harness(self.store, robot)
        harness.thread_id = 'thread-owned'
        harness.start()
        def perform(context, args, correlation):
            action = compile_action('move', dict(observation_id=args['observation_id'], arm='left',
                position_m=[.01, 0, 1], quaternion_xyzw=[0, 0, 0, 1]), context.identity, context.capabilities)
            return ToolResponse([dict(type='inputImage', imageUrl=5)], kind='action', data=context.execute(action))
        harness.tool_registry.register(ToolSpec(dict(type='function', name='fixture_bad_display',
            description='Recorded action with invalid display.', inputSchema=dict(type='object',
                properties={'observation_id': {'type':'string'}}, required=['observation_id'], additionalProperties=False)),
            perform, effect='physical', output_schemas=output_schemas('robot_wait')))
        message = self.message(method='item/tool/call', callId='native-action', tool='fixture_bad_display',
            arguments={'observation_id': harness.latest['observation_id']})
        client = RecordedClient(CodexConfig(cwd=str(self.root)), self.store, harness.handle_tool, lambda message: None)
        try:
            first = client._approval(message['method'], message['params'])
            repeated = client._approval(message['method'], message['params'])
            self.assertFalse(first['success'])
            self.assertEqual(first, repeated)
            self.assertEqual(robot.executions, 1)
            texts = [json.loads(item['text']) for item in first['contentItems']]
            self.assertEqual(texts[0]['receipt']['status'], 'completed')
            self.assertEqual(texts[-1]['output_contract_error']['reason'], 'invalid_native_content_item')
            self.assertTrue(all(item['type'] == 'inputText' for item in first['contentItems']))
            self.assertEqual(self.store.records('action_receipt')[0]['status'], 'completed')
        finally:
            client.server_pool.shutdown(wait=True)

    def test_main_notification_projection_avoids_global_queue_but_helper_stream_is_preserved(self):
        for route, expected in [(False, 1), (True, 101)]:
            with self.subTest(route_notifications=route):
                observed = []
                client = RecordedClient(CodexConfig(cwd=str(self.root)), self.store, lambda params: {},
                    observed.append, route_notifications=route)
                messages = iter(dict(method='fixture/global', params=dict(ordinal=i)) for i in range(100))
                def read():
                    try: return next(messages)
                    except StopIteration: raise EOFError('fixture transport ended')
                try:
                    with mock.patch('openai_codex.client.CodexClient._read_message', side_effect=read):
                        client._reader_loop()
                    self.assertEqual(len(observed), 100, 'live projection is independent of SDK queue routing')
                    # SDK fail_all leaves one terminal exception; no live backlog.
                    self.assertEqual(client._router._global_notifications.qsize(), expected)
                    if route:
                        self.assertEqual(client.next_notification().method, 'fixture/global')
                finally:
                    client.server_pool.shutdown(wait=True)

    def test_recovery_keeps_native_item_status(self):
        thread = dict(turns=[dict(id='turn-owned',status='failed',items=[dict(id='item-failed',type='commandExecution',status='failed',command='synthetic failure')])])
        self.session.client = SimpleNamespace(thread_read=lambda *args,**kwargs: SimpleNamespace(thread=SimpleNamespace(model_dump=lambda **kwargs: thread)))
        try:
            self.session.recover_history()
            self.assertEqual(self.session.state,'failed')
            item = self.store.read_state('codex_item','item-failed')
            self.assertEqual(item['status'],'failed','recovery provenance must not replace native execution state')
        finally:
            self.session.client = None

    def test_stale_request_from_prior_session_expires(self):
        self.store.set_state('native_request','native-old',dict(request_id='native-old',rpc_id=77,status='pending',thread_id='thread-owned',method='item/commandExecution/requestApproval'))
        self.session.expire_requests('recovery_without_live_rpc')
        self.assertEqual(self.store.read_state('native_request','native-old')['status'],'expired')

    def test_recovery_completes_old_partial_item(self):
        self.store.set_state('codex_item','item-partial',dict(id='item-partial',type='agentMessage',status='running',text='partial',raw_refs=['artifact:old-delta']))
        thread = dict(turns=[dict(id='turn-owned',status='completed',items=[dict(id='item-partial',type='agentMessage',text='complete recovered text')])])
        self.session.client = SimpleNamespace(thread_read=lambda *args,**kwargs: SimpleNamespace(thread=SimpleNamespace(model_dump=lambda **kwargs: thread)))
        try:
            self.session.recover_history()
            item = self.store.read_state('codex_item','item-partial')
            self.assertEqual(item['text'],'complete recovered text')
            self.assertEqual(item['status'],'completed')
            self.assertEqual(item['raw_refs'][0],'artifact:old-delta')
            self.assertEqual(self.store.artifact(item['raw_refs'][1])[0]['kind'],'codex_history_snapshot')
        finally:
            self.session.client = None

    def test_recovery_snapshot_cannot_regress_live_turn_completion(self):
        def read(*args,**kwargs):
            self.session._notification(dict(method='turn/completed',params=dict(threadId='thread-owned',turn=dict(id='turn-owned',status='completed'))))
            snapshot = dict(turns=[dict(id='turn-owned',status='inProgress',items=[])])
            return SimpleNamespace(thread=SimpleNamespace(model_dump=lambda **kwargs: snapshot))
        self.session.client = SimpleNamespace(thread_read=read)
        try:
            self.session.recover_history()
            self.assertEqual(self.session.state,'completed','an older recovery snapshot cannot undo live completion')
            self.assertTrue(self.session.turn_done.is_set())
        finally:
            self.session.client = None

    def test_failed_native_response_keeps_unknown_state(self):
        message = self.message(identifier=24)
        client = RecordedClient(CodexConfig(cwd=str(self.root)),self.store,lambda params: {},lambda message: None,self.session._server_request)
        def fail_write(payload): raise BrokenPipeError('synthetic write failed')
        client._write_message = fail_write
        try:
            client._reply_request(message)
            identifier = self.session._request_id(message)
            result = self.store.read_state('native_request',identifier)
            self.assertEqual(result['status'],'unknown')
            with self.assertRaisesRegex(ValueError,'expired|unknown|not_owned'):
                self.session.reply(identifier,{'decision':'decline'})
        finally:
            client.server_pool.shutdown(wait=True)

    def test_failed_tool_reply_invalidates_image_baseline_for_same_session(self):
        client = RecordedClient(CodexConfig(cwd=str(self.root)), self.store,
            lambda params: {'success': True, 'contentItems': []}, lambda message: None)
        client._write_message = mock.Mock(side_effect=BrokenPipeError('synthetic tool reply failed'))
        message = self.message(identifier=25, method='item/tool/call')
        try:
            client._reply_request(message)
            first = self.store.read_state('runtime', 'image_context_epoch')
            self.assertEqual(first['source'], 'tool_reply_delivery_uncertain')
            self.assertEqual(first['rpc_id'], 25)
            self.assertTrue(first['marker'])
            client._reply_request(message)
            second = self.store.read_state('runtime', 'image_context_epoch')
            self.assertNotEqual(first['marker'], second['marker'])
            self.assertEqual(self.session.thread_id, 'thread-owned')
            client._write_message.side_effect = None
            client._reply_request(message)
            self.assertEqual(self.store.read_state('runtime', 'image_context_epoch'), second)
        finally:
            client.server_pool.shutdown(wait=True)

    def test_rpc_reader_routes_response_while_tool_is_waiting(self):
        started,release = threading.Event(),threading.Event()
        routed,written = [],[]
        def tool(params):
            started.set()
            release.wait(2)
            return {'success':True,'contentItems':[]}
        client = RecordedClient(CodexConfig(cwd=str(self.root)),self.store,tool,lambda message: None)
        messages = iter([self.message(identifier=44,method='item/tool/call'),dict(id=88,result={'responsive':True})])
        def read():
            try: return next(messages)
            except StopIteration: raise EOFError('synthetic transport ended')
        client._read_message = read
        client._write_message = written.append
        client._router = SimpleNamespace(route_response=routed.append,fail_all=lambda error: None)
        try:
            reader = threading.Thread(target=client._reader_loop)
            reader.start(); reader.join(timeout=1)
            self.assertFalse(reader.is_alive(),'RPC reader cannot block on robot/human request work')
            self.assertTrue(started.wait(1))
            self.assertEqual(routed,[dict(id=88,result={'responsive':True})])
        finally:
            release.set()
            client.server_pool.shutdown(wait=True)
        self.assertEqual(written,[dict(id=44,result={'success':True,'contentItems':[]})])

    def test_orphan_cleanup_terminates_only_owned_socket_processes(self):
        uri = f'unix://{self.session.socket_path}'
        processes = [subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)', argument],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                     for argument in [uri, uri, uri + '.unrelated', uri]]
        app_server, terminal, unrelated, starting_server = processes
        self.session.ipc.mkdir(parents=True)
        terminal_file = self.root/'terminal.json'
        other_socket = self.root/'unrelated.sock'
        other_socket.touch()
        try:
            self.session.socket_path.touch()
            terminal_file.write_text(json.dumps({'pid': terminal.pid}))
            self.session._stop_previous_owned_processes({'app_server_pid': unrelated.pid})
            self.assertEqual(terminal.wait(timeout=2), -15)
            self.assertIsNone(unrelated.poll(), 'a PID with only a socket-name prefix must remain alive')
            self.assertIsNone(app_server.poll(), 'a matching process outside recorded candidates must remain alive')
            self.assertFalse(self.session.socket_path.exists())
            self.assertTrue(other_socket.exists())

            terminal_file.write_text(json.dumps({'pid': unrelated.pid}))
            self.session._stop_previous_owned_processes({'app_server_pid': app_server.pid})
            self.assertEqual(app_server.wait(timeout=2), -15)
            self.assertIsNone(unrelated.poll(), 'an unrelated recorded terminal PID must remain alive')
            self.assertIsNone(starting_server.poll(), 'a startup process requires a persisted ownership record')
            self.store.set_state('runtime', 'app_server', {'pid': starting_server.pid, 'socket_uri': uri})
            self.session._stop_previous_owned_processes(None)
            self.assertEqual(starting_server.wait(timeout=2), -15)
            self.assertIsNone(unrelated.poll(), 'early startup cleanup must preserve unrelated work')
            stopped = [event['payload']['pid'] for event in self.store.events()
                       if event['type'] == 'codex.previous_process_stopped']
            self.assertCountEqual(stopped, [app_server.pid, terminal.pid, starting_server.pid])
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=3)


if __name__=='__main__':
    unittest.main()
