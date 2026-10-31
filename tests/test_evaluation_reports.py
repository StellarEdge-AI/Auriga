"""Evidence readers preserve corrupt/missing coverage and unknown execution."""
import json
from pathlib import Path
import tempfile
import unittest

from auriga.core.storage.store import Store
from auriga.apps.library.experiments import Catalog, register_run
from auriga.apps.evaluation.audit import _owned_terminal_interrupts
from scripts.audit_run import audit
from scripts.benchmark_report import build_report


class EvidenceReportsTest(unittest.TestCase):
    def recovery_fixture(self, root, *, thread_id="main", events=(), raw=(), aliases=()):
        store = Store(root, dict(run_id=root.name, episode_id="episode", env_instance_id="env", env_generation=1))
        for payload in events:
            store.event("benchmark.actor_turn_continuation", payload)
        for original, alias in aliases:
            store.set_state("actor_id", alias, dict(original=original, alias=alias))
        store.close()
        (root / "manifest.json").write_text(json.dumps(dict(run_id=root.name, thread_id=thread_id,
            completion_guidance=dict(recovery_guidance=dict(version="configured-v3", instructions="private prose")))))
        (root / "summary.json").write_text(json.dumps(dict(status="unknown")))
        if raw is not None:
            (root / "codex-raw.jsonl").write_text("".join(json.dumps(value)+"\n" for value in raw))

    @staticmethod
    def recovery_attempt(prompt="recovery", version="actual-v2", thread_id="main"):
        return dict(thread_id=thread_id, prompt=prompt, recovery_context=dict(version=version))

    @staticmethod
    def native_start(prompt, thread_id="main", direction="sent"):
        return dict(direction=direction, message=dict(method="turn/start",
            params=dict(threadId=thread_id, input=[dict(type="text", text=prompt)])))

    def test_recovery_configuration_does_not_count_as_an_actual_continuation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.recovery_fixture(root, raw=[self.native_start("initial task")])
            value = build_report([root])["runs"][0]["recovery_guidance"]
            self.assertEqual(value, dict(configured_version="configured-v3", status="available",
                continuation_attempt_count=0, recovery_attempt_count=0, native_turn_start_count=1,
                native_sent_continuation_count=0, native_sent_recovery_count=0,
                unmatched_continuation_attempt_count=0, versions=[]))

    def test_recovery_event_without_native_submission_remains_an_unmatched_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.recovery_fixture(root, events=[self.recovery_attempt()], raw=[self.native_start("initial task")])
            value = build_report([root])["runs"][0]["recovery_guidance"]
            self.assertEqual(value["recovery_attempt_count"], 1)
            self.assertEqual(value["native_sent_recovery_count"], 0)
            self.assertEqual(value["unmatched_continuation_attempt_count"], 1)
            self.assertEqual(value["versions"], [dict(version="actual-v2", attempt_count=1, native_sent_count=0)])

    def test_recovery_counts_exact_owned_submissions_once_and_restores_saved_aliases(self):
        original = "obs-" + "a" * 32
        prompt = "Recovery context for " + original
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.recovery_fixture(root, aliases=[(original, "obs_7")],
                events=[self.recovery_attempt(prompt), self.recovery_attempt(prompt),
                        self.recovery_attempt("foreign only", thread_id="foreign"),
                        dict(thread_id="main", prompt="generic continuation")],
                raw=[self.native_start("initial task"), self.native_start("Recovery context for obs_7", "foreign"),
                     self.native_start("Recovery context for obs_7", direction="received"),
                     self.native_start("prefix Recovery context for obs_7"),
                     self.native_start("Recovery context for obs_7"), self.native_start("foreign only"),
                     self.native_start("generic continuation")])
            before = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
            value = build_report([root])["runs"][0]["recovery_guidance"]
            self.assertEqual(value["continuation_attempt_count"], 3)
            self.assertEqual(value["recovery_attempt_count"], 2)
            self.assertEqual(value["native_turn_start_count"], 5)
            self.assertEqual(value["native_sent_continuation_count"], 2)
            self.assertEqual(value["native_sent_recovery_count"], 1)
            self.assertEqual(value["unmatched_continuation_attempt_count"], 1)
            self.assertEqual(value["versions"], [dict(version="actual-v2", attempt_count=2, native_sent_count=1)])
            self.assertEqual(before, {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()})
            encoded = json.dumps(value)
            for private in (prompt, original, "obs_7", "private prose", "foreign only"):
                self.assertNotIn(private, encoded)

    def test_missing_recovery_native_log_or_main_thread_preserves_unknown_counts(self):
        for thread, raw, status in (("main", None, "native_log_missing"),
                                    (None, [self.native_start("recovery")], "main_thread_unknown")):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.recovery_fixture(root, thread_id=thread, events=[self.recovery_attempt()], raw=raw)
                value = build_report([root])["runs"][0]["recovery_guidance"]
                self.assertEqual(value["status"], status)
                self.assertIsNone(value["native_turn_start_count"])
                self.assertIsNone(value["native_sent_recovery_count"])
                self.assertIsNone(value["unmatched_continuation_attempt_count"])
                if thread:
                    self.assertEqual(value["recovery_attempt_count"], 1)
                    self.assertEqual(value["versions"], [dict(version="actual-v2", attempt_count=1, native_sent_count=None)])
                else:
                    self.assertIsNone(value["recovery_attempt_count"])

    def test_malformed_native_log_is_isolated_instead_of_becoming_zero_recovery_exposure(self):
        bad_records = ("{broken", "null\n", json.dumps(dict(direction="sent", message=[])),
            json.dumps(dict(direction="sent", message=dict(method="turn/start", params=[]))),
            json.dumps(dict(direction="sent", message=dict(method="turn/start", params=dict(threadId="main", input=[None])))))
        for raw in bad_records:
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as directory:
                good, bad = Path(directory)/"good", Path(directory)/"bad"
                self.recovery_fixture(good)
                self.recovery_fixture(bad)
                (bad / "codex-raw.jsonl").write_text(raw)
                report = build_report([good, bad])
                self.assertEqual(report["coverage"]["readable"], 1)
                self.assertEqual(report["coverage"]["unreadable"], 1)
                self.assertNotIn("recovery_guidance", report["runs"][1])
                self.assertIsNone(report["runs"][1]["task_success"])

    def test_native_audit_counts_only_interrupts_bound_to_an_owned_native_terminal(self):
        cases = (
            ("private_evaluator", "native_step_limit", "observer", "turn", "turn", "2026-01-01T00:00:02Z", "2026-01-01T00:00:01Z"),
            ("private_evaluator", "not_native", "observer", "turn", "turn", "2026-01-01T00:00:02Z", "2026-01-01T00:00:01Z"),
            ("actor", "native_step_limit", "observer", "turn", "turn", "2026-01-01T00:00:02Z", "2026-01-01T00:00:01Z"),
            ("private_evaluator", "native_step_limit", "actor", "turn", "turn", "2026-01-01T00:00:02Z", "2026-01-01T00:00:01Z"),
            ("private_evaluator", "native_step_limit", "observer", "turn", "other", "2026-01-01T00:00:02Z", "2026-01-01T00:00:01Z"),
            ("private_evaluator", "native_step_limit", "observer", "turn", "turn", "2026-01-01T00:00:04Z", "2026-01-01T00:00:01Z"),
            ("private_evaluator", "native_step_limit", "observer", "turn", "turn", "2026-01-01T00:00:02Z", "2026-01-01T00:00:03Z"),
            ("private_evaluator", "malformed", "observer", "turn", "turn", "2026-01-01T00:00:02Z", "2026-01-01T00:00:01Z"),
            ("private_evaluator", "native_failure", "observer", "turn", "turn", "2026-01-01T00:00:02Z", "2026-01-01T00:00:01Z"),
            ("private_evaluator", [], "observer", "turn", "turn", "2026-01-01T00:00:02Z", "2026-01-01T00:00:01Z"),
            ("private_evaluator", {}, "observer", "turn", "turn", "2026-01-01T00:00:02Z", "2026-01-01T00:00:01Z"),
        )
        for _ in cases:
            (terminal_source, terminal_reason, terminal_scope, interrupted_turn, interrupt_turn,
             terminal_at, started_at) = _
            with self.subTest(source=terminal_source, reason=terminal_reason,
                              scope=terminal_scope, interrupt_turn=interrupt_turn,
                              terminal_at=terminal_at, started_at=started_at), tempfile.TemporaryDirectory() as directory:
                store = Store(directory, dict(run_id="terminal-interrupt", episode_id="episode",
                                              env_instance_id="environment", env_generation=1))
                terminal = (None if terminal_reason == "malformed" else dict(
                    ended=True, success=False, reason=terminal_reason,
                    native_action_count=10, native_step_limit=10))
                reference = store.put_artifact(json.dumps(terminal).encode(), "native_terminal",
                                               "application/json", terminal_scope)
                store.event("environment.ended", {"result_ref": reference}, source=terminal_source)
                completed_message = {"method": "turn/completed", "params": {
                    "threadId": "thread", "turn": {"id": "turn", "status": "interrupted"}}}
                completed_ref = store.put_artifact(json.dumps(completed_message).encode(), "codex_event",
                                                   "application/json", "observer")
                store.event("codex.turn/completed", {"raw_ref": completed_ref}, {"thread_id": "thread"},
                            source="codex_app_server")
                raw = [
                    {"direction": "received", "received_at": started_at,
                     "message": {"method": "turn/started", "params": {
                         "threadId": "thread", "turn": {"id": "turn", "status": "inProgress"}}}},
                    {"direction": "sent", "received_at": "2026-01-01T00:00:03Z", "message": {"method": "turn/interrupt",
                     "params": {"threadId": "thread", "turnId": interrupt_turn}}},
                    {"direction": "received", "received_at": "2026-01-01T00:00:04Z", "message": {"method": "turn/completed",
                     "params": {"threadId": "thread", "turn": {"id": interrupted_turn,
                                                                       "status": "interrupted"}}}},
                ]
                events = store.events()
                for event in events:
                    if event["type"] == "environment.ended":
                        event["received_at"] = terminal_at
                result = _owned_terminal_interrupts(raw, events, store, "thread")
                valid = (terminal_reason == "native_step_limit" and terminal_source == "private_evaluator"
                         and terminal_scope == "observer" and interrupt_turn == interrupted_turn
                         and terminal_at == "2026-01-01T00:00:02Z" and started_at == "2026-01-01T00:00:01Z")
                self.assertEqual(result, {"turn"} if valid else set())
                store.close()
        self.assertEqual(_owned_terminal_interrupts([None, {"message": []}], [], None, "thread"), set())

    def test_invalid_nested_summary_is_isolated_in_report_and_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            good, bad = root / "good", root / "bad"
            for run in (good, bad):
                register_run(run, category="validation", task="fixture")
                Store(run, {}).close()
                (run / "manifest.json").write_text(json.dumps({"run_id": run.name}))
                (run / "summary.json").write_text(json.dumps({"status": "unknown"}))
            for field, value in (("scoring_policy", None), ("scoring_policy", []),
                                 ("scoring_policy", "legacy"), ("native_episode", "invalid")):
                with self.subTest(field=field, value=value):
                    (bad / "summary.json").write_text(json.dumps({"status": "unknown", field: value}))
                    report = build_report([good, bad])
                    self.assertEqual(report["coverage"], dict(requested=2, readable=1, unreadable=1, duplicate_run_ids=[]))
                    self.assertEqual(report["runs"][1]["report_error"]["type"], "ValueError")
                    self.assertIsNone(report["runs"][1]["task_success"])
                    self.assertIsNone(report["runs"][1]["token_accounting"]["tokens"]["total_tokens"])
                    catalog = Catalog(root)
                    catalog.scan()
                    self.assertEqual(catalog.list()["total"], 1)
                    self.assertEqual(catalog.list()["experiments"][0]["run_id"], "good")
                    self.assertEqual(len(catalog.errors), 1)
                    self.assertIn("summary_field_requires_object:" + field, catalog.errors[0]["error"])

    def test_integrity_rejects_dangling_observation_receipt_and_observer_references(self):
        sources = ("rgb_ref", "planned_trace_ref", "dispatched_trace_ref", "actual_trace_ref", "before_state_ref",
                   "evaluation_event", "video_event", "evaluation_file", "video_file")
        for source in sources:
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                identity = dict(run_id="fixture", episode_id="episode", env_instance_id="env", env_generation=1)
                store = Store(root, identity)
                reference = store.put_artifact(b"referenced evidence", "trace", "application/octet-stream",
                    "observer" if source.startswith(("evaluation", "video")) else "actor")
                store.save("observation", "obs", dict(observation_id="obs",
                    cameras=[{"rgb_ref": reference}] if source == "rgb_ref" else []))
                store.save("action_request", "action", dict(based_on_observation_id="obs"))
                store.save("action_receipt", "action", dict(status="unknown",
                    **{source: reference} if source.endswith("_ref") and source != "rgb_ref" else {}))
                if source.startswith(("evaluation", "video")):
                    video = source.startswith("video")
                    value = {"videos": [{"video_ref": reference}]} if video else {"result_ref": reference}
                    if source.endswith("event"):
                        store.event("observer.video_exported" if video else "observer.evaluated", value)
                    else:
                        (root / ("video.json" if video else "evaluation.json")).write_text(json.dumps(value))
                (root / "manifest.json").write_text(json.dumps(identity))
                store.close()
                self.assertTrue(audit(root)["passed"])
                store = Store(root, identity)
                with store.db:
                    store.db.execute("DELETE FROM artifacts WHERE id=?", (reference.removeprefix("artifact:"),))
                store.close()
                result = audit(root)
                self.assertFalse(result["passed"], result)
                self.assertFalse(result["original_bytes_complete"])
                self.assertTrue(any(reference in failure for failure in result["failures"]), result)
                self.assertEqual(result["unresolved_actions"], ["action"])

    def test_integrity_labels_verified_compacted_rgb_without_claiming_original_bytes(self):
        import io
        import random
        from PIL import Image
        from auriga.core.storage.image_archive import compact_images
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root, {})
            image = Image.frombytes("RGB", (128, 96), random.Random(8).randbytes(128 * 96 * 3))
            encoded = io.BytesIO()
            image.save(encoded, format="PNG")
            reference = store.put_artifact(encoded.getvalue(), "rgb", "image/png")
            store.save("observation", "obs", {"cameras": [{"camera_id": "head", "width": 128,
                "height": 96, "rgb_ref": reference}]})
            compacted = compact_images(store)
            self.assertEqual(compacted["originals_removed"], 1)
            video_path = store.artifact(compacted["segments"][0]["video_ref"])[1]
            store.close()
            (root / "manifest.json").write_text(json.dumps({"run_id": "compacted"}))
            result = audit(root)
            self.assertTrue(result["passed"], result)
            self.assertFalse(result["original_bytes_complete"])
            self.assertEqual(result["reconstructed_images"], [reference.removeprefix("artifact:")])
            self.assertEqual(result["verified_artifacts"], 1)
            video_path.unlink()
            self.assertFalse(audit(root)["passed"])

    def test_integrity_reports_invalid_compacted_image_mapping(self):
        for mapping in (["bad mapping"], "bad mapping", 1, False):
            with self.subTest(mapping=mapping), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                store = Store(root, {})
                reference = store.put_artifact(b"original RGB", "rgb", "image/png")
                metadata, path = store.artifact(reference)
                store.save("observation", "obs", {"cameras": [{"rgb_ref": reference}]})
                store.set_state("image_archive", metadata["artifact_id"], mapping)
                path.unlink()
                store.close()
                (root / "manifest.json").write_text(json.dumps({"run_id": "invalid-mapping"}))
                result = audit(root)
                self.assertFalse(result["passed"], result)
                self.assertFalse(result["original_bytes_complete"])
                self.assertEqual(result["reconstructed_images"], [])
                self.assertIn(reference + ":image_archive_mapping_requires_object", result["failures"])

    def test_bad_runs_do_not_hide_good_runs_or_become_zero_cost_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            good, bad, missing = [root / name for name in ("good", "corrupt", "missing")]
            with self.subTest(run="good"):
                store = Store(good, {})
                store.close()
                (good / "manifest.json").write_text(json.dumps(dict(run_id="good", model="custom-model")))
                (good / "summary.json").write_text(json.dumps(dict(status="unknown")))
            bad.mkdir()
            (bad / "manifest.json").write_text("{broken")
            before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
            report = build_report([good, bad, missing])
            self.assertEqual(report["coverage"], dict(requested=3, readable=1, unreadable=2, duplicate_run_ids=[]))
            self.assertEqual([row["report_status"] for row in report["runs"]], ["readable", "unreadable", "unreadable"])
            self.assertIsNone(report["runs"][1]["task_success"])
            tokens = report["token_accounting"]["tokens"]["total_tokens"]
            self.assertIsNone(tokens["total"])
            self.assertEqual(tokens["unknown_runs"], 3)
            self.assertFalse(missing.exists())
            after = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
            self.assertEqual(before, after)

    def test_integrity_does_not_require_astra_or_native_logs_and_preserves_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root, {})
            reference = store.put_artifact(b"evidence", "trace", "application/octet-stream")
            store.save("observation", "obs", dict(observation_id="obs"))
            store.save("action_request", "action", dict(based_on_observation_id="obs"))
            store.save("action_receipt", "action", dict(status="unknown"))
            _, path = store.artifact(reference)
            store.close()
            (root / "manifest.json").write_text(json.dumps(dict(run_id="integrity", model="another-model")))
            result = audit(root)
            self.assertTrue(result["passed"], result)
            self.assertEqual(result["unresolved_actions"], ["action"])
            self.assertFalse(audit(root, profile="native")["passed"])
            self.assertFalse((root / "events.sqlite-wal").exists())
            self.assertFalse((root / "events.sqlite-shm").exists())
            path.write_bytes(b"tampered")
            corrupt = audit(root)
            self.assertFalse(corrupt["passed"])
            self.assertIn("artifact_digest_mismatch", corrupt["failures"][0])


if __name__ == "__main__":
    unittest.main()
