"""Recover native Codex display bytes from an isolated, offline archive copy.

This is a fresh rendering by the installed Codex TUI, not the original recording.
The helper process cannot create Internet sockets or write outside its cache.
No prompt is sent; the only PTY input answers terminal cursor-position queries.
"""

from collections import Counter
from contextlib import contextmanager
import ctypes
import ctypes.util
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import platform
import pty
import re
import select
import shutil
import signal
import sqlite3
import stat
import struct
import subprocess
import sys
import termios
import threading
import time
import uuid


CACHE_VERSION = 3
COLS, ROWS = 140, 40
_CAPTURES = threading.BoundedSemaphore(2)
_ANSI = re.compile(rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


class NativeReplayError(RuntimeError):
    """The archive could not be safely rendered by the native CLI."""


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _inside(root, path, *, directory=False):
    """Require a real path beneath root, with no symlink in its relative chain."""
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise NativeReplayError("archive path escapes its evidence directory") from exc
    for candidate in (root, *(root.joinpath(*relative.parts[:n])
                              for n in range(1, len(relative.parts) + 1))):
        if candidate.is_symlink():
            raise NativeReplayError("symlinks are not allowed in native replay evidence or cache")
    mode = path.stat().st_mode
    if not (stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)):
        raise NativeReplayError("native replay requires regular files and directories")
    return path


def _history(home, thread_id):
    with sqlite3.connect(home / "thread_history_1.sqlite") as db:
        result = {}
        for table in ("thread_turns", "thread_items"):
            rows = db.execute(f"SELECT * FROM {table} WHERE thread_id=? ORDER BY rowid",
                              (thread_id,)).fetchall()
            result[table] = {"count": len(rows), "sha256": hashlib.sha256(_json(rows).encode()).hexdigest()}
        return result


def _archive_contract(home):
    """Probe the copied native schema before relying on versioned internal SQL."""
    required = {"state_5.sqlite": {"threads": {"id", "model_provider", "model", "reasoning_effort", "rollout_path", "cwd"}},
                "thread_history_1.sqlite": {"thread_turns": {"thread_id"},
                    "thread_items": {"thread_id", "item_json", "rollout_ordinal"}}}
    result = {"adapter": "codex_archive_v1", "databases": {}}
    for name, tables in required.items():
        with sqlite3.connect((home / name).as_uri() + "?mode=ro", uri=True) as db:
            schema = {}
            for table, columns in tables.items():
                actual = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                if not columns <= actual:
                    raise NativeReplayError(f"unsupported native archive schema: {name}/{table} missing {','.join(sorted(columns - actual))}")
                schema[table] = sorted(actual)
            result["databases"][name] = {"user_version": db.execute("PRAGMA user_version").fetchone()[0], "tables": schema}
    return result


def _normalized_text(value):
    # Native Markdown styling and terminal soft wraps change punctuation/spacing.
    return "".join(re.findall(r"\w+", value, flags=re.UNICODE))


def _display_contract(home, thread_id):
    """Use persisted items only to verify that the real TUI rendered its history."""
    with sqlite3.connect(home / "thread_history_1.sqlite") as db:
        rows = db.execute("SELECT item_json FROM thread_items WHERE thread_id=? ORDER BY rollout_ordinal",
                          (thread_id,)).fetchall()
    tools, anchors = [], []
    user_messages = 0
    for (raw,) in rows:
        item = json.loads(raw)
        kind = item.get("type")
        text = ""
        if kind == "userMessage":
            user_messages += 1
            text = "\n".join(part.get("text", "") for part in item.get("content", [])
                             if part.get("type") == "text")
        elif kind == "agentMessage":
            text = item.get("text", "")
        elif kind == "dynamicToolCall":
            label = "Failed" if item.get("success") is False or item.get("status") == "failed" else "Called"
            tools.append([label, item["tool"], item.get("durationMs")])
        normalized = _normalized_text(text)
        if normalized:
            # Both ends cover long prompts/messages without depending on all of
            # Markdown's formatting choices in the middle of the native display.
            anchors.append(normalized[:120])
            if len(normalized) > 120:
                anchors.append(normalized[-80:])
    if not anchors:
        raise NativeReplayError("archive has no textual history anchors for native display verification")
    return {"message_anchors": anchors, "user_messages": user_messages, "tools": tools}


