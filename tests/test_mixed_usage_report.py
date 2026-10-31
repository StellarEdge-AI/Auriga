import json
from pathlib import Path
import tempfile
import unittest

from auriga.core.storage.store import Store
from auriga.cortex.session.usage import load_price_card
from scripts.summarize_mixed_usage import summarize_run, report


class MixedUsageReportTest(unittest.TestCase):
    def test_missing_metadata_is_unknown_and_legacy_transport_success_is_not_failure(self):
        card = load_price_card(Path(__file__).parents[1] / "configs/pricing/openai-astra-sol-standard-2026-10-02.json")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = summarize_run(root, card)
            self.assertEqual(result["metadata_missing"], ["manifest.json", "summary.json"])
            self.assertEqual(result["primary_outcome"], "unscored_infrastructure")
            self.assertIsNone(result["accounting"]["total"]["rate_card_estimate_by_currency"]["USD"]["total"])
            (root / "summary.json").write_text(json.dumps(dict(status="failed", valid_episode=True, task_success=False)))
            (root / "private").mkdir()
            metrics = root / "private/model-requests-fixture.jsonl"
            metrics.write_text(json.dumps(dict(kind="model_request_finished", http_status=200, transport_complete=True))+"\n")
            result = summarize_run(root, card)
            self.assertEqual(len(result["entries"]), 1)
            self.assertEqual(result["outcome_classification_basis"], "offline_current_protocol")
            self.assertFalse(result["original_native_result"]["task_success"])
            self.assertIsNone(result["task_success"])
            metrics.write_text(json.dumps(dict(kind="model_request_finished", http_status=200))+"\n")
            result = summarize_run(root, card)
            self.assertEqual(result["entries"][-1]["source"], "unattributed_unknown_attempts")
            metrics.unlink()
            (root / "summary.json").write_text(json.dumps(dict(status="failed",
                model_requests=dict(http_attempt_count=0, completed_calls=0))))
            result = summarize_run(root, card)
            self.assertEqual(result["accounting"]["total"]["rate_card_estimate_by_currency"]["USD"]["total"], "0")

    def test_proxy_ledger_excludes_duplicate_actor_snapshot_and_helper_event(self):
        card = load_price_card(Path(__file__).parents[1] / "configs/pricing/openai-astra-sol-standard-2026-10-02.json")
        usage = dict(input_tokens=1000, input_tokens_details=dict(cached_tokens=200, cache_write_tokens=0),
                     output_tokens=100, output_tokens_details=dict(reasoning_tokens=60), total_tokens=1100)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = dict(run_id="r", episode_id="e", env_instance_id="env", env_generation=1)
            (root / "manifest.json").write_text(json.dumps(dict(**identity, model="gpt-6-astra", provider="custom")))
            (root / "summary.json").write_text(json.dumps(dict(status="completed", valid_episode=True,
                task_success=True, scored_episode=True, primary_outcome="scored_success",
                token_usage=dict(total=dict(input_tokens=9999999)))))
            store = Store(root, identity)
            store.event("visual.model_reviewed", dict(model="gpt-6.1-sol", usage=usage, request_id="duplicate"))
            store.close()
            (root / "private").mkdir()
            attempts = [dict(kind="model_request_finished", logical_call_id="logical-"+str(i),
                attempt_index=1, usage=value, requested_model=model, served_model=model, source=source, http_status=200)
                for i, (model, source, value) in enumerate((
                    ("gpt-6-astra", "actor", usage), ("gpt-6.1-sol", "sol_visual_review", usage),
                    ("gpt-6-astra", "actor", None)))]
            (root / "private/model-requests-fixture.jsonl").write_text("\n".join(map(json.dumps, attempts))+"\n")
            result = summarize_run(root, card)
            self.assertEqual(len(result["entries"]), 3)
            costs = result["accounting"]["total"]["rate_card_estimate_by_currency"]["USD"]
            self.assertEqual(costs["known_component_subtotal"], "0.01582")
            self.assertIsNone(costs["total"])
            self.assertEqual(costs["unknown_runs"], 1)
            unscored = {**result, "run_id": "unscored", "scored_episode": False, "task_success": None, "entries": []}
            failed = {**result, "run_id": "failure", "scored_episode": True, "task_success": False, "entries": []}
            aggregate = report([result, failed, unscored])
            self.assertEqual(aggregate["outcomes"], dict(episodes=3, scored_episodes=2, scored_successes=1,
                                                       scored_failures=1, unscored_episodes=1))
            per_success = aggregate["cost_per_scored_success"]["by_currency"]["USD"]
            self.assertEqual(per_success["known_component_cost_per_scored_success"], "0.01582")
            self.assertIsNone(per_success["total_cost_per_scored_success"])


if __name__ == "__main__":
    unittest.main()
