# Benchmark OOM Retries

`scripts/benchmark_queue.py` schedules a new episode for failures that **did not reach a valid native terminal state and have native GPU OOM evidence**. Normal policy failures, `native_step_limit`, model request budget exhaustion, and video/cleanup OOMs occurring after a completed episode are never retried. The initial unscored attempt is always preserved; retrying never rewrites it into a first-attempt success.

## Configuration and Scheduling

The queue manifest accepts an `oom_retry` block:

```json
{
  "oom_retry": {
    "max_retries": 2,
    "initial_delay_s": 60,
    "backoff_multiplier": 2,
    "max_delay_s": 300
  },
  "cases": []
}
```

Populate `cases` with your full case definitions (`cases` cannot be left empty in a real run). The example above shows the default policy: up to 2 retries (3 total attempts), waiting 60 seconds before the first retry and 120 seconds before the second. Setting `max_retries: 0` disables automatic retries, and the maximum allowed value is `20`; all other numeric fields must be finite and at least `1`. Backoff waiting does not consume the GPU wait timeout; once the backoff timer expires, the scheduler re-checks GPU utilization, free memory, shared reservations, launch rate limits, and manifest concurrency limits. The current defaults (11 GiB per case, 0 extra headroom, and dynamic slot backfilling) remain in effect, and other pending cases may launch while a failed case is cooling down.

Each retry launches a fresh gateway process preserving the original case's source checkout directory, task, seed, layout, guidance settings, and other parameters, while writing to a sibling directory of the original `run_dir` named `<run_dir>.oom-retry-01`, `.oom-retry-02`, and so on. If the target retry directory already exists, the launch is rejected—trajectories are never reused and `--resume` is never passed. Model request and action budgets are computed fresh for each new episode under the original rules without altering budget limits.

A retry is only permitted after the gateway process has exited and worker termination has been confirmed. Existing worker PID identity checks and lease isolation remain enforced; if worker exit cannot be confirmed, the GPU memory reservation is retained, the attempt is marked `stop_unconfirmed`, and the queue halts. `SIGINT` / `SIGTERM` cancels pending retries without triggering new attempts.

## Evidence and Reporting

OOM detection primarily checks for native error identifiers inside the structured `infrastructure_error` field of `summary.json`, such as `robot_gpu_out_of_memory:`.

Previously, `OutOfMemoryError` exceptions raised inside the worker by the motion planner (cuRobo / PyTorch) were caught by the worker and returned as `backend_error: OutOfMemoryError: ...` failure receipts or `OutOfMemoryError: ...` error responses, causing older versions to treat them as ordinary planning failures that could end the episode with a valid score of `0`. `RobotProcess` now inspects the exception type in the worker response (rather than matching quoted message text) alongside signatures in the worker log such as `CUDA out of memory` and `torch.OutOfMemoryError:`, classifying them identically to Vulkan/PhysX allocation failures under `robot_gpu_out_of_memory:`:
- The action request is treated as `unknown` and is not replayed.
- `wait_turn` stops the episode with `robot_gpu_out_of_memory`.
- `summary.json` records the same identifier in `infrastructure_error`, producing an `unscored_infrastructure` outcome eligible for OOM retry.

If the native episode had already reached its terminal state beforehand, the native terminal outcome takes precedence. The signature list in `auriga.robot.backends.robodojo.robot_process.native_gpu_oom_signature` is shared with queue evidence verification. For older runs, `bootstrap_failed` or worker disconnects can be corroborated by explicit CUDA, Vulkan, or PhysX allocation failures in `private/robot/robot-worker.log`. Only the last 1 MiB of the log file is read once upon failure exit, recording the source path, matched signature, byte offset, excerpt, and SHA-256 without repeated full-file scans. Neither actor conversation logs nor queue `stdout` are scanned, and generic `oom` substrings, `SIGKILL`, or exit code `137` alone are never treated as GPU OOMs. Allocation errors that scrolled past the 1 MiB log tail are not auto-retried.

`state.json` and `events.jsonl` persist every attempt and retry decision:

