"""Real isolated app-server -> loopback Responses, with no provider/robot calls."""
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest

from PIL import Image
from codex_cli_bin import bundled_codex_path
from openai_codex.client import CodexConfig
from openai_codex.generated.v2_all import ConfigReadResponse

from auriga.core.presentation.actor_output import ActorOutput
from auriga.core.presentation.actor_presentation import ActorPresentation
from auriga.cortex.session.codex import RecordedClient, write_direct_model_catalog
from auriga.core.contracts import canonical
from auriga.core.storage.store import Store
from auriga.core.tool_output import ToolOutput
from auriga.runtime.tools.definitions import (DEPTH_REQUEST_DESCRIPTIONS,
    GEOMETRY_REQUEST_DESCRIPTIONS, PUBLIC_EXAMPLES, public_definitions)


class CodexImageTransportTests(unittest.TestCase):
    def test_native_dynamic_image_and_text_reach_actual_responses_request(self):
        requests, calls, notifications, endpoints = [], [], [], []
        # Inspect every schema in this transport fixture; discovery has its own
        # full CodexSession lifecycle test.
        public_tools = [dict(tool, deferLoading=False) for tool in public_definitions()]
        evidence = os.environ.get('AURIGA_CODEX_IMAGE_TEST_EVIDENCE')
        evidence_dir = Path(evidence) if evidence else None
        if evidence_dir:
            evidence_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        buffer = io.BytesIO()
        Image.new('RGB', (9, 7), (204, 51, 17)).save(buffer, format='PNG')
        image_bytes = buffer.getvalue()
        image_sha = hashlib.sha256(image_bytes).hexdigest()
        image_url = 'data:image/png;base64,' + base64.b64encode(image_bytes).decode()
        log = 'begin-native-log|' + 'diagnostic ' * 60 + '|end-native-log'
        proof_url = 'https://example.invalid/source-proof'
        metadata_log = 'fixture progress\n' + json.dumps(dict(hash=image_sha, source_url=proof_url,
            measured_position_m=[0.125, 0.0, 1.25], source_kind='measurement')) + '\nfixture end\n'
        result = dict(job_id='fixture-job', status='succeeded', stdout_text=log,
                      stderr_text=metadata_log, hash=image_sha, source_url=proof_url,
                      nested=dict(contentDigest=image_sha, checksums=[image_sha], sourceUrl=proof_url),
                      version=image_sha)
        image_part = dict(type='inputImage', imageUrl=image_url, hash=image_sha, source_url=proof_url)

        class ResponsesHandler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *_):
                pass

            def do_GET(self):
                # A provider/model discovery request cannot leave loopback.
                endpoints.append(dict(method='GET', path=self.path))
                body = json.dumps({'data': []}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append(payload)
                endpoints.append(dict(method='POST', path=self.path))
                if evidence_dir:
                    (evidence_dir / ('request-' + str(len(requests)) + '.json')).write_text(json.dumps(payload))
                if len(requests) == 1:
                    item = dict(type='function_call', id='fc-native-image', call_id='native-image-call',
                                name='image_fixture', arguments='{}', status='completed')
                else:
                    item = dict(type='message', id='msg-fixture-finish', role='assistant',
                                status='completed', content=[dict(type='output_text', text='fixture complete', annotations=[])])
                events = [
                    ('response.created', {'response': {'id': 'resp-fixture-' + str(len(requests)),
                         'status': 'in_progress', 'output': []}}),
                    ('response.output_item.added', {'output_index': 0, 'item': item}),
                    ('response.output_item.done', {'output_index': 0, 'item': item}),
                    ('response.completed', {'response': {'id': 'resp-fixture-' + str(len(requests)),
                         'status': 'completed', 'output': [item],
                         'usage': {'input_tokens': 10, 'output_tokens': 2, 'total_tokens': 12}}}),
                ]
                body = ''.join('event: ' + event + '\ndata: ' + json.dumps({'type': event, **value})
                               + '\n\n' for event, value in events).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(('127.0.0.1', 0), ResponsesHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory(prefix='auriga-native-image-') as directory:
            root = Path(directory)
            codex_home = root / 'codex-home'
            codex_home.mkdir()
            catalog = write_direct_model_catalog(codex_home / 'models.json')
            base_url = 'http://127.0.0.1:' + str(server.server_address[1]) + '/v1'
            (codex_home / 'config.toml').write_text(
                'model = "gpt-6.1-sol"\nmodel_provider = "fixture"\n'
                'model_catalog_json = ' + json.dumps(str(catalog)) + '\n'
                'approval_policy = "never"\nsandbox_mode = "danger-full-access"\n'
                'web_search = "disabled"\n'
                '[model_providers.fixture]\nname = "Loopback fixture"\n'
                'base_url = ' + json.dumps(base_url) + '\nwire_api = "responses"\n'
                'requires_openai_auth = false\nsupports_websockets = false\n'
                '[features]\ncode_mode_host = false\nshell_tool = true\nunified_exec = true\n'
                'memories = false\napps = false\nplugins = false\n'
                '[features.code_mode]\nenabled = false\n')
            store = Store(root / 'run', dict(run_id='native-image-transport', episode_id='episode',
                env_instance_id='synthetic', env_generation=1))
            presenter = ActorOutput(store, ActorPresentation(store))

            def tool_handler(params):
                calls.append(params)
                self.assertEqual(params['tool'], 'image_fixture')
                return presenter.present(ToolOutput(result, extra_content=(image_part,)).response(),
                                         tool=params['tool'])

            # The SDK normally copies os.environ. Start a completely separate
            # app-server without host auth, proxy, or orchestrator context.
            client = RecordedClient(CodexConfig(cwd=str(root), launch_args_override=(
                '/usr/bin/env', '-i', 'PATH=/usr/bin:/bin', 'HOME=' + str(root),
                'CODEX_HOME=' + str(codex_home), 'TMPDIR=' + str(root),
                str(bundled_codex_path()), '--no-daemon', 'app-server', '--listen', 'stdio://')),
                store, tool_handler, notifications.append)
            try:
                client.start()
                client.initialize()
                effective = client.request('config/read', {'includeLayers': True}, response_model=ConfigReadResponse)
                features = effective.model_dump(by_alias=True)['config']['features']
                self.assertFalse(features['code_mode']['enabled'])
                self.assertFalse(features['code_mode_host'])
                started = client.thread_start(dict(model='gpt-6.1-sol', modelProvider='fixture',
                    cwd=str(root), approvalPolicy='never', sandbox='danger-full-access',
                    dynamicTools=public_tools + [dict(type='function', name='image_fixture', deferLoading=False,
                        description='Return deterministic fixture image and diagnostic log.',
                        inputSchema=dict(type='object', properties={}, additionalProperties=False))]))
                turn = client.turn_start(started.thread.id, 'Call image_fixture, then finish.')
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    terminal = next((message for message in notifications
                        if message.get('method') == 'turn/completed'
                        and message.get('params', {}).get('turn', {}).get('id') == turn.turn.id), None)
                    if terminal:
                        break
                    time.sleep(.01)
                else:
                    self.fail('isolated native turn did not finish; stderr: ' + '\n'.join(client._stderr_lines)[-2500:])
                self.assertEqual(terminal['params']['turn']['status'], 'completed', terminal)
                self.assertEqual(len(calls), 1)
                self.assertEqual(len(requests), 2)
                self.assertEqual(endpoints, [dict(method='POST', path='/v1/responses')] * 2)
                self.assertEqual(requests[1]['model'], 'gpt-6.1-sol')
                tool_specs = requests[0].get('tools', []) + [tool
                    for item in requests[0]['input'] if item.get('type') == 'additional_tools'
                    for tool in item.get('tools', [])]
                flat_specs = [spec for spec in tool_specs if spec.get('type') == 'function']
                flat_specs += [tool for spec in tool_specs for tool in spec.get('tools', [])
                               if tool.get('type') == 'function']
                native_tools = {spec['name']: spec for spec in flat_specs}
                names = sorted({spec['name'] for spec in tool_specs if spec.get('name')} | set(native_tools))
                description_coverage = {}
                for definition in public_tools:
                    name = definition['name']
                    self.assertIn(name, native_tools)
                    self.assertEqual(native_tools[name]['description'], definition['description'], name)
                    for example in PUBLIC_EXAMPLES.get(name, []):
                        self.assertIn(canonical(example), native_tools[name]['description'], name)
                    def schema_nodes(value, path=()):
                        if isinstance(value, dict):
                            yield path, value
                            for key, child in value.items():
                                if key not in {'description', 'examples', 'default', 'enum', 'const', 'if', 'not'}:
                                    yield from schema_nodes(child, path + (key,))
                        elif isinstance(value, list):
                            for index, child in enumerate(value):
                                yield from schema_nodes(child, path + (index,))
                    local_nodes = dict(schema_nodes(definition['inputSchema']))
                    local_help = {path: node['description'] for path, node in local_nodes.items()
                                  if 'description' in node}
                    native_help = {path: node['description'] for path, node in
                                   schema_nodes(native_tools[name]['parameters']) if 'description' in node}
                    for path, help_text in local_help.items():
                        self.assertTrue(native_help.get(path) == help_text or
                            help_text in native_tools[name]['description'], (name, path))
                    lost_limits = ('minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum',
                                   'maxItems', 'minLength', 'maxLength', 'pattern', 'uniqueItems')
                    limit_nodes = [node for node in local_nodes.values() if any(key in node for key in lost_limits)]
                    # Constraint prose is checked by description tests. The
                    # exact equality above proves its complete native delivery.
                    description_coverage[name] = dict(
                        local_schema_field_guides=len(local_help),
                        native_schema_field_guides=sum(native_help.get(path) == help_text
                            for path, help_text in local_help.items()),
                        native_visible_distinct_schema_descriptions=len(set(local_help.values())),
                        native_visible_limit_nodes=len(limit_nodes),
                        native_schema_bytes=len(canonical(native_tools[name]['parameters']).encode()),
                        top_description_bytes=len(native_tools[name]['description'].encode()),
                        top_description_equal=True)
                self.assertEqual(native_tools['robot_action']['parameters']['properties']['stages']
                    ['items']['properties']['angles_deg']['items']['type'], 'number')
                request_guide_counts = {}
                for name, fields in (('geometry_query', GEOMETRY_REQUEST_DESCRIPTIONS),
                                     ('depth_query', DEPTH_REQUEST_DESCRIPTIONS)):
                    for field, help_text in fields.items():
                        self.assertIn(help_text, native_tools[name]['description'], (name, field))
                    request_guide_counts[name] = len(fields)
                output = next(x for x in requests[1]['input']
                    if x.get('type') == 'function_call_output' and x.get('call_id') == 'native-image-call')
                self.assertIsInstance(output['output'], list)
                images = [part for part in output['output'] if part.get('type') == 'input_image']
                self.assertEqual(len(images), 1)
                self.assertEqual(images[0]['image_url'], image_url)
                self.assertIn(images[0].get('detail'), (None, 'high'))
                received = base64.b64decode(images[0]['image_url'].split(',', 1)[1], validate=True)
                self.assertEqual(received, image_bytes)
                self.assertEqual(hashlib.sha256(received).hexdigest(), image_sha)
                text = '\n'.join(part['text'] for part in output['output'] if part['type'] == 'input_text')
                self.assertNotIn('data:image', text)
                self.assertIn(log, text)
                self.assertNotIn(image_sha, text)
                self.assertNotIn(proof_url, text)
                shown_result = json.loads(next(part['text'] for part in output['output']
                    if part['type'] == 'input_text'))
                self.assertEqual(shown_result, dict(job_id='fixture-job', status='succeeded', stdout_text=log,
                    stderr_text='fixture progress\n' + canonical(dict(measured_position_m=[0.125, 0.0, 1.25],
                        source_kind='measurement')) + '\nfixture end\n', nested={}))
                self.assertEqual(result['hash'], image_sha)
                self.assertEqual(result['stderr_text'], metadata_log)
                self.assertIn('image_fixture', names)
                self.assertNotIn('exec', names)
                if evidence_dir:
                    (evidence_dir / 'summary.json').write_text(json.dumps(dict(
                        request_count=len(requests), endpoints=endpoints, model=requests[1]['model'],
                        tool_names=names, image_count=len(images), image_sha256=image_sha,
                        public_tool_count=len(public_tools), public_description_coverage=description_coverage,
                        request_field_guides=request_guide_counts,
                        native_detail=images[0].get('detail'), source_bytes_equal=received == image_bytes,
                        stdout_equal=shown_result['stdout_text'] == log,
                        wire_metadata_removed=image_sha not in text and proof_url not in text,
                        jsonl_measurements_preserved=shown_result['stderr_text'].splitlines()[1]
                            == canonical(dict(measured_position_m=[0.125, 0.0, 1.25], source_kind='measurement')),
                        feature_flags=features, app_server_args=list(client._proc.args),
                        request_body_sha256=[hashlib.sha256(json.dumps(x).encode()).hexdigest() for x in requests],
                        external_model_or_robot_calls=0, catalog_discovery_requests=0,
                        turn_status=terminal['params']['turn']['status']), indent=2))
            finally:
                process = client._proc
                client.close()
                if process:
                    for stream in (process.stdout, process.stderr):
                        if stream:
                            stream.close()
                client.server_pool.shutdown(wait=True)
                store.close()


if __name__ == '__main__':
    unittest.main()