def _display_validation(data, contract):
    visible = _ANSI.sub(b"", data).decode("utf-8", "replace")
    normalized = _normalized_text(visible)
    matched_anchors = sum(anchor in normalized for anchor in contract["message_anchors"])
    observed = re.findall(r"\b(Called|Failed)\s+([\w.]+)\s*·\s*(\d+)ms\b", visible)
    observed_counts = Counter((label, tool.rsplit(".", 1)[-1], int(duration))
                              for label, tool, duration in observed)
    expected_counts = Counter((label, tool.rsplit(".", 1)[-1], duration)
                              for label, tool, duration in contract["tools"] if duration is not None)
    observed_names = Counter((label, tool) for label, tool, _ in observed_counts.elements())
    expected_names = Counter((label, tool.rsplit(".", 1)[-1]) for label, tool, _ in contract["tools"])
    tools_complete = (not (expected_counts - observed_counts)
                      and not (expected_names - observed_names))
    complete = (bool(contract["message_anchors"])
                and matched_anchors == len(contract["message_anchors"])
                and tools_complete and "OpenAI Codex" in visible
                and "Ask Codex to do anything" in visible)
    return {"complete": complete, "expected_tools": len(contract["tools"]),
            "observed_tools": len(observed), "expected_message_anchors": len(contract["message_anchors"]),
            "matched_message_anchors": matched_anchors}


