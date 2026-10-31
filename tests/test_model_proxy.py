"""Actual HTTP checks for completion budgets, retries and no partial tool delivery."""
import http.client
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from auriga.cortex.session.model_proxy import ModelRequestProxy


def complete_payload(index=1):
    return ('data: ' + json.dumps(dict(type='response.completed', response=dict(
        id=f'resp-{index}', model='fixture', status='completed', output=[],
        usage=dict(input_tokens=7, output_tokens=2)))) + '\n\n').encode()


class ModelProxyTest(unittest.TestCase):
    def setUp(self):
        self.requests, self.script = [], []
        self.behavior = 'complete'
        self.failed_error = dict(code='context_length_exceeded', message='fixture-private')
        self.entered = threading.Event()
        self.release = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *_): pass
            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                owner.requests.append((self.path, body, dict(self.headers)))
                behavior = owner.script.pop(0) if owner.script else owner.behavior
                if behavior in ('blocked', 'slow'):
                    owner.entered.set()
                    owner.release.wait(3) if behavior == 'blocked' else time.sleep(1.2)
                status = (int(behavior) if behavior.isdigit() else
                          503 if behavior == 'truncated_error' else 400 if behavior == 'bad_request' else 200)
                self.send_response(status)
                self.send_header('Content-Type', 'text/event-stream')
                if status >= 400:
                    payload = json.dumps({'error': {'code': '-4003' if status == 400 and behavior != 'bad_request' else 'test',
                                                   'message': 'fixture-private'}}).encode()
                    if status == 429:
                        self.send_header('Retry-After', '.03')
                elif behavior == 'partial':
                    payload = b'data: {"type":"response.output_item.done","output_index":0,"item":{"type":"function_call","name":"unsafe_tool"}}\n\n'
                elif behavior == 'cut':
                    payload = b'data: {"type":"response.created"}\n\n'
                elif behavior == 'no_terminal':
                    payload = b'data: {"type":"response.output_text.delta","delta":"x"}\n\n'
                elif behavior == 'api_error':
                    payload = b'data: {"type":"error","code":"server_error","message":"fixture-private"}\n\n'
                elif behavior == 'compact':
                    payload = b'{"object":"response.compaction","output":[],"usage":{"input_tokens":3,"output_tokens":1}}'
                elif behavior in ('failed', 'incomplete'):
                    payload = ('data: ' + json.dumps(dict(type='response.'+behavior, response=dict(
                        status=behavior, output=[dict(type='function_call', name='unsafe_terminal_tool')],
                        error=owner.failed_error if behavior == 'failed' else None,
                        usage=dict(input_tokens=3, output_tokens=1)))) + '\n\n').encode()
                    if behavior == 'failed':
                        payload = b'data: {"type":"response.output_item.done","output_index":0,"item":{"type":"function_call","name":"unsafe_tool"}}\n\n' + payload
                else:
                    payload = complete_payload(len(owner.requests))
                if behavior not in ('trickle', 'terminal_open'):
                    self.send_header('Content-Length', str(len(payload) + (10 if behavior in ('truncated', 'truncated_error', 'cut') else 0)))
                self.send_header('Connection', 'close')
                self.end_headers()
                self.close_connection = True
                try:
                    if behavior == 'trickle':
                        for _ in range(150):
                            self.wfile.write(b': ping\n\n')
                            self.wfile.flush()
                            time.sleep(.01)
                    else:
                        self.wfile.write(payload)
                        self.wfile.flush()
                        if behavior == 'terminal_open':
                            owner.entered.set()
                            owner.release.wait(3)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        self.thread.start()
        self.upstream = f'http://127.0.0.1:{self.server.server_port}/v1'

    def tearDown(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(1)

    @staticmethod
    def request(proxy, payload=b'{"model":"fixture","input":[]}', path='/responses', headers=None):
        with urlopen(Request(proxy.base_url + path, data=payload, headers={
            'Authorization': 'Bearer fixture-private', 'Content-Type': 'application/json', **(headers or {})}), timeout=5) as response:
            return response.read()

    def assert_completed_calls(self, proxy, expected):
        # HTTP Content-Length can let the client finish reading before the
        # server records successful delivery. Wait for that bookkeeping, not
        # for unrelated active requests or the asynchronous callback worker.
        deadline = time.monotonic() + 2
        while True:
            with proxy.lock:
                finished = any(event['kind'] == 'model_call_finished'
                               and event['completed_calls'] >= expected
                               for event in proxy.events)
                count = proxy.request_count
            if finished or time.monotonic() >= deadline:
                break
            time.sleep(.005)
        self.assertTrue(finished, 'completed delivery bookkeeping did not finish')
        self.assertEqual(count, expected)

    def test_exact_forwarding_180_completed_budget_and_private_metrics(self):
        limits = []
        with tempfile.TemporaryDirectory() as directory:
            metrics = Path(directory) / 'transport.jsonl'
            with ModelRequestProxy(self.upstream, on_limit=limits.append, metrics_path=metrics) as proxy:
                self.assertEqual(proxy.max_retries, 12)
                self.assertIsNone(proxy.request_timeout_s)
                payload = b'{"input":"literal \\u4e2d", "stream":true}'
                for index in range(180):
                    self.assertEqual(self.request(proxy, payload), complete_payload(index+1))
                for _ in range(2):
                    with self.assertRaises(HTTPError) as rejected:
                        self.request(proxy)
                    self.assertEqual(rejected.exception.code, 429)
                self.assert_completed_calls(proxy, 180)
            self.assertEqual(len(self.requests), 180)
            self.assertEqual(self.requests[0][:2], ('/v1/responses', payload))
            self.assertEqual(self.requests[0][2]['Authorization'], 'Bearer fixture-private')
            self.assertEqual(len(limits), 1)
            text = metrics.read_text()
            self.assertNotIn('fixture-private', text)
            self.assertNotIn('Authorization', text)
            events = [json.loads(line) for line in text.splitlines()]
            first = [e for e in events if e.get('logical_call_index') == 1]
            phases = [next(e for e in first if e['kind'] == kind) for kind in
                      ('model_request_body_received', 'model_upstream_connected', 'model_request_forwarded')]
            self.assertEqual(phases[0]['body_bytes'], len(payload))
            self.assertEqual([e['wall_time'] for e in phases], sorted(e['wall_time'] for e in phases))
            finished = [e for e in events if e['kind'] == 'model_request_finished']
            self.assertEqual(len(finished), 180)
            self.assertTrue(all(e['transport_complete'] and not e['timed_out'] and e['usage_known'] for e in finished))
            self.assertEqual(metrics.stat().st_mode & 0o777, 0o600)

    def test_partial_output_is_retried_then_blocked_without_tool_delivery(self):
        self.behavior = 'partial'
        with ModelRequestProxy(self.upstream) as proxy:
            with mock.patch.object(proxy.policy, '_retry_delay_seconds', return_value=0):
                raw = self.request(proxy, b'{"stream":true}')
            self.assertNotIn(b'unsafe_tool', raw)
            self.assertIn(b'model_request_failed', raw)
            # Nothing was released, so the stream without a terminal is resent within the bounded retries.
            self.assertEqual(proxy.attempt_count, 13)
            self.assertEqual(proxy.request_count, 0)
            self.assertTrue(proxy.transport_blocked)
            with self.assertRaisesRegex(ValueError, 'explicit_provider_error'):
                proxy.manual_retry()
            with self.assertRaises(HTTPError) as denied:
                self.request(proxy)
            self.assertEqual(denied.exception.code, 409)
            self.assertEqual(len(self.requests), 13)

    def test_complete_stream_without_terminal_retries_without_releasing_output(self):
        self.script = ['no_terminal', 'complete']
        with ModelRequestProxy(self.upstream) as proxy:
            with mock.patch.object(proxy.policy, '_retry_delay_seconds', return_value=.001):
                self.assertIn(b'response.completed', self.request(proxy))
            self.assertEqual(proxy.attempt_count, 2)
            self.assert_completed_calls(proxy, 1)
            self.assertFalse(proxy.transport_blocked)
            retries = [e for e in proxy.events if e['kind'] == 'model_request_retry']
            self.assertEqual([e['error']['classification'] for e in retries], ['stream_incomplete'])
        self.behavior = 'no_terminal'
        with ModelRequestProxy(self.upstream) as proxy:
            with mock.patch.object(proxy.policy, '_retry_delay_seconds', return_value=0):
                raw = self.request(proxy, b'{"stream":true}')
            self.assertIn(b'model_request_failed', raw)
            self.assertNotIn(b'"delta":"x"', raw)
            self.assertEqual(proxy.attempt_count, 13)
            self.assertEqual(proxy.request_count, 0)
            self.assertTrue(proxy.transport_blocked)

    def test_host_auth_overrides_client_case_insensitively_without_logging_credentials(self):
        host = {'authorization': 'Bearer fixture-host-secret', 'chatgpt-account-id': 'fixture-host-account'}
        with tempfile.TemporaryDirectory() as directory:
            metrics = Path(directory) / 'transport.jsonl'
            with ModelRequestProxy(self.upstream, upstream_auth_headers=host, metrics_path=metrics,
                                   max_retries=1) as proxy:
                host['authorization'] = 'Bearer changed-after-construction'
                self.script = ['503', 'complete']
                self.request(proxy, headers={'aUtHoRiZaTiOn': 'Bearer fixture-client-secret',
                    'cHaTgPt-AcCoUnT-Id': 'fixture-client-account', 'Cookie': 'fixture-client-cookie',
                    'X-Api-Key': 'fixture-client-api-key', 'Api-Key': 'fixture-client-api-key',
                    'OpenAI-Organization': 'fixture-client-org', 'OpenAI-Project': 'fixture-client-project'})
                self.behavior = 'compact'
                connection = http.client.HTTPConnection('127.0.0.1', proxy.server.server_port, timeout=5)
                try:
                    connection.request('POST', '/responses/compact', body=b'{"model":"fixture","input":[]}',
                                       headers={'Content-Type': 'application/json'})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    response.read()
                finally:
                    connection.close()
                self.assert_completed_calls(proxy, 2)
            self.assertEqual(len(self.requests), 3)
            for _, _, headers in self.requests:
                self.assertFalse({'cookie', 'x-api-key', 'api-key', 'openai-organization', 'openai-project'}
                                 .intersection(key.lower() for key in headers))
                authentication = {key.lower(): value for key, value in headers.items()
                                  if key.lower() in {'authorization', 'chatgpt-account-id'}}
                self.assertEqual(authentication, {'authorization': 'Bearer fixture-host-secret',
                                                  'chatgpt-account-id': 'fixture-host-account'})
            recorded = metrics.read_text() + json.dumps(proxy.events)
            for private in ('fixture-host-secret', 'fixture-host-account', 'fixture-client-secret',
                            'fixture-client-account', 'changed-after-construction'):
                self.assertNotIn(private, recorded)

    def test_host_auth_mapping_strips_client_account_and_can_explicitly_disable_auth(self):
        for host in ({'Authorization': 'Bearer fixture-host-secret'}, {}):
            with self.subTest(host_headers=list(host)), ModelRequestProxy(
                    self.upstream, upstream_auth_headers=host) as proxy:
                self.request(proxy, headers={'ChatGPT-Account-Id': 'must-not-select-client-account'})
                sent = {key.lower(): value for key, value in self.requests[-1][2].items()}
                self.assertNotIn('chatgpt-account-id', sent)
                self.assertEqual(sent.get('authorization'), host.get('Authorization'))

    def test_invalid_host_auth_is_rejected_without_echoing_values(self):
        for value in ([], {'Host': 'fixture-secret'}, {'Authorization': 'fixture-secret\r\nInjected: yes'},
                      {'Authorization': ''}, {'Authorization': 123},
                      {'Authorization': 'fixture-secret', 'authorization': 'duplicate-secret'}):
            with self.subTest(header_names=list(value)):
                with self.assertRaises(ValueError) as raised:
                    ModelRequestProxy(self.upstream, upstream_auth_headers=value)
                self.assertEqual(str(raised.exception), 'invalid_upstream_auth_headers')

    def test_session_projects_resumed_history_and_compact_before_actual_http(self):
        from auriga.cortex.session.codex import CodexSession
        from auriga.core.config import Config
        from auriga.core.storage.store import Store
        checksum, native_id = 'f' * 64, 'b' * 64
        image = dict(type='input_image', image_url='data:image/png;base64,' + 'c' * 64)
        audio = dict(type='input_audio', input_audio=dict(data='d' * 64, format='wav'))
        schema = dict(type='object', properties={'sha256': dict(type='string', enum=[checksum]),
                                               'note': dict(type='string', description='build ' + checksum)},
                      required=['sha256'], additionalProperties=False)
        request = dict(model='fixture-' + native_id, instructions='old startup sha256=' + checksum,
            previous_response_id=native_id, metadata=dict(thread_id=native_id),
            input=[dict(role='user', id=native_id, content=[
                dict(type='input_text', text='Recovered user instruction ' + checksum), image, audio]),
                dict(type='function_call', id=native_id, call_id=native_id, name='fixture',
                     arguments=json.dumps(dict(sha256=checksum, position_m=[0.123456789]))),
                dict(type='function_call_output', call_id=native_id,
                     output=json.dumps(dict(hash=checksum, status='completed'))),
                dict(type='function_call_output', call_id=native_id, output=[
                    dict(type='input_text', text='Recovered log hash: ' + checksum), image]),
                dict(type='reasoning', id=native_id, encrypted_content=checksum,
                     summary=[dict(type='summary_text', text='Recovered summary ' + checksum)]),
                dict(type='custom_tool_call', call_id=native_id, name='fixture', input='old source ' + checksum),
                dict(role='assistant', content=[dict(type='refusal', refusal='Rejected ' + checksum)])],
            tools=[dict(type='function', name='fixture', description='build ' + checksum, parameters=schema)],
            text=dict(format=dict(type='json_schema', name='fixture', schema=schema)))
        original = deepcopy(request)
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory, dict(run_id='projection', episode_id='ep', env_instance_id='env', env_generation=1))
            self.addCleanup(store.close)
            session = CodexSession(directory, store, lambda _: {}, config=Config.from_dict({}))
            with ModelRequestProxy(self.upstream, image_reuse=True,
                                   request_projection=session._present_model_request) as proxy:
                for index, endpoint in enumerate(('/responses', '/responses/compact'), 1):
                    self.behavior = 'complete' if index == 1 else 'compact'
                    self.request(proxy, json.dumps(request).encode(), path=endpoint)
                    self.assert_completed_calls(proxy, index)
                    sent = json.loads(self.requests[-1][1])
                    self.assertNotIn(checksum, sent['instructions'])
                    self.assertNotIn(checksum, json.dumps(sent['tools']))
                    self.assertNotIn(checksum, json.dumps(sent['text']))
                    for item in sent['input']:
                        readable = {key: value for key, value in item.items()
                                    if key not in {'id', 'call_id', 'encrypted_content'}}
                        self.assertNotIn(checksum, json.dumps(readable))
                    self.assertEqual(sent['model'], request['model'])
                    self.assertEqual(sent['previous_response_id'], native_id)
                    self.assertEqual(sent['metadata'], request['metadata'])
                    self.assertEqual(sent['input'][1]['id'], native_id)
                    self.assertEqual(sent['input'][1]['call_id'], native_id)
                    self.assertEqual(sent['input'][4]['encrypted_content'], checksum)
                    self.assertEqual(sent['input'][0]['content'][1:], [image, audio])
                    if index == 1:
                        self.assertEqual(sent['input'][3]['output'][1]['type'], 'input_text')
                    else:
                        self.assertEqual(sent['input'][3]['output'][1], image)
                    projected_schema = sent['tools'][0]['parameters']
                    self.assertEqual(set(projected_schema['properties']), {'sha256', 'note'})
                    self.assertEqual(projected_schema['required'], ['sha256'])
                    self.assertEqual(json.loads(sent['input'][1]['arguments'])['position_m'], [0.123456789])
                self.assertEqual(proxy.attempt_count, 2)
                self.assertFalse(proxy.transport_blocked)
        self.assertEqual(request, original)

    def test_official_task_sections_survive_turn_and_http_projection_verbatim(self):
        from auriga.cortex.session.codex import CodexSession
        from auriga.core.config import Config
        from auriga.cortex.context.public_task_rules import CATALOG_PATH, public_task_rules
        from auriga.core.storage.store import Store
        texts = [public_task_rules(task)["text"]
                 for task in json.loads(CATALOG_PATH.read_text())["tasks"]]
        self.assertEqual(len(texts), 54)
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory, dict(run_id='official-text', episode_id='ep',
                                          env_instance_id='env', env_generation=1))
            self.addCleanup(store.close)
            session = CodexSession(directory, store, lambda _: {}, config=Config.from_dict({}))
            inputs = []
            for text in texts:
                projected = session.presentation.present_text(text)
                self.assertEqual(projected, text)
                inputs.append(dict(role='user', content=[dict(type='input_text', text=projected)]))
            request = dict(model='fixture', input=inputs)
            with ModelRequestProxy(self.upstream, request_projection=session._present_model_request) as proxy:
                for endpoint, behavior in (('/responses', 'complete'), ('/responses/compact', 'compact')):
                    self.behavior = behavior
                    self.request(proxy, json.dumps(request).encode(), path=endpoint)
                    sent = json.loads(self.requests[-1][1])
                    self.assertEqual([item['content'][0]['text'] for item in sent['input']], texts)

    def test_request_projection_failure_never_forwards_or_charges_completion(self):
        def broken(_request):
            raise ValueError('synthetic_projection_failure')
        with ModelRequestProxy(self.upstream, request_projection=broken) as proxy:
            with self.assertRaises(HTTPError) as rejected:
                self.request(proxy)
            self.assertEqual(rejected.exception.code, 502)
            self.assert_completed_calls(proxy, 0)
            self.assertEqual(self.requests, [])
            self.assertEqual(proxy.attempt_count, 0)
            self.assertEqual(proxy.request_count, 0)
            self.assertEqual(proxy.reserved_calls, 0)
            self.assertTrue(proxy.transport_blocked)

    def test_request_context_uses_owned_attribution_before_projection(self):
        calls, identities = [], []
        def context(request, identity):
            calls.append('context')
            identities.append(dict(identity))
            identity['source'] = 'callback_cannot_reassign_owner'
            return {**request, 'context': dict(text='current context ' + str(len(identities)),
                                              private_note='remove before forwarding')}
        def project(request):
            calls.append('projection')
            self.assertIn('private_note', request['context'])
            return {**request, 'context': {'text': request['context']['text']}}
        with ModelRequestProxy(self.upstream, request_context=context, request_projection=project) as proxy:
            self.request(proxy, headers={'thread-id': 'actor-thread'})
            proxy.register_thread('helper-thread', 'sol_visual_review', 'review-owned')
            self.request(proxy, headers={'thread-id': 'helper-thread',
                'X-Auriga-Source': 'actor', 'X-Auriga-Request-Id': 'untrusted'})
            self.behavior = 'compact'
            self.request(proxy, path='/responses/compact', headers={'thread-id': 'actor-thread'})
            self.assert_completed_calls(proxy, 3)
            self.assertEqual((proxy.logical_call_count, proxy.attempt_count), (3, 3))
            finished = [event for event in proxy.events if event['kind'] == 'model_request_finished']
            self.assertEqual([item['source'] for item in finished], ['actor', 'sol_visual_review', 'actor'])
        self.assertEqual(calls, ['context', 'projection'] * 3)
        self.assertEqual([(item['endpoint'], item['native_thread_id'], item['source'], item['caller_request_id'])
                         for item in identities], [
            ('/responses', 'actor-thread', 'actor', ''),
            ('/responses', 'helper-thread', 'sol_visual_review', 'review-owned'),
            ('/responses/compact', 'actor-thread', 'actor', '')])
        self.assertEqual(len({item['logical_call_id'] for item in identities}), 3)
        self.assertEqual([json.loads(body)['context'] for _, body, _ in self.requests],
            [dict(text='current context ' + str(index)) for index in (1, 2, 3)])

    def test_context_only_is_serialized_once_per_logical_request_across_retries(self):
        calls = []
        def context(request, identity):
            calls.append(identity)
            return {**request, 'context_sequence': len(calls)}
        self.script = ['503', 'complete', 'complete']
        with ModelRequestProxy(self.upstream, request_context=context, max_retries=1) as proxy:
            with mock.patch.object(proxy.policy, '_retry_delay_seconds', return_value=.001):
                self.request(proxy)
                self.request(proxy)
            self.assert_completed_calls(proxy, 2)
            self.assertEqual((proxy.logical_call_count, proxy.attempt_count), (2, 3))
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.requests[0][1], self.requests[1][1])
        self.assertEqual([json.loads(body)['context_sequence'] for _, body, _ in self.requests], [1, 1, 2])

    def test_live_task_attention_reaches_actor_http_after_filtering_without_history_accumulation(self):
        from auriga.cortex.session.codex import CodexSession
        from auriga.cortex.context.task_planning import DECISION_BOUNDARY_INSTRUCTIONS
        from auriga.core.storage.store import Store
        from auriga.cortex.context.task_planning import TaskPlanning
        checksum = 'f' * 64
        plan = 'Move the target using normal action feedback. sha256=' + checksum
        attention = [dict(concern=concern, check=check,
                          when='Before work that depends on the external response.')
                     for concern, check in (
                         ('External response is expected.', 'Check the normal final RGB for the response.'),
                         ('The response did not occur; the prerequisite remains unresolved.',
                          'Revise the alignment hypothesis or select independent work.'))]
        request = dict(model='fixture', instructions='Fixture instructions remain unchanged.',
            input=[dict(role='user', content=[dict(type='input_text', text='Continue the current task.')])])
        original = deepcopy(request)
        payload = json.dumps(request).encode()
        with tempfile.TemporaryDirectory() as directory:
            identity = dict(run_id='attention-http', episode_id='ep', env_instance_id='env', env_generation=1)
            store = Store(directory, identity)
            self.addCleanup(store.close)
            planning = TaskPlanning(store)
            observation = dict(identity, observation_id='obs_0')
            planning.call(dict(plan=plan, attention=[attention[0]], ledger=[]), identity, observation, {})
            session = CodexSession(directory, store, lambda _: {}, task_planning=planning)
            session.thread_id, session.turn_id, session.state = 'actor-thread', 'ongoing-turn', 'running'
            with ModelRequestProxy(self.upstream, request_context=session._task_decision_context,
                    request_projection=session._present_model_request, max_retries=1) as proxy, \
                    mock.patch.object(planning, 'decision_context', wraps=planning.decision_context) as snapshot, \
                    mock.patch.object(proxy.policy, '_retry_delay_seconds', return_value=.001):
                proxy.register_thread('actor-thread', 'actor', 'decision-test')
                proxy.register_thread('helper-thread', 'sol_visual_review', 'review-test')
                self.behavior = 'complete'
                self.request(proxy, payload, path='/responses', headers={'thread-id': 'actor-thread'})
                first_normal = json.loads(self.requests[-1][1])
                self.assertEqual(first_normal, original)
                extended = dict(original, input=original['input'] + [
                    dict(role='user', content=[dict(type='input_text', text='Follow-up turn.')])])
                self.request(proxy, json.dumps(extended).encode(), path='/responses', headers={'thread-id': 'actor-thread'})
                second_normal = json.loads(self.requests[-1][1])
                self.assertEqual(second_normal['input'][:len(first_normal['input'])], first_normal['input'])
                self.assertEqual(snapshot.call_count, 0)

                for index in (1, 2):
                    selected = [attention[index]] if index < len(attention) else []
                    observation = dict(identity, observation_id=f'obs_{index}')
                    planning.call(dict(operation='update', attention=selected), identity, observation,
                                  dict(thread_id=session.thread_id, turn_id=session.turn_id))
                    if index == 1:
                        self.script = ['503', 'complete']
                    self.behavior = 'compact'
                    self.request(proxy, payload, path='/responses/compact', headers={'thread-id': 'actor-thread'})
                    if index == 1:
                        self.assertEqual(self.requests[-2][1], self.requests[-1][1])
                    sent = json.loads(self.requests[-1][1])
                    self.assertEqual(sent['input'][:-1], original['input'])
                    self.assertEqual(sent['instructions'], original['instructions'])
                    self.assertEqual(len(sent['input']), len(original['input']) + 1)
                    reminder = sent['input'][-1]
                    self.assertEqual(reminder['role'], 'assistant')
                    text = reminder['content'][0]['text']
                    self.assertIn('Current task decision boundary', text)
                    self.assertEqual(text.count(DECISION_BOUNDARY_INSTRUCTIONS), 1)
                    self.assertNotIn('tested-candidate ledger', text)
                    context = json.loads(text.rsplit('\n', 1)[-1])
                    self.assertEqual(context['revision'], index + 1)
                    self.assertEqual(context['plan_id'], 'P' + str(index + 1))
                    self.assertEqual(context['attention'], selected)
                    self.assertEqual(context['plan_observation_id'], 'obs_0')
                    self.assertEqual(context['attention_observation_id'], f'obs_{index}')
                    self.assertNotIn('based_on_observation_id', context)
                    self.assertTrue(context['plan'].startswith('Move the target using normal action feedback.'))
                    self.assertNotIn(checksum, json.dumps(sent))
                    for old in attention[:index]:
                        self.assertNotIn(old['concern'], json.dumps(sent))
                    self.assertEqual(planning.current(identity)['plan'], plan)
                    self.assertEqual(snapshot.call_count, index)
                    self.assertEqual((session.turn_id, session.state), ('ongoing-turn', 'running'))
                    self.assertEqual(store.events_by_type(['codex.turn/completed',
                        'benchmark.actor_turn_continuation']), [])
                    self.assert_completed_calls(proxy, index + 2)
                self.behavior = 'compact'
                self.request(proxy, payload, path='/responses/compact', headers={'thread-id': 'helper-thread',
                    'X-Auriga-Source': 'actor', 'X-Auriga-Request-Id': 'spoofed-actor'})
                self.assertEqual(json.loads(self.requests[-1][1]), original)
                self.assertEqual(snapshot.call_count, 2)
                self.assert_completed_calls(proxy, 5)
                self.assertEqual((proxy.logical_call_count, proxy.attempt_count), (5, 6))
                self.assertFalse(proxy.transport_blocked)
            self.assertEqual(len(self.requests), 6)
        self.assertEqual(request, original)

    def test_recovery_prompt_and_live_plan_deliver_next_step_guidance_once(self):
        from types import SimpleNamespace
        from auriga.cortex.session.codex import CodexSession
        from auriga.cortex.supervision.recovery_guidance import EVIDENCE_INSTRUCTIONS, recovery_guidance
        from auriga.cortex.context.task_planning import DECISION_BOUNDARY_INSTRUCTIONS
        from auriga.core.storage.store import Store
        from auriga.cortex.context.task_planning import TaskPlanning
        with tempfile.TemporaryDirectory() as directory:
            identity = dict(run_id='recovery-http', episode_id='ep', env_instance_id='env', env_generation=1)
            store = Store(directory, identity)
            self.addCleanup(store.close)
            planning = TaskPlanning(store)
            observation = dict(identity, observation_id='obs_0')
            planning.call(dict(plan='Resolve the unmet prerequisite before dependent work.', ledger=[]),
                          identity, observation, {})
            _, continuation = recovery_guidance(SimpleNamespace(latest=observation),
                evidence=dict(version='recovery-evidence-v1', tested_candidate_count=0))
            request = dict(model='fixture', instructions='Fixture instructions remain unchanged.',
                input=[dict(role='user', content=[dict(type='input_text', text=continuation)])])
            original = deepcopy(request)
            session = CodexSession(directory, store, lambda _: {}, task_planning=planning)
            session.thread_id = 'actor-thread'
            with ModelRequestProxy(self.upstream, request_context=session._task_decision_context,
                    request_projection=session._present_model_request) as proxy:
                proxy.register_thread('actor-thread', 'actor', 'decision-test')
                self.request(proxy, json.dumps(request).encode(), path='/responses', headers={'thread-id': 'actor-thread'})
                normal_sent = json.loads(self.requests[-1][1])
                self.assertEqual(normal_sent, original)
                self.behavior = 'compact'
                self.request(proxy, json.dumps(request).encode(), path='/responses/compact', headers={'thread-id': 'actor-thread'})
                sent = json.loads(self.requests[-1][1])
                self.assertEqual(sent['instructions'], original['instructions'])
                self.assertEqual(sent['input'][:-1], original['input'])
                self.assertEqual(sent['input'][-1]['role'], 'assistant')
                text = '\n'.join(item['text'] for message in sent['input'] for item in message['content'])
                self.assertEqual(text.count(DECISION_BOUNDARY_INSTRUCTIONS), 1)
                self.assertEqual(text.count(EVIDENCE_INSTRUCTIONS), 1)
                self.assert_completed_calls(proxy, 2)
            self.assertEqual(request, original)

    def test_context_and_following_projection_fail_closed_without_upstream_calls(self):
        for failed_phase in ('context', 'projection'):
            with self.subTest(failed_phase=failed_phase):
                calls = []
                def context(request, identity):
                    calls.append('context')
                    if failed_phase == 'context':
                        raise ValueError('synthetic_context_failure')
                    return dict(request, context='not yet filtered')
                def project(request):
                    calls.append('projection')
                    raise ValueError('synthetic_projection_failure')
                with ModelRequestProxy(self.upstream, request_context=context, request_projection=project) as proxy:
                    with self.assertRaises(HTTPError) as rejected:
                        self.request(proxy)
                    self.assertEqual(rejected.exception.code, 502)
                    self.assert_completed_calls(proxy, 0)
                    self.assertEqual((proxy.attempt_count, proxy.request_count, proxy.reserved_calls), (0, 0, 0))
                    self.assertTrue(proxy.transport_blocked)
                self.assertEqual(calls, ['context'] if failed_phase == 'context' else ['context', 'projection'])
        self.assertEqual(self.requests, [])

    def test_request_context_requires_callable(self):
        for value in (False, 1, {}, 'callback'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'request_context_requires_callable'):
                ModelRequestProxy(self.upstream, request_context=value)

    def test_explicit_http_errors_retry_to_success_without_manual_reset(self):
        for status in ('400', '408', '429', '503', 'truncated_error'):
            with self.subTest(status=status):
                self.script = [status, 'complete']
                with ModelRequestProxy(self.upstream) as proxy:
                    with mock.patch.object(proxy.policy, '_retry_delay_seconds', return_value=.001):
                        self.assertIn(b'response.completed', self.request(proxy))
                    self.assertEqual(proxy.attempt_count, 2)
                    self.assert_completed_calls(proxy, 1)
                    self.assertFalse(proxy.transport_blocked)
                    self.assertEqual(sum(e['kind']=='model_request_retry' for e in proxy.events), 1)
                    self.assertNotIn('fixture-private', json.dumps(proxy.events))

    def test_retry_exhaustion_is_bounded_and_auth_errors_are_terminal(self):
        for status, expected in (('503', 13), ('401', 1), ('403', 1), ('bad_request', 1), ('404', 1)):
            with self.subTest(status=status):
                self.behavior = status
                with ModelRequestProxy(self.upstream) as proxy:
                    with mock.patch.object(proxy.policy, '_retry_delay_seconds', return_value=0):
                        with self.assertRaises(HTTPError) as error:
                            self.request(proxy)
                    self.assertEqual(error.exception.code, 400 if status == 'bad_request' else int(status))
                    self.assertEqual(proxy.attempt_count, expected)
                    self.assertEqual(proxy.request_count, 0)
                    self.assertTrue(proxy.transport_blocked)
                    with self.assertRaises(HTTPError) as denied:
                        self.request(proxy, path='/responses/compact')
                    self.assertEqual(denied.exception.code, 409)
                    self.assertEqual(proxy.attempt_count, expected)

    def test_connect_failures_retry_because_no_request_reached_the_provider(self):
        import socket
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            closed = f'http://127.0.0.1:{probe.getsockname()[1]}/v1'
        # Refused connects (like DNS resolution failures) retry, then the restored upstream succeeds.
        with ModelRequestProxy(closed) as proxy:
            def restore(*_):
                proxy.upstream_url = self.upstream
                return .001
            with mock.patch.object(proxy.policy, '_retry_delay_seconds', side_effect=restore):
                self.assertIn(b'response.completed', self.request(proxy))
            self.assertEqual(proxy.attempt_count, 2)
            self.assert_completed_calls(proxy, 1)
            self.assertFalse(proxy.transport_blocked)
            retries = [e for e in proxy.events if e['kind'] == 'model_request_retry']
            self.assertEqual(len(retries), 1)
            self.assertEqual(retries[0]['error']['classification'], 'transport_connect_failed')
        # A provider that never becomes reachable still ends after the bounded retries.
        with ModelRequestProxy(closed) as proxy:
            with mock.patch.object(proxy.policy, '_retry_delay_seconds', return_value=0):
                with self.assertRaises(HTTPError):
                    self.request(proxy)
            self.assertEqual(proxy.attempt_count, 13)
            self.assertEqual(proxy.request_count, 0)
            self.assertTrue(proxy.transport_blocked)

    def test_client_disconnect_after_full_delivery_does_not_block_episode(self):
        # A helper (visual review, self-briefing) may close its connection between
        # delivering the full response and the call being marked finished.
        def state(delivered):
            return dict(cancel=threading.Event(), finished=threading.Event(), cancel_reason=None,
                        identity=dict(logical_call_id='helper:1'), process=None,
                        reading_client_body=False, client=None, delivered=delivered)
        with ModelRequestProxy(self.upstream) as proxy:
            done = state(True)
            proxy._cancel(done, 'client_disconnected')
            self.assertIsNone(proxy.blocked_error)
            self.assertFalse(done['cancel'].is_set())
            pending = state(False)
            proxy._cancel(pending, 'client_disconnected')
            self.assertEqual(proxy.blocked_error['error_type'], 'ClientDisconnected')
            self.assertTrue(pending['cancel'].is_set())

    def test_terminal_sse_finishes_without_waiting_for_http_eof(self):
        self.behavior = 'terminal_open'
        with ModelRequestProxy(self.upstream) as proxy:
            started = time.monotonic()
            self.assertEqual(self.request(proxy), complete_payload())
            self.assertLess(time.monotonic()-started, 1)
            self.assertTrue(self.entered.is_set())
            self.assertFalse(self.release.is_set())
            self.assertEqual(proxy.attempt_count, 1)
        # The complete terminal is authoritative even if a HTTP length claims
        # more body bytes; incomplete streams without a terminal remain blocked.
        self.behavior = 'truncated'
        with ModelRequestProxy(self.upstream) as proxy:
            self.assertIn(b'response.completed', self.request(proxy))

    def test_slow_response_ignores_legacy_deadline_and_emits_only_keepalives(self):
        self.behavior = 'slow'
        with ModelRequestProxy(self.upstream, max_requests=1) as proxy:
            # An inherited 60-second policy timeout is no longer read, either.
            with mock.patch.object(proxy.policy, '_DEFAULT_TIMEOUT_S', .01):
                raw = self.request(proxy, b'{"stream":true}', headers={
                    'X-Auriga-Deadline-Unix-Ms': str((time.time()+.05)*1000)})
            events = [json.loads(line[5:]) for line in raw.splitlines() if line.startswith(b'data:')]
            self.assertGreaterEqual(len(events), 2)
            self.assertTrue(all(e == {'type':'auriga.keepalive'} for e in events[:-1]))
            self.assertEqual(events[-1]['type'], 'response.completed')
            self.assertEqual(proxy.attempt_count, 1)
            self.assertFalse(any(e['kind']=='model_request_timeout' for e in proxy.events))
            self.assertNotIn('X-Auriga-Deadline-Unix-Ms', self.requests[-1][2])

    def test_failed_and_incomplete_terminals_never_release_any_tools_or_charge_calls(self):
        for status in ('failed', 'incomplete'):
            with self.subTest(status=status):
                self.behavior = status
                with ModelRequestProxy(self.upstream) as proxy:
                    raw = self.request(proxy, b'{"stream":true}')
                    self.assertIn(('response.'+status).encode(), raw)
                    self.assertNotIn(b'unsafe', raw)
                    self.assertEqual(proxy.request_count, 0)
                    self.assertEqual(proxy.attempt_count, 1)
                    self.assertTrue(proxy.transport_blocked)
                    self.assertEqual(proxy.blocked_error['manual_retry_allowed'], status=='failed')

    def test_failed_response_classification_controls_retry_without_releasing_failed_tools(self):
        for error, classification, retry in (({'code':'server_error'}, 'retryable', True),
                ({'code':'server_is_overloaded'}, 'server_overloaded', True),
                ({'code':'context_length_exceeded'}, 'context_window_exceeded', False),
                ({'code':'invalid_api_key'}, 'authentication_error', False),
                ({'type':'invalid_request_error'}, 'configuration_error', False),
                ({'type':'invalid_request_error','code':'image_compression_failed'}, 'retryable', True),
                ({'code':'refusal'}, 'refusal', False),
                ({'code':'insufficient_quota'}, 'quota_exceeded', False),
                ({'code':'flex_unavailable'}, 'flex_unavailable', False),
                ({'code':'rate_limit_exceeded','message':'Please try again in 30 ms.'}, 'rate_limit', True)):
            with self.subTest(classification=classification):
                self.failed_error, self.behavior = error, 'failed'
                self.script = ['failed', 'complete']
                with ModelRequestProxy(self.upstream) as proxy:
                    with mock.patch.object(proxy.policy, '_retry_delay_seconds', return_value=.001):
                        raw = self.request(proxy, b'{"stream":true}')
                    self.assertNotIn(b'unsafe', raw)
                    self.assertEqual(proxy.attempt_count, 2 if retry else 1)
                    self.assertEqual(proxy.request_count, int(retry))
                    finished = next(e for e in proxy.events if e['kind']=='model_request_finished')
                    self.assertEqual(finished['usage'], dict(input_tokens=3,output_tokens=1))
                    self.assertEqual(finished['response_error']['classification'], classification)
                    self.assertEqual(any(e['kind']=='model_request_retry' for e in proxy.events), retry)
                    self.assertEqual(proxy.transport_blocked, not retry)

    def test_request_deadlines_remain_disabled_and_retry_budget_is_validated(self):
        for kwargs in ({'request_timeout_s':90}, {'max_retries':13}, {'max_retries':-1}, {'max_retries':True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ModelRequestProxy(self.upstream, **kwargs)
        with ModelRequestProxy(self.upstream, max_retries=None) as proxy:
            self.assertEqual(proxy.max_retries, proxy.policy._DEFAULT_MAX_RETRIES)

    def test_explicit_sse_error_exhausts_retry_budget_and_records_safe_evidence(self):
        self.behavior = 'api_error'
        with ModelRequestProxy(self.upstream, max_retries=1) as proxy:
            with mock.patch.object(proxy.policy, '_retry_delay_seconds', return_value=.001):
                raw = self.request(proxy, b'{"stream":true}')
            self.assertIn(b'server_error', raw)
            self.assertTrue(proxy.transport_blocked)
            self.assertTrue(proxy.blocked_error['manual_retry_allowed'])
            self.assertEqual(proxy.attempt_count, 2)
            self.assertNotIn('fixture-private', json.dumps(proxy.events))

    def test_wait_clock_counts_union_of_concurrent_actor_and_helper_requests(self):
        self.behavior = 'blocked'
        results = []
        with ModelRequestProxy(self.upstream) as proxy:
            started = time.monotonic()
            threads = [threading.Thread(target=lambda source=source: results.append(
                self.request(proxy, headers={'X-Auriga-Source':source}))) for source in ('actor','helper')]
            for thread in threads: thread.start()
            limit = time.monotonic()+2
            while len(self.requests)<2 and time.monotonic()<limit: time.sleep(.01)
            self.assertEqual(len(self.requests), 2)
            self.assertTrue(proxy.pending_requests)
            first = proxy.model_wait_seconds
            time.sleep(.12)
            elapsed = proxy.model_wait_seconds-first
            self.assertGreaterEqual(elapsed, .1)
            self.assertLess(elapsed, .22)  # Two waiting calls do not double the clock.
            self.release.set()
            for thread in threads: thread.join(2)
            self.assertEqual(len(results), 2)
            limit = time.monotonic()+1
            while proxy.pending_requests and time.monotonic()<limit: time.sleep(.01)
            self.assertFalse(proxy.pending_requests)
            finished = proxy.model_wait_seconds
            time.sleep(.03)
            self.assertEqual(proxy.model_wait_seconds, finished)
            self.assertLessEqual(finished, time.monotonic()-started)

    def test_pending_request_and_synthetic_keepalive_do_not_refresh_activity(self):
        self.behavior = 'blocked'
        forwarded = threading.Event()
        results = []
        with ModelRequestProxy(self.upstream, on_event=lambda event:
                forwarded.set() if event['kind'] == 'model_request_forwarded' else None) as proxy:
            self.assertIsNone(proxy.last_activity_at)
            started = time.monotonic()
            thread = threading.Thread(target=lambda: results.append(self.request(proxy, b'{"stream":true}')))
            thread.start()
            try:
                self.assertTrue(forwarded.wait(2))
                activity = proxy.last_activity_at
                self.assertGreaterEqual(activity, started)
                time.sleep(1.1)
                self.assertTrue(proxy.pending_requests)
                self.assertEqual(proxy.last_activity_at, activity)
            finally:
                self.release.set()
                thread.join(2)
            self.assert_completed_calls(proxy, 1)
            self.assertGreater(proxy.last_activity_at, activity)
            self.assertIn(b'auriga.keepalive', results[0])

    def test_upstream_bytes_refresh_activity_before_response_finishes(self):
        self.behavior = 'trickle'
        first_byte = threading.Event()
        results = []
        with ModelRequestProxy(self.upstream, on_event=lambda event:
                first_byte.set() if event['kind'] == 'model_response_first_byte' else None) as proxy:
            thread = threading.Thread(target=lambda: results.append(self.request(proxy, b'{"stream":true}')))
            thread.start()
            try:
                self.assertTrue(first_byte.wait(2))
                activity = proxy.last_activity_at
                time.sleep(.12)
                self.assertTrue(proxy.pending_requests)
                self.assertEqual(results, [])
                self.assertGreater(proxy.last_activity_at, activity)
                self.assertFalse(any(e['kind'] == 'model_request_finished' for e in proxy.events))
                self.assertFalse(any(e['kind'] == 'model_response_bytes_received' for e in proxy.events))
            finally:
                proxy.cancel_pending('operator_cancelled')
                thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(proxy.attempt_count, 1)
            self.assertFalse(proxy.transport_blocked)

    def test_silent_http_request_is_stopped_by_shared_inactivity_supervisor(self):
        import asyncio
        from types import SimpleNamespace
        from auriga.runtime.supervision import INACTIVITY_REASON, wait_turn
        self.behavior = 'blocked'
        forwarded = threading.Event()
        errors = []
        with ModelRequestProxy(self.upstream, on_event=lambda event:
                forwarded.set() if event['kind'] == 'model_request_forwarded' else None) as proxy:
            def pending():
                try:
                    self.request(proxy)
                except HTTPError as error:
                    errors.append(error.code)
            thread = threading.Thread(target=pending)
            thread.start()
            try:
                self.assertTrue(forwarded.wait(2))
                with proxy.lock:
                    child = next(iter(proxy.active.values()))['process']
                session = SimpleNamespace(model_proxy=proxy, turn_done=threading.Event())
                harness = SimpleNamespace(started_at=time.monotonic(), episode_done=threading.Event())
                with mock.patch('auriga.runtime.supervision.INACTIVITY_TIMEOUT_S', .05):
                    self.assertFalse(asyncio.run(wait_turn(session, asyncio.Event(), harness=harness)))
                self.assertEqual(harness.wait_stop_reason, INACTIVITY_REASON)
                self.assertTrue(proxy.pending_requests)
                proxy.cancel_pending(harness.wait_stop_reason)
            finally:
                proxy.cancel_pending('test_cleanup')
                thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [502])
            self.assertIsNotNone(child.poll())
            self.assertFalse(proxy.pending_requests)
            self.assertFalse(proxy.transport_blocked)
            self.assertEqual(proxy.attempt_count, 1)

    def test_downstream_disconnect_keeps_usage_without_consuming_completed_call(self):
        finished = threading.Event()
        with ModelRequestProxy(self.upstream, max_requests=1, on_event=lambda e:
                finished.set() if e['kind'] == 'model_call_finished' else None) as proxy:
            complete = proxy._complete
            def disconnect_after_complete(state, *args):
                result = complete(state, *args)
                proxy._shutdown(state['client'])
                return result
            # Isolate the delivery window after the ordinary disconnect watcher
            # has had its last chance to cancel an in-flight model request.
            with mock.patch.object(proxy, '_complete', side_effect=disconnect_after_complete), \
                 mock.patch.object(proxy, '_watch_client', return_value=None):
                with self.assertRaises(http.client.RemoteDisconnected):
                    self.request(proxy)
                self.assertTrue(finished.wait(2))
            self.assertEqual(proxy.request_count, 0)
            self.assertEqual(proxy.reserved_calls, 0)
            self.assertEqual(proxy.attempt_count, 1)
            attempt = next(e for e in proxy.events if e['kind'] == 'model_request_finished')
            self.assertTrue(attempt['transport_complete'])
            self.assertTrue(attempt['usage_known'])
            self.assertEqual(attempt['usage'], dict(input_tokens=7, output_tokens=2))
            failed = next(e for e in proxy.events if e['kind'] == 'model_call_failed')
            self.assertFalse(failed['completed_call_charged'])
            self.assertFalse(next(e for e in proxy.events if e['kind'] == 'model_call_finished')['delivered'])
            self.assertTrue(proxy.transport_blocked)
            self.assertEqual(proxy.attempt_count, 1)

    def test_exception_after_spawn_kills_and_reaps_worker(self):
        import subprocess
        children = []
        finished = threading.Event()
        original = subprocess.Popen
        def capture(*args, **kwargs):
            child = original(*args, **kwargs)
            children.append(child)
            return child
        with ModelRequestProxy(self.upstream, on_event=lambda e:
                finished.set() if e['kind'] == 'model_call_finished' else None) as proxy:
            record = proxy._record
            def fail_started(kind, **fields):
                if kind == 'model_request_started':
                    raise OSError('synthetic metrics failure')
                return record(kind, **fields)
            with mock.patch('auriga.cortex.session.model_proxy.subprocess.Popen', side_effect=capture), \
                 mock.patch.object(proxy, '_record', side_effect=fail_started):
                with self.assertRaises(HTTPError):
                    self.request(proxy)
                self.assertTrue(finished.wait(2))
            self.assertEqual(len(children), 1)
            self.assertIsNotNone(children[0].poll())
            self.assertFalse(proxy.active)
            self.assertEqual(proxy.request_count, 0)

    def test_worker_exit_during_stdin_close_retains_cleanup_and_failure_evidence(self):
        import subprocess
        children = []
        finished = threading.Event()
        original = subprocess.Popen
        def capture(*args, **kwargs):
            child = original(*args, **kwargs)
            children.append(child)
            close = child.stdin.close
            def exit_before_flush():
                child.kill()
                child.wait()
                close()  # Buffered input flush now fails with BrokenPipeError.
            child.stdin.close = exit_before_flush
            return child
        with ModelRequestProxy(self.upstream, on_event=lambda e:
                finished.set() if e['kind'] == 'model_call_finished' else None) as proxy:
            with mock.patch('auriga.cortex.session.model_proxy.subprocess.Popen', side_effect=capture):
                with self.assertRaises(HTTPError):
                    self.request(proxy)
                self.assertTrue(finished.wait(2))
            self.assertIsNotNone(children[0].poll())
            self.assertIsNone(children[0].stdin)
            self.assertFalse(proxy.active)
            self.assertEqual(proxy.attempt_count, 1)
            attempt = next(e for e in proxy.events if e['kind'] == 'model_request_finished')
            self.assertFalse(attempt['transport_complete'])
            failure = next(e for e in proxy.events if e['kind'] == 'model_call_failed')
            self.assertEqual(failure['error_type'], 'BrokenPipeError')

    def test_slow_callback_cannot_delay_attempt_or_trigger_false_timeout(self):
        callback_entered, callback_release = threading.Event(), threading.Event()
        def callback(event):
            if event['kind'] == 'model_call_started':
                callback_entered.set()
                callback_release.wait(3)
        with ModelRequestProxy(self.upstream, on_event=callback) as proxy:
            try:
                started = time.monotonic()
                self.assertEqual(self.request(proxy), complete_payload())
                self.assertLess(time.monotonic()-started, .8)
                self.assertTrue(callback_entered.is_set())
            finally:
                callback_release.set()

    def test_compact_helper_and_actor_share_concurrent_reservations(self):
        self.behavior = 'blocked'
        results = []
        with ModelRequestProxy(self.upstream, max_requests=1) as proxy:
            thread = threading.Thread(target=lambda: results.append(self.request(proxy, headers={
                'X-Auriga-Source': 'sol_visual_review', 'X-Auriga-Request-Id': 'review-1'})))
            thread.start()
            self.assertTrue(self.entered.wait(2))
            with self.assertRaises(HTTPError):
                self.request(proxy, path='/responses/compact')
            self.assertEqual(len(self.requests), 1)
            self.release.set()
            thread.join(2)
            self.assertEqual(len(results), 1)
            self.assert_completed_calls(proxy, 1)
        self.assertFalse(any(k.lower().startswith('x-auriga-') for k in self.requests[0][2]))
        finished = next(e for e in proxy.events if e['kind'] == 'model_request_finished')
        self.assertEqual(finished['source'], 'sol_visual_review')
        self.assertEqual(finished['caller_request_id'], 'review-1')
        self.behavior = 'compact'
        with ModelRequestProxy(self.upstream, max_requests=1) as proxy:
            self.request(proxy, path='/responses/compact')
            self.assert_completed_calls(proxy, 1)

    def test_native_thread_registration_attributes_official_requests(self):
        with ModelRequestProxy(self.upstream) as proxy:
            proxy.register_thread('helper-thread', 'sol_visual_review', 'review-17')
            self.request(proxy, headers={'Thread-Id': 'helper-thread', 'X-Auriga-Source': 'untrusted_override'})
            proxy.unregister_thread('helper-thread')
            self.request(proxy, headers={'thread-id': 'actor-thread'})
            finished = [event for event in proxy.events if event['kind'] == 'model_request_finished']
            self.assertEqual(finished[0]['native_thread_id'], 'helper-thread')
            self.assertEqual(finished[0]['source'], 'sol_visual_review')
            self.assertEqual(finished[0]['caller_request_id'], 'review-17')
            self.assertEqual(finished[1]['source'], 'actor')
            self.assert_completed_calls(proxy, 2)
            headers = {key.lower(): value for key, value in self.requests[0][2].items()}
            self.assertEqual(headers['thread-id'], 'helper-thread')
            self.assertNotIn('x-auriga-source', headers)

    def test_cancel_helper_thread_preserves_concurrent_actor_request(self):
        self.behavior = 'blocked'
        results, errors = {}, {}
        with ModelRequestProxy(self.upstream) as proxy:
            proxy.register_thread('helper-thread', 'sol_visual_review', 'review-18')
            def pending(thread_id):
                try: results[thread_id] = self.request(proxy, headers={'thread-id': thread_id})
                except HTTPError as error: errors[thread_id] = error.code
            actor = threading.Thread(target=pending, args=('actor-thread',))
            helper = threading.Thread(target=pending, args=('helper-thread',))
            actor.start(); helper.start()
            deadline = time.monotonic() + 2
            while len(self.requests) < 2 and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(len(self.requests), 2)
            proxy.cancel_thread('helper-thread')
            helper.join(2)
            self.assertFalse(helper.is_alive())
            self.assertTrue(actor.is_alive())
            self.assertEqual(errors, {'helper-thread': 502})
            self.assertFalse(proxy.transport_blocked)
            self.assertTrue(proxy.pending_requests)
            self.assertEqual(proxy.request_count, 0)
            self.release.set()
            actor.join(2)
            self.assertFalse(actor.is_alive())
            self.assertIn(b'response.completed', results['actor-thread'])
            self.assert_completed_calls(proxy, 1)
            self.assertEqual(proxy.attempt_count, 2)
            self.assertFalse(proxy.transport_blocked)
            cancelled = [event for event in proxy.events if event['kind'] == 'model_call_cancelled']
            self.assertEqual(len(cancelled), 1)
            self.assertEqual(cancelled[0]['source'], 'sol_visual_review')
            self.assertEqual(cancelled[0]['reason'], 'helper_turn_interrupted')

    def test_cancel_before_helper_dispatch_rejects_late_request_after_unregister(self):
        with ModelRequestProxy(self.upstream, max_requests=1) as proxy:
            proxy.register_thread('late-helper', 'sol_visual_review', 'review-19')
            proxy.cancel_thread('late-helper')
            proxy.unregister_thread('late-helper')
            with self.assertRaises(HTTPError) as error:
                self.request(proxy, headers={'thread-id': 'late-helper'})
            self.assertEqual(error.exception.code, 409)
            self.assertEqual(proxy.logical_call_count, 0)
            self.assertEqual(proxy.attempt_count, 0)
            self.assertEqual(proxy.request_count, 0)
            self.assertFalse(proxy.pending_requests)
            self.assertFalse(proxy.transport_blocked)
            self.assertEqual(self.requests, [])
            with self.assertRaisesRegex(ValueError, 'already_cancelled'):
                proxy.register_thread('late-helper', 'sol_visual_review', 'review-20')
            self.assertIn(b'response.completed', self.request(proxy, headers={'thread-id': 'actor-thread'}))
            self.assert_completed_calls(proxy, 1)
            self.assertEqual(proxy.attempt_count, 1)

    def test_explicit_cancel_interrupts_blocked_worker_without_retry(self):
        self.behavior = 'blocked'
        errors = []
        with ModelRequestProxy(self.upstream) as proxy:
            def pending():
                try: self.request(proxy)
                except HTTPError as error: errors.append(error.code)
            thread = threading.Thread(target=pending)
            thread.start()
            self.assertTrue(self.entered.wait(2))
            proxy.cancel_pending('operator_cancelled')
            thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [502])
            self.assertEqual(proxy.attempt_count, 1)
            self.assertEqual(proxy.request_count, 0)
            self.assertFalse(proxy.transport_blocked)

    def test_cancel_during_error_backoff_prevents_retry_and_wait_clock_includes_backoff(self):
        self.behavior = '429'
        retrying, errors = threading.Event(), []
        with ModelRequestProxy(self.upstream, on_event=lambda event:
                retrying.set() if event['kind'] == 'model_request_retry' else None) as proxy:
            def pending():
                try: self.request(proxy)
                except HTTPError as error: errors.append(error.code)
            with mock.patch.object(proxy.policy, '_retry_delay_seconds', return_value=60):
                thread = threading.Thread(target=pending)
                thread.start()
                self.assertTrue(retrying.wait(2))
                before = proxy.model_wait_seconds
                time.sleep(.03)
                self.assertGreater(proxy.model_wait_seconds, before + .02)
                self.assertTrue(proxy.pending_requests)
                self.assertFalse(proxy.transport_blocked)
                proxy.cancel_pending('operator_cancelled')
                thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [429])
            self.assertEqual(len(self.requests), 1)
            self.assertFalse(proxy.transport_blocked)

    def test_explicit_cancel_releases_incomplete_downstream_request_body(self):
        started, finished = threading.Event(), threading.Event()
        def event(value):
            if value['kind'] == 'model_call_started': started.set()
            if value['kind'] == 'model_call_finished': finished.set()
        with ModelRequestProxy(self.upstream, on_event=event) as proxy:
            connection = http.client.HTTPConnection('127.0.0.1', proxy.server.server_port)
            try:
                connection.putrequest('POST', '/responses')
                connection.putheader('Content-Length', '100')
                connection.endheaders()
                connection.send(b'{')
                self.assertTrue(started.wait(2))
                proxy.cancel_pending('operator_cancelled')
                self.assertTrue(finished.wait(2))
                self.assertFalse(proxy.pending_requests)
                self.assertEqual(proxy.reserved_calls, 0)
                self.assertEqual(proxy.attempt_count, 0)
                self.assertEqual(self.requests, [])
                self.assertFalse(proxy.transport_blocked)
                waiting = proxy.model_wait_seconds
                time.sleep(.02)
                self.assertEqual(proxy.model_wait_seconds, waiting)
            finally:
                connection.close()

    def test_client_disconnect_blocks_new_requests_after_unknown_upstream_outcome(self):
        self.behavior = 'blocked'
        with ModelRequestProxy(self.upstream) as proxy:
            proxy.register_thread('disconnected-helper', 'sol_visual_review', 'review-21')
            connection = http.client.HTTPConnection('127.0.0.1', proxy.server.server_port)
            connection.request('POST', '/responses', b'{"stream":true}',
                               {'Content-Type': 'application/json', 'thread-id': 'disconnected-helper'})
            response = connection.getresponse()
            self.assertTrue(self.entered.wait(2))
            response.close()
            connection.close()
            deadline = time.monotonic()+2
            while proxy.active and time.monotonic()<deadline:
                time.sleep(.02)
            self.assertFalse(proxy.active)
            self.assertEqual(proxy.request_count, 0)
            self.assertEqual(proxy.attempt_count, 1)
            self.assertTrue(any(e['kind']=='model_call_cancelled' for e in proxy.events))
            self.assertTrue(proxy.transport_blocked)
            self.assertFalse(proxy.blocked_error['manual_retry_allowed'])
            with self.assertRaisesRegex(ValueError, 'explicit_provider_error'):
                proxy.manual_retry()
            with self.assertRaises(HTTPError) as blocked:
                self.request(proxy)
            self.assertEqual(blocked.exception.code, 409)
            self.assertEqual(len(self.requests), 1)

    def test_trim_oldest_images_and_image_compression_failed_retry(self):
        err = ModelRequestProxy._failed_response_error({'error': {'code': 'image_compression_failed', 'message': 'payload too large'}})
        self.assertEqual(err['classification'], 'retryable')
        self.assertTrue(err['retryable'])
        body = json.dumps({
            'model': 'fixture',
            'input': [
                {'type': 'message', 'content': [{'type': 'input_image', 'image_url': f'data:image/jpeg;base64,img{i}'}]}
                for i in range(5)
            ],
        }).encode()
        trimmed = json.loads(ModelRequestProxy._trim_oldest_images(body, keep_latest=2).decode())
        types = [item['content'][0]['type'] for item in trimmed['input']]
        self.assertEqual(types, ['input_text', 'input_text', 'input_text', 'input_image', 'input_image'])

        # Verify fixed-prefix watermark stability across turns, no dangling duplicate references, and compaction reset
        with ModelRequestProxy(self.upstream, image_reuse=True) as proxy:
            items_t1 = [
                {'type': 'message', 'content': [{'type': 'input_image', 'image_url': f'data:image/jpeg;base64,img{i % 10}'}]}
                for i in range(20)
            ]
            state_t1 = {'identity': {'native_thread_id': 'thread-cache'}}
            req_t1 = {'model': 'fixture', 'input': items_t1}
            raw_t1 = json.dumps(req_t1).encode()
            # Simulate advancing watermark after image_compression_failed (retry_index=0 -> keeps 16, watermark=4)
            out_t1, _ = proxy._prepare_responses_body(state_t1, req_t1, raw_t1, force_advance=True, retry_index=0)
            self.assertEqual(proxy._image_trim_watermarks['thread-cache'], (4, 20))
            # First 4 images trimmed; item 10 (duplicate of trimmed img0) must be retained as actual input_image, not dangling text
            self.assertEqual([item['content'][0]['type'] for item in out_t1['input'][:4]], ['input_text'] * 4)
            self.assertEqual(out_t1['input'][10]['content'][0]['type'], 'input_image')

            # Next turn appends 2 new images: watermark stays fixed at 4, so historical prefix [:20] is 100% identical for prompt cache
            items_t2 = items_t1 + [
                {'type': 'message', 'content': [{'type': 'input_image', 'image_url': 'data:image/jpeg;base64,new20'}]},
                {'type': 'message', 'content': [{'type': 'input_image', 'image_url': 'data:image/jpeg;base64,new21'}]},
            ]
            req_t2 = {'model': 'fixture', 'input': items_t2}
            raw_t2 = json.dumps(req_t2).encode()
            out_t2, _ = proxy._prepare_responses_body(state_t1, req_t2, raw_t2)
            self.assertEqual(proxy._image_trim_watermarks['thread-cache'], (4, 22))
            self.assertEqual(out_t2['input'][:20], out_t1['input'][:20])

            # After context compaction reduces image count, watermark resets to 0
            items_compacted = items_t2[-3:]
            req_compacted = {'model': 'fixture', 'input': items_compacted}
            raw_compacted = json.dumps(req_compacted).encode()
            out_compacted, _ = proxy._prepare_responses_body(state_t1, req_compacted, raw_compacted)
            self.assertEqual(proxy._image_trim_watermarks['thread-cache'], (0, 3))
            self.assertEqual([item['content'][0]['type'] for item in out_compacted['input']], ['input_image'] * 3)

    def test_over_4_5mb_single_shot_middle_window_compression_preserves_prefix_cache_and_observation_labels(self):
        with ModelRequestProxy(self.upstream, image_reuse=True) as proxy:
            state = {'identity': {'native_thread_id': 'thread-45mb'}}
            steps = []
            sent = []
            # Each step has 1 observation text part + 3 camera images (~300KB per step -> 16 steps > 4.5MB)
            for s in range(18):
                obs_id = f'obs_{s + 1}'
                meta = json.dumps({
                    'bundle': {
                        'observation_id': obs_id,
                        'cameras': [{'camera_id': 'cam_head'}, {'camera_id': 'cam_left'}, {'camera_id': 'cam_right'}],
                    }
                })
                steps.extend([
                    {'type': 'function_call', 'name': 'robot_observe', 'call_id': f'call_{s}', 'arguments': '{}'},
                    {'type': 'function_call_output', 'call_id': f'call_{s}', 'output': [
                        {'type': 'input_text', 'text': meta},
                        {'type': 'input_image', 'image_url': f'data:image/webp;base64,{obs_id}_h_' + ('A' * 95_000)},
                        {'type': 'input_image', 'image_url': f'data:image/webp;base64,{obs_id}_l_' + ('B' * 95_000)},
                        {'type': 'input_image', 'image_url': f'data:image/webp;base64,{obs_id}_r_' + ('C' * 95_000)},
                    ]},
                ])
                req = {'model': 'fixture', 'input': list(steps)}
                raw = json.dumps(req, separators=(',', ':')).encode()
                out_req, out_body = proxy._prepare_responses_body(state, req, raw)
                self.assertLessEqual(len(out_body), 4_500_000)
                sent.append((len(raw), len(out_body), out_req['input'],
                             proxy._image_trim_starts.get('thread-45mb', 0),
                             proxy._image_trim_watermarks.get('thread-45mb', (0, 0))[0]))

            # Before 4.5MB (first 15 steps): zero trimming, every request extends the previous one
            self.assertEqual(sent[14][3], 0)
            self.assertEqual(sent[14][4], 0)
            for i in range(14):
                self.assertEqual(sent[i + 1][2][:len(sent[i][2])], sent[i][2])

            # At step 15 (16th step, raw > 4.5MB): single-shot middle-window compression triggers once
            trigger_idx = next(i for i, item in enumerate(sent) if item[4] > 0)
            _, fw_bytes, trig_input, start_idx, wm = sent[trigger_idx]
            self.assertGreater(start_idx, 0)
            self.assertGreater(wm, start_idx)
            self.assertLessEqual(fw_bytes, 2_400_000)
            # Preserved prefix [0, start_idx) matches a complete earlier request byte-for-byte
            matched_earlier = any(trig_input[:len(prev[2])] == prev[2] for prev in sent[:trigger_idx])
            self.assertTrue(matched_earlier)
            # Omitted middle images carry exact observation_id and camera recovery labels
            trig_json = json.dumps(trig_input)
            self.assertIn("observation_id=obs_", trig_json)
            self.assertIn("camera=cam_head", trig_json)
            self.assertIn("history_view_observation(observation_id='obs_", trig_json)
            # Subsequent steps after trigger keep the exact same (start_idx, wm) and extend the prefix
            for i in range(trigger_idx, len(sent) - 1):
                self.assertEqual((sent[i + 1][3], sent[i + 1][4]), (start_idx, wm))
                self.assertEqual(sent[i + 1][2][:len(sent[i][2])], sent[i][2])
            # Exactly one rewrite is recorded, with the window it applied.
            trimmed = [e for e in proxy.events if e['kind'] == 'model_images_trimmed']
            self.assertEqual(len(trimmed), 1)
            self.assertEqual(trimmed[0]['reason'], 'body_bytes')
            self.assertEqual((trimmed[0]['window_start'], trimmed[0]['window_end']), (start_idx, wm))
            self.assertEqual(trimmed[0]['previous_window'], [0, 0])


    def test_oversized_request_trim_switch_off_forwards_native_history_unchanged(self):
        def oversized(steps):
            items = []
            for s in range(steps):
                meta = json.dumps({'bundle': {'observation_id': f'obs_{s}', 'cameras': [{'camera_id': 'cam_head'}]}})
                items += [{'type': 'function_call', 'name': 'robot_observe', 'call_id': f'call_{s}', 'arguments': '{}'},
                          {'type': 'function_call_output', 'call_id': f'call_{s}', 'output': [
                              {'type': 'input_text', 'text': meta},
                              {'type': 'input_image', 'image_url': f'data:image/webp;base64,{s}_' + 'A' * 300_000}]}]
            return {'model': 'fixture', 'input': items}
        request = oversized(18)
        raw = json.dumps(request, separators=(',', ':')).encode()
        self.assertGreater(len(raw), 4_500_000)
        with ModelRequestProxy(self.upstream, image_reuse=True, image_trim=False) as proxy:
            state = {'identity': {'native_thread_id': 'thread-off'}}
            out_request, out_body = proxy._prepare_responses_body(state, request, raw)
            self.assertIs(out_body, raw)
            self.assertEqual(out_request, request)
            self.assertFalse(any(e['kind'] == 'model_images_trimmed' for e in proxy.events))
        with ModelRequestProxy(self.upstream, image_reuse=True) as proxy:
            state = {'identity': {'native_thread_id': 'thread-on'}}
            _, out_body = proxy._prepare_responses_body(state, request, raw)
            self.assertLessEqual(len(out_body), 4_500_000)
        with self.assertRaises(ValueError):
            ModelRequestProxy(self.upstream, image_trim=1)

        # Without trimming an image_compression_failed reply is not retried with the same body.
        self.failed_error, self.behavior = {'type': 'invalid_request_error', 'code': 'image_compression_failed'}, 'failed'
        self.script = ['failed', 'complete']
        with ModelRequestProxy(self.upstream, image_trim=False) as proxy:
            with mock.patch.object(proxy.policy, '_retry_delay_seconds', return_value=.001):
                self.request(proxy, b'{"stream":true}')
            self.assertEqual(proxy.attempt_count, 1)
            self.assertTrue(proxy.transport_blocked)

    RECOVERY = dict(max_rounds=3, initial_delay_s=.01, backoff_multiplier=2, max_delay_s=.02,
                    max_episode_delay_s=10, transport_incomplete=True)

    def unavailable(self, script):
        self.failed_error = {'code': 'server_error', 'type': 'service_unavailable_error', 'message': 'fixture-private'}
        self.behavior, self.script = 'failed', list(script)

    def recovery_proxy(self, gate=lambda: None, **policy):
        return ModelRequestProxy(self.upstream, max_retries=1, transport_recovery={**self.RECOVERY, **policy},
                                 recovery_gate=gate)

    def kinds(self, proxy, kind):
        return [event for event in proxy.events if event['kind'] == kind]

    def test_transport_recovery_resends_same_body_after_retry_exhaustion(self):
        self.unavailable(['failed', 'failed', 'complete'])
        gate_calls = []
        payload = b'{"model":"fixture","input":[],"stream":true}'
        with self.recovery_proxy(gate=lambda: gate_calls.append(1)) as proxy:
            with mock.patch.object(proxy.policy, '_retry_delay_seconds', return_value=.001):
                raw = self.request(proxy, payload)
            self.assertIn(b'response.completed', raw)
            self.assertNotIn(b'unsafe', raw)
            self.assertNotIn(b'recovery', raw)
            self.assert_completed_calls(proxy, 1)
            self.assertEqual(proxy.attempt_count, 3)
            self.assertFalse(proxy.transport_blocked)
            # One logical call: the identical body is resent; no new turn or tool call exists.
            self.assertEqual({body for _, body, _ in self.requests}, {payload})
            self.assertEqual(len(self.kinds(proxy, 'model_request_retry')), 1)
            scheduled = self.kinds(proxy, 'model_transport_recovery_scheduled')
            self.assertEqual([(e['recovery_round'], e['attempt_index'], e['delay_s']) for e in scheduled], [(1, 2, .01)])
            self.assertEqual(scheduled[0]['error']['type'], 'service_unavailable_error')
            self.assertEqual(scheduled[0]['logical_call_index'], 1)
            self.assertEqual(len(self.kinds(proxy, 'model_transport_recovered')), 1)
            # The gate is consulted before scheduling and again before the resend.
            self.assertEqual(len(gate_calls), 2)
            self.assertEqual(proxy.transport_recovery_summary,
                             dict(policy=self.RECOVERY, rounds=1, recovered_calls=1, denials=0, cooldown_s=.01))
            self.assertNotIn('fixture-private', json.dumps(proxy.events))

    def test_transport_recovery_is_bounded_by_rounds_and_episode_cooldown(self):
        for policy, attempts, rounds, reason in (
                ({}, 5, 3, 'recovery_rounds_exhausted'),
                ({'max_episode_delay_s': .025}, 3, 1, 'episode_recovery_budget_exhausted')):
            with self.subTest(reason=reason):
                self.unavailable([])
                with self.recovery_proxy(**policy) as proxy:
                    with mock.patch.object(proxy.policy, '_retry_delay_seconds', return_value=.001):
                        raw = self.request(proxy, b'{"stream":true}')
                    self.assertIn(b'response.failed', raw)
                    self.assertNotIn(b'unsafe', raw)
                    self.assertEqual(proxy.attempt_count, attempts)
                    self.assertEqual(proxy.request_count, 0)
                    self.assertTrue(proxy.transport_blocked)
                    self.assertTrue(proxy.blocked_error['manual_retry_allowed'])
                    self.assertEqual(len(self.kinds(proxy, 'model_transport_recovery_scheduled')), rounds)
                    denied = self.kinds(proxy, 'model_transport_recovery_denied')
                    self.assertEqual([(e['reason'], e['recovery_rounds']) for e in denied], [(reason, rounds)])
                    self.assertLessEqual(proxy.transport_recovery_summary['cooldown_s'],
                                         {**self.RECOVERY, **policy}['max_episode_delay_s'])

    def test_transport_recovery_refused_while_actions_pending_or_gate_missing(self):
        flips = iter([None, 'action_in_progress'])
        for gate, attempts, reason in ((lambda: 'action_status_unknown', 2, 'action_status_unknown'),
                                       (None, 2, 'recovery_gate_unavailable'),
                                       (lambda: 1 / 0, 2, 'recovery_gate_error:ZeroDivisionError'),
                                       # An action admitted during the cooldown cancels the resend.
                                       (lambda: next(flips), 2, 'action_in_progress')):
            with self.subTest(reason=reason):
                self.unavailable([])
                with self.recovery_proxy(gate=gate) as proxy:
                    with mock.patch.object(proxy.policy, '_retry_delay_seconds', return_value=.001):
                        self.request(proxy, b'{"stream":true}')
                    self.assertEqual(proxy.attempt_count, attempts)
                    self.assertTrue(proxy.transport_blocked)
                    self.assertEqual([e['reason'] for e in self.kinds(proxy, 'model_transport_recovery_denied')], [reason])
                    self.assertFalse(self.kinds(proxy, 'model_transport_recovered'))

    def test_recovery_disabled_by_default_and_never_applies_to_terminal_errors(self):
        self.unavailable([])
        with ModelRequestProxy(self.upstream, max_retries=1, recovery_gate=lambda: None) as proxy:
            with mock.patch.object(proxy.policy, '_retry_delay_seconds', return_value=.001):
                self.request(proxy, b'{"stream":true}')
            self.assertEqual(proxy.attempt_count, 2)
            self.assertFalse(self.kinds(proxy, 'model_transport_recovery_denied'))
            self.assertIsNone(proxy.transport_recovery_summary['policy'])
        with self.recovery_proxy(max_rounds=0) as proxy:
            self.assertIsNone(proxy.transport_recovery)
        self.failed_error, self.behavior = {'code': 'invalid_api_key'}, 'failed'
        with self.recovery_proxy() as proxy:
            self.request(proxy, b'{"stream":true}')
            self.assertEqual(proxy.attempt_count, 1)
            self.assertFalse(self.kinds(proxy, 'model_transport_recovery_scheduled'))
        for kwargs in ({'transport_recovery': {'max_rounds': 1}}, {'recovery_gate': 'yes'},
                       {'max_inline_images': 8}, {'max_inline_images': True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ModelRequestProxy(self.upstream, **kwargs)

    def test_quota_exhaustion_waits_for_gated_recovery_without_immediate_retries(self):
        self.failed_error = {'code': 'credit_balance_exhausted', 'type': 'insufficient_quota', 'message': 'fixture-private'}
        self.behavior, self.script = 'failed', ['failed', 'complete']
        with self.recovery_proxy() as proxy:
            with mock.patch.object(proxy.policy, '_retry_delay_seconds', return_value=.001):
                raw = self.request(proxy, b'{"model":"fixture","input":[],"stream":true}')
            self.assertIn(b'response.completed', raw)
            self.assertEqual(proxy.attempt_count, 2)
            self.assertFalse(self.kinds(proxy, 'model_request_retry'))
            scheduled = self.kinds(proxy, 'model_transport_recovery_scheduled')
            self.assertEqual(len(scheduled), 1)
            self.assertEqual(scheduled[0]['error']['classification'], 'quota_exceeded')
        self.script = ['failed', 'complete']
        with self.recovery_proxy(gate=lambda: 'action_executing') as proxy:
            self.request(proxy, b'{"stream":true}')
            self.assertEqual(proxy.attempt_count, 1)
            self.assertTrue(proxy.transport_blocked)

    def test_incomplete_upstream_transport_recovers_only_when_enabled(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                self.script = ['cut', 'complete']
                with self.recovery_proxy(transport_incomplete=enabled) as proxy:
                    if enabled:
                        self.assertIn(b'response.completed', self.request(proxy))
                        self.assertEqual(proxy.attempt_count, 2)
                        self.assertFalse(proxy.transport_blocked)
                        scheduled = self.kinds(proxy, 'model_transport_recovery_scheduled')
                        self.assertEqual(scheduled[0]['error']['classification'], 'transport_incomplete')
                    else:
                        with self.assertRaises(HTTPError):
                            self.request(proxy)
                        self.assertEqual(proxy.attempt_count, 1)
                        self.assertTrue(proxy.transport_blocked)
                        self.assertFalse(proxy.blocked_error['manual_retry_allowed'])
                        self.assertFalse(self.kinds(proxy, 'model_transport_recovery_denied'))

    def test_cancel_during_recovery_cooldown_prevents_resend(self):
        self.unavailable([])
        with self.recovery_proxy(initial_delay_s=5, max_delay_s=5) as proxy:
            with mock.patch.object(proxy.policy, '_retry_delay_seconds', return_value=.001):
                replies = []
                thread = threading.Thread(target=lambda: replies.append(self.request(proxy, b'{"stream":true}')))
                thread.start()
                deadline = time.monotonic() + 3
                while not self.kinds(proxy, 'model_transport_recovery_scheduled') and time.monotonic() < deadline:
                    time.sleep(.01)
                proxy.cancel_pending('episode_stopped')
                thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertIn(b'model_request_failed', replies[0])
            self.assertEqual(proxy.attempt_count, 2)
            self.assertEqual(proxy.request_count, 0)

    @staticmethod
    def image_steps(count, payload=200):
        items = []
        for s in range(count):
            meta = json.dumps({'bundle': {'observation_id': f'obs_{s}', 'cameras': [
                {'camera_id': 'cam_head'}, {'camera_id': 'cam_left'}, {'camera_id': 'cam_right'}]}})
            items += [{'type': 'function_call', 'name': 'robot_observe', 'call_id': f'call_{s}', 'arguments': '{}'},
                      {'type': 'function_call_output', 'call_id': f'call_{s}', 'output': [{'type': 'input_text', 'text': meta}] + [
                          {'type': 'input_image', 'image_url': f'data:image/webp;base64,{s}{cam}' + 'A' * payload}
                          for cam in 'hlr']}]
        return {'model': 'fixture', 'input': items}

    def test_inline_image_count_triggers_cache_preserving_middle_trim(self):
        with ModelRequestProxy(self.upstream, image_reuse=True, max_inline_images=60) as proxy:
            state = {'identity': {'native_thread_id': 'thread-count'}}
            sent = []
            for steps in range(1, 26):
                request = self.image_steps(steps)
                raw = json.dumps(request, separators=(',', ':')).encode()
                self.assertLess(len(raw), 4_500_000)
                out, _ = proxy._prepare_responses_body(state, request, raw)
                count = len(ModelRequestProxy._inline_image_locations(out))
                self.assertLessEqual(count, 60)
                sent.append((steps * 3, count, out['input']))
            # Up to 60 images (20 steps) the request is forwarded unchanged.
            self.assertEqual([c for total, c, _ in sent[:20]], [total for total, _, _ in sent[:20]])
            trimmed = self.kinds(proxy, 'model_images_trimmed')
            self.assertEqual(len(trimmed), 1)
            event = trimmed[0]
            self.assertEqual((event['reason'], event['images_total']), ('image_count', 63))
            self.assertLessEqual(event['images_forwarded'], 40)
            start, end = event['window_start'], event['window_end']
            self.assertGreater(start, 0)
            self.assertLessEqual(end, 63 - 12)
            trigger = sent[20][2]
            # The kept opening is byte-identical to an earlier forwarded request.
            self.assertTrue(any(trigger[:len(prev)] == prev for _, _, prev in sent[:20] if len(prev) <= 2 * (start // 3)))
            self.assertEqual(json.dumps(trigger[:2 * (start // 3)]), json.dumps(sent[19][2][:2 * (start // 3)]))
            text = json.dumps(trigger)
            self.assertIn("history_view_observation(observation_id='obs_", text)
            self.assertIn('camera=cam_left', text)
            # The window stays locked: later requests extend the trimmed one.
            for i in range(20, len(sent) - 1):
                self.assertEqual(sent[i + 1][2][:len(sent[i][2])], sent[i][2])

    def test_inline_image_count_limit_disabled_or_inactive_without_trim(self):
        request = self.image_steps(25)
        raw = json.dumps(request, separators=(',', ':')).encode()
        for kwargs in ({'max_inline_images': 0}, {'max_inline_images': 60, 'image_trim': False}):
            with self.subTest(kwargs=kwargs), ModelRequestProxy(self.upstream, **kwargs) as proxy:
                out, body = proxy._prepare_responses_body({'identity': {'native_thread_id': 't'}}, request, raw)
                self.assertIs(body, raw)
                self.assertEqual(len(ModelRequestProxy._inline_image_locations(out)), 75)

    def test_inprocess_http_reuses_keepalive_upstream_connection(self):
        client_ports = []
        class KeepAliveHandler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *args): pass
            def do_POST(self):
                length = int(self.headers.get('Content-Length', 0))
                if length:
                    self.rfile.read(length)
                client_ports.append(self.client_address[1])
                payload = b'data: ' + json.dumps({'type': 'response.completed', 'response': {
                    'id': f'resp_{len(client_ports)}', 'model': 'gpt-5.4-fixture', 'status': 'completed',
                    'output': [], 'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}}).encode() + b'\n\n'
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(payload)))
                self.send_header('Connection', 'keep-alive')
                self.end_headers()
                self.wfile.write(payload)
                self.wfile.flush()

        server = ThreadingHTTPServer(('127.0.0.1', 0), KeepAliveHandler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
        thread.start()
        try:
            url = f'http://127.0.0.1:{server.server_port}/v1'
            with ModelRequestProxy(url, max_requests=5) as proxy:
                for _ in range(3):
                    self.assertIn(b'response.completed', self.request(proxy))
                self.assertEqual(len(client_ports), 3)
                self.assertEqual(len(set(client_ports)), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(1)

    def test_inprocess_http_discards_keepalive_connection_after_sse_error(self):
        client_ports = []
        class StickyErrorKeepAliveHandler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *args): pass
            def do_POST(self):
                length = int(self.headers.get('Content-Length', 0))
                if length:
                    self.rfile.read(length)
                client_ports.append(self.client_address[1])
                if len(client_ports) == 1:
                    payload = b'event: error\ndata: {"type":"error","error":{"type":"upstream_error","code":"server_error","message":"bad node"}}\n\n'
                else:
                    payload = b'data: ' + json.dumps({'type': 'response.completed', 'response': {
                        'id': f'resp_{len(client_ports)}', 'model': 'gpt-5.4-fixture', 'status': 'completed',
                        'output': [], 'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}}).encode() + b'\n\n'
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(payload)))
                self.send_header('Connection', 'keep-alive')
                self.end_headers()
                self.wfile.write(payload)
                self.wfile.flush()

        server = ThreadingHTTPServer(('127.0.0.1', 0), StickyErrorKeepAliveHandler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
        thread.start()
        try:
            url = f'http://127.0.0.1:{server.server_port}/v1'
            with ModelRequestProxy(url, max_requests=5) as proxy:
                with mock.patch.object(proxy.policy, '_retry_delay_seconds', return_value=.001):
                    self.assertIn(b'response.completed', self.request(proxy))
                self.assertEqual(len(client_ports), 2)
                self.assertEqual(len(set(client_ports)), 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(1)


if __name__ == '__main__':
    unittest.main()
