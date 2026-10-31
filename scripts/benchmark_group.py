"""Expand a versioned benchmark group into an existing queue manifest, offline."""
import argparse
from collections import Counter
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import re

if __package__:
    from .benchmark_queue import command, queue_config, configured_manifest
else:
    from benchmark_queue import command, queue_config, configured_manifest

from auriga.core.config import QueueConfig

ROOT = Path(__file__).resolve().parents[1]


def build_manifest(group_id, run_root, *, pi05=None, max_parallel=None, config=None, partition=None):
    config = config or queue_config({})
    pi05 = config.features.pi05 if pi05 is None else pi05
    max_parallel = (config.queue.max_parallel or QueueConfig().max_parallel) if max_parallel is None else max_parallel
    config = config.with_overrides({"features": {"pi05": pi05}, "queue": {"max_parallel": max_parallel}})
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", group_id):
        raise ValueError("Invalid benchmark group name")
    if type(max_parallel) is not int or max_parallel < 1:
        raise ValueError("max_parallel must be a positive integer")
    directory = ROOT / "benchmarks" / group_id
    definition = (directory / "group.json").read_bytes()
    group = json.loads(definition)
    if group["schema_version"] != 1 or group["group_id"] != group_id:
        raise ValueError("Benchmark group identity/schema mismatch")
    raw = (directory / "cases.csv").read_bytes()
    if hashlib.sha256(raw).hexdigest() != group["cases_sha256"]:
        raise ValueError("Benchmark cases checksum mismatch")
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8"))))
    counts = Counter(row["task"] for row in rows)
    if (len(rows) != group["cases_per_method"] or len({r["case_id"] for r in rows}) != len(rows)
            or counts != Counter({task: group["episodes_per_task"] for task in group["tasks"]})):
        raise ValueError("Benchmark case coverage mismatch")
    if partition is not None:
        if (not isinstance(partition, dict)
                or set(partition) != {"group_id", "partition_id", "tasks"}
                or partition["group_id"] != group_id
                or not isinstance(partition["partition_id"], str)
                or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", partition["partition_id"])
                or not isinstance(partition["tasks"], list) or not partition["tasks"]
                or any(not isinstance(task, str) for task in partition["tasks"])
                or len(set(partition["tasks"])) != len(partition["tasks"])
                or not set(partition["tasks"]).issubset(counts)):
            raise ValueError("Invalid benchmark partition")
        rows = [row for row in rows if row["task"] in partition["tasks"]]
    run_root = Path(run_root).expanduser().resolve()
    cases = []
    for row in rows:
        for key in ("eval_seed", "layout_id", "reset_seed", "simulator_initial_seed", "policy_rng_seed", "max_control_steps"):
            row[key] = int(row[key])
            if row[key] < 0: raise ValueError("Negative case setting: " + key)
        row["control_dt_s"] = float(row["control_dt_s"])
        if row["max_control_steps"] == 0 or not math.isfinite(row["control_dt_s"]) or row["control_dt_s"] <= 0:
            raise ValueError("Control budget and dt must be positive and finite")
        if row["variant"] not in ("standard", "random") or not re.fullmatch(r"[A-Za-z0-9_]+", row["task"]):
            raise ValueError("Invalid task or variant")
        expected_id = f"{row['task']}__{row['variant']}__g{row['eval_seed']}__l{row['layout_id']}"
        if row["case_id"] != expected_id or row["reset_seed"] != row["layout_id"]:
            raise ValueError("Case identity/reset seed mismatch")
        task = row["task"] + ("_random" if row["variant"] == "random" else "")
        case = dict(cwd=str(ROOT), run_dir=str(run_root / row["case_id"]), task=task,
                    seed=row["eval_seed"], layout_id=row["layout_id"], pi05=pi05,
                    title=f"{group_id}: {row['case_id']}",
                    tag=[group_id, row["case_id"], "auriga-pi05" if pi05 else "auriga"])
        command(case, 0, config=config)  # Reuse the runner's validation; no GPU or subprocess.
        if partition is not None:
            case["tag"].append(partition["partition_id"])
        cases.append(case)
    manifest = dict(benchmark_group=group_id, group_definition_sha256=hashlib.sha256(definition).hexdigest(),
                comparison_scope=group["auriga_adapter"]["comparison_scope"],
                method="auriga-pi05" if pi05 else "auriga", reference_protocol=group,
                protocol_cases=rows, max_parallel=max_parallel, cases=cases)
    if partition is not None:
        manifest["benchmark_partition"] = partition
    return configured_manifest(manifest, config)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Unified Auriga JSON configuration")
    parser.add_argument("--group", required=True, help="Group directory under benchmarks/, e.g. gpt-as-policy-10")
    parser.add_argument("--partition", type=Path, help="JSON task partition within the benchmark group")
    parser.add_argument("--run-root", type=Path, required=True, help="Root for new case run directories")
    parser.add_argument("--output", type=Path, required=True, help="New queue manifest JSON; never overwrites")
    parser.add_argument("--pi05", action=argparse.BooleanOptionalAction, default=None, help="Enable Auriga pi05 tool; not the author's mix controller")
    parser.add_argument("--max-parallel", type=int)
    args = parser.parse_args()
    manifest = build_manifest(args.group, args.run_root, pi05=args.pi05, max_parallel=args.max_parallel,
                              config=queue_config({}, args.config),
                              partition=json.loads(args.partition.read_text()) if args.partition else None)
    output = args.output.expanduser().resolve()
    paths = [Path(case["run_dir"]) for case in manifest["cases"]]
    if any(output == path or path in output.parents or output in path.parents for path in paths):
        parser.error("Manifest output and case run directories must be distinct and non-nested")
    with output.open("x") as stream:
        stream.write(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(f"{args.group}: {len(manifest['cases'])} cases -> {output} (offline; no experiments started)")


if __name__ == "__main__":
    main()
