"""Task limits are scored failures; native terminals and invalid evidence differ."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import shutil
import tempfile
import unittest

from auriga.apps.library.scoring import classify_episode_result
from auriga.apps.library.scoring import load_effective_summary, reconcile_summary
from auriga.core.storage.store import Store


TASK_LIMITS = (
    "model_request_limit", "official_startup_no_action_timeout",
    "official_no_action_progress_timeout", "actor_repeated_inability", "actor_no_progress",
    "development_wall_budget",
)


class TaskLimitScoringChecks(unittest.TestCase):
    def snapshot(self, reason="official_no_action_progress_timeout", **changes):
        result = dict(benchmark=True, status="stopped", cleanup_status="completed",
            termination_reason=reason, valid_episode=True, task_success=False,
            normalized_score=.4, native_process_score=40., native_reward=0.,
            native_episode=dict(ended=False, native_action_count=641, native_step_limit=700),
            infrastructure_error=None,
            authority="installed RoboDojo task.run_reward / RewardManager",
            success_authority="native_final_check")
        result.update(changes)
        return result

    def assert_failure(self, result, score, process):
        self.assertEqual(result["primary_outcome"], "scored_failure")
        self.assertIs(result["scored_episode"], True)
        self.assertIs(result["task_success"], False)
        self.assertEqual(result["normalized_score"], score)
        self.assertEqual(result["native_process_score"], process)
        self.assertEqual(result["termination_authority"], "harness")
        self.assertIs(result["native_episode"]["ended"], False)

    def test_every_task_limit_retains_zero_and_partial_process_score_as_failure(self):
        for reason in TASK_LIMITS:
            for score, process in ((0., 0.), (.4, 40.)):
                with self.subTest(reason=reason, score=score):
                    original = self.snapshot(reason, normalized_score=score, native_process_score=process)
                    saved = deepcopy(original)
                    result = classify_episode_result(original, "stopped")
                    self.assert_failure(result, score, process)
                    self.assertEqual(original, saved, "effective scoring must not change source evidence")

    def test_development_wall_budget_also_scores_outside_benchmark_mode(self):
        result = classify_episode_result(self.snapshot("development_wall_budget", benchmark=False), "stopped")
        self.assertEqual(result["primary_outcome"], "scored_failure")
        self.assertFalse(result["task_success"])
        self.assertEqual(result["normalized_score"], .4)

    def test_success_snapshot_without_native_terminal_does_not_satisfy_task_completion(self):
        for reason in TASK_LIMITS:
            with self.subTest(reason=reason):
                result = classify_episode_result(self.snapshot(reason, task_success=True,
                    normalized_score=1., native_process_score=40., native_reward=1.), "stopped")
                self.assert_failure(result, .4, 40.)
                self.assertTrue(result["native_evaluation_snapshot"]["task_success"])
                self.assertEqual(result["native_evaluation_snapshot"]["normalized_score"], 1.)
                self.assertEqual(result["success_authority"], "harness_task_limit")
                self.assertEqual(result["failure_reason"]["code"], reason)
                self.assertEqual(result["failure_reason"]["authority"], "harness")
                self.assertTrue(result["failure_reason"]["label"])

    def test_binary_native_evaluation_with_explicitly_absent_process_scores_zero_failure(self):
        result = classify_episode_result(self.snapshot(task_success=True, normalized_score=1.,
                                                        native_process_score=None), "stopped")
        self.assert_failure(result, 0., None)
        self.assertTrue(result["native_evaluation_snapshot"]["task_success"])

    def test_missing_process_field_cannot_derive_failure_score_from_raw_success(self):
        original = self.snapshot(task_success=True, normalized_score=1.)
        del original["native_process_score"]
        result = classify_episode_result(original, "stopped")
        self.assertFalse(result["scored_episode"])
        self.assertIsNone(result["normalized_score"])
        self.assertFalse(result["task_success"])
        self.assertEqual(result["task_outcome"], "failure")

    def test_native_terminal_success_wins_racing_task_limit(self):
        for reason in TASK_LIMITS:
            with self.subTest(reason=reason):
                result = classify_episode_result(self.snapshot(reason, task_success=True,
                    normalized_score=1., native_process_score=40.,
                    native_episode=dict(ended=True, success=True, reason="native_success")), "stopped")
                self.assertEqual(result["primary_outcome"], "scored_success")
                self.assertTrue(result["task_success"])
                self.assertEqual(result["normalized_score"], 1.)
                self.assertEqual(result["native_process_score"], 40.)
                self.assertEqual(result["termination_authority"], "robodojo_environment")

    def test_native_terminal_failure_wins_racing_task_limit(self):
        for reason in TASK_LIMITS:
            with self.subTest(reason=reason):
                result = classify_episode_result(self.snapshot(reason,
                    native_episode=dict(ended=True, success=False, reason="native_step_limit")), "stopped")
                self.assertEqual(result["primary_outcome"], "scored_failure")
                self.assertFalse(result["task_success"])
                self.assertEqual(result["normalized_score"], .4)
                self.assertEqual(result["termination_authority"], "robodojo_environment")

    def test_native_terminal_result_survives_unknown_cleanup_status(self):
        for success, score in ((True, 1.), (False, .4)):
            with self.subTest(success=success):
                result = classify_episode_result(self.snapshot(task_success=success, normalized_score=score,
                    infrastructure_error="cleanup resource release failed",
                    native_episode=dict(ended=True, success=success,
                                        reason="native_success" if success else "native_step_limit")), "unknown")
                self.assertTrue(result["scored_episode"])
                self.assertIs(result["task_success"], success)
                self.assertEqual(result["normalized_score"], score)
                self.assertEqual(result["termination_authority"], "robodojo_environment")

    def test_infrastructure_failure_and_unknown_state_are_never_manufactured_failures(self):
        for reason in TASK_LIMITS:
            for status, changes in (
                ("unknown", {}), ("stopped", {"infrastructure_error": "robot transport disconnected"}),
                ("failed", {"infrastructure_error": "model stream disconnected"}),
            ):
                with self.subTest(reason=reason, status=status, changes=changes):
                    result = classify_episode_result(self.snapshot(reason, **changes), status)
                    self.assertEqual(result["primary_outcome"], "unscored_infrastructure")
                    self.assertFalse(result["scored_episode"])
                    self.assertIsNone(result["normalized_score"])
                    self.assertIsNone(result["task_success"])
                    self.assertEqual(result["partial_native_result"]["normalized_score"], .4)

    def test_invalid_native_evaluation_cannot_create_a_task_score(self):
        changes = [{"valid_episode": value} for value in (False, None, 1, "true")]
        changes += [{"task_success": value} for value in (None, 0, "false")]
        changes += [{"normalized_score": value} for value in
                    (None, False, "0.4", float("nan"), float("inf"), -.1, 1.1)]
        for reason in TASK_LIMITS:
            for invalid in changes:
                with self.subTest(reason=reason, invalid=invalid):
                    result = classify_episode_result(self.snapshot(reason, **invalid), "stopped")
                    self.assertFalse(result["scored_episode"])
                    self.assertIsNone(result["normalized_score"])
                    if "valid_episode" in invalid:
                        self.assertIsNone(result["task_success"])
                        self.assertEqual(result["task_outcome"], "unknown")
                    else:
                        self.assertIs(result["task_success"], False)
                        self.assertEqual(result["task_outcome"], "failure")
                        self.assertEqual(result["score_status"], "unavailable")
                        self.assertEqual(result["failure_reason"]["code"], reason)
            original = self.snapshot(reason)
            del original["normalized_score"]
            self.assertFalse(classify_episode_result(original, "stopped")["scored_episode"])
            self.assertFalse(classify_episode_result(original, "stopped")["task_success"])

    def test_external_shutdown_without_task_limit_stays_unscored(self):
        result = classify_episode_result(self.snapshot("shutdown-signal"), "stopped")
        self.assertEqual(result["primary_outcome"], "unscored_interrupted")
        self.assertFalse(result["scored_episode"])
        self.assertIsNone(result["normalized_score"])
        self.assertIsNone(result["task_success"])


class HistoricalScoringReconciliationChecks(unittest.TestCase):
    def historical(self, reason="official_no_action_progress_timeout", **changes):
        raw = dict(valid_episode=True, task_success=False, normalized_score=.4,
                   native_process_score=40., native_reward=0.,
                   success_authority="native_final_check",
                   authority="installed RoboDojo task.run_reward / RewardManager")
        summary = dict(benchmark=True, status="stopped", cleanup_status="completed",
            termination_reason=reason, termination_authority="harness", infrastructure_error=None,
            native_episode=dict(ended=False, native_action_count=641, native_step_limit=700),
            valid_episode=True, primary_outcome="unscored_harness_timeout", scored_episode=False,
            task_success=None, normalized_score=None, native_process_score=40.,
            success_authority="native_final_check", partial_native_result=raw)
        summary.update(changes)
        return summary

    def test_verified_original_bottle_snapshot_becomes_effective_failure_without_source_rewrite(self):
        original = self.historical()
        saved = deepcopy(original)
        result = reconcile_summary(original, evidence_verified=True)
        self.assertEqual(original, saved)
        self.assertEqual(result["primary_outcome"], "scored_failure")
        self.assertTrue(result["scored_episode"])
        self.assertFalse(result["task_success"])
        self.assertEqual(result["normalized_score"], .4)
        self.assertEqual(result["native_process_score"], 40.)
        self.assertFalse(result["native_episode"]["ended"])
        self.assertEqual(result["termination_authority"], "harness")
        self.assertEqual(result["partial_native_result"], saved["partial_native_result"])
        self.assertEqual(result["scoring_policy"]["version"], "native_terminal_and_task_limits_v3")
        before = result["scoring_reconciliation"]["original"]
        for key in ("primary_outcome", "scored_episode", "task_success", "normalized_score", "success_authority"):
            self.assertEqual(before[key], saved[key])
        self.assertEqual(result["failure_reason"]["code"], "official_no_action_progress_timeout")

    def test_verified_evaluation_envelope_overrides_older_partial_snapshot(self):
        original = self.historical()
        original["partial_native_result"] = dict(valid_episode=True, task_success=False,
                                                normalized_score=0., native_process_score=0.)
        evaluation = dict(result=dict(valid_episode=True, task_success=False, normalized_score=.4,
                                      native_process_score=40., native_reward=0.,
                                      success_authority="native_final_check"))
        saved_original, saved_evaluation = deepcopy(original), deepcopy(evaluation)
        result = reconcile_summary(original, evaluation, evidence_verified=True)
        self.assertEqual(result["normalized_score"], .4)
        self.assertEqual(result["native_evaluation_snapshot"]["native_process_score"], 40.)
        self.assertEqual(original, saved_original)
        self.assertEqual(evaluation, saved_evaluation)
        self.assertEqual(result["partial_native_result"], saved_original["partial_native_result"])

    def test_no_verified_evidence_keeps_original_classification(self):
        original = self.historical()
        for kwargs in ({}, {"evidence_verified": False}):
            result = reconcile_summary(original, **kwargs)
            self.assertEqual(result["primary_outcome"], "unscored_harness_timeout")
            self.assertFalse(result["scored_episode"])
            self.assertIsNone(result["task_success"])
            self.assertIsNone(result["normalized_score"])

    def test_unsafe_or_missing_cleanup_prevents_historical_reclassification(self):
        changes = ({"cleanup_status": None}, {"cleanup_status": "failed"},
                   {"status": "unknown"}, {"infrastructure_error": "robot disconnected"},
                   {"status": "failed"}, {"termination_reason": "shutdown-signal"})
        for unsafe in changes:
            with self.subTest(unsafe=unsafe):
                original = self.historical(**unsafe)
                result = reconcile_summary(original, evidence_verified=True)
                self.assertFalse(result["scored_episode"])
                self.assertIsNone(result["normalized_score"])
                self.assertIsNone(result["task_success"])
        original = self.historical()
        del original["cleanup_status"]
        self.assertFalse(reconcile_summary(original, evidence_verified=True)["scored_episode"])

    def test_missing_or_invalid_verified_evaluation_does_not_manufacture_score(self):
        for changes in ({"partial_native_result": None},
                        {"partial_native_result": {"valid_episode": False, "task_success": False,
                                                   "normalized_score": .4, "native_process_score": 40.}},
                        {"partial_native_result": {"valid_episode": True, "task_success": False,
                                                   "normalized_score": "0.4", "native_process_score": 40.}}):
            with self.subTest(changes=changes):
                result = reconcile_summary(self.historical(**changes), evidence_verified=True)
                self.assertFalse(result["scored_episode"])
                self.assertIsNone(result["normalized_score"])
                if (changes.get("partial_native_result") or {}).get("valid_episode") is True:
                    self.assertFalse(result["task_success"])
                else:
                    self.assertIsNone(result["task_success"])

    def test_verified_clean_model_budget_failure_can_reconcile(self):
        original = self.historical("model_request_limit", status="failed", model_budget_exhausted=True,
                                   primary_outcome="unscored_model_budget")
        result = reconcile_summary(original, evidence_verified=True)
        self.assertEqual(result["primary_outcome"], "scored_failure")
        self.assertFalse(result["task_success"])
        self.assertEqual(result["normalized_score"], .4)
        self.assertEqual(result["failure_reason"]["code"], "model_request_limit")

    def test_current_policy_is_rechecked_when_cleanup_becomes_unknown(self):
        for ended in (False, True):
            with self.subTest(native_ended=ended):
                native = self.historical()["partial_native_result"]
                source = {**self.historical(), **native}
                if ended:
                    source.update(task_success=True, normalized_score=1.,
                        native_episode=dict(ended=True, success=True, reason="native_success"))
                current = classify_episode_result(source, "stopped")
                self.assertTrue(current["scored_episode"])
                current.update(status="unknown", cleanup_status="failed",
                               infrastructure_error="cleanup resource release failed")
                result = reconcile_summary(current)
                if ended:
                    self.assertTrue(result["scored_episode"])
                    self.assertTrue(result["task_success"])
                    self.assertEqual(result["normalized_score"], 1.)
                else:
                    self.assertFalse(result["scored_episode"])
                    self.assertIsNone(result["task_success"])
                    self.assertIsNone(result["normalized_score"])
                    self.assertEqual(result["task_outcome"], "unknown")


class EffectiveSummaryEvidenceChecks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def fixture(self, name="run", *, later_event=None, artifact_value=None):
        root = self.base / name
        identity = dict(run_id="recorded-bottle-fixture", episode_id="episode-1",
                        env_instance_id="native-env", env_generation=1)
        summary = HistoricalScoringReconciliationChecks().historical()
        native = {**summary["partial_native_result"], "evaluator_actions": 641,
                  "evaluation_id": "verified-current-native-evaluation"}
        recorded = {**identity, "result": native}
        store = Store(root, identity)
        try:
            store.set_state("runtime", "identity", identity)
            # Earlier physical work is allowed; later physical work invalidates
            # the evaluator snapshot even if the summary still looks terminal.
            store.event("action.requested", {"action_id": "earlier-action"})
            encoded = json.dumps(recorded if artifact_value is None else artifact_value).encode()
            reference = store.put_artifact(encoded, "native_evaluation", "application/json", "observer")
            metadata, blob = store.artifact(reference)
            evaluation = {**recorded, "result_ref": reference}
            store.event("observer.evaluated", {"result_ref": reference})
            if later_event:
                store.event(later_event, {})
            (root / "summary.json").write_text(json.dumps(summary))
            (root / "evaluation.json").write_text(json.dumps(evaluation))
        finally:
            store.close()
        return root, summary, evaluation, metadata, blob

    def assert_unreconciled(self, root):
        result = load_effective_summary(root)
        self.assertEqual(result["primary_outcome"], "unscored_harness_timeout")
        self.assertFalse(result["scored_episode"])
        self.assertIsNone(result["normalized_score"])
        self.assertIsNone(result["task_success"])
        self.assertNotIn("scoring_reconciliation", result)

    def test_valid_sqlite_artifact_and_current_evaluation_restore_original_point_four_failure(self):
        root, summary, evaluation, _, _ = self.fixture()
        before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
        result = load_effective_summary(root)
        self.assertEqual(result["primary_outcome"], "scored_failure")
        self.assertFalse(result["task_success"])
        self.assertEqual(result["normalized_score"], .4)
        self.assertEqual(result["native_process_score"], 40.)
        self.assertFalse(result["native_episode"]["ended"])
        self.assertEqual(result["failure_reason"]["code"], "official_no_action_progress_timeout")
        self.assertEqual(result["scoring_reconciliation"]["result_ref"], evaluation["result_ref"])
        self.assertEqual(result["partial_native_result"], summary["partial_native_result"])
        after = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
        self.assertEqual(after, before, "derived historical scoring must not write archived evidence")

    def test_sealed_database_read_does_not_create_wal_or_shm(self):
        root, _, _, _, _ = self.fixture()
        for suffix in ("-wal", "-shm"):
            self.assertFalse((root / ("events.sqlite" + suffix)).exists())
        self.assertTrue(load_effective_summary(root)["scored_episode"])
        for suffix in ("-wal", "-shm"):
            self.assertFalse((root / ("events.sqlite" + suffix)).exists())

    def test_historical_scoring_catalog_and_report_preserve_sealed_wal_archive(self):
        from auriga.apps.library.experiments import Catalog, register_run
        from auriga.apps.evaluation.report import summarize
        root, _, evaluation, _, _ = self.fixture()
        register_run(root, category="benchmark", task="fixture")
        store = Store(root, {})
        try:
            store.db.execute("PRAGMA wal_autocheckpoint=0")
            store.set_state("runtime", "wal_fixture", {"committed": True})
            archive = self.base / "catalog" / "archive"
            shutil.copytree(root, archive, ignore=shutil.ignore_patterns("events.sqlite-shm"))
        finally:
            store.close()
        def fingerprint():
            return {path.relative_to(archive): (path.read_bytes(), path.stat().st_mtime_ns)
                    for path in archive.rglob("*") if path.is_file()}
        before = fingerprint()
        self.assertTrue((archive / "events.sqlite-wal").exists())
        direct = load_effective_summary(archive)
        catalog = Catalog(archive.parent)
        catalog.scan()
        for summary in (direct, catalog.list()["experiments"][0]["summary"], summarize(archive)):
            self.assertEqual(summary["normalized_score"], .4)
            self.assertFalse(summary["task_success"])
            self.assertEqual(summary["scoring_reconciliation"]["result_ref"], evaluation["result_ref"])
        self.assertEqual(fingerprint(), before)

    def test_import_preserves_original_summary_and_effective_scoring(self):
        from auriga.apps.library.experiments import Catalog, import_run
        for current in (False, True):
            with self.subTest(current=current):
                root, original, _, _, _ = self.fixture("current" if current else "historical")
                effective = load_effective_summary(root)
                if current:
                    original = effective
                    (root / "summary.json").write_text(json.dumps(original))
                before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
                copied = import_run(root, self.base / "imports", category="benchmark", task="fixture")
                self.assertEqual(json.loads((copied / "summary.json").read_text()), original)
                self.assertEqual(json.loads((copied / "runtime.json").read_text())["status"], "stopped")
                catalog = Catalog(self.base / "imports")
                catalog.scan()
                imported = next(entry["item"]["summary"] for entry in catalog.entries.values() if entry["path"] == copied)
                for key in ("task_success", "normalized_score", "primary_outcome", "scored_episode", "failure_reason",
                            "scoring_policy", "native_episode", "cleanup_status", "native_evaluation_snapshot"):
                    self.assertEqual(imported[key], effective[key], key)
                self.assertEqual(before, {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()})

    def test_unrelated_events_do_not_hide_evaluation_but_a_later_reset_invalidates_it(self):
        root, _, evaluation, _, _ = self.fixture()
        identity = {key: evaluation[key] for key in ("run_id", "episode_id", "env_instance_id", "env_generation")}
        store = Store(root, identity)
        try:
            for _ in range(4100):
                store.event("codex.item_delta", {"text": "unrelated terminal output"})
            self.assertTrue(load_effective_summary(root)["scored_episode"])
            store.event("environment.reset_requested", {})
            self.assert_unreconciled(root)
        finally:
            store.close()

    def test_evaluation_identity_and_latest_observer_identity_must_match_database(self):
        for source in ("evaluation", "observer"):
            with self.subTest(source=source):
                root, _, evaluation, _, _ = self.fixture(source)
                if source == "evaluation":
                    evaluation["episode_id"] = "foreign-episode"
                    (root / "evaluation.json").write_text(json.dumps(evaluation))
                else:
                    with sqlite3.connect(root / "events.sqlite") as database:
                        row = database.execute("SELECT seq,body FROM events ORDER BY seq DESC LIMIT 1").fetchone()
                        event = json.loads(row[1])
                        event["env_generation"] = 2
                        database.execute("UPDATE events SET body=? WHERE seq=?", (json.dumps(event), row[0]))
                self.assert_unreconciled(root)

    def test_actions_and_resets_after_evaluation_reject_historical_backfill(self):
        for event in ("action.requested", "environment.reset_requested", "environment.recovery_reset"):
            with self.subTest(event=event):
                root, _, _, _, _ = self.fixture(event, later_event=event)
                self.assert_unreconciled(root)

    def test_native_action_counter_and_latest_evaluation_reference_must_match(self):
        for mismatch in ("counter", "reference"):
            with self.subTest(mismatch=mismatch):
                root, summary, _, _, _ = self.fixture(mismatch)
                if mismatch == "counter":
                    summary["native_episode"]["native_action_count"] = 640
                    (root / "summary.json").write_text(json.dumps(summary))
                else:
                    with sqlite3.connect(root / "events.sqlite") as database:
                        row = database.execute("SELECT seq,body FROM events ORDER BY seq DESC LIMIT 1").fetchone()
                        event = json.loads(row[1])
                        event["payload"]["result_ref"] = "artifact:another-evaluation"
                        database.execute("UPDATE events SET body=? WHERE seq=?", (json.dumps(event), row[0]))
                self.assert_unreconciled(root)

    def test_artifact_content_and_digest_mismatches_reject_backfill(self):
        for defect in ("digest", "content", "foreign_identity", "missing_blob", "array_body"):
            with self.subTest(defect=defect):
                artifact_value = None
                if defect == "content":
                    artifact_value = dict(run_id="recorded-bottle-fixture", episode_id="episode-1",
                        env_instance_id="native-env", env_generation=1,
                        result={"valid_episode": True, "task_success": False,
                                "normalized_score": .9, "native_process_score": 90., "evaluator_actions": 641})
                elif defect == "foreign_identity":
                    artifact_value = dict(run_id="foreign-run", result={})
                elif defect == "array_body":
                    artifact_value = []
                root, _, _, _, blob = self.fixture(defect, artifact_value=artifact_value)
                if defect == "digest":
                    blob.write_bytes(blob.read_bytes().replace(b'0.4', b'0.9', 1))
                elif defect == "missing_blob":
                    blob.unlink()
                self.assert_unreconciled(root)

    def test_cached_verification_does_not_hide_later_artifact_corruption(self):
        root, _, _, _, blob = self.fixture()
        self.assertTrue(load_effective_summary(root)["scored_episode"])
        blob.write_bytes(blob.read_bytes().replace(b'0.4', b'0.9', 1))
        self.assert_unreconciled(root)


if __name__ == "__main__":
    unittest.main()
