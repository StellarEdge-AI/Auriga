"""No-model Unix WebSocket/stdio regression, using the active Python environment."""

import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest

from aiohttp import web, WSMsgType

from auriga.cortex.session.codex import CodexSession
from auriga.core.contracts import canonical
from auriga.core.storage.store import Store


class CodexCheck(unittest.IsolatedAsyncioTestCase):
    async def test_large_jsonl_echo_eof_and_notification_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            socket = Path(temporary) / "fixture-app-server.sock"

            async def echo(request):
                connection = web.WebSocketResponse(max_msg_size=2 * 1024 * 1024)
                await connection.prepare(request)
                async for message in connection:
                    if message.type == WSMsgType.TEXT:
                        await connection.send_str(message.data)
                return connection

            app = web.Application()
            app.router.add_get("/", echo)
            runner = web.AppRunner(app)
            await runner.setup()
            process = None
            try:
                await web.UnixSite(runner, str(socket)).start()
                process = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "auriga.cortex.session.codex", "proxy", str(socket),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, limit=2 * 1024 * 1024,
                )
                payload = dict(jsonrpc="2.0", id=1, result=dict(data_origin="synthetic_fixture", payload="x" * (128 * 1024)))
                line = (canonical(payload) + "\n").encode()
                process.stdin.write(line)
                await process.stdin.drain()
                echoed = await asyncio.wait_for(process.stdout.readline(), timeout=10)
                self.assertEqual(echoed, line)
                self.assertEqual(json.loads(echoed), payload)
                process.stdin.close()
                await process.stdin.wait_closed()
                self.assertEqual(await asyncio.wait_for(process.wait(), timeout=5), 0)
                self.assertEqual(await process.stderr.read(), b"")
            finally:
                if process is not None and process.returncode is None:
                    process.kill()
                    await process.wait()
                await runner.cleanup()

            identity = dict(run_id="synthetic-codex", episode_id="fixture-episode", env_instance_id="fixture-env", env_generation=1)
            store = Store(Path(temporary) / "store", identity)
            try:
                # No app-server/model launch: exercise only raw notification indexing.
                session = CodexSession(temporary, store, lambda params: None)
                session.thread_id, session.turn_id, session.state = "owned-thread", "current-turn", "running"
                session._notification(dict(method="future/fixtureNotification", params=None))
                for thread, turn in [("other-thread", "other-turn"), ("owned-thread", "old-turn")]:
                    session._notification(dict(method="turn/completed", params=dict(threadId=thread, turn=dict(id=turn, status="completed"))))
                    self.assertEqual(session.state, "running")
                    self.assertFalse(session.turn_done.is_set())
                session._notification(dict(method="turn/completed", params=dict(threadId="owned-thread", turn=dict(id="current-turn", status="completed"))))
                self.assertEqual(session.state, "completed")
                self.assertTrue(session.turn_done.is_set())
                self.assertEqual(len(store.events()), 4)
                native_result = session.turn_result
                session.state = "running"
                session.turn_done.clear()
                session.close()
                self.assertEqual(session.state, "closed")
                self.assertTrue(session.turn_done.is_set())
                self.assertEqual(session.turn_result, native_result)

                session.state, session.thread_id, session.turn_id = "running", "owned-thread", "current-turn"

                class CompletedTurnClient:
                    def turn_interrupt(self, _thread_id, _turn_id):
                        from auriga.cortex.session.codex import JsonRpcError
                        raise JsonRpcError(-32600, "no active turn to interrupt")

                session.client = CompletedTurnClient()
                session.interrupt()
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()

