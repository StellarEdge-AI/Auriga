# Running Guide

This guide covers environment setup, single-episode execution, session resumption, replay, and batch benchmark evaluation. For an overview of the framework architecture, see the [README](../../README.md); for configuration fields and feature flags, see [Configuration](configuration.md); and for the current action interface, see [Unified Actions and Program Approval](unified-actions.md).

## Environment Setup

Harness development, tests, scripts, and services run in the Miniforge `Auriga` environment; do not create a project `.venv`. When the environment is not active, prefix commands with `mamba run -n Auriga python`, or explicitly activate `Auriga` first and run `python`. The robot worker invokes a separately installed RoboDojo / Isaac Sim environment (see [RoboDojo Runtime Environment](robodojo-setup.md)). Optional OpenPI policies also run in their own dedicated Python environment rather than merging simulation and policy dependencies into the Harness environment.

Before running, ensure you have:

- A complete Git checkout and the dependencies specified in `environment.yml` / `requirements.lock.txt`.
- An installed RoboDojo checkout, task assets, a compatible Isaac / CUDA runtime (see [RoboDojo Runtime Environment](robodojo-setup.md) for pinned revisions and patches), and an idle GPU.
- A local native Codex configuration with provider and authentication credentials capable of accessing the selected model; new experiments default to `~/.codex` with model `gpt-6.1-sol` and reasoning effort `high`. See [Configuration](configuration.md) for provider setup, authentication, and legacy compatibility.
- The OS sandbox is disabled by default (`features.sandbox_enabled=false`). When enabled, it requires a Linux host supporting Landlock ABI >= 4 and `libseccomp`; if isolation prerequisites are not met, the framework refuses to start rather than silently downgrading.
- When the sandbox is enabled, the primary Codex session additionally requires Linux, `bwrap`, system `python3`, C++ runtime libraries, and `ldd` / `ldconfig`, discovered from the current environment. Unprivileged user namespaces are preferred; `sudo -n` and `setpriv` are only used as a privilege-dropping fallback when user namespaces are disabled on the host. On Debian/Ubuntu, these correspond to `bubblewrap`, `util-linux`, `python3`, `libc-bin`, and `libstdc++6`, plus `ripgrep` to provide `rg` for native search tools. The sandbox runs under the current UID/GID with capabilities cleared and aborts startup if isolation is unavailable. Web search is disabled, and Python packages can only be installed into the private workspace through the controlled PyPI proxy; see [Configuration](configuration.md) for path and IPC details.

