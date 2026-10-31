"""The published 50-case panel must survive expansion without variant collisions."""
import csv
from collections import Counter
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import benchmark_group as group
from scripts import benchmark_queue as queue


class BenchmarkGroupCheck(unittest.TestCase):
    def test_parallel_default_and_explicit_overrides(self):
        with tempfile.TemporaryDirectory() as temp:
            for configured, explicit, expected in (({}, None, 60), ({'max_parallel': None}, None, 60),
                    ({'max_parallel': 2}, None, 2), ({}, 1, 1), ({'max_parallel': 2}, 3, 3)):
                with self.subTest(configured=configured, explicit=explicit):
                    config = queue.Config.from_dict({'queue': configured})
                    manifest = group.build_manifest('gpt-as-policy-10', Path(temp) / 'runs',
                        config=config, max_parallel=explicit)
                    self.assertEqual(manifest['max_parallel'], expected)
                    self.assertEqual(manifest['auriga_config']['queue']['max_parallel'], expected)
            self.assertFalse(any(Path(temp).iterdir()))

    def test_robodojo_single_seed_distinct_scenes_and_disjoint_partitions(self):
        directory = group.ROOT / "benchmarks/robodojo-42-15"
        with tempfile.TemporaryDirectory() as temp:
            full = group.build_manifest("robodojo-42-15", Path(temp) / "full")
            rows = full["protocol_cases"]
            self.assertEqual(len(rows), 630)
            self.assertEqual(len({r["case_id"] for r in rows}), 630)
            self.assertEqual(Counter(r["task"] for r in rows),
                             Counter({t: 15 for t in full["reference_protocol"]["tasks"]}))
            self.assertEqual(len(full["reference_protocol"]["tasks"]), 42)
            self.assertEqual({r["eval_seed"] for r in rows}, {0})
            self.assertEqual(full["reference_protocol"]["reference_protocol"]["selected_eval_seeds"], [0])
            for task in full["reference_protocol"]["tasks"]:
                scenes = [(r["variant"], r["layout_id"]) for r in rows if r["task"] == task]
                self.assertEqual(len(scenes), 15)
                self.assertEqual(len(set(scenes)), 15)
            for row in rows:
                self.assertEqual(row["reset_seed"], row["layout_id"])
                self.assertEqual(row["simulator_initial_seed"], row["layout_id"])
                if row["dimension"] != "Generalization":
                    self.assertEqual(row["variant"], "standard")
                    self.assertIn(row["layout_id"], range(15))
            generalization = [r for r in rows if r["dimension"] == "Generalization"]
            self.assertEqual(Counter((r["eval_seed"], r["variant"]) for r in generalization),
                             Counter({(0, v): 90 for v in ("standard", "random")}))
            tasks = {r["task"] for r in generalization}
            self.assertEqual(len(tasks), 12)
            totals = Counter()
            for task in tasks:
                counts = Counter(r["variant"] for r in generalization if r["task"] == task)
                totals[counts["standard"]] += 1
                self.assertEqual(sorted(counts.values()), [7, 8])
                for variant, count in counts.items():
                    self.assertEqual({r["layout_id"] for r in generalization
                                      if r["task"] == task and r["variant"] == variant}, set(range(count)))
            self.assertEqual(totals, {7: 6, 8: 6})
            selected = []
            for i, count in enumerate((165, 165, 150, 150), 1):
                partition = json.loads((directory / f"partition-{i}.json").read_text())
                manifest = group.build_manifest("robodojo-42-15", Path(temp) / f"p{i}", partition=partition)
                self.assertEqual(len(manifest["cases"]), count)
                self.assertEqual({r["task"] for r in manifest["protocol_cases"]}, set(partition["tasks"]))
                self.assertEqual(manifest["benchmark_partition"], partition)
                selected.extend(r["case_id"] for r in manifest["protocol_cases"])
                for row, case in zip(manifest["protocol_cases"], manifest["cases"]):
                    self.assertEqual(case["seed"], row["eval_seed"])
                    self.assertEqual(case["layout_id"], row["layout_id"])
                    self.assertEqual(case["task"], row["task"] + ("_random" if row["variant"] == "random" else ""))
                    self.assertIn(partition["partition_id"], case["tag"])
            self.assertCountEqual(selected, [r["case_id"] for r in rows])
            self.assertTrue({"push_T", "swap_T", "play_Xylophone"}.issubset({c["task"] for c in full["cases"]}))
            self.assertFalse(any(Path(temp).iterdir()))

    def test_partition_rejects_wrong_identity_unknown_and_duplicate_tasks(self):
        valid = dict(group_id="robodojo-42-15", partition_id="partition-1", tasks=["push_T"])
        invalid = [None, [], ["not_a_task"], ["push_T", "push_T"], [{}]]
        partitions = [{**valid, "tasks": tasks} for tasks in invalid]
        partitions += [{**valid, "group_id": "other"}, {**valid, "partition_id": "../other"},
                       {**valid, "extra": True}, []]
        with tempfile.TemporaryDirectory() as temp:
            for partition in partitions:
                with self.subTest(partition=partition), self.assertRaisesRegex(ValueError, "partition"):
                    group.build_manifest("robodojo-42-15", temp, partition=partition)

    def test_published_panel_seeds_budgets_and_runtime_mapping(self):
        limits = dict(organize_table=1000, classify_objects_by_language=1100,
                      imitate_sorting_sequence=1600, arrange_largest_number=1050,
                      pack_objects_into_box=1300, classify_objects=1100,
                      build_tower=1050, make_kong=600, fold_clothes=500,
                      put_bottles_into_dustbin=700)
        generalization = {"arrange_largest_number", "pack_objects_into_box", "fold_clothes"}
        expected = {(task, variant, layout) for task in limits
                    for variant, layouts in (("standard", range(2 if task in generalization else 5)),
                                            ("random", range(3 if task in generalization else 0)))
                    for layout in layouts}
        with tempfile.TemporaryDirectory() as temp:
            manifests = [group.build_manifest("gpt-as-policy-10", Path(temp) / method, pi05=pi05)
                         for method, pi05 in (("direct", False), ("with-pi05", True))]
            for manifest in manifests:
                self.assertEqual(len(manifest["cases"]), 50)
                rows = manifest["protocol_cases"]
                self.assertEqual({(r["task"], r["variant"], r["layout_id"]) for r in rows}, expected)
                self.assertEqual(len({c["run_dir"] for c in manifest["cases"]}), 50)
                self.assertEqual(manifest["comparison_scope"], "same_case_set_re_evaluation")
                self.assertIn("do not override", manifest["reference_protocol"]["auriga_adapter"]["method_settings"]["reasoning_effort"])
                for row, case in zip(rows, manifest["cases"]):
                    self.assertEqual([row[k] for k in ("eval_seed", "simulator_initial_seed", "policy_rng_seed")], [0, 0, 0])
                    self.assertEqual(row["reset_seed"], row["layout_id"])
                    self.assertEqual(row["control_dt_s"], .04)
                    self.assertEqual(row["max_control_steps"], limits[row["task"]])
                    self.assertTrue(row["native_instruction"])
                    self.assertEqual(Path(case["run_dir"]).name, row["case_id"])
                    self.assertIn(row["case_id"], case["tag"])
                    command = queue.command(case, 3)
                    self.assertEqual(command[command.index("--seed") + 1], "0")
                    self.assertEqual(command[command.index("--layout-id") + 1], str(row["layout_id"]))
                    self.assertEqual(command[command.index("--task") + 1],
                                     row["task"] + ("_random" if row["variant"] == "random" else ""))
                    self.assertEqual("--pi05" in command, case["pi05"])
                    self.assertNotIn("--task-notes", command)
                    self.assertNotIn("--model", command)
                    self.assertNotIn("--reasoning-effort", command)
            self.assertEqual(manifests[0]["protocol_cases"], manifests[1]["protocol_cases"])
            self.assertFalse(any(Path(temp).iterdir()))

    def test_cli_only_writes_manifest_and_refuses_overwrite_or_used_case(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output, run_root = root / "queue.json", root / "runs"
            argv = ["benchmark_group.py", "--group", "gpt-as-policy-10", "--run-root", str(run_root),
                    "--output", str(output), "--max-parallel", "2"]
            with mock.patch("sys.argv", argv), mock.patch("builtins.print"), \
                 mock.patch.object(queue, "sample_gpus", side_effect=AssertionError("GPU probe")), \
                 mock.patch.object(queue.subprocess, "Popen", side_effect=AssertionError("process launch")):
                group.main()
                manifest = json.loads(output.read_text())
                self.assertEqual(manifest["max_parallel"], 2)
                self.assertFalse(run_root.exists())
                original = output.read_bytes()
                with self.assertRaises(FileExistsError): group.main()
                self.assertEqual(output.read_bytes(), original)
            invalid_root = root / "file-as-run-root"
            with mock.patch("sys.argv", ["benchmark_group.py", "--group", "gpt-as-policy-10",
                                         "--run-root", str(invalid_root), "--output", str(invalid_root)]), \
                 mock.patch("sys.stderr"), self.assertRaises(SystemExit):
                group.main()
            self.assertFalse(invalid_root.exists())
            Path(manifest["cases"][0]["run_dir"]).mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, "already exists"):
                group.build_manifest("gpt-as-policy-10", run_root)
            with self.assertRaises(ValueError): group.build_manifest("../gpt-as-policy-10", run_root)
            with self.assertRaises(ValueError): group.build_manifest("gpt-as-policy-10", run_root, max_parallel=0)

    def test_csv_is_pinned_and_includes_missing_score_cases(self):
        directory = group.ROOT / "benchmarks/gpt-as-policy-10"
        with (directory / "cases.csv").open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        ids = {row["case_id"] for row in rows}
        self.assertIn("classify_objects_by_language__standard__g0__l1", ids)
        self.assertIn("pack_objects_into_box__random__g0__l2", ids)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy = root / "benchmarks/gpt-as-policy-10"
            copy.mkdir(parents=True)
            (copy / "group.json").write_bytes((directory / "group.json").read_bytes())
            (copy / "cases.csv").write_bytes((directory / "cases.csv").read_bytes().replace(b",random,", b",standard,", 1))
            with mock.patch.object(group, "ROOT", root), self.assertRaisesRegex(ValueError, "checksum"):
                group.build_manifest("gpt-as-policy-10", root / "runs")


if __name__ == "__main__":
    unittest.main()