class NativeTerminalReplay:
    def __init__(self, cache_root, *, command_prefix=None, cli_version=None,
                 timeout=30.0, quiet_period=2.0):
        self.cache_root = Path(cache_root).absolute()
        # Reject a symlinked cache root, including pre-existing parent components.
        for path in (self.cache_root, *self.cache_root.parents):
            if path.is_symlink():
                raise NativeReplayError("native replay cache cannot traverse symlinks")
        self.cache_root = self.cache_root.resolve()
        self.cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _inside(self.cache_root, self.cache_root, directory=True)
        if command_prefix is None:
            try:
                from codex_cli_bin import bundled_codex_path
                command_prefix = (str(bundled_codex_path()),)
            except ImportError:
                command_prefix = (shutil.which("codex") or "codex",)
        self.command_prefix = tuple(str(value) for value in command_prefix)
        try:
            self.cli_version = cli_version or version("openai-codex-cli-bin")
        except PackageNotFoundError:
            self.cli_version = "unavailable"
        self.timeout, self.quiet_period = float(timeout), float(quiet_period)
        if not self.command_prefix or self.timeout <= 0 or self.quiet_period <= 0:
            raise ValueError("a CLI command and positive replay deadlines are required")

    def _source(self, evidence, thread_id):
        if not isinstance(thread_id, str) or str(uuid.UUID(thread_id)) != thread_id:
            raise NativeReplayError("a canonical native thread UUID is required")
        evidence = Path(evidence).absolute()
        if evidence.resolve(strict=True) != evidence:
            raise NativeReplayError("native replay evidence must not traverse symlinks or parent paths")
        _inside(evidence, evidence, directory=True)
        if self.cache_root.is_relative_to(evidence):
            raise NativeReplayError("native replay cache must be outside the evidence directory")
        home = _inside(evidence, evidence / "private" / "codex-home", directory=True)
        sessions = _inside(evidence, home / "sessions", directory=True)
        matches = list(sessions.glob(f"**/*-{thread_id}.jsonl"))
        if len(matches) != 1:
            raise NativeReplayError("archive must contain exactly one rollout for this thread")
        rollout = _inside(evidence, matches[0])
        files = [rollout]
        for name in ("state_5.sqlite", "thread_history_1.sqlite"):
            files.append(_inside(evidence, home / name))
            wal = home / (name + "-wal")
            if wal.exists() or wal.is_symlink():
                files.append(_inside(evidence, wal))
        original = evidence / "terminal.bin"
        if original.exists() or original.is_symlink():
            files.append(_inside(evidence, original))
        signatures = []
        for path in files:
            info = path.stat()
            signatures.append([str(path.relative_to(evidence)), info.st_size, info.st_mtime_ns,
                               info.st_ino, info.st_ctime_ns])
        identity = {"cache_version": CACHE_VERSION, "evidence": str(evidence),
                    "thread_id": thread_id, "cli_version": self.cli_version,
                    "cols": COLS, "rows": ROWS, "files": signatures}
        key = hashlib.sha256(_json(identity).encode()).hexdigest()
        return {"evidence": evidence, "home": home, "rollout": rollout,
                "files": files, "identity": identity, "key": key, "thread_id": thread_id}

    def available(self, evidence, thread_id):
        try:
            self._source(evidence, thread_id)
            return platform.system() == "Linux" and Path(self.command_prefix[0]).is_file()
        except (OSError, ValueError, NativeReplayError):
            return False

    def _peek(self, source):
        directory = self.cache_root / source["key"]
        try:
            _inside(self.cache_root, directory, directory=True)
            metadata = json.loads(_inside(self.cache_root, directory / "metadata.json").read_text())
            path = _inside(self.cache_root, directory / "terminal.bin")
            status = metadata["status"]
            if (metadata.get("complete") is not True or metadata.get("identity") != source["identity"]
                    or metadata.get("cache_version") != CACHE_VERSION
                    or status.get("cursor") != path.stat().st_size
                    or metadata.get("terminal_sha256") != _sha(path)):
                return None
            if not _display_validation(path.read_bytes(), metadata["display_contract"])["complete"]:
                return None
            return {"path": path, "status": status}
        except (OSError, ValueError, KeyError, TypeError, NativeReplayError):
            return None

    def peek(self, evidence, thread_id):
        """Inspect a completed cache only; never start Codex or touch the archive."""
        try:
            return self._peek(self._source(evidence, thread_id))
        except (OSError, ValueError, NativeReplayError):
            return None

    @contextmanager
    def _lock(self, source):
        identity = hashlib.sha256((str(source["evidence"]) + source["thread_id"]).encode()).hexdigest()
        fd = os.open(self.cache_root / (identity + ".lock"),
                     os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        deadline = time.monotonic() + self.timeout + 10
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise NativeReplayError("timed out waiting for this archive's native replay")
                    time.sleep(.05)
            yield
        finally:
            os.close(fd)

    def get(self, evidence, thread_id):
        source = self._source(evidence, thread_id)
        with self._lock(source):
            source = self._source(evidence, thread_id)
            cached = self._peek(source)
            if cached:
                return cached
            if not _CAPTURES.acquire(timeout=self.timeout):
                raise NativeReplayError("native replay capacity is busy")
            try:
                return self._render(source)
            finally:
                _CAPTURES.release()

    def _prepare(self, source, directory):
        home, workspace = directory / "codex-home", directory / "workspace"
        home.mkdir(); workspace.mkdir()
        for path in source["files"]:
            if not path.is_relative_to(source["home"]):
                continue
            destination = home / path.relative_to(source["home"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
        rollout = home / source["rollout"].relative_to(source["home"])
        source["compatibility"] = _archive_contract(home)
        with rollout.open() as stream:
            metadata = json.loads(stream.readline()).get("payload", {})
        if metadata.get("id", metadata.get("session_id")) != source["thread_id"]:
            raise NativeReplayError("rollout metadata does not match the requested native thread")
        with sqlite3.connect(home / "state_5.sqlite") as db:
            row = db.execute("SELECT model_provider, model, reasoning_effort FROM threads WHERE id=?",
                             (source["thread_id"],)).fetchone()
            if not row:
                raise NativeReplayError("native thread is missing from its archived state index")
            provider, model, effort = row
            provider = provider if provider not in ("openai", "ollama", "lmstudio") else "auriga_archive"
            if not provider or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", provider):
                raise NativeReplayError("unsupported archived provider identifier")
            db.execute("UPDATE threads SET rollout_path=?, cwd=?, model_provider=? WHERE id=?",
                       (str(rollout), str(workspace), provider, source["thread_id"]))
        model, effort = model or "gpt-6-astra", effort or "high"
        config = f'''model = {json.dumps(model)}
model_provider = {json.dumps(provider)}
model_reasoning_effort = {json.dumps(effort)}
approval_policy = "never"
sandbox_mode = "read-only"
web_search = "disabled"
check_for_update_on_startup = false
cli_auth_credentials_store = "file"
[features]
code_mode_host = true
shell_tool = false
unified_exec = false
browser_use = false
browser_use_external = false
computer_use = false
multi_agent = false
multi_agent_v2 = false
apps = false
plugins = false
artifact = false
image_generation = false
[features.code_mode]
enabled = true
[model_providers.{json.dumps(provider)}]
name = "Offline archive viewer"
base_url = "http://127.0.0.1:1"
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false
request_max_retries = 0
stream_max_retries = 0
[tui]
screen_reader_detection_done = true
[projects.{json.dumps(str(workspace))}]
trust_level = "trusted"
'''
        (home / "config.toml").write_text(config)
        return home, workspace

    def _render(self, source):
        directory = self.cache_root / source["key"]
        if directory.exists() or directory.is_symlink():
            _inside(self.cache_root, directory, directory=True)
            # Only this generated cache entry is replaced; evidence is never removed.
            shutil.rmtree(directory)
        directory.mkdir(mode=0o700)
        hashes = {str(path.relative_to(source["evidence"])): _sha(path) for path in source["files"]}
        try:
            home, workspace = self._prepare(source, directory)
            before = _history(home, source["thread_id"])
            if not before["thread_turns"]["count"]:
                raise NativeReplayError("native archive has no recorded turns")
            display_contract = _display_contract(home, source["thread_id"])
            exit_code, stopped, display_validation = self._capture(
                source, directory, home, workspace, display_contract)
            after = _history(home, source["thread_id"])
            if before != after:
                raise NativeReplayError("native replay unexpectedly changed archived turns or items")
            if (source["identity"] != self._source(source["evidence"], source["thread_id"])["identity"]
                    or hashes != {str(path.relative_to(source["evidence"])): _sha(path)
                                  for path in source["files"]}):
                raise NativeReplayError("archive changed while its native replay was being generated")
            path = directory / "terminal.bin"
            original = source["evidence"] / "terminal.bin"
            original_failed = original.exists() and b"Error: account/read failed during TUI bootstrap:" in original.read_bytes()
            status = {"session_id": "native-replay-" + source["key"][:24],
                      "thread_id": source["thread_id"], "cursor": path.stat().st_size,
                      "cols": COLS, "rows": ROWS, "mode": "read_only", "pid": None,
                      "exit_code": 0, "bootstrap_failed": False, "error": None,
                      "replay": {"kind": "recovered_native_tui",
                                 "original_recording": False, "derived_from": "archived_native_history",
                                 "compatibility": source["compatibility"],
                                 "recovered_at": datetime.now(timezone.utc).isoformat(),
                                 "cli_version": self.cli_version,
                                 "source_sha256": hashlib.sha256(_json(hashes).encode()).hexdigest(),
                                 "original_bootstrap_failed": bool(original_failed),
                                 "capture_exit_code": exit_code, "viewer_stopped_after_capture": stopped,
                                 "history_unchanged": True,
                                 "display_validation": display_validation,
                                 "recorded_turns": before["thread_turns"]["count"],
                                 "recorded_items": before["thread_items"]["count"]}}
            metadata = {"complete": True, "cache_version": CACHE_VERSION,
                        "identity": source["identity"], "source_files_sha256": hashes,
                        "terminal_sha256": _sha(path), "history": before, "status": status,
                        "display_contract": display_contract}
            pending = directory / "metadata.pending.json"
            pending.write_text(json.dumps(metadata, indent=2))
            pending.replace(directory / "metadata.json")
            return {"path": path, "status": status}
        except BaseException as exc:
            # Keep real failure bytes for diagnosis, but never publish an incomplete cache.
            (directory / "failure.json").write_text(json.dumps({"error": str(exc)}, indent=2))
            raise

    def _capture(self, source, directory, home, workspace, display_contract):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
        env = {key: value for key, value in os.environ.items()
               if not any(word in key.upper() for word in ("CODEX", "OPENAI", "ANTHROPIC", "API_KEY", "AUTH", "TOKEN"))}
        env.update(CODEX_HOME=str(home), TERM="xterm-256color", COLORTERM="truecolor",
                   PYTHONDONTWRITEBYTECODE="1")
        native = [*self.command_prefix, "resume", "--no-daemon", "--no-alt-screen",
                  "-C", str(workspace), "-s", "read-only", "-a", "never", source["thread_id"]]
        command = [sys.executable, "-B", str(Path(__file__).resolve()), "--sandbox-exec",
                   str(directory), str(os.getpid()), *native]
        process = None
        chunks, query_tail, captured = [], b"", 0
        stable = stopped = False
        started = last = time.monotonic()
        try:
            process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave,
                                       cwd=workspace, env=env, start_new_session=True)
            os.close(slave); slave = None
            with (directory / "terminal.bin").open("wb") as output:
                while time.monotonic() - started < self.timeout:
                    ready, _, _ = select.select([master], [], [], .1)
                    if ready:
                        try:
                            data = os.read(master, 65536)
                        except OSError as exc:
                            if exc.errno != errno.EIO:
                                raise
                            data = b""
                        if not data:
                            break
                        output.write(data); output.flush()
                        chunks.append(data)
                        captured += len(data)
                        if captured > 16 * 1024 * 1024:
                            raise NativeReplayError("native replay exceeded its display capture limit")
                        combined = query_tail + data
                        for _ in range(combined.count(b"\x1b[6n")):
                            os.write(master, b"\x1b[1;1R")
                        query_tail = combined[-3:]
                        # Ignore native empty redraw pulses when measuring display stability.
                        if _ANSI.sub(b"", data).strip():
                            last = time.monotonic()
                    all_bytes = b"".join(chunks)
                    validation = _display_validation(all_bytes, display_contract)
                    # The composer is visible during "Resuming session…" too.
                    # Require the actual archived messages and every tool result
                    # before a quiet interval can complete this capture.
                    if (validation["complete"]
                            and time.monotonic() - last >= self.quiet_period):
                        stable = True
                        break
                    if process.poll() is not None:
                        break
                if process.poll() is None:
                    stopped = True
                    _stop_process(process)
                while select.select([master], [], [], 0)[0]:
                    try:
                        data = os.read(master, 65536)
                    except OSError as exc:
                        if exc.errno != errno.EIO:
                            raise
                        break
                    if not data:
                        break
                    output.write(data); chunks.append(data)
            data = b"".join(chunks)
            validation = _display_validation(data, display_contract)
            if (not stable or not validation["complete"]
                    or b"Error: account/read failed during TUI bootstrap:" in data):
                raise NativeReplayError("native TUI did not finish rendering archived history before exit or deadline: "
                                        + _json(validation))
            return process.wait(timeout=2), stopped, validation
        finally:
            if process is not None:
                _stop_process(process)
            os.close(master)
            if slave is not None:
                os.close(slave)


def _stop_process(process):
    # Signal the whole group even if the leader already exited, so children cannot linger.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)
    finally:
        # A child may outlive a terminated group leader or ignore SIGTERM.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _isolate(directory, expected_parent):
    """Called only in a fresh helper process, before exec of the genuine CLI."""
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "aarch64"):
        raise NativeReplayError("native replay requires Linux seccomp and Landlock")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) or os.getppid() != expected_parent:
        raise NativeReplayError("native replay supervisor is no longer available")
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    abi = libc.syscall(444, 0, 0, 1)
    if abi < 3:
        raise NativeReplayError("native replay requires Landlock ABI 3 or newer")
    # Handle every filesystem write capability, including truncate and cross-directory rename.
    write_rights = sum(1 << bit for bit in (1, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14))
    ruleset_attr = ctypes.c_uint64(write_rights)
    ruleset = libc.syscall(444, ctypes.byref(ruleset_attr), ctypes.sizeof(ruleset_attr), 0)
    if ruleset < 0:
        raise NativeReplayError("could not create native replay filesystem restrictions")

    class PathRule(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]

    try:
        # Terminal and null device writes are transport, not archive modifications.
        for path, rights in ((directory, write_rights), ("/dev/tty", 1 << 1), ("/dev/null", 1 << 1)):
            fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = PathRule(rights, fd)
                if libc.syscall(445, ruleset, 1, ctypes.byref(rule), 0):
                    raise NativeReplayError("could not restrict native replay filesystem writes")
            finally:
                os.close(fd)
        if libc.prctl(38, 1, 0, 0, 0) or libc.syscall(446, ruleset, 0):
            raise NativeReplayError("could not apply native replay filesystem restrictions")
    finally:
        os.close(ruleset)
    library = ctypes.util.find_library("seccomp")
    if not library:
        raise NativeReplayError("native replay requires libseccomp")
    sec = ctypes.CDLL(library, use_errno=True)
    sec.seccomp_init.argtypes = [ctypes.c_uint32]; sec.seccomp_init.restype = ctypes.c_void_p
    sec.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    sec.seccomp_syscall_resolve_name.restype = ctypes.c_int

    class Comparison(ctypes.Structure):
        _fields_ = [("arg", ctypes.c_uint), ("op", ctypes.c_int),
                    ("datum_a", ctypes.c_uint64), ("datum_b", ctypes.c_uint64)]

    sec.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int,
                                         ctypes.c_uint, ctypes.POINTER(Comparison)]
    sec.seccomp_load.argtypes = [ctypes.c_void_p]
    sec.seccomp_release.argtypes = [ctypes.c_void_p]
    context = sec.seccomp_init(0x7fff0000)
    if not context:
        raise NativeReplayError("could not create native replay network restrictions")
    try:
        for domain in (2, 10):  # AF_INET and AF_INET6; local Unix sockets remain available.
            comparison = Comparison(0, 4, domain, 0)
            if sec.seccomp_rule_add_array(context, 0x00050000 | errno.EPERM,
                                         sec.seccomp_syscall_resolve_name(b"socket"), 1,
                                         ctypes.byref(comparison)):
                raise NativeReplayError("could not restrict native replay network access")
        if sec.seccomp_load(context):
            raise NativeReplayError("could not apply native replay network restrictions")
    finally:
        sec.seccomp_release(context)


if __name__ == "__main__":
    if len(sys.argv) < 5 or sys.argv[1] != "--sandbox-exec":
        raise SystemExit("native replay helper requires --sandbox-exec")
    _isolate(sys.argv[2], int(sys.argv[3]))
    os.execvpe(sys.argv[4], sys.argv[4:], os.environ)