When the sandbox is disabled (default), the primary/auxiliary Codex sessions and `program_run` do not apply OS-level isolation, so executed code has the same host file and network permissions as the launching user while Harness observation projection, action admission, and program review remain active. To enable isolation for a new run, pass `--config` with `{"version": 1, "features": {"sandbox_enabled": true}}`; see [Code Execution Sandbox](configuration.md#code-execution-sandbox) for full details.

From the repository root, create the primary environment and build the web frontend (skip environment creation if `Auriga` already exists):

```sh
mamba env create -f environment.yml
mamba run -n Auriga python -c "import sys; from importlib.metadata import version; print(sys.executable); print('SDK', version('openai-codex')); print('CLI', version('openai-codex-cli-bin'))"
npm --prefix web ci
npm --prefix web run build
```

Auriga uses the native Codex binary bundled with the Python SDK rather than wrapper scripts on `PATH`, archiving the resolved SDK and CLI versions and binary paths with every run. Before starting or resuming a run, Auriga verifies that both package versions match the project's protocol snapshot; any mismatch raises `codex_version_mismatch` before stopping existing processes or launching a new app-server. Always update dependencies in the `Auriga` environment via `requirements.lock.txt` rather than only updating a global `codex` binary on `PATH`. For external RoboDojo revisions, patches, and the optional OpenPI environment, see [RoboDojo Runtime Environment](robodojo-setup.md).

To install Auriga into an isolated location, build a complete wheel using the script below. The build script stages all Python subpackages, compiled web assets, policy bridge scripts, and pinned reference materials in a temporary directory without mutating the checkout or deploying services. Always use `scripts/build_package.py` instead of plain `pip wheel .`, which would omit unbuilt web assets:

```sh
mamba run -n Auriga python scripts/build_package.py --output dist
mamba run -n Auriga python scripts/check_package.py dist/*.whl
mamba run -n Auriga python -m pip install dist/*.whl
```

`check_package.py` runs `pip install --no-deps --target` with the current interpreter into a temporary directory and validates the wheel outside the checkout using Python's `-I` isolated mode, without creating or using a `.venv`. Once installed in the `Auriga` environment, the console scripts `auriga`, `auriga-queue`, `auriga-report`, and `auriga-audit` are available. The legacy source scripts `scripts/benchmark_queue.py`, `benchmark_report.py`, and `audit_run.py` continue to forward to the same implementations. RoboDojo, Isaac, and optional policy environments remain configured externally; the wheel never bundles model weights, credentials, or external simulation environments. Fixed benchmark group generators and development probes continue to run from a full repository checkout.

The default configuration file is [`configs/auriga.json`](../../configs/auriga.json). You can override machine-specific defaults in a Git-ignored `configs/auriga.local.json`; keep provider configurations and API keys in an external Codex home directory outside the repository and store only its path in the local JSON (see [Configuration](configuration.md)). To customize machine paths or model settings, copy the relevant fields from [`configs/auriga.example.json`](../../configs/auriga.example.json) into your own configuration file. The default backend paths are `~/code/robodojo` and `~/miniforge3/envs/RoboDojo/bin/python`, which can also be overridden via `AURIGA_ROBODOJO_ROOT` and `AURIGA_ROBODOJO_PYTHON`.

```sh
# Inspect the resolved effective configuration without starting the robot, model, or server.
mamba run -n Auriga python -m auriga.apps.gateway \
  --config configs/my-experiment.json --print-config
```

Configuration values merge in the following order: class defaults -> compatible legacy environment settings -> repository default config -> local default config (`configs/auriga.local.json`, if present) -> explicit config file -> explicit CLI flags. Every new run snapshots its effective configuration, while resume and replay use the archived configuration. Model credentials and machine secrets must never be committed to version control.

## Launching a Single Run

Runs execute unattended by default. The model autonomously determines its strategy, tool parameters, and program action approvals, handling recovery and progression on its own without prompting a human operator. Native user-input requests immediately receive empty answers, and command/file approvals belonging to the current session are automatically approved with `acceptForSession`. When `features.sandbox_enabled` is enabled, additional permissions apply strictly inside the outer namespace and programs are isolated by Landlock. When disabled (default), native commands can access host files and network resources available to the launching user while Harness observation projection, action admission, and program review remain enforced. Manual stop controls remain available at all times.

Select an idle GPU on your machine and specify a fresh directory for the new run. The command below launches both the live model session and the robot simulation:

```sh
mamba run -n Auriga python -u -m auriga.apps.gateway \
  --benchmark --task general_pickup --seed 0 --layout-id 0 \
  --gpu 0 --port 8767 --run-dir runs/my-first-task
```

Open `http://127.0.0.1:8767` in your browser to view the robot camera feeds, native Codex terminal, executed actions, and run records. `--benchmark` uses the public native task instruction provided by the environment and runs according to native step limits and terminal conditions; upon completion, it runs the independent evaluator, exports videos, and releases hardware resources.

During development, you can run interface checks with a custom prompt and bounded budgets:

```sh
mamba run -n Auriga python -u -m auriga.apps.gateway \
  --gpu 0 --port 8767 --run-dir runs/my-interface-check \
  --prompt 'Observe the current scene, submit a plan, execute a small end-effector motion, and inspect the feedback.' \
  --max-actions 2 --wall-budget-s 900 --exit-after-turn
```

`--max-actions` and `--wall-budget-s` are restricted to development runs and are rejected in official `--benchmark` runs. Official benchmarks retain the model response budget and inactivity safeguards; for exact accounting and termination semantics, see [Configuration](configuration.md) and [Model-Led Operation](model-led-operation.md).

## Interaction, Stopping, and Resuming

Web UI interactions are submitted through `Harness` into the same native Codex thread, with write control coordinated through a lease. The web terminal displays native output, while new prompts and stop requests are handled by the gateway. Unattended requests are processed automatically and logged, so manual replies to already handled or expired approval/question prompts are rejected. When no browser session holds the write lease, you can also submit prompts via the CLI:

```sh
curl -fsS http://127.0.0.1:8767/v1/control \
  -H 'Content-Type: application/json' \
  --data '{"request_id":"review-1","command":"prompt","prompt":"Review the previous request and measured feedback; do not move the robot."}'
```

Each `request_id` must be unique; resending the exact same payload returns the original response, whereas reusing a `request_id` with different content is rejected. New prompts are accepted only when the previous turn and action have finished and the run is still accepting work.

Clicking Stop in the web UI or sending `command: "stop"` first closes action admission and requests cancellation, then interrupts the model; the final stop state is determined by actual execution receipts. Closing the browser tab does not stop the robot. Send `Ctrl-C` or `SIGTERM` to shut down the gateway service completely.

```sh
# Read-only replay requiring neither a GPU nor a model.
mamba run -n Auriga python -m auriga.apps.gateway \
  --replay --run-dir runs/my-first-task --port 8767

# Resume the original thread for a development run (creates a fresh physical episode).
mamba run -n Auriga python -m auriga.apps.gateway \
  --resume --run-dir runs/my-interface-check --port 8767
```

Resuming a run does not reconstruct prior physical simulator state—the reloaded environment starts a new episode/generation, and any still-running gateway or robot worker process blocks resumption. `--benchmark` cannot be combined with `--resume`; passing `--resume` alone reuses the archived benchmark settings, but the new episode does not constitute a continuous physical execution of the original rollout. `features.sandbox_enabled` must match the archived session mode; switching sandbox modes requires starting a new run. Use read-only `--replay` to inspect historical results.

## Experiment Library and Batch Evaluation

When `--run-dir` is omitted, new runs are automatically archived under `~/exp/auriga/runs/YYYY/MM/DD/category/task/run-id/` using UTC+8 dates. You can annotate runs with `--category`, `--title`, and `--tag`. The experiment library listens on `http://127.0.0.1:8780` by default, providing search, live workbench links, and read-only replay:

```sh
mamba run -n Auriga python -m auriga.apps.library
```

For persistent systemd deployment, raw archive imports, and remote access setup, see [Experiment Library](experiment-library.md). Cloning the source repository does not copy historical run archives, deploy services, or grant remote access.

The pinned benchmark suite [`gpt-as-policy-10`](../../benchmarks/gpt-as-policy-10/README.md) contains 50 cases. First generate a fresh manifest offline, then launch it through the evaluation queue (the output file and queue directory must not already exist):

```sh
mamba run -n Auriga python scripts/benchmark_group.py --group gpt-as-policy-10 \
  --run-root ~/exp/auriga/benchmark/gpt-as-policy-10/trial-001 \
  --output /tmp/gpt-as-policy-10-trial-001.json

mamba run -n Auriga python -m auriga.apps.evaluation.queue \
  /tmp/gpt-as-policy-10-trial-001.json \
  --output ~/exp/auriga/queues/gpt-as-policy-10-trial-001
```

The group protocol does not override your local model or reasoning effort settings. The queue manages GPU admission, concurrency, and fault recovery according to the resolved configuration while recording exact versions, seeds, observation profiles, and any protocol differences with each run. Use this group definition to evaluate changes across an identical case set; strict reproduction additionally requires verifying upstream revisions and all runtime conditions.

To aggregate statistics across completed runs offline without launching a robot or model:

```sh
mamba run -n Auriga python scripts/benchmark_report.py \
  /path/to/run1 /path/to/run2 --output /path/to/new-report.json
```

## Verification and Standalone Services

Run the standard verification suite from the repository root:

```sh
mamba run -n Auriga python -m unittest discover -s tests -v
npm --prefix web run check
npm --prefix web run build
```

To audit run evidence, execute `mamba run -n Auriga python scripts/audit_run.py <run-dir>`. By default, it checks record linkage and artifact integrity while preserving unconfirmed actions and without requiring a specific model or native logs. Passing `--profile native` additionally verifies native model calls, actual image delivery, executed actions, and completed turns; adding `--require-continuity` further requires multi-turn continuity. If original images were compacted and source bytes cannot be verified, the `native` audit profile fails explicitly. Neither audit profile replaces task scoring by the native evaluator.

`mamba run -n Auriga python scripts/benchmark_report.py <run-dir> ...` preserves per-directory errors for corrupted archives alongside overall readable coverage; missing token usage remains `unknown` rather than being treated as zero cost or task failure. The evaluation queue verifies source file byte digests at queue startup and before launching each case, halting further scheduling and cleaning up owned processes if source drift is detected. Always use a dedicated, unmodified checkout for official benchmark runs, as pre-launch checksum checks cannot make a writable working tree immutable at the OS level.

To run the real-robot validation matrix, execute `mamba run -n Auriga python scripts/robot_validation.py --gpu <idle-gpu> --run-dir <new-dir>`, which allocates a GPU and executes physical robot motions; passing CPU unit tests is not a substitute for real task validation. CI automatically runs the CPU test suite, schema checks, web frontend checks, and out-of-checkout wheel installation checks, while real GPU/model acceptance runs are archived separately as experiment records.

The standalone scene service can be started via `mamba run -n Auriga python -m auriga.perception.scene.scene_service --root <data-dir> --port 8770`, exposing its OpenAPI specification at `/v1/openapi.json`. Supported capabilities include importing permitted observations, bounded geometry reconstruction, versioning, and snapshots; full Isaac physics rollout is not implemented. This module holds no action or reset authority over the evaluated environment and is not exposed to new tasks as `scene_*` tools. For design documents and validation entry points, see the [Documentation Index](../README.md).
