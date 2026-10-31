# Experiment Completion Hook

Experiments are scheduled by a primary Codex session. Once a batch of experiments finishes, `scripts/experiment_hook.py` (implemented in `auriga/apps/evaluation/completion.py`) sends a single notification line back to that same primary Codex session:

```text
Experiment finished, results directory: /absolute/path/to/queue
```

Using its existing conversation context, the primary session reads the results, decides on improvements, and plans the next batch of experiments. The hook does not inject research strategies, output formatting rules, iteration counts, or development time limits. After launching a queue, the primary session ends its current turn; while the queue runs, only normal background processes execute, with zero model polling.

If the queue process has exited but its records are incomplete, the message instead reports that the stop status of some experiments remains unconfirmed. If scheduling has finished but clean shutdown was not confirmed or required forced reclamation, the message prompts the session to inspect the cleanup records. Both fallback messages still include the results directory path.

## Sending Notifications from the Queue

The target primary session UUID is resolved in the following order:
1. The CLI flag `--wake-thread UUID`
2. The unified configuration field `queue.wake_thread`
3. The UUID saved in `~/.config/auriga/completion-thread` (if present)

Passing `--wake-thread ''` explicitly disables notifications without reading `~/.config/auriga/completion-thread`; `--no-wake-enabled` or `queue.wake_enabled=false` likewise disables notifications. When no recipient is configured, the queue runs standalone. Experiment actors continue to read `codex_home` and `price_card` from `experiments.json` with credentials kept in the local profile; the notification recipient is stored separately so the two never interfere.

```sh
mamba run -n Auriga python scripts/benchmark_queue.py \
  /absolute/path/cases.json --output /absolute/path/new-queue \
  --wait-timeout-s 86400 --wake-thread MAIN_THREAD_UUID
```

The queue resolves and freezes the recipient before launch and sends a single notification after execution and cleanup complete. If the notification is not accepted (i.e., the receipt status is not `sent`), the queue exits with a non-zero code. GPU wait timeouts and per-benchmark budgets belong to the experiment configuration and are independent of the primary session's research turns.

`--wake-codex /absolute/path/to/codex` (config field `queue.wake_codex`, defaulting to `codex` on `PATH`) specifies the Codex executable used to send the notification, and `--wake-remote unix:///path/to/app-server.sock` (config field `queue.wake_remote`) specifies the app-server endpoint hosting the recipient session. Both options apply exclusively to completion notifications and do not alter the experiment actor's model or environment. If a wrapper script on `PATH` injects `--no-daemon`, it will conflict with the notification's `--remote` flag; pass the native binary path explicitly in that case. When an interactive TUI runs against a standalone daemon, notifications can resume the same thread UUID through a verified endpoint, though this does not directly wake an unattached TUI process. Always verify notification delivery after switching machines or endpoints.

GPU scheduling is handled by the background queue process: by default, every 90 seconds (configurable via `sample_interval_s` in the manifest) it inspects all visible GPUs, schedules pending cases based on utilization and available memory (allowing multiple cases per GPU), and re-verifies availability immediately before launching each case. By default, each case reserves `11264` MiB (11 GiB) with `0` MiB extra GPU headroom, adjustable via `memory_per_case_mib` and `gpu_headroom_mib` in the manifest. New queues default to `max_parallel=60`, with actual concurrency bounded by pending cases and resource admission; explicit config or manifest settings can override this ceiling. Completed case reclamation and stop signals are processed immediately without waiting for the next sampling interval.

Messages are submitted to the current user's shared app-server via native `codex queue --thread UUID --message TEXT --remote unix://...`. The default socket is `~/.codex/app-server-control/app-server-control.sock`, and the hook CLI accepts `--remote` to target another verified endpoint. Specifying an explicit endpoint prevents an experiment actor's isolated `CODEX_HOME` from routing the message to the wrong daemon. An idle session starts a new turn automatically, while an active session processes the queued message once its current turn finishes.

## Attaching to an Already Running Queue

For a queue that was started without notification flags, you can attach a one-shot watcher:

```sh
mamba run -n Auriga python scripts/experiment_hook.py \
  --queue-output /absolute/path/existing-queue --thread MAIN_THREAD_UUID \
  --pid QUEUE_PID --start-ticks PROC_START_TICKS
```

`--pid` and `--start-ticks` must be provided together; when both are omitted, the hook does not wait for a process and notifies immediately only if the queue has already written a terminal state. `--codex` and `--remote` have the same meaning as `--wake-codex` and `--wake-remote` on the queue CLI.

The watcher verifies the PID, `/proc` start ticks, queue command line (`benchmark_queue.py` or `-m auriga.apps.evaluation.queue`), and output directory before blocking on process exit via Linux `pidfd`. If the process has already exited, the watcher sends the normal notification only when the queue left a terminal state and no cases remain running. Normal terminal states include failure, interruption, and GPU wait timeout—missing individual case summaries do not prevent the primary session from being notified of a failure. If process exit is confirmed but queue records are incomplete, the watcher sends the fallback alert noting that stop status is unconfirmed for some runs.

Each queue uses an exclusive `.main-session-wake.json` claim file to prevent duplicate notifications, and writes the prompt, queue snapshot, command, CLI output, and receipt under `completion-hook/`. A `sent` status only confirms that the native queue accepted the message; whether the primary session executed a turn is determined by the native turn records. If delivery is interrupted or its outcome is ambiguous, `delivery_unknown` is recorded without automatic retries. No separate background research thread is created, and `codex exec resume` is never used to contend with the primary session.

## Verification

```sh
mamba run -n Auriga python -m unittest tests.test_experiment_hook -v
```

The test suite covers exact primary session targeting, post-success and post-failure notifications, duplicate/ambiguous delivery handling, real `pidfd` waiting, and rejection of still-running queue records.
