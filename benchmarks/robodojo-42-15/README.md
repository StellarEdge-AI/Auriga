# RoboDojo 42 Tasks × 15 Cases: 4-Partition Parallel Benchmark Protocol

Benchmark group name: **`robodojo-42-15`**. Each evaluated method runs **630 episodes** in total (42 base tasks × 15 distinct layouts per task), which can be split across four balanced task partitions (`partition-1.json` through `partition-4.json`) for parallel execution across multiple machines. Pass a partition file to `scripts/benchmark_group.py --partition` to generate that machine's queue manifest (partition files are inputs to `benchmark_group.py`, not runtime `--config` files for `auriga.apps.gateway`).

## Official Benchmark Scope and 15-Case Sampling Rule

Verified: `2026-10-09` against the [official RoboDojo repository](https://github.com/RoboDojo-Benchmark/RoboDojo/tree/726e9aabfaa642203722eb126f5eaf0f37f3e1ad) (`README.md`, `scripts/internal/task_inventory.py`, and `scripts/internal/summarize_result.py`), which defines **42 base tasks** across five capability dimensions: **12 Generalization**, **6 Memory**, **8 Precision**, **8 Long-Horizon**, and **8 Open** tasks. The 12 `<task>_random` environments are scene variants of the 12 Generalization tasks rather than separate base tasks. Exact upstream commit hashes and file digests are recorded in [`group.json`](group.json).

Whereas the full official RoboDojo leaderboard protocol runs 3 evaluation seeds (`0, 1, 2`) × 50 episodes per seed = 150 episodes per task (split 25 `standard` + 25 `random` per seed on Generalization tasks), **`robodojo-42-15` uses `eval_seed=0` across all tasks and selects 15 distinct scenes per task (1 episode per scene)**:

- **30 non-Generalization tasks**: `standard` layouts **`0–14`** (15 layouts per task).
- **12 Generalization tasks** (indexed `i = 0..11` in the order listed in `group.json`):
  - Even-indexed tasks (`i = 0, 2, ..., 10`): **8 `standard` layouts (`0–7`) + 7 `random` layouts (`0–6`)**.
  - Odd-indexed tasks (`i = 1, 3, ..., 11`): **7 `standard` layouts (`0–6`) + 8 `random` layouts (`0–7`)**.
- Because 15 is odd, alternating `8 + 7` and `7 + 8` across the 12 Generalization tasks yields an exact **90 `standard` + 90 `random`** split overall (1:1 ratio). Note that the same numeric `layout_id` under `standard` and `random` represents two completely different physical scenes.

[`cases.csv`](cases.csv) pins all 630 cases deterministically (`eval_seed=0`, 15 unique `(variant, layout_id)` pairs per task, with zero outcome-based filtering). `eval_seed` selects the official layout directory and `layout_id` selects the scene file within it; native environment resets and Auriga simulator initialization continue to use `layout_id` directly.

This single-seed 15-layout panel replaces the earlier 3-seed × 5-layout panel. Because generated queue manifests are frozen snapshots, switching to this panel requires generating a fresh manifest with new output and run directories. Task descriptions use the [Official Task Rules and Scoring Tables v2](../../docs/development/public-task-rules.md), injecting the verbatim website Instruction, Description, optional Comment, and full Scoring table alongside the live native instruction on every episode without altering the 630-case list or execution parameters.

This panel is a **630-episode (15-case/task) reduced benchmark** designed for controlled development and ablation; it must not be misrepresented as a full 150-case/task official leaderboard run.

## Four-Way Balanced Task Partitions

Each partition contains **3 Generalization**, **2 Precision**, **2 Long-Horizon**, and **2 Open** tasks, plus **1 or 2 Memory** tasks, balanced by total native control-step budget (step budgets provide a rough proxy for simulation horizon rather than model inference wall-clock time). Within each partition, cases preserve their canonical ordering from `cases.csv`:

| Partition File | Tasks | Cases (Episodes) | Total Native Control-Step Budget |
| --- | ---: | ---: | ---: |
| [`partition-1.json`](partition-1.json) | 11 | 165 | 123,750 |
| [`partition-2.json`](partition-2.json) | 11 | 165 | 126,000 |
| [`partition-3.json`](partition-3.json) | 10 | 150 | 120,000 |
| [`partition-4.json`](partition-4.json) | 10 | 150 | 122,250 |
| **Total** | **42** | **630** | **492,000** |

- **Partition 1 (11 tasks, 165 cases)**: `make_toast`, `store_laptop_and_headphones`, `pour_liquid_into_cup`, `build_tower`, `deposit_coin`, `fill_pen_holder`, `put_bottles_into_dustbin`, `match_and_pick_from_conveyor`, `swap_blocks`, `pour_by_language`, `solve_equation`.
- **Partition 2 (11 tasks, 165 cases)**: `pack_objects_into_box`, `stack_bowls`, `fold_clothes`, `pour_balls_into_vase`, `plug_in_charger`, `play_stacking_toy`, `fill_egg_holder`, `imitate_sorting_sequence`, `swap_T`, `pick_from_conveyor_by_image`, `general_pickup`.
- **Partition 3 (10 tasks, 150 cases)**: `arrange_largest_number`, `sweep_blocks`, `stack_blocks`, `insert_tubes`, `play_Xylophone`, `classify_objects`, `organize_table`, `cover_blocks`, `classify_objects_by_language`, `stack_blocks_by_language`.
- **Partition 4 (10 tasks, 150 cases)**: `sort_nesting_dolls_by_size`, `hang_mugs`, `push_T`, `fasten_screws`, `insert_key`, `play_tic_tac_toe`, `make_kong`, `press_by_number`, `store_tools_in_toolbox`, `align_blocks`.

## Generating Manifests and Launching Queues

Run the commands below from the root of the Auriga checkout on each worker machine, setting `PART=1`, `2`, `3`, or `4` and choosing a fresh `BATCH` identifier for each experimental run. First, generate the partition's queue manifest offline:

```sh
PART=1
BATCH=trial-001
PANEL=robodojo-42-15
RUN_ROOT="$HOME/exp/auriga/benchmark/$PANEL/$BATCH/partition-$PART"
MANIFEST="/tmp/$PANEL-$BATCH-partition-$PART.json"

mamba run -n Auriga python scripts/benchmark_group.py \
  --group "$PANEL" \
  --partition "benchmarks/$PANEL/partition-$PART.json" \
  --config configs/benchmark-shared-memory.json \
  --run-root "$RUN_ROOT" \
  --output "$MANIFEST"
```

`benchmark_group.py` verifies the master CSV checksum, case coverage, and partition membership, refuses to overwrite existing manifest files or run directories, and freezes the active model, tool, and queue settings into the generated manifest. [`configs/benchmark-shared-memory.json`](../../configs/benchmark-shared-memory.json) pins the primary model to **`gpt-6.1-sol` / `high`** across all four partitions while inheriting all other repository defaults. All four partitions must be generated from the exact same source revision, model, reasoning effort, observation profile, and `--config` file. Omit `--partition` if you want a single manifest containing all 630 cases.

Once the runtime environment is ready, launch the evaluation queue for that partition:

```sh
mamba run -n Auriga python -m auriga.apps.evaluation.queue \
  "$MANIFEST" \
  --output "$HOME/exp/auriga/queues/$PANEL-$BATCH-partition-$PART"
```

Queue concurrency defaults to `queue.max_parallel` (`60` in the repository configuration) and can be overridden at generation time via `--max-parallel N`; actual parallel execution is automatically bounded by remaining cases, available GPU memory, and shared lease admission. Keep `--run-root` and queue `--output` directories isolated per partition.

When using `configs/benchmark-shared-memory.json`, `features.grasp_memory_dir` points to `~/.local/share/auriga/grasp-memory`: queues running under the same user account on the same host share that SQLite store, whereas queues distributed across separate machines maintain machine-local stores unless explicitly synchronized. Always document initial memory state, sharing scope, and execution ordering in benchmark reports.

## Validation and Result Aggregation

Run the offline unit test suite to verify the 630 unique cases, 15 distinct layouts per task, `eval_seed=0` uniformity, Generalization `standard`/`random` balance, disjoint and exhaustive 4-partition coverage, and case-sensitive task name mappings (such as `play_Xylophone` and `swap_T`):

```sh
PYTHONPATH=tests mamba run -n Auriga python -m unittest test_benchmark_group test_queue_config
```

All 630 referenced layout JSONs, task YAMLs, and task Python modules have been verified against the upstream RoboDojo tree; see [RoboDojo Runtime Setup](../../docs/development/robodojo-setup.md) for host environment setup.

When aggregating results across partitions, use the fixed denominators: **15** per task, **165 / 165 / 150 / 150** per partition, and **630** overall. Deduplicate and verify completeness by full `case_id`; because partitions 1–2 have 165 cases and partitions 3–4 have 150 cases, compute the overall success rate by summing total successes and dividing by **630** (or averaging the 42 per-task success rates), never by taking an unweighted average of the four partition percentages. Keep missing, invalid, or unfinished cases in the fixed 630 denominator as `unknown`/`null`, and report mean native `Score` alongside its valid sample count `n`.
