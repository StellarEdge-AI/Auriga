"""Native replay transport selection; fixture bytes never represent a real run."""
import base64
from pathlib import Path
import tempfile
import unittest
from aiohttp import ClientSession, web
from auriga.apps.library.experiments import allocate_run, atomic_json, catalog_id
from auriga.apps.library.service import Library
from auriga.core.storage.store import Store


class NativeTerminalTransport(unittest.IsolatedAsyncioTestCase):
    async def test_native_restore_preserves_original_and_reconnect_cursor(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = allocate_run(root, category='synthetic', task='native-display')
            original = b'Error: account/read failed during TUI bootstrap: workspace routing discovery failed'
            (run / 'terminal.bin').write_bytes(original)
            atomic_json(run / 'terminal.json', dict(thread_id='fixture-thread', session_id='old', exit_code=1))
            identity = dict(run_id=run.name, episode_id='fixture-episode', env_instance_id='fixture-env', env_generation=1)
            store = Store(run, identity)
            try:
                store.set_state('runtime', 'codex', dict(thread_id='fixture-thread'))
            finally:
                store.close()
            identifier = catalog_id(run.relative_to(root))
            restored = b'\x1b[32mSynthetic native capture fixture\x1b[0m\r\n'
            cache = root / 'cache.bin'; cache.write_bytes(restored)
            result = dict(path=cache, status=dict(session_id='recovered-fixture', cursor=len(restored), cols=140, rows=40,
                          bootstrap_failed=False, exit_code=0, mode='read_only', replay=dict(kind='recovered_native_tui')))
            class Replay:
                value = None
                calls = 0
                fail = False
                def available(self, evidence, thread): return thread == 'fixture-thread'
                def peek(self, *args): return self.value
                def get(self, *args):
                    self.calls += 1
                    if self.fail: raise RuntimeError('private diagnostics must not reach browser')
                    self.value = result
                    return result
            library = Library(root); replay = Replay(); library.native_terminals = replay
            runner = web.AppRunner(library.app(), shutdown_timeout=.2)
            await runner.setup()
            try:
                site = web.TCPSite(runner, '127.0.0.1', 0); await site.start()
                origin = f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'
                endpoint = origin + f'/runs/{identifier}/v1/'
                async with ClientSession() as client:
                    async with client.get(endpoint + 'run') as response:
                        self.assertEqual((await response.json())['terminal']['replay']['kind'], 'native_resume_pending')
                    async with client.ws_connect(endpoint + 'terminal') as socket:
                        self.assertEqual((await socket.receive_json())['type'], 'restoring')
                        ready = await socket.receive_json(); self.assertEqual(ready['status']['session_id'], 'recovered-fixture')
                        data = await socket.receive_json(); self.assertEqual(base64.b64decode(data['data']), restored)
                        self.assertEqual((await socket.receive_json())['type'], 'status')
                        await socket.send_json(dict(type='input', data='unauthorized input'))
                        self.assertEqual((await socket.receive_json())['message'], 'replay_is_read_only')
                    self.assertEqual(replay.calls, 1)
                    async with client.get(endpoint + 'run') as response:
                        self.assertEqual((await response.json())['terminal']['replay']['kind'], 'recovered_native_tui')
                    async with client.ws_connect(endpoint + f'terminal?after={len(restored)}&session_id=recovered-fixture') as socket:
                        self.assertEqual((await socket.receive_json())['type'], 'ready')
                        self.assertEqual((await socket.receive_json())['type'], 'status')
                    async with client.ws_connect(endpoint + 'terminal?after=7&session_id=recovered-fixture') as socket:
                        await socket.receive_json()
                        data = await socket.receive_json(); self.assertEqual(base64.b64decode(data['data']), restored[7:])
                    async with client.get(endpoint + 'terminal-log') as response:
                        self.assertEqual(response.status, 200); self.assertEqual(await response.read(), original)
                    self.assertEqual((run / 'terminal.bin').read_bytes(), original)
                    replay.value = None; replay.fail = True
                    async with client.ws_connect(endpoint + 'terminal') as socket:
                        self.assertEqual((await socket.receive_json())['type'], 'restoring')
                        error = await socket.receive_json(); self.assertEqual(error['type'], 'error'); self.assertTrue(error['terminal'])
                        self.assertNotIn('private diagnostics', str(error))
            finally:
                await runner.cleanup()


if __name__ == '__main__': unittest.main()