- Each entry in `cases` represents a distinct trajectory with `logical_task_id`, `attempt_number`, links to prior/subsequent runs, raw summary, OOM evidence, `retry_status`, backoff duration in seconds, and UTC epoch `retry_not_before`.
- `logical_tasks` aggregates attempts by logical task, reporting both `first_attempt_outcome` and `final_attempt_outcome` while preserving all attempts, `had_oom`, and launched retry counts.
- `retry_status` values (`pending`, `scheduled`, `exhausted`, `blocked_cleanup`, and `cancelled`) distinguish waiting, scheduled, exhausted, cleanup-blocked, and signal-cancelled states. The queue exits non-zero if any task exhausts its retries; if retries succeed, the queue can exit zero while retaining all historical OOM records and first-attempt outcomes in the report.

Legacy analysis scripts that iterate over `cases` row-by-row count individual attempts; logical task success rates must aggregate over `logical_tasks` and report both first-attempt and final valid-attempt metrics. Recovery queues also record original attempts under `prior_attempts`, including the source queue path and state/summary SHA-256 digests.

## Recovering Completed Legacy Queues

Active frozen queues continue running under their original scheduler without hot-patching source code or restarting. Once a queue finishes naturally, you can schedule OOM retries using the current scheduler:

```bash
python -m scripts.benchmark_queue \
  --recover-oom-from /absolute/path/to/old-queue \
  --output /absolute/path/to/new-oom-retry-queue \
  --wake-thread ''
```

Passing an empty string to `--wake-thread` explicitly disables completion notifications so the recovery queue does not read the default recipient from `~/.config/auriga/completion-thread` and notify a stale session.

Recovery selects only the last launched attempt of each logical task that has verified OOM evidence, confirmed gateway/worker exit, and remaining retry budget. Succeeded runs, normal task failures, budget terminations, and attempts with unconfirmed cleanup are excluded. During recovery, `summary.json` is re-read from disk and cross-checked against the queue's terminal state, rejecting mismatches so stale cached OOM labels cannot override a native terminal state. Source manifests, state files, summaries, trajectories, and protocols are read-only, while the new queue preserves the attempt chain and evidence hashes. Each case's `cwd` continues to point to the frozen experiment checkout, and the original gateway command (including Python interpreter, observation profile, and feature flags) is reused with only the GPU index and new run directory substituted, preventing newer scheduler defaults from altering historical protocols. The recovery queue's `--output` directory must not exist yet, and deterministic retry directory names prevent overwriting previously executed attempts. Existing pending retry records retain their original backoff deadlines; if an older queue lacked a retry timestamp, the backoff clock starts from the recovery invocation time.

Recovery and retries bind to the `settings_file` recorded on the original attempt and verify that `codex_home` and `price_card` paths match the original record, rejecting missing files or path changes. Credential contents are neither read into retry reports nor archived, and configuration/authentication file contents at those paths may still be updated by the user. If the original attempt explicitly ran without a settings file, the current default settings file is not injected; instead, the report records `inherited_environment_unrecorded` to indicate that the unarchived inherited environment cannot be reconstructed. Legacy queues lacking recorded gateway commands or experiment settings cannot be recovered automatically.

In-place checkpoint resumption of an existing queue directory is not supported. `--recover-oom-from` only accepts queues in a terminal state, rejecting active (`running`) queues, crashed queues with residual non-terminal state, or queues where any case is still marked `starting` or `running`; stale PIDs are never used to take over live processes. Attempts marked `stop_unconfirmed` cannot be cleared through this interface. If no qualifying OOM attempts remain, the command exits with `No cleanup-confirmed OOM attempts with retries remaining` without creating a new queue directory. Recovery is an explicit opt-in invocation and never triggers automatically after a user stops a queue.

When scheduling OOM recovery for an existing experiment batch, link the original queue, the recovery queue, and the retry policy in your experiment registry, preserving the protocol's rules on whether replacement attempts are permitted. Never restart a running experiment merely to upgrade the scheduler, and never run recovery when no OOM occurred.

## CPU Verification

`python -m unittest tests.test_benchmark_queue -q` covers baseline scheduling behavior as well as native OOM signatures, exclusion of normal failures and unknown kills, native terminal state precedence, unconfirmed-cleanup isolation, independent retry run directories and linkage, backoff timing and fresh GPU admission, wait-timeout boundaries, retry exhaustion, stop cancellation, terminal queue recovery, historical hash verification, and directory overwrite protection. All tests use synthetic GPU sampling and lightweight CPU stub processes without launching simulators, models, or real GPU allocations.
