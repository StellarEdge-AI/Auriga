"""Real CodexSession -> private relay -> loopback, including native discovery/resume."""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from auriga.cortex.session.codex import CodexSession, DYNAMIC_TOOL_NAMESPACE
from auriga.core.config import Config
from auriga.runtime.harness import Harness
from auriga.core.storage.store import Store
from auriga.core.tool_output import ToolOutput
from auriga.runtime.tools.registry import ToolRegistry, ToolSpec
from test_harness import FixtureRobot


class NativeDeferredToolsTests(unittest.TestCase):
    def test_discovery_calls_validation_disabled_tools_and_resume_share_native_path(self):
        self._native_lifecycle(deferred=True)

    def test_legacy_flat_catalog_resumes_without_namespace_and_rejects_changed_catalog(self):
        self._native_lifecycle(deferred=False)

    def _native_lifecycle(self, *, deferred):
        requests, dispatched = [], []
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = Store(root / 'run', dict(run_id='native-discovery', episode_id='episode',
                env_instance_id='synthetic', env_generation=1))
            self.addCleanup(store.close)
            robot = FixtureRobot(root)
            harness = Harness(store, robot)
            description = ('Read a deterministic fixture value. Input field guide: value is an integer; '
                'minimum 0, maximum 10. ' + 'Complete deferred field help. ' * 200)
            def read(context, args, correlation):
                dispatched.append(deepcopy(args))
                return ToolOutput({'value': args['value']}).response()
            definition = dict(type='function', name='fixture_read', description=description,
                inputSchema=dict(type='object', properties={'value': dict(type='integer', minimum=0,
                    maximum=10, description='Integer fixture value from 0 through 10.')},
                    required=['value'], additionalProperties=False))
            if deferred:
                definition['deferLoading'] = True
            result_schema = {'data': dict(type='object', properties={'value': {'type': 'integer'}},
                                         required=['value'], additionalProperties=False)}
            harness.tool_registry = ToolRegistry([
                ToolSpec(definition, read, output_schemas=result_schema),
                ToolSpec(dict(type='function', name='fixture_eager', description='Eager fixture sentinel.',
                    inputSchema={'type': 'object'}), read, output_schemas=result_schema),
                ToolSpec(dict(type='function', name='fixture_program_only', description='Program-only sentinel.',
                    inputSchema={'type': 'object'}, deferLoading=True), read,
                    roles=frozenset({'program'}), output_schemas=result_schema)])
            catalog = harness.tool_registry.catalog(harness)
            self.assertNotIn('fixture_program_only', [tool['name'] for tool in catalog])
            search_count = int(deferred)

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *_): pass
                def do_POST(self):
                    request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    requests.append(request)
                    index = len(requests)
                    if deferred and index == 1:
                        item = dict(type='tool_search_call', id='search-item', call_id='search-call',
                            execution='client', arguments={'query': 'fixture_read', 'limit': 1}, status='completed')
                    elif index - search_count in (1, 2, 3, 5):
                        phase = index - search_count
                        harness.disabled_tools = {'fixture_read'} if phase == 3 else set()
                        item = dict(type='function_call', id=f'call-item-{index}', call_id=f'call-{index}',
                            name='fixture_read', arguments=json.dumps({'value': {1: 7, 2: 11, 3: 8, 5: 9}[phase]}),
                            status='completed')
                        if deferred:
                            item['namespace'] = DYNAMIC_TOOL_NAMESPACE
                        elif phase == 5:
                            item['namespace'] = 'functions'
                    else:
                        harness.disabled_tools = set()
                        item = dict(type='message', id=f'message-{index}', role='assistant', status='completed',
                            content=[dict(type='output_text', text='Fixture complete.', annotations=[])])
                    response = dict(id=f'response-{index}', model=request['model'], status='completed', output=[item],
                        usage=dict(input_tokens=10, output_tokens=2, total_tokens=12))
                    events = [dict(type='response.created', response={**response, 'status': 'in_progress', 'output': []}),
                        dict(type='response.output_item.added', output_index=0, item=item),
                        dict(type='response.output_item.done', output_index=0, item=item),
                        dict(type='response.completed', response=response)]
                    payload = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)

            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            source = root / 'source'
            source.mkdir()
            (source / 'config.toml').write_text('model_provider="fixture"\n[model_providers.fixture]\n'
                f'base_url="http://127.0.0.1:{server.server_port}/v1"\n')
            config = Config.from_dict(dict(model=dict(codex_home=str(source), native_memories=False)))

            def session_for_run():
                session = CodexSession(store.root, store, harness.handle_tool, config=config)
                self.addCleanup(close_session, session)
                return session

            def close_session(session):
                bridge = session.client._proc if session.client else None
                session.close()
                if bridge:
                    for stream in (bridge.stdin, bridge.stdout, bridge.stderr):
                        if stream and not stream.closed:
                            stream.close()

            session = session_for_run()
            harness.thread_id = session.start(catalog, 'Exercise only the deterministic fixture tools.')
            session.run_turn('Use the fixture and finish.')
            self.assertTrue(session.turn_done.wait(15), list(session.store.events())[-3:])
            self.assertEqual(session.state, 'completed')
            self.assertEqual(len(requests), 4 + search_count)
            self.assertEqual(dispatched, [{'value': 7}])
            self.assertEqual(session.model_proxy.request_count, len(requests))
            self.assertEqual(robot.executions, 0)
            first_tools = [tool for item in requests[0]['input'] if item.get('type') == 'additional_tools'
                           for tool in item['tools']]
            first_functions = first_tools + [tool for namespace in first_tools for tool in namespace.get('tools', [])]
            self.assertTrue(any(tool.get('name') == 'fixture_eager' for tool in first_functions))
            self.assertEqual(any(tool.get('type') == 'tool_search' for tool in first_tools), deferred)
            self.assertTrue(any(tool.get('name') == 'exec_command' for namespace in first_tools
                                for tool in namespace.get('tools', [])))
            self.assertTrue(any(part.get('text', '').startswith('You are Codex, an agent based on GPT-6.')
                for item in requests[0]['input'] for part in item.get('content', [])))
            if deferred:
                self.assertNotIn(description, json.dumps(requests[0]))
                found = next(item for item in requests[1]['input'] if item.get('type') == 'tool_search_output')
                self.assertEqual(found['execution'], 'client')
                self.assertEqual(found['tools'][0]['name'], DYNAMIC_TOOL_NAMESPACE)
                self.assertEqual(found['tools'][0]['tools'][0]['description'], description)
            outputs = [item for item in requests[-1]['input'] if item.get('type') == 'function_call_output']
            self.assertEqual(len(outputs), 3)
            self.assertIn('"value":7', json.dumps(outputs[0], separators=(',', ':')).replace('\\', ''))
            self.assertIn('maximum', json.dumps(outputs[1]))
            self.assertIn('tool_disabled_by_config:fixture_read', json.dumps(outputs[2]))
            thread_id = session.thread_id
            close_session(session)

            resumed = session_for_run()
            changed = deepcopy(catalog)
            changed[0]['deferLoading'] = not deferred
            with mock.patch.object(resumed, '_prepare') as prepare, \
                    mock.patch.object(resumed, '_stop_previous_owned_processes') as stop:
                with self.assertRaisesRegex(ValueError, 'resume_tool_registry_conflict'):
                    resumed.start(changed, 'Changed publication.', resume_thread_id=thread_id)
                prepare.assert_not_called()
                stop.assert_not_called()
            harness.thread_id = resumed.start(catalog, 'Exercise only the deterministic fixture tools.',
                                              resume_thread_id=thread_id)
            self.assertEqual(harness.thread_id, thread_id)
            resumed.run_turn('Continue with the previously declared fixture.')
            self.assertTrue(resumed.turn_done.wait(15))
            self.assertEqual(resumed.state, 'completed')
            self.assertEqual(dispatched, [{'value': 7}, {'value': 9}])
            self.assertEqual(len(requests), 6 + search_count)
            self.assertEqual(resumed.model_proxy.request_count, 2)
            self.assertEqual(store.read_state('runtime', 'model_requests')['completed_calls'], len(requests))
            self.assertEqual(len(store.records('tool_call')), 4)
            self.assertNotIn('Program-only sentinel.', json.dumps(requests))
            self.assertEqual(robot.executions, 0)
