"""Optional view tools: clean-process imports and real loopback native delivery."""
import asyncio
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.abc
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest


TOOLS = ('robotic_view', 'stereo_guide')
CONDITIONS = {
    'default': {},
    'off': dict(robotic_view=False, stereo_guide=False),
    'view': dict(robotic_view=True),
    'stereo': dict(stereo_guide=True),
    'both': dict(robotic_view=True, stereo_guide=True),
    'disabled': dict(robotic_view=True, stereo_guide=True, disabled_tools=list(TOOLS)),
}


def _capture(root, features):
    """A fresh interpreter imports only this checkout; provider traffic is local."""
    class RefuseDisabledImport(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            for name in TOOLS:
                if fullname == 'auriga.runtime.tools.plugins.' + name and not features.get(name, False):
                    raise AssertionError('Disabled plugin imported: ' + fullname)
            return None

    sys.meta_path.insert(0, RefuseDisabledImport())
    from auriga.core.config import Config
    from auriga.apps.gateway.workbench import Workbench
    from test_harness import FixtureRobot

    root = Path(root)
    root.mkdir(parents=True)
    requests, endpoints = [], []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(request)
            endpoints.append(self.path)
            index = len(requests)
            if index == 1:
                item = dict(type='function_call', id='observation-item', call_id='observation-call',
                            name='robot_observe', arguments='{}', status='completed')
            else:
                item = dict(type='message', id='fixture-message', role='assistant', status='completed',
                            content=[dict(type='output_text', text='Fixture complete.', annotations=[])])
            response = dict(id='fixture-response-' + str(index), model=request['model'],
                status='completed', output=[item], usage=dict(input_tokens=10, output_tokens=2, total_tokens=12))
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

    class NoPhysicsRobot(FixtureRobot):
        def observe(self):
            observation = super().observe()
            observation.update(camera_calibration={'cameras': {'fixture_camera': {
                'K': [[1, 0, 0], [0, 1, 0], [0, 0, 1]], 'image_size': [1, 1],
                'distortion': {'model': 'opencv', 'coefficients': []}}}},
                camera_pose_sample={'cameras': {'fixture_camera': {'T_world_camera': [
                    [1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]}}},
                robot_model={'base_link': 'fixture-base', 'joints': []})
            return observation

        def execute(self, *args, **kwargs):
            raise AssertionError('No physical actions in delivery fixture')

        def close(self):
            return {'process_exited': True}

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    source = root / 'bootstrap'
    source.mkdir()
    (source / 'config.toml').write_text('model_provider="fixture"\n[model_providers.fixture]\n'
        f'base_url="http://127.0.0.1:{server.server_port}/v1"\n'
        'requires_openai_auth=false\nwire_api="responses"\nsupports_websockets=false\n')
    config = Config.from_dict(dict(model=dict(name='gpt-6.1-sol', effort='high',
        codex_home=str(source), native_memories=False), features=dict(
            sandbox_enabled=False, motion_review=False, terminal=False, record_video=False,
            export_video=False, grasp_memory_dir=str(root / 'memory'), **features)))
    robot = NoPhysicsRobot(root)
    workbench = Workbench(root / 'run', robot=robot, config=config)

    async def exercise():
        try:
            await workbench.start('Inspect the fixed observation and finish.', benchmark=False)
            if not await asyncio.to_thread(workbench.session.turn_done.wait, 25):
                raise AssertionError('Native loopback turn timed out')
            if workbench.session.state != 'completed':
                raise AssertionError('Native loopback state: ' + workbench.session.state)
            return dict(requests=requests, endpoints=endpoints, robot_executions=robot.executions,
                catalogs={role: workbench.harness.tool_registry.catalog(workbench.harness, role=role)
                          for role in ('actor', 'program')},
                imported={name: 'auriga.runtime.tools.plugins.' + name in sys.modules for name in TOOLS})
        finally:
            bridge = workbench.session.client._proc if workbench.session.client else None
            report = await workbench.close()
            if bridge:
                for stream in (bridge.stdin, bridge.stdout, bridge.stderr):
                    if stream and not stream.closed:
                        stream.close()
            if report['errors']:
                raise AssertionError(report['errors'])

    try:
        return asyncio.run(exercise())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _normalize(value):
    """Only native random IDs and turn-start time; preserve prompts and schemas."""
    identities = {}
    uuid = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'

    def identity(text):
        return re.sub(uuid, lambda match: identities.setdefault(match[0], '<id-' + str(len(identities)) + '>'), text)

    def metadata(item):
        names = {'root_turn_id', 'thread_id', 'session_id', 'turn_id', 'installation_id',
                 'window_id', 'context_window_id', 'x-codex-installation-id', 'x-codex-window-id'}
        return {key: identity(child) if key in names and isinstance(child, str) else
                '<turn-start-time>' if key == 'turn_started_at_unix_ms' and type(child) is int else
                json.dumps(metadata(json.loads(child)), separators=(',', ':'), sort_keys=True)
                if key == 'x-codex-turn-metadata' else child for key, child in sorted(item.items())}

    def walk(item):
        if isinstance(item, list):
            return [walk(child) for child in item]
        if not isinstance(item, dict):
            return item
        out = {}
        for key, child in sorted(item.items()):
            if key == 'prompt_cache_key':
                out[key] = identity(child)
            elif key == 'client_metadata':
                out[key] = metadata(child)
            elif (key == 'id' and item.get('type') in {
                    'additional_tools', 'message', 'tool_search_output', 'function_call_output'}
                    and isinstance(child, str) and re.fullmatch(r'(at|msg|tso|fco)_' + uuid, child)):
                out[key] = identity(child)
            else:
                out[key] = walk(child)
        return out
    return walk(value)


def _without_tools(value, names):
    if isinstance(value, list):
        return [_without_tools(child, names) for child in value
                if not (isinstance(child, dict) and child.get('type') == 'function' and child.get('name') in names)]
    if isinstance(value, dict):
        return {key: _without_tools(child, names) for key, child in value.items()}
    return value


class OptionalViewIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='view-isolation-')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.fixture = Path(cls.temporary.name) / 'fixed-fixture'
        cls.captures = {}

    @classmethod
    def capture(cls, mode):
        if mode not in cls.captures:
            shutil.rmtree(cls.fixture, ignore_errors=True)
            checkout = Path(__file__).resolve().parents[1]
            env = dict(PATH=os.defpath, LANG='C.UTF-8', HOME=cls.temporary.name,
                TMPDIR=cls.temporary.name, PYTHONDONTWRITEBYTECODE='1',
                PYTHONPATH=os.pathsep.join((str(checkout), str(checkout / 'tests'))))
            completed = subprocess.run([sys.executable, '-P', str(Path(__file__).resolve()),
                '--capture', str(cls.fixture), json.dumps(CONDITIONS[mode])], cwd=cls.temporary.name,
                env=env, capture_output=True, text=True, timeout=60)
            if completed.returncode:
                raise AssertionError('Loopback fixture failed: ' + completed.stderr[-8000:])
            cls.captures[mode] = json.loads(completed.stdout)
        return deepcopy(cls.captures[mode])

    def test_default_explicit_off_and_disabled_have_identical_native_requests(self):
        baseline = self.capture('default')
        self.assertEqual(baseline['imported'], dict.fromkeys(TOOLS, False))
        for mode in ('default', 'off', 'disabled'):
            with self.subTest(mode=mode):
                value = self.capture(mode)
                self.assertEqual(value['catalogs'], baseline['catalogs'])
                self.assertEqual(_normalize(value['requests']), _normalize(baseline['requests']))
                self.assertEqual(value['endpoints'], ['/v1/responses'] * 2)
                self.assertEqual(value['robot_executions'], 0)
                for tool in TOOLS:
                    self.assertNotIn(tool, json.dumps(value['requests']))
                if mode != 'disabled':
                    self.assertEqual(value['imported'], dict.fromkeys(TOOLS, False))

    def test_flags_add_only_their_own_schema_for_actor_program_and_native(self):
        baseline = self.capture('default')
        for mode, added in (('view', {TOOLS[0]}), ('stereo', {TOOLS[1]}), ('both', set(TOOLS))):
            with self.subTest(mode=mode):
                value = self.capture(mode)
                self.assertEqual(value['imported'], {name: name in added for name in TOOLS})
                for role in ('actor', 'program'):
                    catalog = value['catalogs'][role]
                    self.assertEqual([tool for tool in catalog if tool['name'] not in added], baseline['catalogs'][role])
                    self.assertEqual({tool['name'] for tool in catalog} & set(TOOLS), added)
                self.assertEqual(_normalize(_without_tools(value['requests'], added)), _normalize(baseline['requests']))
                native = [tool for item in value['requests'][0]['input'] if item.get('type') == 'additional_tools'
                          for tool in item.get('tools', [])]
                native += [tool for namespace in list(native) for tool in namespace.get('tools', [])]
                for name in added:
                    declared = next(tool for tool in value['catalogs']['actor'] if tool['name'] == name)
                    self.assertEqual(declared, next(tool for tool in value['catalogs']['program'] if tool['name'] == name))
                    delivered = [tool for tool in native if tool.get('name') == name]
                    self.assertEqual(len(delivered), 1)
                    self.assertEqual(delivered[0]['description'], declared['description'])
                    self.assertEqual(set(delivered[0]['parameters']['properties']), set(declared['inputSchema']['properties']))
                    self.assertEqual(delivered[0]['parameters']['required'], declared['inputSchema']['required'])
                self.assertEqual(value['robot_executions'], 0)


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--capture':
        print(json.dumps(_capture(sys.argv[2], json.loads(sys.argv[3]))))
    else:
        unittest.main()
