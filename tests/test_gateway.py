"""Synthetic startup/stop lifecycle fixtures; no RoboDojo, Codex, or GPU."""

import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from aiohttp import WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer

from tool_fixture import install_registry

from auriga.apps.gateway.workbench import Workbench
from auriga.core.config import Config
from auriga.cortex.session.terminal import Terminal


class GatewayCheck(unittest.IsolatedAsyncioTestCase):
    async def test_interrupt_ack_waits_for_native_turn_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            workbench = Workbench(Path(temporary) / "synthetic-stop", robot=SimpleNamespace(close=lambda: None))
            workbench.starting = False
            session = SimpleNamespace(state="running", interrupt=lambda: None, close=lambda: None)
            workbench.session = session
            completion = asyncio.get_running_loop().call_later(.1, setattr, session, "state", "interrupted")
            try:
                result = await workbench.stop("budget")
                self.assertEqual(session.state, "interrupted")
                self.assertEqual(result["status"], "stopped")
            finally:
                completion.cancel()
                await workbench.close()

    async def test_http_turn_ownership_and_terminal_boundaries(self):
        with tempfile.TemporaryDirectory() as temporary:
            workbench = Workbench(Path(temporary) / "synthetic-http", robot=SimpleNamespace(close=lambda: None))
            workbench.starting, workbench.status = False, "running"
            turns = []
            session = SimpleNamespace(thread_id="fixture-thread", turn_id=None, state="completed")

            def run_turn(prompt):
                turns.append(prompt)
                session.turn_id, session.state = "fixture-turn", "running"
                return session.turn_id

            session.run_turn = run_turn
            session.interrupt = lambda: setattr(session, "state", "interrupted")
            session.close = lambda: setattr(session, "state", "closed")
            workbench.session = session
            # A real Terminal object with no start() call: no Codex/PTY process.
            workbench.terminal = Terminal(workbench.root, workbench.root)
            client = TestClient(TestServer(workbench.app()))
            connection = None
            try:
                await client.start_server()
                request = dict(command="prompt", request_id="fixture-prompt", prompt="synthetic fixture task")
                mismatch = await client.post("/v1/control", json=request, headers={"X-Auriga-Run-ID": "another-run"})
                self.assertEqual(mismatch.status, 404)
                self.assertEqual(turns, [])
                first = await client.post("/v1/control", json=request)
                self.assertEqual(first.status, 200)
                original = await first.json()
                duplicate = await client.post("/v1/control", json=request)
                self.assertEqual(duplicate.status, 200)
                self.assertEqual(await duplicate.json(), original)
                self.assertEqual(turns, [request["prompt"]])
                changed = await client.post("/v1/control", json=dict(request, prompt="changed task"))
                self.assertEqual(changed.status, 409)
                self.assertEqual((await client.get("/v1/runs/another-run")).status, 404)
                cross_origin = await client.post("/v1/control", json=dict(request, request_id="foreign"),
                                                 headers={"Origin": "http://foreign.invalid"})
                self.assertEqual(cross_origin.status, 403)
                with self.assertRaises(WSServerHandshakeError) as rejected:
                    await client.ws_connect("/v1/terminal", origin="http://foreign.invalid")
                self.assertEqual(rejected.exception.status, 403)
                connection = await client.ws_connect("/v1/terminal")
                self.assertEqual((await connection.receive_json(timeout=2))["type"], "ready")
                await connection.send_json(dict(type="input", data="must not reach the native terminal"))
                reply = await connection.receive_json(timeout=2)
                self.assertEqual(reply, dict(type="error", message="terminal_input_is_read_only"))
                self.assertEqual(turns, [request["prompt"]])
                self.assertIsNone(workbench.terminal.process)
            finally:
                if connection is not None:
                    await connection.close()
                await client.close()
                await workbench.close()

    async def test_stop_at_startup_boundaries_and_cleanup(self):
        for phase in ("robot", "session", "turn", "terminal", "success", "unsandboxed"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temporary:
                calls = []
                entered, release = threading.Event(), threading.Event()
                closing, close_release = threading.Event(), threading.Event()

                def gate(stage):
                    calls.append(stage)
                    if stage == phase:
                        entered.set()
                        if not release.wait(5):
                            raise TimeoutError("synthetic startup gate not released")

                def close_robot():
                    calls.append("robot.close")
                    closing.set()
                    if not close_release.wait(5):
                        raise TimeoutError("synthetic cleanup gate not released")

                sandbox_enabled = phase != "unsandboxed"
                workbench = Workbench(Path(temporary) / "synthetic-run", robot=SimpleNamespace(close=close_robot),
                                      config=Config.from_dict({'features': {'sandbox_enabled': sandbox_enabled}}),
                                      visual_review_model=None)
                harness = SimpleNamespace(accepting=True, active_action=None, states={}, action_count=0,
                                          capabilities=dict(data_origin="synthetic_fixture"), thread_id=None,
                                          scene=SimpleNamespace(capabilities=lambda: dict(physics="unsupported")))
                harness.start = lambda: gate("robot")
                harness.request_stop = lambda: setattr(harness, "accepting", False)
                session = SimpleNamespace(thread_id=None, turn_id=None, state="not_started", actor_dir=workbench.root,
                                          model="fixture-model", provider="fixture-provider", effort="high", price_card=None,
                                          cli_executable=Path("/fixture/native/codex"),
                                          command_prefix=["fixture-sandbox"] if sandbox_enabled else [],
                                          socket_path=workbench.root / "fixture.sock", env={}, process=SimpleNamespace(pid=-1))

                def start_session(*arguments):
                    gate("session")
                    session.thread_id, session.state = "fixture-thread", "idle"
                    return session.thread_id

                def run_turn(prompt):
                    session.state = "running"
                    gate("turn")
                    session.turn_id = "fixture-turn"
                    return session.turn_id

                def close_session():
                    calls.append("session.close")
                    session.state = "closed"

                session.start, session.run_turn, session.close = start_session, run_turn, close_session
                session.interrupt = lambda: setattr(session, "state", "interrupted")
                install_registry(harness, workbench.config)
                workbench.harness, workbench.session = harness, session

                class FixtureTerminal:
                    def __init__(self, *arguments, **keywords):
                        self.session_id, self.process = "fixture-pty", SimpleNamespace(pid=-1)
                        self.command_prefix = keywords["command_prefix"]

                    async def start(self, *arguments):
                        await asyncio.to_thread(gate, "terminal")

                    async def close(self):
                        calls.append("terminal.close")

                    def status(self):
                        return dict(mode="read_only", session_id=self.session_id)

                startup = None
                try:
                    with patch("auriga.apps.gateway.workbench.Terminal", FixtureTerminal):
                        startup = asyncio.create_task(workbench.start("synthetic fixture prompt", wall_budget_s=1200))
                        if phase in ("success", "unsandboxed"):
                            await asyncio.wait_for(startup, 3)
                            self.assertEqual(workbench.status, "running")
                            self.assertFalse(workbench.starting)
                            self.assertTrue((workbench.root / "manifest.json").exists())
                            manifest = json.loads((workbench.root / "manifest.json").read_text())
                            self.assertEqual(manifest["actor_wait_budget_s"], 1200)
                            self.assertEqual(manifest["codex_cli_executable"], str(session.cli_executable))
                            expected_prefix = session.command_prefix + [str(session.cli_executable)]
                            self.assertEqual(workbench.terminal.command_prefix[:len(expected_prefix)], expected_prefix)
                            self.assertEqual(manifest['effective_config']['features']['sandbox_enabled'], sandbox_enabled)
                            with self.assertRaisesRegex(ValueError, "artifact_access_scope_denied"):
                                workbench.store.resolve_artifact(manifest["source_ref"])
                            _, content = workbench.store.resolve_artifact(manifest["source_ref"], allowed_scopes=("observer",))
                            sources = json.loads(content)
                            self.assertEqual({name: hashlib.sha256(source.encode()).hexdigest() for name, source in sources.items()}, manifest["source_sha256"])
                            self.assertEqual(calls, ["robot", "session", "turn", "terminal"])
                            continue
                        self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                        if phase == "robot":
                            client = TestClient(TestServer(workbench.app()))
                            await client.start_server()
                            try:
                                response = await client.post("/v1/control", json=dict(command="prompt", request_id="early", prompt="too early"))
                                self.assertEqual(response.status, 400)
                                self.assertEqual((await response.json())["error"], "run_not_ready_for_new_task")
                            finally:
                                await client.close()
                        first = await workbench.stop("stop-1")
                        self.assertEqual(first["status"], "stop_requested")
                        self.assertEqual(await workbench.stop("stop-1"), first)
                        self.assertEqual((await workbench.stop("stop-2"))["status"], "stop_requested")
                        self.assertTrue(workbench.starting)
                        self.assertFalse(startup.done())
                        release.set()
                        self.assertTrue(await asyncio.to_thread(closing.wait, 3))
                        self.assertTrue(workbench.starting)
                        self.assertEqual((await workbench.stop("during-cleanup"))["status"], "stop_requested")
                        close_release.set()
                        await asyncio.wait_for(startup, 3)
                        self.assertFalse(workbench.starting)
                        self.assertEqual(workbench.status, "stopped")
                        self.assertFalse((workbench.root / "manifest.json").exists())
                        self.assertFalse(any(event["type"] == "run.started" for event in workbench.store.events()))
                        phases = [stage for stage in calls if stage in ("robot", "session", "turn", "terminal")]
                        self.assertEqual(phases, ["robot", "session", "turn", "terminal"][:["robot", "session", "turn", "terminal"].index(phase) + 1])
                        self.assertEqual(calls.count("robot.close"), 1)
                        self.assertEqual(calls.count("session.close"), 1)
                        self.assertEqual(calls.count("terminal.close"), int(phase == "terminal"))
                finally:
                    release.set()
                    close_release.set()
                    if startup is not None and not startup.done():
                        await asyncio.wait_for(startup, 3)
                    await workbench.close()


if __name__ == "__main__":
    unittest.main()
