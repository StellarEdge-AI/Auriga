"""Native usage snapshots stay cumulative, durable, and honest about missing fields."""

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from auriga.cortex.session.codex import CodexSession
from auriga.core.config import Config
from auriga.core.storage.store import Store
from auriga.cortex.session.usage import account_usage, account_responses_usage, aggregate_accounting, load_price_card, mixed_accounting


class TokenUsageCheck(unittest.TestCase):
    def test_responses_mixed_cost_has_no_reasoning_double_count_and_retains_unknowns(self):
        card = load_price_card(Path(__file__).parents[1] / "configs/pricing/openai-astra-sol-standard-2026-10-02.json")
        usage = dict(input_tokens=1000, input_tokens_details=dict(cached_tokens=200, cache_write_tokens=0),
                     output_tokens=100, output_tokens_details=dict(reasoning_tokens=60), total_tokens=1100)
        def account(value, model="gpt-6.1-sol"):
            return account_responses_usage(value, model=model, provider="custom", price_card=card)
        priced = account(usage)
        self.assertEqual(priced["rate_card_estimate"]["total"], "0.00262")
        self.assertEqual(priced["scope"], "single_request")
        partial = account({**usage, "input_tokens_details": dict(cached_tokens=200)})
        self.assertIsNone(partial["tokens"]["cache_write_input_tokens"])
        self.assertIsNone(partial["rate_card_estimate"]["total"])
        self.assertEqual(partial["rate_card_estimate"]["known_subtotal"], "0.00102")
        writes = account({**usage, "input_tokens_details": dict(cached_tokens=200, cache_write_tokens=100)})
        self.assertEqual(writes["rate_card_estimate"]["known_subtotal"], "0.00242")
        self.assertIsNone(writes["rate_card_estimate"]["total"])
        rows = [dict(accounting_id="sol1", source="sol_visual_review", accounting=priced),
                dict(accounting_id="failed", source="actor", accounting=account(None, "gpt-6-astra")),
                dict(accounting_id="sol2", source="sol_visual_review", accounting=partial)]
        result = mixed_accounting(rows)
        costs = result["total"]["rate_card_estimate_by_currency"]["USD"]
        self.assertIsNone(costs["total"])
        self.assertEqual(costs["known_component_subtotal"], "0.00364")
        self.assertEqual(costs["unknown_runs"], 2)
        self.assertEqual(len(result["by_model_source"]), 2)
        with self.assertRaisesRegex(ValueError, "duplicate_mixed_accounting_id"):
            mixed_accounting(rows + rows[:1])

    def test_native_snapshots_duplicate_missing_and_foreign_thread(self):
        identity = dict(run_id="synthetic-usage", episode_id="ep", env_instance_id="env", env_generation=1)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = Store(root, identity)
            try:
                session = CodexSession(root, store, lambda params: None)
                session.thread_id = "owned-thread"
                counters = dict(inputTokens=10919, cachedInputTokens=10240, cacheWriteInputTokens=0,
                                outputTokens=109, reasoningOutputTokens=17, totalTokens=11028)
                message = dict(method="thread/tokenUsage/updated", params=dict(
                    threadId=session.thread_id, turnId="turn-1", tokenUsage=dict(
                        total=counters, last=counters, modelContextWindow=258400, futureField="preserved")))
                session._notification(message)
                session._notification(message)  # A repeated cumulative snapshot must not add any tokens.
                usage = store.read_state("runtime", "token_usage")
                self.assertEqual(usage["total"], dict(input_tokens=10919, cached_input_tokens=10240,
                    cache_write_input_tokens=0, output_tokens=109, reasoning_output_tokens=17, total_tokens=11028))
                self.assertEqual(usage["last"], usage["total"])
                self.assertEqual(usage["model_context_window"], 258400)
                self.assertEqual(store.read_state("token_usage", "turn-1"), usage)
                self.assertEqual(store.events()[-1]["payload"]["token_usage"], usage)
                accounting = store.read_state("runtime", "token_accounting")
                self.assertEqual(accounting["tokens"]["uncached_input_tokens"], 679)
                self.assertEqual(accounting["tokens"]["total_tokens"], 11028)
                self.assertEqual(store.read_state("token_accounting", "turn-1"), accounting)
                self.assertIsNone(accounting["rate_card_estimate"]["total"])
                self.assertIsNone(accounting["provider_reported"]["amount"])
                _, raw = store.resolve_artifact(usage["raw_ref"], allowed_scopes=("observer",))
                self.assertEqual(json.loads(raw), message)

                session._notification(dict(method=message["method"], params=dict(
                    threadId="other-thread", turnId="other-turn", tokenUsage=dict(total=dict(inputTokens=99999)))))
                self.assertEqual(store.read_state("runtime", "token_usage"), usage)
                self.assertIsNone(store.read_state("token_usage", "other-turn"))

                session._notification(dict(method=message["method"], params=dict(
                    threadId=session.thread_id, turnId="turn-2", tokenUsage=dict(
                        last=dict(outputTokens=0, inputTokens=True, cachedInputTokens=-1)))))
                partial = store.read_state("runtime", "token_usage")
                self.assertIsNone(partial["total"])
                self.assertIsNone(partial["model_context_window"])
                self.assertEqual(partial["last"]["output_tokens"], 0)
                self.assertTrue(all(value is None for key, value in partial["last"].items() if key != "output_tokens"))
                self.assertEqual(store.read_state("token_usage", "turn-1"), usage)
                self.assertIsNone(store.read_state("runtime", "token_accounting")["tokens"]["uncached_input_tokens"])
            finally:
                store.close()
            reopened = Store(root, identity, read_only=True)
            try:
                self.assertEqual(reopened.read_state("runtime", "token_usage"), partial)
            finally:
                reopened.close()

    def test_decimal_cost_identity_unknown_and_aggregate_coverage(self):
        # Synthetic prices are arithmetic fixtures, not any provider's real prices.
        card = dict(version="synthetic-1", source="synthetic_fixture", currency="USD", rates=[dict(
            provider="fixture", model="test-model", uncached_input_per_million="2.5",
            cached_input_per_million="0.25", output_per_million="10")])
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "price.json"
            path.write_text(json.dumps(card))
            loaded = load_price_card(path)
            self.assertEqual(len(loaded["sha256"]), 64)
            identity = dict(run_id="synthetic-priced", episode_id="ep", env_instance_id="env", env_generation=1)
            store = Store(Path(temporary) / "run", identity)
            session = CodexSession(store.root, store, lambda params: None, model="test-model", price_card=loaded,
                                   config=Config.from_dict({"model": {"codex_home": temporary}}))
            try:
                session.thread_id, session.provider = "fixture-thread", "fixture"
                notification = dict(method="thread/tokenUsage/updated", params=dict(threadId=session.thread_id,
                    turnId="turn", tokenUsage=dict(total=dict(inputTokens=1_000_000, cachedInputTokens=800_000,
                    cacheWriteInputTokens=0, outputTokens=100_000, reasoningOutputTokens=25_000, totalTokens=1_100_000))))
                session._notification(notification)
                session._notification(notification)
                self.assertEqual(store.read_state("runtime", "token_accounting")["rate_card_estimate"]["total"], "1.70")
                config = Path(temporary) / "config.toml"
                config.write_text('model_provider = "different-provider"\n')
                with self.assertRaisesRegex(ValueError, "resume_usage_identity_conflict"):
                    session._prepare(("test-model", "fixture"))
            finally:
                session.close()
                store.close()
            snapshot = dict(total=dict(input_tokens=1_000_000, cached_input_tokens=800_000,
                cache_write_input_tokens=0, output_tokens=100_000, reasoning_output_tokens=25_000,
                total_tokens=1_100_000))
            def account(value=snapshot, **kwargs):
                return account_usage(value, model="test-model", provider="fixture", price_card=loaded, **kwargs)
            value = account()
            estimate = value["rate_card_estimate"]
            self.assertEqual(estimate["total"], "1.70")  # Reasoning already belongs to output.
            self.assertEqual((estimate["uncached_input"], estimate["cached_input"], estimate["output"]), ("0.5", "0.20", "1"))
            self.assertEqual(value["cache_hit_rate"], "0.8")
            self.assertIsNone(value["provider_reported"]["amount"])
            for change in (dict(provider="other", model="test-model"), dict(provider="fixture", model="other"), dict(provider=None, model="test-model")):
                unmatched = account_usage(snapshot, price_card=loaded, **change)
                self.assertIsNone(unmatched["rate_card_estimate"]["total"])
            for change, issue in ((dict(cached_input_tokens=1_000_001), "invalid_usage_counters"),
                                  (dict(reasoning_output_tokens=100_001), "invalid_usage_counters"),
                                  (dict(cache_write_input_tokens=1), "cache_write_pricing_unknown"),
                                  (dict(cache_write_input_tokens=None), "cache_write_pricing_unknown"),
                                  (dict(output_tokens=True), "token_counter_missing")):
                invalid = account(dict(total={**snapshot["total"], **change}))
                self.assertEqual(invalid["rate_card_estimate"]["reason"], issue)
                self.assertIsNone(invalid["rate_card_estimate"]["total"])
            zero = account(dict(total={key: 0 for key in snapshot["total"]}))
            self.assertEqual(zero["rate_card_estimate"]["total"], "0.00")
            self.assertIsNone(zero["cache_hit_rate"])
            absent = account({})
            rows = [dict(run_id="complete", token_accounting=value), dict(run_id="failed", token_accounting=absent)]
            totals = aggregate_accounting(rows)
            self.assertEqual(totals["tokens"]["input_tokens"], dict(total=None, known_subtotal=1_000_000, known_runs=1, unknown_runs=1))
            self.assertEqual(totals["rate_card_estimate_by_currency"]["USD"], dict(total=None, known_subtotal="1.70", known_runs=1, unknown_runs=1))
            with self.assertRaisesRegex(ValueError, "duplicate_run_id"):
                aggregate_accounting(rows + rows[:1])
            bounded_card = {**card, "billing_basis": "synthetic Standard reference estimate",
                            "rates": [{**card["rates"][0], "max_input_tokens": 272000}]}
            path.write_text(json.dumps(bounded_card))
            bounded = load_price_card(path)
            for window in (258400, 272000):
                # The cumulative 1M input does not trigger long-context pricing.
                priced = account_usage({**snapshot, "model_context_window": window}, model="test-model",
                                       provider="fixture", price_card=bounded)
                self.assertEqual(priced["rate_card_estimate"]["total"], "1.70")
                self.assertEqual(priced["model_context_window"], window)
                self.assertEqual(priced["rate_card_estimate"]["price_card"]["billing_basis"], bounded_card["billing_basis"])
            for window, reason in ((None, "request_input_bound_unknown"), (0, "request_input_bound_unknown"),
                                   (True, "request_input_bound_unknown"), ("258400", "request_input_bound_unknown"),
                                   (272001, "request_input_bound_exceeds_flat_rate_limit")):
                unknown = account_usage({**snapshot, "model_context_window": window}, model="test-model",
                                        provider="fixture", price_card=bounded)["rate_card_estimate"]
                self.assertIsNone(unknown["total"])
                self.assertEqual(unknown["reason"], reason)
            for invalid_limit in (0, -1, True, "272000", None):
                bounded_card["rates"][0]["max_input_tokens"] = invalid_limit
                path.write_text(json.dumps(bounded_card))
                with self.assertRaisesRegex(ValueError, "max_input_tokens"):
                    load_price_card(path)
            for invalid in ("NaN", "Infinity", "-1", 1.0, None):
                card["rates"][0]["output_per_million"] = invalid
                path.write_text(json.dumps(card))
                with self.assertRaises(ValueError): load_price_card(path)


