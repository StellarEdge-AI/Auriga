import json
import asyncio
import threading
import io
from types import SimpleNamespace
from PIL import Image
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from auriga.core.storage.store import Store
from auriga.runtime.harness import Harness
from auriga.core.contracts import digest
from auriga.perception.scene.store import SceneStore
from auriga.runtime.supervision import wait_turn
from auriga.apps.evaluation.episode import observer_summary
from auriga.apps.library.scoring import classify_episode_result
from geometry_fixture import IDENTITY, recorded_plane
from scripts.benchmark_report import summarize
from auriga.cortex.session.usage import account_usage, load_price_card
from test_harness import FixtureRobot


class BenchmarkReportCheck(unittest.TestCase):
    def test_model_limit_is_recognized_before_delayed_callback_and_failed_turn(self):
        async def check():
            with tempfile.TemporaryDirectory() as directory:
                store = Store(directory, IDENTITY)
                self.addCleanup(store.close)
                harness = Harness(store, FixtureRobot(directory))
                harness.start()
                done = threading.Event()
                done.set()
                session = SimpleNamespace(turn_done=done, model_limit_reached=threading.Event(),
                                          model_proxy=SimpleNamespace(limit_notified=True))
                self.assertFalse(await wait_turn(session, asyncio.Event(), None, harness))
                self.assertEqual(harness.wait_stop_reason, "model_request_limit")
                result = dict(valid_episode=True, task_success=False, normalized_score=0,
                              native_episode=dict(ended=False), model_budget_exhausted=True,
                              termination_reason=harness.wait_stop_reason)
                self.assertEqual(classify_episode_result(result, "failed")["primary_outcome"], "scored_failure")
                self.assertEqual(classify_episode_result({**result, "infrastructure_error": "robot transport error"},
                                                        "failed")["primary_outcome"], "unscored_infrastructure")
                self.assertEqual(classify_episode_result(result, "unknown")["primary_outcome"], "unscored_infrastructure")
        asyncio.run(check())

    def test_partial_native_score_is_not_a_policy_failure(self):
        partial = dict(valid_episode=True, task_success=False, normalized_score=0,
                       native_episode=dict(ended=False), infrastructure_error="request timeout")
        value = classify_episode_result(partial, "failed")
        self.assertEqual(value["primary_outcome"], "unscored_infrastructure")
        self.assertIsNone(value["task_success"])
        self.assertFalse(value["partial_native_result"]["task_success"])
        native = {**partial, "native_episode": dict(ended=True), "task_success": True, "normalized_score": 1}
        self.assertEqual(classify_episode_result(native, "completed")["primary_outcome"], "scored_success")
        failure = {**partial, "infrastructure_error": None, "termination_reason": "model_request_limit"}
        self.assertEqual(classify_episode_result(failure, "stopped")["primary_outcome"], "scored_failure")
        self.assertEqual(classify_episode_result({**partial, "infrastructure_error": None}, "stopped")["primary_outcome"], "unscored_interrupted")

    def test_budgets_remain_private_and_native_terminal_closes_motion(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "store", IDENTITY)
            self.addCleanup(store.close)
            robot = FixtureRobot(directory)
            harness = Harness(store, robot)
            initial = harness.start()
            self.assertIsNone(harness.max_actions)
            def call(name, args=None):
                result = harness.dispatch(name, args or {}, dict(tool_call_id="budget-test"))
                return json.loads(result["contentItems"][0]["text"])
            self.assertNotIn("runtime_budget", call("robot_capabilities"))
            self.assertNotIn("runtime_budget", call("history_view_observation", dict(observation_id=initial["observation_id"])))
            execute = robot.execute
            def native_execute(*args, **kwargs):
                return {**execute(*args, **kwargs), "episode_terminal": dict(ended=True, success=True, reason="success", native_action_count=17, native_step_limit=200)}
            robot.execute = native_execute
            harness.capabilities.update(bounded_wait=dict(max_native_steps=50), path_kinds=["backend_free", "hold"])
            args = dict(observation_id=initial["observation_id"], native_steps=5, action_id="terminal")
            result = call("robot_wait", args)
            self.assertNotIn("episode_terminal", result["receipt"])
            self.assertNotIn("runtime_budget", result)
            self.assertTrue(harness.episode_done.is_set())
            self.assertFalse(harness.accepting)
            self.assertEqual(store.read_state("runtime", "native_episode")["native_action_count"], 17)
            rejected = call("robot_wait", {**args, "action_id":"after-success", "observation_id":harness.latest["observation_id"]})
            self.assertEqual(rejected["receipt"]["status"], "rejected")
            self.assertEqual(robot.executions, 1)

    def test_inspection_continues_without_action_and_no_custom_benchmark_deadline(self):
        async def check():
            with tempfile.TemporaryDirectory() as directory:
                store = Store(directory, IDENTITY)
                self.addCleanup(store.close)
                harness = Harness(store, FixtureRobot(directory))
                initial = harness.start()
                for _ in range(16):
                    self.assertTrue(harness.dispatch("robot_capabilities", {}, {})["success"])
                    self.assertTrue(harness.dispatch("history_view_observation",
                        dict(observation_id=initial["observation_id"]), {})["success"])
                self.assertTrue(harness.dispatch("robot_observe", {}, {})["success"])
                self.assertEqual(harness.robot.executions, 0)
                session = SimpleNamespace(turn_done=threading.Event())
                harness._native_progress(dict(ended=True, success=False, reason="step_limit", native_action_count=200, native_step_limit=200))
                self.assertFalse(await wait_turn(session, asyncio.Event(), None, harness))
                self.assertIsNone(harness.wall_deadline)
                self.assertEqual(harness.wait_stop_reason, "native_episode_ended")
        asyncio.run(check())

    def test_developer_snapshot_remains_immutable_but_cannot_enter_actor_tools(self):
        fixture = json.loads((Path(__file__).resolve().parents[1] / "contracts/fixtures/scene_plane_box.json").read_text())
        with tempfile.TemporaryDirectory() as directory:
            identity = {key: fixture["observations"][0][key] for key in IDENTITY}
            store = Store(Path(directory) / "store", identity)
            self.addCleanup(store.close)
            harness = Harness(store, object())
            harness.scene = SceneStore(Path(directory) / "scene")
            for observation in fixture["observations"]: harness.scene.import_observation(observation)
            harness.scene.apply_patch(fixture["patches"][0])
            with self.assertRaisesRegex(ValueError, "unknown_tool"):
                harness.dispatch("scene_snapshot", dict(scene_version=1, anchor_observation_id="obs-scene-0"), {})
            manifest = harness.scene.snapshot(identity, 1, "obs-scene-0")
            self.assertEqual(manifest, harness.scene.read_snapshot(identity, manifest["snapshot_id"]))
            expected = manifest.pop("manifest_digest")
            self.assertEqual(digest(manifest), expected)

    def test_evaluation_is_current_and_stop_confirmed(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory, IDENTITY)
            try:
                score = dict(task_success=True, normalized_score=1, valid_episode=True,
                    native_reward=1.0, native_process_score=100.0,
                    authority="installed RoboDojo task.run_reward / RewardManager",
                    success_authority="locked_native_terminal")
                ref = store.put_artifact(json.dumps({**IDENTITY, "result": score}).encode(), "native_evaluation", "application/json", "observer")
                def evaluate(): store.event("observer.evaluated", {"result_ref": ref})
                evaluate()
                self.assertEqual(observer_summary(store, IDENTITY, "completed"), score)
                self.assertEqual(observer_summary(store, IDENTITY, "unknown"), {})
                self.assertEqual(observer_summary(store, {**IDENTITY, "env_generation": 2}, "completed"), {})
                for change in ("action.requested", "environment.reset_requested", "environment.recovery_reset"):
                    store.event(change, {})
                    self.assertEqual(observer_summary(store, IDENTITY, "completed"), {})
                    evaluate()
                    self.assertEqual(observer_summary(store, IDENTITY, "completed"), score)
            finally:
                store.close()

    def test_optional_crop_preserves_original_and_records_pixel_transform(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory, IDENTITY)
            try:
                observation = recorded_plane(store)[0]
                observation.update(observation_profile="rgb_proprio_v1", calibration_id=None)
                observation.pop("robot_model_ref")
                observation["cameras"][0].pop("calibration_ref")
                observation["robot_state_ref"] = store.put_artifact(b'{"arms":{}}', "robot_state", "application/json")
                store.save("observation", observation["observation_id"], observation)
                harness = Harness(store, object())
                original_ref = observation["cameras"][0]["rgb_ref"]
                original_bytes = store.resolve_artifact(original_ref)[1]
                result = harness.dispatch("history_view_observation", dict(observation_id="obs-0", camera_id="head", roi=[100, 80, 40, 30], scale=2), {})
                view = json.loads(result["contentItems"][0]["text"])["image_view"]
                self.assertTrue(view["original_rgb_available"])
                self.assertTrue(view["rgb_available"])
                delivered = store.records("delivery")[-1]["images"][0]
                self.assertEqual(delivered["original_rgb_ref"], original_ref)
                self.assertEqual(view["pixel_transform"], [[2, 0, -199.5], [0, 2, -159.5], [0, 0, 1]])
                with Image.open(io.BytesIO(store.resolve_artifact(delivered["rgb_ref"])[1])) as image:
                    self.assertEqual(image.size, (80, 60))
                self.assertEqual(store.resolve_artifact(original_ref)[1], original_bytes)
                self.assertEqual(len(result["contentItems"]), 2)
                with self.assertRaisesRegex(ValueError, "out_of_bounds"):
                    harness.dispatch("history_view_observation", dict(observation_id="obs-0", camera_id="head", roi=[600, 400, 100, 100]), {})
            finally:
                store.close()

    def test_shutdown_interrupts_long_turn_wait(self):
        async def check():
            session = SimpleNamespace(turn_done=threading.Event())
            shutdown = asyncio.Event()
            waiter = asyncio.create_task(wait_turn(session, shutdown, 1200))
            await asyncio.sleep(.01)
            shutdown.set()
            self.assertFalse(await asyncio.wait_for(waiter, .5))
            session.turn_done.set()
            self.assertTrue(await wait_turn(session, asyncio.Event(), 1200))
        asyncio.run(check())

    def test_calibrated_overlay_cannot_cross_into_actor_store(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            main, twin = Store(root / "main", IDENTITY), Store(root / "twin", IDENTITY)
            try:
                harness = Harness(main, object())
                harness.scene = SceneStore(root / "scene", twin.resolve_artifact, twin.put_artifact)
                for observation in recorded_plane(twin): harness.scene.import_observation(observation)
                with self.assertRaisesRegex(ValueError, "tool_unavailable"):
                    harness.dispatch("geometry_query", {"request": {"kind": "project_points",
                        "observation_id": "obs-0", "camera_id": "head", "points_m": [[0, 0, 2]]}}, {})
                self.assertFalse(main.events())
            finally:
                main.close(); twin.close()

    def test_costs_and_failure_remain_distinct_from_motion_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = dict(run_id="pilot", episode_id="ep", env_instance_id="env", env_generation=1)
            store = Store(root, identity)
            try:
                store.save("tool_call", "call", {"params": {"tool": "robot_move_ee"}})
                store.save("delivery", "model", {"delivered_to_actor": True, "images": [{}, {}, {}]})
                store.save("delivery", "program", {"delivered_to_actor": False, "images": [{}]})
            finally:
                store.close()
            (root / "manifest.json").write_text(json.dumps(dict(task="general_pickup", seed=0, layout_id=0)))
            (root / "summary.json").write_text(json.dumps(dict(status="completed", action_count=1,
                valid_episode=True, task_success=False, normalized_score=0,
                token_usage={"total": {"input_tokens": 12000, "cached_input_tokens": 10000, "output_tokens": 500}})))
            rollout_dir = root / "private" / "codex-home" / "sessions"
            rollout_dir.mkdir(parents=True)
            with (rollout_dir / "synthetic.jsonl").open("w") as stream:
                for kind in ("custom_tool_call_output", "function_call_output"):
                    stream.write(json.dumps({"type": "response_item", "payload": {"type": kind,
                        "output": [{"type": "input_image"}, {"type": "input_text", "text": "fixture"}]}}) + "\n")
                stream.write(json.dumps({"type": "event_msg", "payload": {"type": "item_completed", "output": [{"type": "input_image"}]}}) + "\n")
            result = summarize(root)
            self.assertFalse(result["task_success"])
            self.assertEqual(result["uncached_input_tokens"], 2000)
            self.assertEqual(result["actor_image_deliveries"], 3)
            self.assertEqual(result["native_model_input_images"], 2)
            self.assertEqual(result["actor_tools"], {"robot_move_ee": 1})
            self.assertEqual(result["token_accounting"]["tokens"]["uncached_input_tokens"], 2000)
            self.assertIsNone(result["token_accounting"]["rate_card_estimate"]["total"])
            # A frozen per-run provider plus an explicit price card can price old
            # archives without consulting today's global model/configuration.
            (root / "manifest.json").write_text(json.dumps(dict(model="fixture", task="general_pickup")))
            (root / "private/codex-home/config.toml").write_text('model = "fixture"\nmodel_provider = "test-provider"\n')
            saved = json.loads((root / "summary.json").read_text())
            saved["token_usage"]["total"]["cache_write_input_tokens"] = 0
            (root / "summary.json").write_text(json.dumps(saved))
            card = root / "synthetic-price-card.json"
            card.write_text(json.dumps(dict(version="synthetic", source="synthetic_fixture", currency="USD", rates=[dict(
                provider="test-provider", model="fixture", uncached_input_per_million="2",
                cached_input_per_million="0.5", output_per_million="8")])))
            priced = summarize(root, load_price_card(card))["token_accounting"]
            self.assertEqual(priced["provider"], "test-provider")
            self.assertEqual(priced["rate_card_estimate"]["total"], "0.013")
            self.assertEqual(priced["identity_source"], "private/codex-home/config.toml")
            (root / "private/codex-home/config.toml").write_text('model = "different-model"\nmodel_provider = "test-provider"\n')
            self.assertIsNone(summarize(root, load_price_card(card))["token_accounting"]["rate_card_estimate"]["total"])
            # A TUI failure after model work leaves a cost-bearing summary before
            # manifest creation. Preserve its frozen estimate, or reprice with
            # an explicit card, without using today's/global model config.
            saved.update(status="failed", infrastructure_error="synthetic terminal startup failure")
            saved["token_usage"].update(thread_id="failed-thread", turn_id="turn", model_context_window=258400)
            saved["token_accounting"] = account_usage(saved["token_usage"], model="fixture",
                                                       provider="test-provider", price_card=load_price_card(card))
            (root / "manifest.json").unlink()
            (root / "summary.json").write_text(json.dumps(saved))
            failed = summarize(root)
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["conditions"]["model"], "fixture")
            self.assertEqual(failed["token_accounting"]["rate_card_estimate"]["total"], "0.013")
            self.assertEqual(failed["token_accounting"]["identity_source"], "summary.token_accounting")
            changed_card = json.loads(card.read_text())
            changed_card["rates"][0]["uncached_input_per_million"] = "4"
            card.write_text(json.dumps(changed_card))
            self.assertEqual(summarize(root, load_price_card(card))["token_accounting"]["rate_card_estimate"]["total"], "0.017")
            self.assertEqual(summarize(root)["token_accounting"]["rate_card_estimate"]["total"], "0.013")
            for key, changed in (("thread_id", "foreign-thread"), ("model_context_window", 999999),
                                 ("tokens", {})):
                stale = {**saved, "token_accounting": {**saved["token_accounting"], key: changed}}
                (root / "summary.json").write_text(json.dumps(stale))
                self.assertIsNone(summarize(root)["token_accounting"]["rate_card_estimate"]["total"])
                self.assertIsNone(summarize(root, load_price_card(card))["token_accounting"]["rate_card_estimate"]["total"])
            (root / "summary.json").write_text(json.dumps(dict(status="failed", task_success=None)))
            failed = summarize(root)
            self.assertIsNone(failed["task_success"])
            self.assertIsNone(failed["token_usage"])
            self.assertIsNone(failed["uncached_input_tokens"])


if __name__ == "__main__":
    unittest.main()
