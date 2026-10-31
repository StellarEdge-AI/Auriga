# Validation and Release Guide

Validation must demonstrate that changes behave correctly along their actual call paths and clearly state the scope of collected evidence. For minor documentation edits, checking links and content is sufficient; changes affecting permissions, physical execution, state transitions, archiving, or scoring require regression tests covering failure and edge paths. The CI workflow is defined in [`ci.yml`](../../.github/workflows/ci.yml).

## Environment and Standard Checks

Run all commands from the repository root. Development uses the Miniforge `Auriga` environment by default—never create a project `.venv` or upgrade an environment in place while live experiments are using it. Auriga requires Python >= 3.11 (CI runs in a clean environment with Python 3.12 and Node 22); dependency versions are governed by `environment.yml`, `requirements.lock.txt`, and `pyproject.toml`, with the Codex SDK/CLI currently pinned to `0.160.0`. Before running checks, verify the resolved SDK/CLI versions from the `Auriga` interpreter rather than relying on whatever `codex` binary is on `PATH` (see [Environment Setup](running.md#environment-setup)). `ffmpeg` is required for video-related checks.

```sh
# Skip environment creation if Auriga already exists; verify dependencies per the Running Guide first.
mamba env create -f environment.yml
npm ci --prefix web
mamba run -n Auriga python -m unittest discover -s tests
npm run check --prefix web
npm run build --prefix web
mamba run -n Auriga python scripts/build_package.py --output dist
mamba run -n Auriga python scripts/check_package.py dist/auriga_harness-0.2.0-py3-none-any.whl
git diff --check
```

When validating subsequent releases, pass the exact path to the newly built wheel so `dist/*.whl` does not inadvertently pick up older wheels. `check_package.py` installs the wheel into a temporary directory via `pip --target` using the current interpreter and verifies it outside the repository checkout under Python's `-I` isolated mode without creating a virtual environment. The CPU test suite requires neither a live model nor a GPU simulator; a few asset-mapping tests are skipped when an external RoboDojo checkout is absent. To exercise those asset tests, set `AURIGA_ROBODOJO_ROOT` and `AURIGA_ROBODOJO_PYTHON` to your local installation paths and re-run, recording pass and skip counts separately. Native app-server tests run against a local loopback model stub; if SDK/CLI versions do not match, switch to the proper environment rather than skipping failures.

## Targeted Regressions by Scope

Native Codex sandbox regressions with isolation enabled (`features.sandbox_enabled=true`, disabled by default) require Linux, a discoverable `bwrap` binary, and system `python3`; unprivileged user namespaces are preferred, falling back to `sudo -n` and `setpriv` when user namespaces are disabled on the host. These tests verify isolation boundaries using real files, subprocesses, and a loopback Responses stub while covering tool installation path variations and IPC directory configurations. Actual PyPI package downloads and authenticated upstream model calls are recorded separately and are never triggered implicitly by the CPU test suite. Regressions for `features.sandbox_enabled=false` must verify that the primary Codex session, terminal, evidence agent, and `program_run` start cleanly while sandbox probing/loading is forbidden, that private home directories, placeholder credentials, broker review, and resource limits remain enforced, and that cross-mode session resumption is rejected. Because host file and loopback network access are expected when the sandbox is disabled, test them against temporary files and local stubs—never report unsandboxed test passes as proof of OS isolation.

The table below lists targeted test modules under `tests/` (for example, `PYTHONPATH=tests mamba run -n Auriga python -m unittest test_tool_registry test_typed_actor_output`). When changes span multiple public boundaries or prepare a release, run the full CPU test suite after targeted tests pass.

| Change Scope | Primary Test Modules | Behavior to Verify |
| --- | --- | --- |
| `ToolSpec` / handlers / output | `test_tool_registry`, `test_typed_actor_output`, `test_program_review` | Both actor and program entry points, zero side effects on invalid inputs, role permissions, typed facts, and no action replay on post-execution presentation errors. |
| Actions / worker / stopping | `test_unified_actions`, `test_native_wait`, `test_runtime_lifecycle` | Budgets, idempotency, and checkpoints; `partial`/`stopped`/`unknown` handling; terminal capture failure preserves receipts and invalidates stale observations. |
| Scene evidence | `test_scene`, `test_scene_contracts`, `test_scene_service` | Per-entity provenance and clocks, dirty/freshness invalidation, and service/job lifecycle. |
| Store / scoring / archives / reports | `test_contract_store`, `test_read_models`, `test_scoring_policy`, `test_experiments`, `test_evaluation_reports` | Unchanged file bytes/hashes/mtimes on sealed WAL archives, corrupted archive isolation, fixed case denominator, complete references and scopes, and preserved scoring facts on import. |
| Evaluation queue and completion hook | `test_benchmark_queue`, `test_experiment_hook` | Exact `argv` recognition across current and legacy entry points, PID/start-ticks/output ownership checks, and OOM retry boundaries. |
| Web UI / read-only API | `npm run check --prefix web`, corresponding Python API tests, browser fixtures | Pagination, terminal state refresh, read-only replay, input injection guards, and mobile layout. |
| Config / assets / entry points / wheel | `test_config`, full wheel build and check | Out-of-checkout imports and CLI console scripts, default config bundling, static web assets, and provenance metadata. |
| Native Codex tools and permissions | `test_codex_sandbox`, `test_actor_source`, `test_codex_native_preferences`, `test_actor_isolation`, `test_program_isolation`, `test_codex_image_transport`, `test_codex_evidence_agent` | Real native shell/memory operations, host file/network denial when sandboxed, zero sandbox dependency with preserved broker/review/budgets when unsandboxed (default), resume mode consistency, source snapshot publishing and placeholder auth, and dynamic image transport. |

Use `find tests -name 'test_*.py'` (or `rg --files tests`) to check test module names before running, and add new regression tests to the existing test module owned by the affected subsystem. Fault-injection tests must assert persisted state facts and side-effect counts rather than merely matching error message substrings. If a historical minimal reproducer asserts buggy legacy behavior, keep it as baseline documentation rather than including it in the green regression suite.

## Browser and Native Evidence

The synthetic-only mode of `web/check-task-review.mjs` injects synthetic data in a headless browser without launching a model or robot. Build the web assets first, then run with an installed Playwright setup (setting `PLAYWRIGHT_MODULE` to your local module path if needed):

```sh
AURIGA_BROWSER=firefox \
AURIGA_URL=http://127.0.0.1:18787 \
AURIGA_PREVIEW_ASSETS="$PWD/web/dist" \
AURIGA_SYNTHETIC_ONLY=1 \
AURIGA_SCREENSHOT_DIR=/tmp/auriga-browser-check \
node web/check-task-review.mjs
```

This check validates synthetic Web UI states, interactive controls, and read-only boundaries, but does not prove live task replay synchronization or GPU execution. Changes to physical motion semantics additionally require running on explicitly authorized hardware with frozen source code, configuration, provider, model, task, seed, and layout, recording the complete action requests, execution receipts, measured trajectories, independent native evaluator scores, original/replay media, and audit results. Passing software tests, launching a queue, model self-reports, or producing a video file are never substitutes for verified task success, and prior experiments do not automatically serve as evidence for modified source code.

`audit_run.py --profile native` normally requires a completed Codex turn to satisfy session checks; if `Harness` interrupts an active turn after a native terminal state has already been recorded, the audit accepts it only when the archive simultaneously proves that the terminal state came from the private native evaluator, the turn had started on the same thread, and the owner's interrupt request occurred after the native terminal timestamp. Such turns are counted under `native_terminal_interrupted_turns` without incrementing `native_completed_turns` and do not imply task success; a standalone `interrupted` turn never satisfies the native audit. `--require-continuity` still requires at least two completed turns.

Evaluation reports preserve `null` for missing, invalid, or unknown runs against a fixed case denominator, detailing how many runs were scorable alongside their failure categories. Integrity audits, raw RGB byte verification, reconstructed image availability, and task evaluation scores are reported as separate dimensions. After importing legacy archives, verify both key native summary fields and byte-level immutability of the source directory.

In `benchmark_report.py`, per-run `recovery_guidance` distinguishes the manifest's `configured_version`, the count of `benchmark.actor_turn_continuation` attempts on the primary thread, and the count of matching `sent turn/start` requests in `codex-raw.jsonl`. After restoring short references via the saved alias map, the reporter performs an exact match between the full event prompt and the `text` input items of native requests: each request is matched at most once, and secondary threads, `received` log entries, or partial prompt substrings are excluded. `versions` breaks down attempt and sent counts by the recovery version actually recorded in each event rather than backfilling from the configured version, and separates standard continuations from continuations carrying recovery context. Reports never copy raw prompt bodies or hashes.

`native_sent_recovery_count` only proves that the log recorded a request submitting that continuation prompt to the native runtime—it does not prove that the upstream accepted the request, that the model followed the guidance, or that task performance improved. `unmatched_continuation_attempt_count` indicates recorded continuation attempts for which no corresponding native send entry was found. For older archives lacking raw logs or primary thread identity, sent counts remain `null`; when both the primary thread identity and complete readable logs exist with zero matching events, the count is `0`. Malformed JSON-RPC log entries remain isolated in the report denominator under existing corrupted-archive rules rather than being coerced to zero triggers (see `test_evaluation_reports`).

## Release Checklist

Before cutting a release, save any uncommitted work and branch baselines, confirming the independent changes on both target `main` and the branch being merged. Use a standard merge commit to preserve ancestry rather than `reset` or `force push`, resolving conflicts according to module ownership and test coverage. Existing release tags are immutable annotated tags and must never be moved.

On the final merged tree, bump the package version in `pyproject.toml`, update the release notes, and run the complete software, web, and wheel verification suite. Verify the wheel metadata version, the commits pointed to by the previous and new tags, that both the prior `main` HEAD and merged branch HEAD are ancestors of the new `main`, that features unique to `main` are preserved, and that the working tree is clean. Create the new annotated tag only on the verified commit, push normally, and inspect remote refs to confirm. Synchronizing a Git tag and package version does not publish to PyPI; external deployments and official benchmark runs are separate deliverables.
