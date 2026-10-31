"""Exercise the real serve/final-summary path without a model or robot."""
import asyncio
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web

from auriga.apps.gateway.server import serve
from auriga.core.storage.store import Store


class NativeFailureSummaryTest(unittest.IsolatedAsyncioTestCase):
    async def test_native_failure_is_distinct_from_stop_and_keeps_unknown_motion(self):
        original = '{"error":{"message":"Failed to read request body","type":"invalid_request_error"}}'
        for native_status, unknown, message, limit in [
            ("failed", False, original, False), ("failed", True, original, False),
            ("failed", False, "x" * 5000, False), ("completed", False, None, False),
            ("interrupted", False, None, False), ("failed", False, "model request limit", True),
        ]:
            with self.subTest(status=native_status, unknown=unknown, length=len(message or "")), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                identity = dict(run_id="fixture", episode_id="episode", env_instance_id="env", env_generation=1)
                store = Store(root, identity)
                terminal = threading.Event()
                terminal.set()
                session = SimpleNamespace(
                    thread_id="fixture-thread", state=native_status, turn_done=terminal,
                    turn_result={"status": native_status, "error": {"message": message} if message else None},
                    model="fixture", provider="fixture", price_card=None,
                    model_proxy=SimpleNamespace(limit_notified=limit),
                )
                native_terminal = ({"ended": True, "success": False, "reason": "native_step_limit"}
                                   if native_status == "completed" else None)
                episode_done = threading.Event()
                if native_terminal:
                    episode_done.set()
                    store.set_state("runtime", "native_episode", native_terminal)

                def evaluate():
                    evaluation = {**identity, "result": {"task_success": False, "normalized_score": 0, "valid_episode": True}}
                    ref = store.put_artifact(json.dumps(evaluation).encode(), "evaluation", "application/json", "observer")
                    store.event("observer.evaluated", {"result_ref": ref})
                    return evaluation

                cleanup = {"steps": {"robot": "completed"}, "errors": [],
                    "robot": {"native_close_acknowledged": False,
                              "forced_termination": None if native_status == "interrupted" else "SIGTERM",
                              "process_exit_confirmed": True, "errors": ["synthetic native close timeout"]}}
                if native_status == "completed":
                    # A close ACK cannot conceal a later abnormal worker exit.
                    cleanup["robot"].update(native_close_acknowledged=True, forced_termination=None,
                                            returncode=-11, errors=[])
                def close():
                    store.close()
                    return cleanup

                workbench = SimpleNamespace(
                    store=store, identity=identity, session=session, status="running",
                    harness=SimpleNamespace(action_count=0, active_action=None, episode_done=episode_done,
                        episode_terminal=native_terminal, native_action_count=0, started_at=time.monotonic()),
                    app=web.Application, snapshot=lambda: {"status": "running"},
                    start=AsyncMock(), stop=AsyncMock(), close=AsyncMock(side_effect=close),
                    current_unknown_actions=lambda: unknown, control_lock=asyncio.Lock(),
                    has_active_programs=lambda: False,
                    observer_evaluate=Mock(side_effect=evaluate), observer_video=Mock(return_value={}),
                )
                args = SimpleNamespace(
                    run_dir=str(root), resume=False, replay=False, price_card=None,
                    category="diagnostic", task="fixture", title="fixture", tag=[],
                    gpu=0, max_actions=1, observation_profile="rgb_proprio_v1", seed=0, layout_id=0,
                    port=0, prompt="fixture", task_notes=None, task_notes_source=None,
                    wall_budget_s=1, exit_after_turn=True, benchmark=True,
                )
                with patch("auriga.apps.gateway.server.Workbench", return_value=workbench), redirect_stdout(io.StringIO()):
                    await serve(args)
                result = json.loads((root / "summary.json").read_text())
                expected = "unknown" if unknown else "stopped" if native_status == "interrupted" or limit else native_status
                self.assertEqual(result["status"], expected)
                self.assertEqual(result["codex_turn_status"], native_status)
                self.assertEqual(result["cleanup"], cleanup)
                expected_cleanup = ("worker_exit_failed" if native_status == "completed" else
                                    "native_close_unconfirmed" if native_status == "interrupted" else "forced_termination")
                self.assertEqual(result["cleanup_status"], expected_cleanup)
                self.assertEqual(result["cleanup"]["robot"]["native_close_acknowledged"], native_status == "completed")
                if message and not limit:
                    self.assertEqual(result["infrastructure_error"], "codex_turn_failed: " + message[:4096])
                else:
                    self.assertIsNone(result["infrastructure_error"])
                self.assertEqual(workbench.observer_evaluate.call_count, 0 if unknown else 1)
                self.assertEqual(result.get("valid_episode"), None if unknown else True)
                workbench.start.assert_awaited_once()
                if limit:
                    workbench.stop.assert_awaited_once()
                    # A budget stop is scoreable only after confirmed cleanup;
                    # this fixture required SIGTERM without a native close ACK.
                    self.assertEqual(result["primary_outcome"], "unscored_infrastructure")
                    self.assertFalse(result["scored_episode"])
                    self.assertIsNone(result["task_success"])
                    self.assertIsNone(result["normalized_score"])
                    self.assertEqual(result["native_evaluation_snapshot"]["normalized_score"], 0)
                    self.assertTrue(result["model_budget_exhausted"])
                elif native_terminal:
                    workbench.stop.assert_awaited_once()
                    self.assertEqual(result["termination_authority"], "robodojo_environment")
                else:
                    workbench.stop.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
