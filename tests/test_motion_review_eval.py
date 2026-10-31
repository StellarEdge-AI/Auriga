import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from auriga.core.storage.store import Store
from scripts.motion_review_eval import ANALYZER_VERSION, CASES, PROFILE, prepare, sha, source_files, summarize, summarize_case, token_report, verify


class MotionReviewEvaluationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.source, self.native, self.experiment = (root / name for name in ("source", "native", "experiment"))
        for name in ("apps/gateway/cli", "apps/gateway/workbench", "robot/motion/motion_review"):
            path = self.source / "auriga" / (name + ".py")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# frozen test source\n")
        for task, _, seed, layout, *_ in CASES:
            for name in (f"task/RoboDojo/tasks/{task}.py", f"Assets/Eval_Layout/RoboDojo/arx_x5/{seed}/{task}_{layout}.json"):
                path = self.native / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}\n")
        with patch('auriga.core.config.LOCAL_CONFIG_PATH', root / 'absent.local.json'):
            prepare(self.source, self.experiment, self.native)
        self.manifest = json.loads((self.experiment / "manifest.json").read_text())
        self.protocol = json.loads((self.experiment / "protocol.json").read_text())

    def tearDown(self):
        self.temporary.cleanup()

    def test_pairs_have_only_intended_difference_and_isolated_empty_memory(self):
        self.assertEqual(self.protocol["model"]["model"], "gpt-6.1-sol")
        self.assertEqual(self.protocol["model"]["effort"], "high")
        self.assertEqual(self.protocol["model"]["codex_home"], str(Path("~/.codex").expanduser()))
        cases = self.manifest["cases"]
        self.assertEqual(len(cases), 12)
        self.assertEqual(len({case["grasp_memory_dir"] for case in cases}), 12)
        self.assertFalse(cases[0]["motion_review"])
        self.assertTrue(cases[1]["motion_review"])
        self.assertEqual(cases[0]["task"], "classify_objects_by_language")
        self.assertEqual(sum(pair["order"][0] == "control" for pair in self.protocol["pairs"]), 3)
        for pair in self.protocol["pairs"]:
            a, b = [case for case in cases if pair["pair"] in case["tag"]]
            self.assertEqual({key for key in a if a[key] != b[key]},
                             {"run_dir", "title", "tag", "motion_review", "grasp_memory_dir"})
            self.assertTrue(a["enable_wait"])
            self.assertEqual(a["completion_guidance"], "both")
            self.assertEqual(a["observation_profile"], PROFILE)
        self.assertTrue(all(not Path(case["grasp_memory_dir"]).exists() for case in cases))
        self.assertEqual(self.manifest["max_parallel"], 1)
        self.assertEqual(self.manifest["memory_per_case_mib"], 11264)
        self.assertEqual(self.protocol["execution"]["memory_per_case_mib"], 11264)
        self.assertEqual(self.manifest["oom_retry"], {"max_retries": 0})
        for name in ("pilot-manifest.json", "remaining-manifest.json"):
            phase = json.loads((self.experiment / name).read_text())
            self.assertEqual(phase["oom_retry"], {"max_retries": 0})
            self.assertEqual(phase["memory_per_case_mib"], 11264)
        self.assertEqual(self.protocol["execution"]["oom_retry"], {"max_retries": 0})

    def test_fixed_protocol_freezes_machine_config_before_queue_launch(self):
        from auriga.core.config import Config
        from scripts.benchmark_queue import queue_config
        path = Path(self.temporary.name) / "machine.json"
        path.write_text(json.dumps({
            "backend": {"python": "/machine/native/python", "root": "/wrong/native"},
            "model": {"name": "fixture-model", "effort": "low", "codex_home": "/machine/auth"},
            "features": {"enable_wait": False, "vision_compare": False, "default_task_notes": False,
                         "actor_payload_profile": "lean_v1", "disabled_tools": ["robot_preview_motion"]},
            "images": {"transport_format": "jpeg", "jpeg_quality": 87},
            "run": {"resume": True, "max_actions": 4, "prompt": "unrelated task"},
            "queue": {"max_parallel": 4, "memory_per_case_mib": 1, "oom_retry": {"max_retries": 2}},
        }))
        destination = Path(self.temporary.name) / "configured-experiment"
        prepare(self.source, destination, self.native, path)
        manifest = json.loads((destination / "manifest.json").read_text())
        protocol = json.loads((destination / "protocol.json").read_text())
        self.assertEqual(manifest["auriga_config"], protocol["auriga_config"])
        frozen = Config.from_dict(manifest["auriga_config"])
        self.assertEqual(frozen.backend.python, "/machine/native/python")
        self.assertEqual(frozen.backend.root, str(self.native))
        self.assertEqual(frozen.model.codex_home, "/machine/auth")
        self.assertEqual((frozen.model.name, frozen.model.effort), ("fixture-model", "low"))
        self.assertEqual(protocol["model"]["model"], "fixture-model")
        self.assertEqual(protocol["model"]["effort"], "low")
        self.assertEqual(protocol["model"]["codex_home"], "/machine/auth")
        self.assertEqual(frozen.features.actor_payload_profile, "full_v1")
        self.assertTrue(frozen.features.enable_wait)
        self.assertTrue(frozen.features.vision_compare)
        self.assertTrue(frozen.features.default_task_notes)
        self.assertEqual(frozen.features.disabled_tools, ())
        self.assertIsNone(frozen.run.max_actions)
        self.assertIsNone(frozen.run.prompt)
        self.assertFalse(frozen.run.resume)
        self.assertEqual((frozen.images.transport_format, frozen.images.jpeg_quality), ("jpeg", 87))
        self.assertEqual((frozen.queue.max_parallel, frozen.queue.memory_per_case_mib,
                          frozen.queue.oom_retry.max_retries), (1, 11264, 0))
        path.write_text('{"features": {"enable_wait": false}}')
        with patch("scripts.benchmark_queue.experiment_environment", side_effect=AssertionError("frozen")):
            self.assertEqual(queue_config(manifest), frozen)
        for name in ("pilot-manifest.json", "remaining-manifest.json"):
            self.assertEqual(json.loads((destination / name).read_text())["auriga_config"], frozen.to_dict())
        self.assertTrue(verify(destination)["valid"])

    def test_source_hashes_include_dependency_pins_config_and_vendored_source(self):
        names = ("requirements.lock.txt", "environment.yml", "configs/policy.yaml", "third_party/pinned/module.py", "third_party/pinned/README.md")
        for name in names:
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture\n")
        snapshot = source_files(self.source)
        self.assertTrue(set(names) <= snapshot.keys())

    def test_hash_verification_detects_source_and_native_drift_and_refuses_overwrite(self):
        self.assertTrue(verify(self.experiment)["valid"])
        (self.source / "auriga/apps/gateway/workbench.py").write_text("changed\n")
        native_file = next(iter(self.protocol["native_input_sha256"]))
        (self.native / native_file).write_text("changed\n")
        result = verify(self.experiment)
        self.assertFalse(result["valid"])
        self.assertIn("source:auriga/apps/gateway/workbench.py", result["differences"])
        self.assertIn("native:" + native_file, result["differences"])
        with self.assertRaises(FileExistsError):
            prepare(self.source, self.experiment, self.native)

    def test_missing_runs_remain_unscored_and_unknown(self):
        report = summarize(self.experiment)
        self.assertEqual(report["outcomes"]["control"],
                         dict(planned=6, valid_scored=0, successes=0, unscored_or_unknown=6))
        self.assertTrue(all(pair["outcome"] == "unresolved" for pair in report["pairs"]))
        for run in report["runs"]:
            self.assertIsNone(run["tools"])
            self.assertIsNone(run["task_success"])
            self.assertIsNone(run["token_usage"]["accounting"]["total"]["tokens"]["total_tokens"]["total"])

    def test_report_preserves_unscored_process_evidence_without_promoting_it(self):
        case = self.manifest["cases"][0]
        root = Path(case["run_dir"])
        root.mkdir(parents=True, exist_ok=True)
        partial = dict(normalized_score=0.4, native_process_score=40, task_success=False)
        (root / "summary.json").write_text(json.dumps(dict(status="stopped", valid_episode=True,
            scored_episode=False, primary_outcome="unscored_harness_timeout", normalized_score=None,
            task_success=None, native_process_score=40, partial_native_result=partial,
            termination_authority="harness")))
        value = summarize(self.experiment)
        row = next(item for item in value["runs"] if item["run_dir"] == str(root))
        self.assertIsNone(row["normalized_score"])
        self.assertIsNone(row["task_success"])
        self.assertEqual(row["native_process_score"], 40)
        self.assertEqual(row["partial_native_result"], partial)
        self.assertEqual(value["outcomes"][row["condition"]]["valid_scored"], 0)
        self.assertEqual(value["outcomes"][row["condition"]]["successes"], 0)

    def test_saved_preview_and_terminal_evidence_are_reported_without_visual_inference(self):
        for case in self.manifest["cases"][:2]:
            root = Path(case["run_dir"])
            identity = dict(run_id=root.name, episode_id="ep", env_instance_id="env", env_generation=1)
            store = Store(root, identity)
            try:
                store.save("tool_call", "call", dict(params=dict(tool="robot_preview_motion")))
                store.save("motion_preview", "plan", dict(review=dict(status="blocked")))
                store.event("motion.preview_failed", dict(status="ik_failed"))
                store.event("motion.preview_failed", dict(status="failed", reason="path_infeasible", planner_failure_stage="inverse_kinematics"))
                store.save("action_receipt", "ik", dict(status="failed", reason="path_infeasible", planner_failure_stage="inverse_kinematics"))
                store.save("action_receipt", "path", dict(status="failed", reason="path_infeasible", planner_failure_stage="path_planning"))
            finally:
                store.close()
            manifest = dict(model=self.protocol["model"]["model"], effort=self.protocol["model"]["effort"], observation_profile=PROFILE,
                            motion_review=case["motion_review"], actor_payload_profile="full_v1", enable_wait=True,
                            model_request_limit=170, completion_guidance=dict(mode="both"),
                            grasp_memory_dir=case["grasp_memory_dir"])
            summary = dict(valid_episode=True, scored_episode=True, task_success=case["motion_review"],
                           native_episode=dict(ended=True, native_step_limit=1100))
            (root / "manifest.json").write_text(json.dumps(manifest))
            (root / "summary.json").write_text(json.dumps(summary))
        report = summarize(self.experiment)
        self.assertEqual(report["pairs"][0]["outcome"], "review_win")
        run = report["runs"][1]
        self.assertTrue(all(value is True for value in run["configuration_checks"].values()))
        self.assertEqual(run["preview_statuses"], {"blocked": 1, "planning_failed:ik_failed": 1, "planning_failed:failed": 1})
        self.assertEqual(run["explicit_ik_failure_receipts"], 1)
        self.assertEqual(run["explicit_ik_failure_previews"], 1)
        self.assertEqual(run["failed_receipts_missing_reason"], 0)
        self.assertEqual(run["tools"]["robot_preview_motion"], 1)
        self.assertIsNone(run["visual_mechanisms"]["empty_lifts"])

    def test_saved_legacy_model_is_checked_without_reloading_future_defaults(self):
        config = Path(self.temporary.name) / "legacy.json"
        config.write_text(json.dumps({"model": {"name": "gpt-6-astra", "effort": "medium",
                                               "codex_home": "/historical/auth"}}))
        destination = Path(self.temporary.name) / "historical-experiment"
        prepare(self.source, destination, self.native, config)
        frozen = json.loads((destination / "manifest.json").read_text())
        case = frozen["cases"][0]
        run = Path(case["run_dir"])
        run.mkdir(parents=True)
        (run / "manifest.json").write_text(json.dumps(dict(model="gpt-6-astra", effort="medium")))
        (run / "summary.json").write_text("{}")
        with patch("auriga.core.config.Config.load", side_effect=AssertionError("history reloaded")):
            report = summarize(destination)
        self.assertTrue(report["runs"][0]["configuration_checks"]["model"])
        self.assertTrue(report["runs"][0]["configuration_checks"]["effort"])
        self.assertEqual(report["runs"][0]["expected"]["effort"], "medium")
        # Earlier protocol files recorded model/effort before unified snapshots.
        protocol_path = destination / "protocol.json"
        protocol = json.loads(protocol_path.read_text())
        del protocol["auriga_config"]
        protocol_path.write_text(json.dumps(protocol))
        with patch("auriga.core.config.Config.load", side_effect=AssertionError("history reloaded")):
            report = summarize(destination)
        self.assertTrue(report["runs"][0]["configuration_checks"]["model"])
        self.assertTrue(report["runs"][0]["configuration_checks"]["effort"])

    def test_attempt_ledger_keeps_unknown_usage_and_excludes_cumulative_double_count(self):
        root = Path(self.temporary.name) / "usage"
        (root / "private").mkdir(parents=True)
        events = [dict(kind="model_request_finished", logical_call_id="1", attempt_index=1,
                       source="actor", usage=dict(input_tokens=80, output_tokens=20, total_tokens=100)),
                  dict(kind="model_request_started", logical_call_id="2", attempt_index=1, source="actor")]
        (root / "private/model-requests-test.jsonl").write_text("\n".join(json.dumps(item) for item in events))
        report = token_report(root, {}, dict(token_usage=dict(total=dict(total_tokens=9999))))
        total = report["accounting"]["total"]["tokens"]["total_tokens"]
        self.assertEqual(total, dict(total=None, known_subtotal=100, known_runs=1, unknown_runs=1))

    def test_program_attempts_merge_identity_evidence_and_keep_actor_and_receipts_separate(self):
        case = self.manifest["cases"][0]
        root = Path(case["run_dir"])
        store = Store(root, dict(run_id=root.name, episode_id="ep", env_instance_id="env", env_generation=1))
        try:
            store.save("tool_call", "outer", dict(params=dict(tool="program_run", callId="outer")))
            # Equal names/arguments are separate calls; even an equal ID in the
            # other source must not erase a direct actor invocation.
            for identifier in ("op1", "actor2"):
                store.save("tool_call", identifier, dict(params=dict(tool="robot_move_ee", callId=identifier)))
            for identifier, tool, status in (("op1", "robot_move_ee", "completed"),
                                             ("op2", "robot_move_ee", "dispatching")):
                store.set_state("program_call", identifier, dict(program_id="job", operation_id=identifier, status=status))
                correlation = dict(program_id="job", tool_call_id=identifier)
                store.event("program.call_started", dict(tool=tool), correlation)
                store.event("program.call_started", dict(tool=tool), correlation)
                if status == "completed":
                    store.event("program.call_completed", dict(tool=tool), correlation)
            # A mirrored tool record is evidence for op1, not another attempt.
            store.save("tool_call", "mirror", dict(params=dict(tool="robot_move_ee"),
                correlation=dict(program_id="job", tool_call_id="op1")))
            store.event("program.call_started", dict(tool="geometry_query"),
                        dict(program_id="job", tool_call_id="event-only"))
            store.set_state("program_call", "unnamed", dict(program_id="job", status="dispatching"))
            store.save("action_receipt", "action1", dict(status="rejected", reason="fixture"))
        finally:
            store.close()
        (root / "manifest.json").write_text("{}")
        (root / "summary.json").write_text("{}")
        run = summarize_case(case)
        self.assertEqual(run["tool_attempts_by_source"], {
            "actor": {"program_run": 1, "robot_move_ee": 2},
            "program": {"robot_move_ee": 2, "geometry_query": 1, "unknown": 1}})
        self.assertEqual(run["all_tool_attempts"], {"program_run": 1, "robot_move_ee": 4, "geometry_query": 1, "unknown": 1})
        self.assertEqual(run["tools"]["robot_move_ee"], 4)
        self.assertEqual(run["receipt_statuses"], {"rejected": 1})
        self.assertEqual(run["tool_count_evidence"]["unnamed_program_attempts"], 1)
        self.assertEqual(run["tool_count_evidence"]["identity_coverage"], "complete")
        self.assertEqual(run["analyzer"]["version"], ANALYZER_VERSION)
        self.assertEqual(run["analyzer"]["sha256"], sha(Path(__file__).resolve().parents[1] / "scripts/motion_review_eval.py"))
        report = summarize(self.experiment)
        self.assertEqual(report["analyzer"], run["analyzer"])
        self.assertEqual(report["frozen_runtime_source"]["root"], str(self.source))
        self.assertEqual(run["evidence_errors"], [])


if __name__ == "__main__":
    unittest.main()
