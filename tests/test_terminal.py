"""Run with python -m unittest discover -s tests -p test_terminal.py."""

import asyncio
import json
import os
from pathlib import Path
import pty
import sys
import tempfile
import unittest
from unittest import mock

from auriga.cortex.session.terminal import Terminal, terminal_bootstrap_status


class TerminalTest(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_environment_does_not_inherit_host_variables(self):
        for environment in (None, {}, {"HOME": "/run-home", "CODEX_HOME": "/run-codex-home"}):
            with self.subTest(environment=environment), tempfile.TemporaryDirectory() as directory:
                code = ("import json,os; print(json.dumps({key: os.environ.get(key) for key in "
                        "['AURIGA_HOST_SECRET', 'HOME', 'CODEX_HOME', 'TERM', 'COLORTERM']}))")
                terminal = Terminal(directory, directory, (sys.executable, "-c", code), env=environment)
                try:
                    with mock.patch.dict(os.environ, {"AURIGA_HOST_SECRET": "host-only",
                            "HOME": "/host-home", "CODEX_HOME": "/host-codex-home"}):
                        await terminal.start("unix:///fixture.sock", "thread-fixture")
                    await asyncio.wait_for(terminal._exit_task, 3)
                    observed = json.loads(terminal.read()[0])
                    self.assertEqual(observed["AURIGA_HOST_SECRET"], "host-only" if environment is None else None)
                    self.assertEqual(observed["HOME"], "/host-home" if environment is None else environment.get("HOME"))
                    self.assertEqual(observed["CODEX_HOME"], "/host-codex-home" if environment is None else environment.get("CODEX_HOME"))
                    self.assertEqual((observed["TERM"], observed["COLORTERM"]), ("xterm-256color", "truecolor"))
                finally:
                    await terminal.close()

    async def test_partial_startup_drain_does_not_block_on_empty_pty(self):
        with tempfile.TemporaryDirectory() as directory:
            terminal = Terminal(directory, directory)
            master, slave = pty.openpty()
            terminal.master = master
            try:
                self.assertTrue(os.get_blocking(master))
                # Verify the syscall precondition without making a bad version
                # hang the suite inside a blocking os.read.
                from unittest import mock
                original_read = os.read
                def checked_read(fd, count):
                    self.assertFalse(os.get_blocking(fd))
                    return original_read(fd, count)
                with mock.patch('auriga.cortex.session.terminal.os.read', checked_read):
                    terminal._receive()
                self.assertEqual(terminal.cursor, 0)
            finally:
                os.close(slave)
                await terminal.close()

    async def test_durable_shared_pty_without_new_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "native_tui_fixture.py"
            fixture.write_text(
                "import json,os,sys,time\n"
                "assert os.isatty(0) and os.isatty(1)\n"
                "with open('/dev/tty', 'rb'): pass\n"
                "print(json.dumps(sys.argv[1:]),flush=True)\n"
                "time.sleep(.1)\n"
                "os.write(1, '\\x1b[32m未来输出\\x1b[0m'.encode())\n"
                "time.sleep(30)\n"
            )
            terminal = Terminal(directory, directory, (sys.executable, str(fixture)),
                                env={"TERM": "xterm", "COLORTERM": "fixture"})
            first = await terminal.start("unix:///fixture.sock", "thread-fixture")
            try:
                duplicate = await terminal.start("unix:///fixture.sock", "thread-fixture")
                self.assertEqual(first["pid"], duplicate["pid"])
                with self.assertRaises(ValueError):
                    await terminal.start("unix:///other.sock", "thread-fixture")

                async def observe():
                    data = b""
                    async for chunk, cursor in terminal.stream():
                        data += chunk
                        if "未来输出".encode() in data:
                            return data, cursor
                a, b = await asyncio.wait_for(asyncio.gather(observe(), observe()), 3)
                self.assertEqual(a, b)
                args = json.loads(a[0].splitlines()[0])
                self.assertEqual(args, ["resume", "--remote", "unix:///fixture.sock", "--no-alt-screen", "thread-fixture"])
                saved, cursor = terminal.read()
                self.assertEqual((saved, cursor), a)
                tail, end = terminal.read(cursor)
                self.assertEqual((tail, end), (b"", cursor))
                terminal.resize(80, 24)
                self.assertEqual(terminal.status()["cols"], 80)
                with self.assertRaises(ValueError):
                    terminal.resize(0, 24)
                with self.assertRaises(ValueError):
                    terminal.read(cursor + 1)
                # Disconnecting one reader does not own or close the shared PTY.
                subscriber = terminal.stream(cursor)
                waiting = asyncio.create_task(anext(subscriber))
                await asyncio.sleep(.01)
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)
                await subscriber.aclose()
                self.assertIsNone(terminal.process.poll())
                self.assertEqual(terminal.status()["pid"], first["pid"])
                reconnect = terminal.stream(0)
                replay_chunk, replay_cursor = await anext(reconnect)
                await reconnect.aclose()
                self.assertEqual((replay_chunk, replay_cursor), a)
                prefix, partial_cursor = terminal.read(0, 7)
                tail, end = terminal.read(partial_cursor)
                self.assertEqual(prefix + tail, a[0])
                self.assertEqual(end, cursor)
            finally:
                await terminal.close()
            recovered = Terminal(directory, directory)
            self.assertEqual(recovered.read(), a)
            self.assertIsNotNone(terminal.status()["exit_code"])
            self.assertEqual(recovered.status()["exit_code"], terminal.status()["exit_code"])

    async def test_native_bootstrap_failure_is_preserved_and_persisted_on_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "failed_tui.py"
            # This is the exact native error observed in the archived run, split
            # across writes so diagnosis cannot depend on PTY chunk boundaries.
            chunks = [b"\x1b[0mError: account/read failed during TUI ",
                      b"bootstrap: account/read failed: ",
                      b"workspace routing discovery failed (code -32603)\n\x1b[0 q"]
            fixture.write_text("import os,time\n" + "".join(
                f"os.write(1, {chunk!r}); time.sleep(.05)\n" for chunk in chunks
            ) + "raise SystemExit(1)\n")
            terminal = Terminal(directory, directory, (sys.executable, str(fixture)))
            await terminal.start("unix:///fixture.sock", "thread-fixture")
            metadata = Path(directory) / "terminal.json"

            async def persisted_exit():
                while True:
                    status = json.loads(metadata.read_text())
                    if status["exit_code"] is not None:
                        return status
                    await asyncio.sleep(.01)

            try:
                # No supervisor close is needed to make natural exit durable.
                status = await asyncio.wait_for(persisted_exit(), 3)
                self.assertEqual(status["exit_code"], 1)
                self.assertTrue(status["bootstrap_failed"])
                self.assertEqual(status["error"]["code"], "workspace_routing_discovery_failed")
                expected = b"".join(chunks).replace(b"\n", b"\r\n")
                self.assertEqual(terminal.read(), (expected, len(expected)))
                self.assertEqual(status["cursor"], len(expected))
                self.assertEqual(status["thread_id"], "thread-fixture")
                recovered = Terminal(directory, directory)
                self.assertEqual(recovered.status()["exit_code"], 1)
                self.assertEqual(recovered.status()["error"], status["error"])
                # An old archive with no diagnostic fields is diagnosed in memory.
                old_status = {key: value for key, value in status.items()
                              if key not in {"bootstrap_failed", "error"}}
                metadata.write_text(json.dumps(old_status))
                recovered = Terminal(directory, directory)
                self.assertTrue(recovered.status()["bootstrap_failed"])
                self.assertEqual(recovered.read(), (expected, len(expected)))
                self.assertEqual(json.loads(metadata.read_text()), old_status)
            finally:
                await terminal.close()

            # A later successful attachment must not inherit an earlier failure
            # from the append-only terminal log.
            clean = Terminal(directory, directory, (sys.executable, "-c", "print('ready')"))
            try:
                await clean.start("unix:///fixture.sock", "thread-next")
                status = await asyncio.wait_for(persisted_exit(), 3)
                self.assertEqual(status["exit_code"], 0)
                self.assertFalse(status["bootstrap_failed"])
                self.assertIsNone(status["error"])
                self.assertFalse(Terminal(directory, directory).status()["bootstrap_failed"])
            finally:
                await clean.close()

    def test_bootstrap_diagnosis_requires_native_error_prefix(self):
        for text in (b"", b"ready", b"Error: unrelated failure",
                     b"workspace routing discovery failed",
                     b"account/read failed during TUI bootstrap: quoted text"):
            with self.subTest(text=text):
                self.assertEqual(terminal_bootstrap_status(text),
                                 {"bootstrap_failed": False, "error": None})
        generic = terminal_bootstrap_status(
            b"Error: account/read failed during TUI bootstrap: account unavailable")
        self.assertTrue(generic["bootstrap_failed"])
        self.assertEqual(generic["error"]["code"], "account_read_bootstrap_failed")

    async def test_eof_waits_for_exit_without_sharing_subscriber_cancellation(self):
        with tempfile.TemporaryDirectory() as directory:
            release = Path(directory) / "release"
            fixture = Path(directory) / "eof_before_exit.py"
            fixture.write_text(
                "import os,signal,time\nfrom pathlib import Path\n"
                "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
                "os.write(1,b'last output')\n"
                "os.close(0); os.close(1); os.close(2)\n"
                f"while not Path({str(release)!r}).exists(): time.sleep(.01)\n"
                "raise SystemExit(7)\n"
            )
            terminal = Terminal(directory, directory, (sys.executable, str(fixture)))
            await terminal.start("unix:///fixture.sock", "thread-fixture")

            async def observe():
                data = b""
                async for chunk, _ in terminal.stream():
                    data += chunk
                return data, terminal.status()

            observers = [asyncio.create_task(observe()) for _ in range(2)]
            try:
                async def eof():
                    while terminal.master is not None:
                        await asyncio.sleep(.01)
                await asyncio.wait_for(eof(), 3)
                self.assertIsNone(terminal.process.poll())
                self.assertTrue(all(not observer.done() for observer in observers))
                observers[0].cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await observers[0]
                self.assertFalse(terminal._exit_task.cancelled())
                self.assertFalse(observers[1].done())
                release.touch()
                data, final = await asyncio.wait_for(observers[1], 3)
                self.assertEqual(data, b"last output")
                self.assertEqual(final["exit_code"], 7)
                saved = json.loads((Path(directory) / "terminal.json").read_text())
                self.assertEqual(saved["exit_code"], 7)
                self.assertEqual(saved["cursor"], len(data))
            finally:
                release.touch()
                await terminal.close()
                for observer in observers:
                    observer.cancel()
                await asyncio.gather(*observers, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
