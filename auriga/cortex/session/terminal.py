"""One durable, read-only browser view of the native Codex remote TUI."""

import asyncio
import errno
import fcntl
import json
import os
from pathlib import Path
import pty
import signal
import struct
import subprocess
import termios
import uuid


def _claim_terminal():
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def terminal_bootstrap_status(data):
    """Recognize the native startup failure without copying arbitrary TUI text."""
    if b"Error: account/read failed during TUI bootstrap:" not in data:
        return {"bootstrap_failed": False, "error": None}
    routing_failed = b"workspace routing discovery failed" in data
    return {
        "bootstrap_failed": True,
        "error": {
            "code": "workspace_routing_discovery_failed" if routing_failed else "account_read_bootstrap_failed",
            "message": "Native Codex TUI account initialization failed"
                       + (": workspace routing discovery failed." if routing_failed else "."),
        },
    }


class Terminal:
    def __init__(self, run_dir, cwd, command_prefix=("codex",), env=None):
        self.path = Path(run_dir) / "terminal.bin"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.cwd = str(cwd)
        self.command_prefix = list(command_prefix)
        self.env = dict(env) if env is not None else None
        self.session_id = "pty-" + uuid.uuid4().hex
        self.thread_id = None
        self.socket_uri = None
        self.cols, self.rows = 100, 32
        self.process = None
        self.master = None
        self.log = None
        self.changed = asyncio.Event()
        self.cursor = self.path.stat().st_size if self.path.exists() else 0
        self._closed = False
        self._exit_task = None
        self._diagnostic_tail = b""
        self._command = None
        self._exit_code = None
        self.bootstrap_failed = False
        self.error = None
        metadata = self.path.with_suffix(".json")
        if metadata.exists():
            saved = json.loads(metadata.read_text())
            self.session_id = saved.get("session_id", self.session_id)
            self.thread_id = saved.get("thread_id")
            self.socket_uri = saved.get("socket_uri")
            self._command = saved.get("command")
            self._exit_code = saved.get("exit_code")
            self.cols, self.rows = saved.get("cols", self.cols), saved.get("rows", self.rows)
        else:
            saved = {}
        if "bootstrap_failed" in saved:
            self.bootstrap_failed = saved["bootstrap_failed"]
            self.error = saved.get("error")
        elif self.path.exists():
            # Old archives predate diagnostic metadata; leave their bytes untouched.
            with self.path.open("rb") as log:
                log.seek(max(0, self.cursor - 65536))
                diagnostic = terminal_bootstrap_status(log.read())
            self.bootstrap_failed, self.error = diagnostic["bootstrap_failed"], diagnostic["error"]

    async def start(self, socket_uri, thread_id):
        """Attach exactly once, without a prompt or a new agent turn."""
        if self.process is not None:
            if (self.socket_uri, self.thread_id) != (socket_uri, thread_id):
                raise ValueError("a terminal cannot change its app-server or thread")
            return self.status()
        if self._closed:
            raise RuntimeError("terminal has been closed")
        if not socket_uri.startswith(("unix://", "ws://", "wss://")) or not thread_id:
            raise ValueError("a remote endpoint and explicit thread id are required")
        self.session_id = "pty-" + uuid.uuid4().hex
        self.socket_uri, self.thread_id = socket_uri, thread_id
        self._exit_code = None
        self._diagnostic_tail = b""
        self.bootstrap_failed, self.error = False, None
        master, slave = pty.openpty()
        self.master = master
        self.resize(self.cols, self.rows)
        env = {**(os.environ if self.env is None else self.env), "TERM": "xterm-256color", "COLORTERM": "truecolor"}
        command = self.command_prefix + [
            "resume", "--remote", socket_uri, "--no-alt-screen", thread_id,
        ]
        self._command = command
        try:
            self.log = self.path.open("ab")
            self.process = subprocess.Popen(
                command, stdin=slave, stdout=slave, stderr=slave,
                cwd=self.cwd, env=env, preexec_fn=_claim_terminal,
            )
        except BaseException:
            os.close(master)
            self.master = None
            if self.log is not None:
                self.log.close()
            raise
        finally:
            os.close(slave)
        os.set_blocking(master, False)
        asyncio.get_running_loop().add_reader(master, self._receive)
        self._persist_status()
        self._exit_task = asyncio.create_task(self._wait_for_exit())
        return self.status()

    def _persist_status(self):
        metadata = self.path.with_suffix(".json")
        temporary = metadata.with_name(f".{metadata.name}.{self.session_id}.tmp")
        temporary.write_text(json.dumps({**self.status(), "socket_uri": self.socket_uri,
                                         "command": self._command}, indent=2))
        temporary.replace(metadata)

    async def _wait_for_exit(self):
        self._exit_code = await asyncio.to_thread(self.process.wait)
        # Drain final diagnostics before publishing the terminal's exit status.
        while self.master is not None:
            before = self.cursor
            self._receive()
            if before == self.cursor:
                break
        self._persist_status()
        self.changed.set()

    def _receive(self):
        if self.master is None:
            return
        try:
            # Startup can fail before setting descriptor flags; the final drain
            # must remain nonblocking even for that partially initialized PTY.
            os.set_blocking(self.master, False)
            data = os.read(self.master, 65536)
        except BlockingIOError:
            return
        except OSError as exc:
            if exc.errno != errno.EIO:
                raise
            data = b""
        if data:
            self.log.write(data)
            self.log.flush()
            self.cursor += len(data)
            diagnostic = terminal_bootstrap_status(self._diagnostic_tail + data)
            self._diagnostic_tail = (self._diagnostic_tail + data)[-4096:]
            if diagnostic["bootstrap_failed"] and diagnostic["error"] != self.error:
                self.bootstrap_failed, self.error = True, diagnostic["error"]
                self._persist_status()
        else:
            asyncio.get_running_loop().remove_reader(self.master)
            os.close(self.master)
            self.master = None
            self._persist_status()
        self.changed.set()

    def read(self, after=0, limit=65536):
        """Return immutable bytes and their exclusive end offset."""
        if not isinstance(after, int) or not 0 <= after <= self.cursor:
            raise ValueError("terminal cursor is outside the persisted stream")
        if not isinstance(limit, int) or not 1 <= limit <= 1048576:
            raise ValueError("terminal read limit must be between 1 and 1048576")
        if not self.path.exists():
            return b"", after
        with self.path.open("rb") as log:
            log.seek(after)
            data = log.read(limit)
        return data, after + len(data)

    async def stream(self, after=0):
        """Independent subscribers replay the log, then follow the same PTY."""
        while True:
            self.changed.clear()
            data, cursor = self.read(after)
            if data:
                after = cursor
                yield data, cursor
                continue
            if self._closed or (self.process is not None and self.master is None):
                if self._exit_task is not None:
                    # EOF can arrive before waitpid observes process exit. A
                    # disconnected subscriber must not cancel the shared waiter.
                    await asyncio.shield(self._exit_task)
                    if after < self.cursor:
                        continue
                return
            await self.changed.wait()

    def resize(self, cols, rows):
        if (not isinstance(cols, int) or not isinstance(rows, int)
                or not 20 <= cols <= 500 or not 5 <= rows <= 200):
            raise ValueError("terminal size must be 20..500 columns and 5..200 rows")
        self.cols, self.rows = cols, rows
        if self.master is not None:
            fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def status(self):
        return {
            "session_id": self.session_id, "thread_id": self.thread_id,
            "cursor": self.cursor, "pid": self.process.pid if self.process else None,
            "exit_code": self.process.poll() if self.process else self._exit_code,
            "cols": self.cols, "rows": self.rows, "mode": "read_only",
            "bootstrap_failed": self.bootstrap_failed, "error": self.error,
        }

    async def close(self):
        """Only the run supervisor closes the PTY; browser disconnects never do."""
        self._closed = True
        if self.process is not None and self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.to_thread(self.process.wait, timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                await asyncio.to_thread(self.process.wait)
        if self._exit_task is not None:
            await self._exit_task
        if self.master is not None:
            self._receive()
        if self.master is not None:
            asyncio.get_running_loop().remove_reader(self.master)
            os.close(self.master)
            self.master = None
        if self.log is not None:
            self.log.close()
        if self.process is not None:
            self._persist_status()
        self.changed.set()
