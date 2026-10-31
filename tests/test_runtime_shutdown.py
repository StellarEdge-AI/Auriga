"""CPU-only shutdown ownership, error isolation and native-close evidence."""
import asyncio
import io
import json
import os
import select
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import ANY, AsyncMock, Mock, patch

from auriga.cortex.session.codex import CodexSession
from auriga.robot.backends.robodojo.environment import launch_application
from auriga.apps.gateway.workbench import Workbench
from auriga.runtime.supervision import start_with_shutdown, wait_turn
from auriga.robot.backends.robodojo.robodojo import RobotBackend, WIRE_PREFIX
from auriga.robot.backends.robodojo.robot_worker import RoboDojoAdapter
from auriga.robot.backends.robodojo.robot_process import RobotProcess


class ShutdownCheck(unittest.IsolatedAsyncioTestCase):
    async def test_startup_deadline_reaps_bridge_created_after_stop_before_returning(self):
        processes, events, actions, emergency = [], [], [], []
        owner = SimpleNamespace(accepting=True, robot=SimpleNamespace(process=None),
            session=SimpleNamespace(process=None, client=None), terminal=None,
            store=SimpleNamespace(event=lambda kind, payload: events.append((kind, payload))))
        owner.abort_startup = Workbench.abort_startup.__get__(owner)
        def initialize():
            time.sleep(.08)  # The resource appears after the 20 ms startup deadline.
            process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            processes.append(process)
            owner.session.client = SimpleNamespace(_proc=process)
            try:
                process.stdout.readline()  # Stand in for an SDK RPC with no local timeout.
                process.wait(timeout=2)
            finally:
                process.stdin.close()
                process.stdout.close()
        async def start(**kwargs):
            await asyncio.to_thread(initialize)
            if owner.accepting:
                actions.append('actor_started')
        async def stop(reason): owner.accepting = False
        owner.start, owner.stop = start, stop
        def release_fixture():
            emergency.append(True)
            for process in processes:
                if process.poll() is None: process.kill()
        fallback = threading.Timer(2, release_fixture)
        fallback.start()
        sleep = asyncio.sleep
        async def shorter_deadline(delay):
            await sleep(.02 if delay == 900 else delay)
        try:
            with patch('auriga.runtime.supervision.asyncio.sleep', side_effect=shorter_deadline):
                with self.assertRaisesRegex(asyncio.TimeoutError, 'official_startup_timeout'):
                    await start_with_shutdown(owner, asyncio.Event())
        finally:
            fallback.cancel()
            fallback.join()
        self.assertFalse(emergency, 'startup needed external cleanup after its deadline')
        self.assertEqual(actions, [])
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())
        self.assertEqual([payload['signal'] for kind, payload in events if kind == 'run.startup_process_signalled'],
                         ['SIGTERM'])
        self.assertTrue(next(payload for kind, payload in events if kind == 'run.startup_process_exit')['process_exit_confirmed'])

    async def test_startup_abort_escalates_when_owned_worker_ignores_sigterm(self):
        process = subprocess.Popen([sys.executable, '-u', '-c',
            'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print("ready"); time.sleep(30)'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        events = []
        owner = SimpleNamespace(robot=SimpleNamespace(process=process),
            session=SimpleNamespace(process=None, client=None), terminal=None,
            store=SimpleNamespace(event=lambda kind, payload: events.append((kind, payload))))
        starting = asyncio.create_task(asyncio.to_thread(process.wait))
        try:
            self.assertEqual(await asyncio.to_thread(process.stdout.readline), 'ready\n')
            started = time.monotonic()
            await asyncio.wait_for(Workbench.abort_startup(owner, starting), timeout=8)
            self.assertGreaterEqual(time.monotonic() - started, 5)
            self.assertEqual(process.returncode, -9)
            self.assertEqual(owner.robot.startup_forced_termination, 'SIGKILL')
            self.assertEqual([payload['signal'] for kind, payload in events if kind == 'run.startup_process_signalled'],
                             ['SIGTERM', 'SIGKILL'])
            self.assertTrue(next(payload for kind, payload in events if kind == 'run.startup_process_exit')['process_exit_confirmed'])
        finally:
            if process.poll() is None:
                process.kill()
            await starting
            process.stdout.close()

    async def test_cleanup_failure_cannot_skip_worker_or_other_resources(self):
        for broken in ("terminal", "policy", "session"):
            with self.subTest(broken=broken):
                calls = []
                def close(name):
                    calls.append(name)
                    if name == broken:
                        raise RuntimeError("synthetic " + name + " close failure")
                    if name == "robot":
                        return dict(native_close_acknowledged=True, process_exit_confirmed=True)
                async def terminal_close(): close("terminal")
                owner = Workbench.__new__(Workbench)
                owner.closed = False
                owner.harness = SimpleNamespace(request_stop=Mock(), active_action=None,
                    programs=None, action_policy=SimpleNamespace(close=lambda: close("policy")))
                owner.robot = SimpleNamespace(close=lambda: close("robot"))
                owner.session = SimpleNamespace(state="completed", close=lambda: close("session"))
                owner.terminal = SimpleNamespace(close=terminal_close)
                owner.scene_service = owner.scene_forwarder = None
                owner.store = SimpleNamespace(close=lambda: close("store"))
                owner.run_lock = SimpleNamespace(close=lambda: close("run_lock"))
                result = await owner.close()
                self.assertEqual(calls, ["robot", "terminal", "policy", "session", "store", "run_lock"])
                self.assertTrue(result["robot"]["native_close_acknowledged"])
                self.assertEqual(result["errors"][0]["step"], broken)
                self.assertIs(await owner.close(), result)

    async def test_startup_shutdown_and_cancellation_keep_initialization_owned(self):
        for outer_cancel in (False, True):
            with self.subTest(outer_cancel=outer_cancel):
                entered, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
                owner = SimpleNamespace(accepting=True)
                async def start(**kwargs):
                    entered.set()
                    await release.wait()
                    self.assertFalse(owner.accepting)
                    closed.set()
                async def stop(reason): owner.accepting = False
                owner.start, owner.stop = start, stop
                shutdown = asyncio.Event()
                task = asyncio.create_task(start_with_shutdown(owner, shutdown, prompt="fixture"))
                await entered.wait()
                if outer_cancel: task.cancel()
                else: shutdown.set()
                for _ in range(8): await asyncio.sleep(0)
                self.assertFalse(owner.accepting)
                self.assertFalse(task.done())
                release.set()
                if outer_cancel:
                    with self.assertRaises(asyncio.CancelledError): await task
                else: await task
                self.assertTrue(closed.is_set())

    async def test_disconnect_stops_admission_and_wakes_supervisor_immediately(self):
        session = CodexSession.__new__(CodexSession)
        session.closing = False
        session.store = SimpleNamespace(event=Mock())
        session.expire_requests = Mock()
        session.on_disconnect = Mock()
        session.turn_done = threading.Event()
        session.model_proxy = None
        session._disconnected(RuntimeError("synthetic broken bridge"))
        session.on_disconnect.assert_called_once()
        self.assertEqual(session.turn_result["status"], "failed")
        self.assertTrue(await asyncio.wait_for(wait_turn(session, asyncio.Event()), .2))
        session.turn_result = {"status": "completed"}
        session._disconnected(RuntimeError("disconnect after completed turn"))
        self.assertEqual(session.turn_result["status"], "completed")


class NativeCloseEvidenceCheck(unittest.TestCase):
    def test_startup_forced_signal_is_retained_in_native_close_evidence(self):
        backend = self.backend()
        backend.process.returncode = -15
        backend.startup_forced_termination = 'SIGTERM'
        result = backend.close()
        backend._request.assert_not_called()
        self.assertFalse(result['native_close_acknowledged'])
        self.assertEqual(result['forced_termination'], 'SIGTERM')
        self.assertEqual(result['exit_mode'], 'after_forced_signal')

    def test_exited_worker_with_blocked_reader_is_not_released_or_replaced(self):
        backend = self.backend()
        backend.reader = Mock(is_alive=Mock(return_value=True))
        result = backend.close()
        self.assertTrue(result['process_exit_confirmed'])
        self.assertFalse(result['reader_exit_confirmed'])
        self.assertIn('robot_reader_stop_unconfirmed', result['errors'])
        backend.process.stdout.close.assert_not_called()
        backend.log.close.assert_not_called()
        backend.operation_lock = threading.Lock()
        backend.validate_reset = Mock()
        backend._request.return_value = {'robot_state': {}}
        backend.start = Mock()
        with self.assertRaisesRegex(RuntimeError, 'reset_requires_previous_worker_exit'):
            backend.reset()
        backend.start.assert_not_called()
        backend.reader.is_alive.return_value = False
        self.assertTrue(backend.close()['reader_exit_confirmed'])
        backend.log.close.assert_called_once()

    def test_failed_process_launch_closes_its_log_and_can_be_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = RobotProcess(directory)
            with patch('auriga.robot.backends.robodojo.robot_process.subprocess.Popen', side_effect=OSError('synthetic spawn failure')):
                with self.assertRaisesRegex(OSError, 'synthetic spawn failure'):
                    backend.start_process(['unused'], cwd=directory, environment={})
            self.assertTrue(backend.log.closed)
            self.assertIsNone(backend.process)

    def test_old_reader_cannot_fail_or_deliver_into_replacement_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = RobotProcess(directory)
            backend.process = Mock()
            backend._process_generation = 2
            old = Mock(stdout=[WIRE_PREFIX + json.dumps({'id': 'pending', 'progress': {'phase': 'started'}})])
            callback = Mock()
            backend.pending['pending'] = (threading.Event(), {}, callback)
            with patch.object(backend, '_check_native_failure') as inspect, patch.object(backend, '_fail') as fail:
                backend._read(old, Mock(), 1)
            callback.assert_not_called()
            inspect.assert_not_called()
            fail.assert_not_called()

    def test_close_retains_blocked_callback_resources_until_reader_acknowledges_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = RobotProcess(directory)
            entered, release = threading.Event(), threading.Event()
            def progress(_):
                entered.set()
                release.wait(2)
            backend.pending['pending'] = (threading.Event(), {}, progress)
            backend.log = io.StringIO()
            backend.process = Mock(pid=123, returncode=0)
            backend.process.poll.return_value = 0
            backend.process.stdout = io.StringIO(WIRE_PREFIX + json.dumps(
                {'id': 'pending', 'progress': {'phase': 'executing'}}) + '\n')
            backend.reader = threading.Thread(target=backend._read)
            backend.reader.start()
            try:
                self.assertTrue(entered.wait(1))
                with patch.object(backend.reader, 'join'):
                    report = backend.close()
                self.assertFalse(report['reader_exit_confirmed'])
                self.assertFalse(backend.log.closed)
                self.assertFalse(backend.process.stdout.closed)
            finally:
                release.set()
                backend.reader.join(2)
                report = backend.close()
            self.assertTrue(report['reader_exit_confirmed'])
            self.assertTrue(backend.log.closed)

    def test_worker_disables_native_fast_process_exit(self):
        launcher, render_sync = Mock(), Mock()
        with patch.dict(sys.modules, {
            "isaaclab.app": SimpleNamespace(AppLauncher=launcher),
            "env.camera_manager.capture.render_sync": SimpleNamespace(add_zero_delay_kit_args=render_sync),
        }):
            app = launch_application(3)
        launcher.assert_called_once()
        arguments = launcher.call_args.args[0]
        self.assertIs(arguments.fast_shutdown, False)
        self.assertTrue(arguments.headless)
        self.assertTrue(arguments.enable_cameras)
        self.assertEqual(arguments.device, "cuda:0")
        self.assertEqual(arguments.device_id, 3)
        render_sync.assert_called_once_with(arguments)
        self.assertIs(app, launcher.return_value.app)

    def test_stage_evidence_precedes_only_what_has_actually_completed(self):
        owner = RoboDojoAdapter.__new__(RoboDojoAdapter)
        stages = []
        owner.env = SimpleNamespace(close=lambda: stages.append("env_returned"))
        owner.app = SimpleNamespace(config={"fast_shutdown": False}, close=lambda: stages.append("app_returned"))
        owner.close(lambda value: stages.append(value["phase"]))
        self.assertEqual(stages, ["env_returned", "environment_closed", "application_close_started",
                                  "app_returned", "application_closed"])

    def test_progress_pipe_failure_still_closes_the_native_application(self):
        owner = RoboDojoAdapter.__new__(RoboDojoAdapter)
        owner.env = SimpleNamespace(close=Mock())
        owner.app = SimpleNamespace(config={"fast_shutdown": False}, close=Mock())
        def broken_progress(value):
            if value["phase"] == "application_close_started":
                raise BrokenPipeError("synthetic IPC loss")
        with self.assertRaises(BrokenPipeError): owner.close(broken_progress)
        owner.app.close.assert_called_once()

    def test_worker_identity_is_captured_at_start_and_never_resampled_at_close(self):
        for unreadable in (False, True):
            with self.subTest(unreadable=unreadable), tempfile.TemporaryDirectory() as directory:
                backend = RobotBackend(run_dir=directory)
                process = Mock(pid=123, returncode=0)
                process.poll.return_value = 0
                fields = ["S"] + ["0"] * 18 + ["6789"]
                with patch("auriga.robot.backends.robodojo.robot_process.subprocess.Popen", return_value=process), \
                        patch("auriga.robot.backends.robodojo.robot_process.threading.Thread", return_value=Mock(is_alive=Mock(return_value=False))), \
                        patch.object(backend, "_request", return_value={}), \
                        patch.object(Path, "read_text", return_value="123 (worker (child)) " + " ".join(fields),
                            side_effect=FileNotFoundError() if unreadable else None) as read_stat:
                    backend.start()
                    report = backend.close()
                    self.assertEqual(report["start_ticks"], None if unreadable else 6789)
                    read_stat.assert_called_once()

    def test_application_close_runs_when_environment_close_raises(self):
        owner = RoboDojoAdapter.__new__(RoboDojoAdapter)
        owner.env = SimpleNamespace(close=Mock(side_effect=RuntimeError("synthetic env failure")))
        owner.app = SimpleNamespace(close=Mock())
        with self.assertRaisesRegex(RuntimeError, "synthetic env failure"):
            owner.close()
        owner.app.close.assert_called_once()

    def backend(self):
        backend = RobotBackend.__new__(RobotBackend)
        process = Mock(pid=123, returncode=None)
        process.poll.side_effect = lambda: process.returncode
        def wait(timeout): process.returncode = 0
        process.wait.side_effect = wait
        backend.process, backend.failure, backend.reader = process, None, None
        backend.log = Mock()
        backend._request = Mock(return_value={"closed": True})
        return backend

    def test_native_ack_and_forced_exit_are_distinct_and_idempotent(self):
        for failure in (None, TimeoutError("synthetic native close timeout")):
            with self.subTest(failure=failure):
                backend = self.backend()
                if failure:
                    backend._request.side_effect = failure
                    def wait(timeout):
                        if timeout == 2: raise subprocess.TimeoutExpired("still alive", 2)
                        backend.process.returncode = 0
                    backend.process.wait.side_effect = wait
                result = backend.close()
                self.assertIsNone(result["start_ticks"])
                self.assertEqual(result["native_close_acknowledged"], failure is None)
                self.assertEqual(result["forced_termination"], "SIGTERM" if failure else None)
                self.assertTrue(result["process_exit_confirmed"])
                self.assertIs(backend.close(), result)
                backend._request.assert_called_once_with("close", timeout=180, progress=ANY)

    def test_eof_during_native_fast_exit_does_not_invent_ack_or_send_signal(self):
        backend = self.backend()
        def closing(operation, *, timeout, progress):
            progress({"phase": "environment_closed"})
            progress({"phase": "application_close_started", "fast_shutdown": True})
            raise RuntimeError("worker disconnected before final ACK")
        backend._request.side_effect = closing
        result = backend.close()
        self.assertTrue(result["environment_close_completed"])
        self.assertTrue(result["application_close_started"])
        self.assertFalse(result["application_close_completed"])
        self.assertFalse(result["native_close_acknowledged"])
        self.assertTrue(result["process_exit_confirmed"])
        self.assertEqual(result["exit_mode"], "self_exit_without_close_ack")
        self.assertIsNone(result["forced_termination"])
        backend.process.terminate.assert_not_called()
        backend.process.kill.assert_not_called()

    def test_unconfirmed_kill_is_not_reported_as_released(self):
        backend = self.backend()
        backend._request.side_effect = TimeoutError("native close timeout")
        backend.process.wait.side_effect = subprocess.TimeoutExpired("fixture", 10)
        result = backend.close()
        self.assertFalse(result["process_exit_confirmed"])
        self.assertFalse(result["native_close_acknowledged"])
        self.assertEqual(result["forced_termination"], "SIGKILL")
        backend.process.stdin.close.assert_not_called()


class WorkerCloseProcessCheck(unittest.TestCase):
    def test_native_close_ack_precedes_direct_exit_and_failure_allows_retry(self):
        source = Path(__file__).resolve().parents[1]
        for fail_first in (False, True):
            with self.subTest(fail_first=fail_first), tempfile.TemporaryDirectory() as directory:
                events = Path(directory) / "events"
                script = textwrap.dedent("""\
                    import atexit, os, sys
                    from pathlib import Path
                    from auriga.robot.backends.robodojo import robot_worker
                    events = Path(sys.argv[1])
                    fail_first = sys.argv[2] == 'True'
                    def record(event):
                        with events.open('a') as stream:
                            stream.write(event + '\\n')
                    def unsafe_python_teardown():
                        record('python_teardown')
                        os._exit(91)
                    atexit.register(unsafe_python_teardown)
                    class Adapter:
                        def __init__(self, arguments): self.calls = 0
                        def close(self, progress):
                            self.calls += 1
                            record('close_called')
                            if fail_first and self.calls == 1:
                                record('close_failed')
                                raise RuntimeError('synthetic native close failure')
                            record('native_close_returned')
                            sys.stderr.write('native close returned\\n')
                    robot_worker.RoboDojoAdapter = Adapter
                    sys.argv = ['worker', '--robodojo-root', 'unused', '--gpu', '0', '--run-dir', str(events.parent)]
                    robot_worker.main()
                    record('worker_main_returned')
                    """)
                environment = dict(os.environ, PYTHONPATH=os.pathsep.join((str(source), os.environ.get("PYTHONPATH", ""))))
                process = subprocess.Popen([sys.executable, "-c", script, str(events), str(fail_first)],
                    cwd=source, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True)
                try:
                    process.stdin.write(json.dumps({"id": "first", "op": "close"}) + "\n")
                    process.stdin.flush()
                    if fail_first:
                        self.assertTrue(select.select([process.stdout], [], [], 5)[0], "missing close failure response")
                        failure = json.loads(process.stdout.readline().removeprefix(WIRE_PREFIX))
                        self.assertEqual(failure["id"], "first")
                        self.assertIn("synthetic native close failure", failure["error"])
                        self.assertNotIn("result", failure)
                        self.assertIsNone(process.poll(), "close failure must not exit successfully")
                        self.assertEqual(events.read_text().splitlines(), ["close_called", "close_failed"])
                        process.stdin.write(json.dumps({"id": "retry", "op": "close"}) + "\n")
                        process.stdin.flush()
                    # Keep stdin open until exit: EOF has its own worker-loss path.
                    self.assertEqual(process.wait(timeout=5), 0)
                    replies = [json.loads(line.removeprefix(WIRE_PREFIX)) for line in process.stdout.read().splitlines()]
                    self.assertEqual(replies, [{"id": "retry" if fail_first else "first", "result": {"closed": True}}])
                    expected = ["close_called", "close_failed"] if fail_first else []
                    self.assertEqual(events.read_text().splitlines(), expected + ["close_called", "native_close_returned"])
                    self.assertIn("native close returned", process.stderr.read())
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
                    for stream in (process.stdin, process.stdout, process.stderr):
                        stream.close()


if __name__ == "__main__":
    unittest.main()
