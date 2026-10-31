# RoboDojo 10-Task Reproduction Protocol: GPT-as-Policy Alignment Panel

Verified: `2026-10-02`. This document specifies the original 10-task RoboDojo evaluation protocol used in **GPT-as-Policy** so that Auriga and other frameworks can evaluate and compare against the exact same case set. **Each method runs 50 episodes.**

## Scope and Published Results of the Original Study

The [GPT-as-Policy repository](https://github.com/anonymous-report-421/GPT-as-Policy/blob/main/README.md) evaluates two methods: `gpt` (**GPT-6 Astra Direct**) and `mix` (**Hybrid**: GPT-6 Astra reviewing and, when necessary, overriding candidate actions proposed by pi0.5). Both methods are evaluated on the **exact same 50 cases** (10 tasks × 5 cases per task), yielding **2 × 50 = 100 recorded evaluation episodes** in total (rather than 100 episodes per method or 100 distinct initial scenes). The published [per-episode records](https://github.com/anonymous-report-421/GPT-as-Policy/blob/main/public_results/evaluation_cases.json) list the `case_id`, method, task, scene variant, seed, binary success, and native process `Score`.

| Method | Successes / Episodes | Success Rate (SR) | Author-Reported Mean Native `Score` |
| --- | ---: | ---: | ---: |
| Direct (`gpt`) | 13 / 50 | 26% | 37.81 / 100 (computed over 48 cases with a recorded native `Score`) |
| Hybrid (`mix`) | 24 / 50 | 48% | 62.60 / 100 (all 50 cases have a recorded native `Score`) |

Both success rates use the fixed denominator of 50 episodes; the 2 Direct cases lacking a native `Score` **remain in the 50-episode SR denominator**. The authors additionally report that in the Hybrid (`mix`) condition, 14.4% of executed control steps used a GPT correction. Other baseline model figures cited in that repository are reweighted public leaderboard references rather than same-seed reruns. All figures above are the original authors' published results, not local reproductions from this repository.

## Evaluated Tasks and Cases (50 Episodes per Method)

In RoboDojo, `standard` and `random` denote distinct scene variants—even when sharing the same numeric `layout_id`, they represent **different physical cases**. The task ordering and layout assignments below follow the authors' [published case manifest](https://github.com/anonymous-report-421/GPT-as-Policy/blob/main/public_results/evaluation_cases.json), and [`cases.csv`](cases.csv) enumerates all 50 full `case_id` identifiers and seeds.

| Task ID | Capability Dimension | `standard` Layouts | `random` Layouts | Episodes / Method | Author Step Limit | Direct Successes | Hybrid Successes |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: |
| `organize_table` | Long-Horizon | 0–4 | — | 5 | 1000 | 0/5 | 0/5 |
| `classify_objects_by_language` | Open | 0–4 | — | 5 | 1100 | 2/5 | 1/5 |
| `imitate_sorting_sequence` | Memory | 0–4 | — | 5 | 1600 | 0/5 | 2/5 |
| `arrange_largest_number` | Generalization | 0–1 | 0–2 | 5 | 1050 | 2/5 | 2/5 |
| `pack_objects_into_box` | Generalization | 0–1 | 0–2 | 5 | 1300 | 1/5 | 1/5 |
| `classify_objects` | Long-Horizon | 0–4 | — | 5 | 1100 | 5/5 | 3/5 |
| `build_tower` | Precision | 0–4 | — | 5 | 1050 | 0/5 | 3/5 |
| `make_kong` | Long-Horizon | 0–4 | — | 5 | 600 | 0/5 | 2/5 |
| `fold_clothes` | Generalization | 0–1 | 0–2 | 5 | 500 | 2/5 | 5/5 |
| `put_bottles_into_dustbin` | Long-Horizon | 0–4 | — | 5 | 700 | 1/5 | 5/5 |
| **Total** | | | | **50** | | **13/50** | **24/50** |

**Case breakdown**: 7 non-Generalization tasks × 5 `standard` layouts = 35 cases; 3 Generalization tasks × (2 `standard` + 3 `random` layouts) = 15 cases; **35 + 15 = 50 cases**. Across all published records, `eval_seed=0`, `simulator_initial_seed=0`, `policy_rng_seed=0`, `reset_seed=layout_id`, and control timestep `dt=0.04 s`. Each `case_id` (for example, `fold_clothes__random__g0__l2`) encodes the task, scene variant, and layout index. Both methods are paired 1-to-1 by `case_id` and seed. Step limits reflect the native task horizons recorded by the authors; when evaluating another framework, log its active step limits and align both horizons and control timesteps for strict comparisons.

## Execution and Reporting Conventions

1. **Pin simulator code and assets**: Use a fixed RoboDojo commit, asset bundle, task evaluator, robot model, camera setup, and control timestep, and build the evaluation queue from [`cases.csv`](cases.csv) by `case_id`. The GPT-as-Policy [`SOURCE.json`](https://github.com/anonymous-report-421/GPT-as-Policy/blob/main/hybrid_rollout/robodojo/SOURCE.json) records the exact RoboDojo/OpenPI commits and pi0.5 checkpoint configuration used by the authors (simulation assets and model weights must be obtained from upstream). Runs on different simulator commits constitute a same-task re-evaluation rather than a bit-exact reproduction.
2. **Record full method metadata**: For each evaluated framework, pin and log the exact model identifier, reasoning effort, prompts/instructions, policy checkpoint weights, observation and action profiles, maximum control steps, timeouts, and recovery budgets. The original GPT-as-Policy codebase used Isaac Sim 5.1 and GPT-6 Astra at `xhigh` reasoning effort (plus OpenPI/JAX and pi0.5 weights for Hybrid), controlled via `ROLLOUT_EVALUATION_METHOD=gpt_only` (Direct) or `pi05_plus_gpt` (Hybrid); see their [execution guide](https://github.com/anonymous-report-421/GPT-as-Policy/blob/main/README.md#run-a-new-evaluation).
3. **Preserve complete per-case evidence**: For every `case_id`, archive raw `success`, native process `Score`, validity status, termination reason, executed native steps, model call count, wall-clock duration, and trajectory artifact paths. Flag simulator or asset crashes explicitly—never silently drop failed episodes from the denominator or backfill missing native scores as `0`.
4. **Report fixed-denominator metrics**: Report both **per-task successes out of 5** and **overall successes out of 50** (since all 10 tasks have 5 cases, the macro-average task SR equals overall SR). Report mean native `Score` alongside its valid sample count `n` rather than conflating `Score` with binary SR. Note that with 5 cases per task, a single episode outcome shifts a task's SR by 20 percentage points.

Reference links: [GPT-as-Policy Repository](https://github.com/anonymous-report-421/GPT-as-Policy) | [Published 100-Episode JSON Results](https://github.com/anonymous-report-421/GPT-as-Policy/blob/main/public_results/evaluation_cases.json).

## Running the `gpt-as-policy-10` Benchmark Group in Auriga

The registered benchmark group name is **`gpt-as-policy-10`**. This group combines the target protocol in [`group.json`](group.json) with the 50-case manifest in [`cases.csv`](cases.csv). When comparing two conditions or methods, generate two separate 50-episode queue manifests pointing to distinct run root directories paired by `case_id`.

**Model and reasoning effort follow your active Auriga configuration.** The group generator does not override Auriga's configured model or reasoning effort (nor does it force `xhigh`); the original authors' settings are stored purely as reference metadata while local reports record the actual model and effort served.

First, generate a queue manifest offline from the repository root (uses only the Python standard library and launches no GPUs, models, or hooks):

```sh
mamba run -n Auriga python scripts/benchmark_group.py --group gpt-as-policy-10 \
  --run-root ~/exp/auriga/benchmark/gpt-as-policy-10/auriga-001 \
  --output /tmp/gpt-as-policy-10-auriga-001.json
```

The `--output` path must not already exist. Every generated run directory and label preserves the full `case_id`: `standard` variants map to `<task>`, and `random` variants map to `<task>_random` (e.g., `fold_clothes_random`). The generated manifest embeds the complete target protocol metadata, source revisions, and file digests so no network access to GitHub is needed at runtime.

To launch the evaluation queue (`--output` specifies a fresh queue state directory that must not overlap with the per-case run directories):

```sh
mamba run -n Auriga python -m auriga.apps.evaluation.queue \
  /tmp/gpt-as-policy-10-auriga-001.json \
  --output ~/exp/auriga/queues/gpt-as-policy-10-auriga-001
```

Pass `--max-parallel N` during manifest generation to override the concurrency cap (which defaults to `queue.max_parallel = 60` from configuration; actual runtime concurrency is dynamically bounded by remaining cases, available GPU VRAM, and shared lease admission). Passing `--pi05` to `benchmark_group.py` generates a queue with Auriga's `robot_policy_step` pi0.5 tool enabled (use a separate manifest path and `--run-root`; note that enabling Auriga's `robot_policy_step` tool is distinct from the authors' `mix` rollout controller).

### Shared vs. Isolated Operation Memory

Standard benchmark group runs can pass `--config configs/benchmark-shared-memory.json` (see [`configs/benchmark-shared-memory.json`](../../configs/benchmark-shared-memory.json)), which sets `features.grasp_memory_dir` to `~/.local/share/auriga/grasp-memory` while inheriting all other repository defaults. When the model invokes `operation_memory` with `scope="shared"`, entries are persisted to `events.sqlite` in that shared directory and become retrievable by subsequent cases, whereas `scope="task"` entries remain isolated to the current episode. Shared memory never distills experiences automatically—the model must explicitly call `kind="record"` with valid action/observation provenance and applicability conditions.

Because shared memory allows later-starting episodes to read entries recorded by earlier episodes, execution order and concurrency become part of the experimental condition; manifests and reports using shared memory should log the shared directory path, initial database state, and cross-case learning policy.

### Scope of Protocol Alignment and Pre-Comparison Checklist

`benchmark_group.py` maps task/variant names, `eval_seed`, `layout_id`, and method switches directly into the queue manifest, and the RoboDojo worker uses `layout_id` as `reset_seed`. Additional author metadata in `cases.csv` is recorded under `reference_protocol` / `protocol_cases` for auditability rather than enforced by the queue runner. Static verification against the installed RoboDojo checkout confirmed matching native step horizons across all 13 task classes (`standard` + `random`) at a 25 Hz control frequency.

Before claiming a strict apples-to-apples reproduction against the original authors' environment, account for the following implementation differences:

- **Upstream source commits**: The authors' `SOURCE.json` pins RoboDojo `ee67a1468510da7624a089164402359f2afc72c8` and OpenPI `432f82b1758c5b1202e42a3dfe014546dbc50871`. `benchmark_group.py` does not check out external repositories or download simulator assets.
- **Simulator initialization seed**: Auriga's [`robot_worker.py`](../../auriga/robot/backends/robodojo/robot_worker.py) initializes the simulator with `[layout_id]`, whereas the authors recorded `simulator_initial_seed=0` alongside `reset_seed=layout_id`; passing `--seed 0` (`eval_seed`) alone does not unify every internal simulator RNG for non-zero layouts.
- **Policy RNG seed**: The authors' `policy_rng_seed=0` denotes the initial JAX PRNG key in a fresh OpenPI process with no warmup inference, which is not identically wired in Auriga's `robot_policy_step` worker.
- **Harness runtime boundaries**: Auriga runs under its standard [Runtime Configuration and Supervision Boundaries](../../docs/development/configuration.md): a 180-request model budget (`model_request_limit`), no per-request cutoff deadline, up to 12 backoff retries on explicit retryable API errors, a 900-second startup timeout, and an 1,800-second global inactivity timeout with no total episode wall-clock limit.

Accordingly, queues generated here are tagged as **`same_case_set_re_evaluation`** in their manifest metadata.

### Frozen Source Provenance and Statistical Verification

This protocol definition is pinned to GPT-as-Policy commit [`8f3d362b077d8efb77e2a7274d5b2c20e2243846`](https://github.com/anonymous-report-421/GPT-as-Policy/tree/8f3d362b077d8efb77e2a7274d5b2c20e2243846), with exact URLs and SHA-256 digests recorded in [`group.json`](group.json). Note that `evaluation.py` in that upstream commit defaults to a different 60-case panel; always use [`cases.csv`](cases.csv) to match the published 50-case results. Also note that in `classify_objects_by_language`, the category-to-basket assignment in the instruction varies by layout and must come live from the native environment at reset rather than a static prompt string.

Independent verification of the published `evaluation_cases.json` confirms:
- **Direct (`gpt`)**: `13 / 50` successes (26% SR); mean native `Score` over the 48 scored episodes = `37.8125 / 100` (the two episodes missing a native `Score` are `classify_objects_by_language__standard__g0__l1` and `pack_objects_into_box__random__g0__l2`, both retained in `cases.csv` and the 50-case denominator).
- **Hybrid (`mix`)**: `24 / 50` successes (48% SR); mean native `Score` over all 50 episodes = `62.60 / 100`.
(Raw scores in `evaluation_cases.json` are on a `0–1` scale and multiplied by 100 in the table above for readability; local archives preserve raw native evaluator units.)