class IpcPathCheck(unittest.TestCase):
    def test_runtime_directory_uses_environment_or_platform_temporary_directory(self):
        identity = dict(run_id="synthetic-ipc-env", episode_id="ep", env_instance_id="env", env_generation=1)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = Store(root / "archive", identity)
            runtime = root / "runtime"
            runtime.mkdir(mode=0o700)
            try:
                with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(runtime)}):
                    session = CodexSession(store.root, store, lambda params: None)
                    self.assertEqual(session.ipc_base, runtime / "auriga")
                    session._prepare_ipc()
                    session.close()
                    runtime.chmod(0o755)
                    with self.assertRaisesRegex(ValueError, "unsafe_codex_runtime_directory"):
                        session._prepare_ipc()
                with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": ""}), \
                     mock.patch("auriga.cortex.session.codex.tempfile.gettempdir", return_value=str(root)):
                    session = CodexSession(store.root, store, lambda params: None)
                    self.assertEqual(session.ipc_base, root / f"auriga-ipc-{os.getuid()}")
                    session._prepare_ipc()
                    session.close()
                for path, error in (("relative", "must_be_absolute"),
                                    (str(root / ("long" * 30)), "codex_ipc_path_too_long")):
                    with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": path}), \
                         self.assertRaisesRegex(ValueError, error):
                        CodexSession(store.root, store, lambda params: None)
            finally:
                store.close()

    def test_long_archive_path_binds_and_cleans_only_its_run(self):
        identity = dict(run_id="synthetic-ipc", episode_id="ep", env_instance_id="env", env_generation=1)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / ("archive" * 12) / ("category" * 12) / ("task" * 20)
            store = Store(root, identity)
            session = CodexSession(root, store, lambda params: None)
            sibling = CodexSession(root / "another-run", store, lambda params: None)
            try:
                self.assertGreater(len(os.fsencode(root / "private/ipc/codex.sock")), 107)
                self.assertLess(len(os.fsencode(session.socket_path)), 108)
                self.assertNotEqual(session.ipc, sibling.ipc)
                session._prepare_ipc()
                sibling._prepare_ipc()
                self.assertEqual(session.ipc.stat().st_mode & 0o777, 0o700)
                self.assertEqual(session.ipc_base.stat().st_mode & 0o777, 0o700)
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                    connection.bind(str(session.socket_path))
                legacy_uri = f"unix://{root / 'private/ipc/codex.sock'}"
                process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", legacy_uri])
                try:
                    session._stop_previous_owned_processes(dict(app_server_pid=process.pid, socket_uri=legacy_uri))
                    self.assertEqual(process.wait(timeout=2), -15)
                finally:
                    if process.poll() is None: process.terminate(); process.wait(timeout=3)
                session.close()
                self.assertFalse(session.ipc.exists())
                self.assertTrue(sibling.ipc.is_dir())
            finally:
                session.close()
                sibling.close()
                store.close()

    def test_ipc_symlink_is_rejected_without_changing_target(self):
        identity = dict(run_id="synthetic-ipc-symlink", episode_id="ep", env_instance_id="env", env_generation=1)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = Store(root, identity)
            session = CodexSession(root, store, lambda params: None)
            try:
                session._prepare_ipc()
                session.ipc.rmdir()
                target = root / "target"
                target.mkdir(mode=0o755)
                session.ipc.symlink_to(target, target_is_directory=True)
                with self.assertRaisesRegex(ValueError, "unsafe_codex_ipc_directory"):
                    session._prepare_ipc()
                self.assertEqual(target.stat().st_mode & 0o777, 0o755)
            finally:
                session.ipc.unlink(missing_ok=True)
                session.ipc_prepared = False
                session.close()
                store.close()


if __name__ == "__main__":
    unittest.main()
