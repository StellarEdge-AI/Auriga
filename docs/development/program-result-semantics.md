# Program Replies, Action Completion, and Native Task Outcomes

Python code running inside `program_run` must inspect the inner receipt of every action. An outer `success=true` returned by `client.call(...)` only indicates that tool dispatch returned normally; an action that was rejected, failed planning, stopped after partial execution, or ended with an unknown outcome can still return `success=true` at the dispatch envelope. Similarly, a Python process exit code of `0` proves neither action completion nor native task success.

This requirement comes directly from a real failure mode: a program set both its overall job budget and each per-action timeout to 120 seconds, causing all 25 actions to be rejected for insufficient remaining budget, yet reported program success because it only checked outer `success`. This document specifies the current program contract; frozen historical experiments retain their original interfaces and archived evidence.

## Internal Broker Evidence

`ProgramRunner` reads `ToolResponse.kind/data` before external presentation formatting: cached plans use `plan`, raw action receipts use `action`, and live observations use `observation`. Action review, action summaries, and terminal observation tracking no longer deserialize `contentItems`. If the internal typed action payload is missing, the outcome is recorded as `unknown`—never upgraded to completed even if display text says `completed`. `AurigaClient.call()` returns `kind` and `data` alongside the compatibility `success` and `contentItems` fields so programs consume structured domain values directly rather than parsing execution status from display text. Data first passes through the producer-specified metadata projection and then through shared provenance filtering and reversible short-reference resolution, without exposing raw observer archives. Legacy display-only replies are marked `kind="untyped", data=null` and must never have receipts fabricated from text. Idempotent replay of an earlier call returns the saved reply without re-executing motion or upgrading legacy text into typed evidence.

Programs invoke tools through frozen `ProgramServices` callbacks; `ProgramRunner` holds no direct reference to `Harness`, `robot`, or `ToolRegistry`. `Harness` binds the `program` role on the server side so callers cannot escalate privileges via tool arguments. Both live program calls and the actor catalog share the same `ToolRegistry` schema, role, and runtime capability checks. Physical actions require primary-model review by default; any new `physical` plugin without a built-in review flow can only be called if that program run was pre-authorized with `allow_direct_actions=true`. Legacy motion/`scene_*` tool names and nested `program_run` calls remain forbidden. See [Tool Registration and Extensions](tool-plugins.md).

## Reading the Four Result Layers Separately

| Layer | Actual Field or Return Shape | What It Establishes |
| --- | --- | --- |
| Program IPC | `AurigaClient._request` reads internal `{ok, result}` and raises on `ok=false` | Whether the broker delivered a reply; `client.call` returns the inner `result` dict directly, so do not index `r["result"]` again |
| Tool dispatch | `r = {success, kind, data, contentItems}` | When `kind="error"`, `data` holds call diagnostics; when `success=true`, you must still inspect `kind` and the inner receipt (`output_contract_error` indicates a delivery/contract issue and forbids automatic replay) |
| Physical action | When `kind="action"`, read `r["data"]["receipt"]` (single receipt layer) | Inspect `receipt.status`, `reason` / `error`, executed step counts, `stop_confirmed`, and `result_observation_id`; `completed` means only that this specific action finished |
| Program & task | Job `status`, `returncode`, and `stop_confirmed` from `program_run` / `program_status`; native task outcome from the environment | Program `succeeded` means only that the Python process exited with `0` (`failed`, `cancelled`, and `unknown` must never be relabeled as success); neither action `completed` nor program `succeeded` establishes native task completion |

`receipt.status` is not a boolean. Only an explicit `"completed"` indicates action completion; `"accepted"`, `"failed"`, `"stopped"`, `"rejected"`, `"unknown"`, or any missing/unrecognized status must halt subsequent actions in the program and return control to the primary model. A `"stopped"` status may be caused by native episode termination, cancellation, or an observation checkpoint—never treat it as task success on your own. Likewise, never automatically replay an action after an IPC exception or an `"unknown"` outcome.

A planning failure does not guarantee zero physical motion: a multi-waypoint or multi-stage action may have executed earlier segments before failing. Always preserve the complete receipt, especially `executed_native_steps`, `completed_native_steps`, `executed_control_steps`, `execution_interval`, `failed_waypoint_index`, `waypoint_results`, `stop_confirmation`, and trace references. Fields may be absent on some failure paths; a missing field must never be interpreted as zero executed steps or confirmed stopping. For the exact meaning of `executed_steps`, check `step_semantics` on the receipt rather than equating program tool-call counts with simulation steps.

On `path_infeasible`, `receipt.failure_feedback` also provides the failed waypoint's requested grasp target, converted end-effector target, solver residuals, and available candidate joint/collision diagnostics; programs should preserve these fields in full when returning control to the model. For field definitions, coordinate frames, and evidence boundaries, see [Motion Planning Failure Feedback](motion-failure-feedback.md).

## Example: Inspecting an Action Reply

The helper below only inspects an already returned action reply—it never selects targets, dispatches motion, or retries automatically. If the action did not complete or lacks a current terminal observation reference, it prints the diagnostic evidence and exits with a non-zero code to return control to the primary model. Even when it returns normally, that only establishes "the action completed and has a current observation reference"; the caller must still inspect measured feedback and RGB images before proceeding.

```python
import json


def inspect_action_reply(reply):
    def return_control(reason, evidence):
        print(json.dumps({"decision": "return_control", "reason": reason,
                          "evidence": evidence}, ensure_ascii=False))
        raise SystemExit(1)

    try:
        result = reply.get("data")
        if reply.get("kind") != "action" or not isinstance(result, dict):
            return_control("missing_action_evidence", reply)
        receipt = result.get("receipt")
        if not isinstance(receipt, dict):
            return_control("missing_receipt", result)
        if receipt.get("status") != "completed" or receipt.get("error"):
            return_control("action_not_completed", result)
        if reply.get("success") is not True or reply.get("output_contract_error"):
            return_control("delivery_or_contract_error_do_not_replay", result)
        if receipt.get("execution_uncertain") or receipt.get("stop_confirmed") is False:
            return_control("execution_not_confirmed", result)
        after = result.get("current_observation_id") or receipt.get("result_observation_id")
        if not after or result.get("result_observation_is_current") is False:
            return_control("current_observation_unavailable", result)
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        return_control("unrecognized_reply", {"error": str(error)})
    return receipt, after
```

Call `receipt, observation_id = inspect_action_reply(reply)` where `reply` is the return value of a completed `client.call(action_tool, arguments)`. Let any exception raised by `client.call` propagate back to the primary actor as well rather than catching it and issuing further actions. This pattern never relabels a failed or unknown receipt as a success; `SystemExit(1)` marks the program job as `failed` while preserving the exact action statuses in program output and the `Store`.

In a full wire reply, subsequent `inputText` items carry `{bundle, robot_state}` and `inputImage` items carry RGB frames. Match the terminal frame using `receipt.result_observation_id`—never mistake an intermediate `temporal_sample` from `robot_wait` for the final observation, and never reuse the pre-action `observation_id`. A terminal observation attached to a receipt reflects the state at capture time; if later operations advance the environment, that observation becomes stale, and subsequent actions are still validated against the gateway's live current observation.

Program sub-calls deliver images to `actor_program` first. `program_run` and `program_status` automatically forward `inputImage` items returned by read-only or preview tools inside the program—including historical observations, motion previews, and geometry projections—as standalone image items to the primary actor, alongside review-pending preview overlays and terminal observations. Programs simply call the relevant tool without printing `imageUrl` or base64 data to `stdout`.

Each forwarded image's `visual_feedback` entry records the originating tool, sub-call ID, arguments, and saved `reply_ref` so the actor knows which operation produced the image and how to inspect the full reply. Forwarding preserves the tool's original `imageUrl`, ROI, and label without re-encoding, maintaining the exact encoding quality from capture and storage. Observation IDs, crop coordinates, and capture timestamps on historical images remain bound to the original frame and never become the current observation simply by being re-forwarded. To evaluate new physical state, the actor must verify stopping/execution status and call `robot_observe` as needed; a historical image cannot confirm that an in-flight action has stopped. Even fresh RGB frames, joint arrival, and completed gripper commands do not by themselves prove grasp retention, physical contact, or native task completion.

## Model-Facing Summaries and Full Reads

The shared [`tool_output.py`](../../auriga/core/tool_output.py) module standardizes output construction, `detail` selection, bounded text excerpts, and pagination validation: the first `inputText` item remains the primary result object, a second `inputText` item carries detailed evidence when present, and images remain separate content items in their original order. It adds no executors, action gates, automatic retries, or task evaluators.

Default model replies for `program_run`, `skill_run` (removed), and `program_status` retain `job_id`, program status, exit code, stop confirmation, call counts, timing/budget metrics, and full log references while omitting only duplicated source digests and call correlation metadata. Standard Python `stdout`/`stderr` preserves all saved text by default without truncating middle diagnostics to fit a byte budget. `*_excerpt.byte_size`, `byte_ranges`, `omitted_bytes`, and `source_complete` continue to report delivered byte spans and source completeness; if a legacy archive only stored a truncated slice, the presentation layer cannot reconstruct missing text and will never mark it complete.

In `summary` mode, only explicit inline base64 image data URLs (`data:image/...;base64,...`) are scrubbed and replaced with an explicit placeholder, while the verbatim text remains accessible via artifact and `detail="full"` reads. When a replacement occurs, the excerpt reports the original byte size, the original byte spans of stripped media, and explicitly notes that `byte_ranges` refer to the transformed presentation UTF-8 text rather than pretending transformed offsets match raw offsets. Ordinary JSON fields, numeric arrays, failure diagnostics, and log lines are never heuristically stripped based on content.

Standalone `inputImage` items and `stdout`/`stderr` text are delivered through separate channels. Images are forwarded solely from trusted tool replies rather than parsed out of arbitrary `stdout` text; printed data URLs remain placeholders in `summary` mode and verbatim text in `full`/artifact reads. Trusted provenance in `visual_feedback` comes exclusively from broker-saved tool replies—even if a program prints JSON that looks like an observation or image record, it remains untrusted program text.

In `action_summary.recent_actions`, the broker populates `stage_results` directly from typed receipts—preserving per-stage status, executed step counts, `endpoint_error`, `grasp_contact_feedback`, failed stage/arm/kind identifiers, first-step joint jumps, step limits, and start-state drift diagnostics. Because these values do not depend on `stdout`, verbose logging or spoofed success messages printed by a program cannot overwrite them (and contact-reference coordinates derived from robot kinematics still do not prove physical contact with an object). Although the field name `recent_actions` is retained for compatibility, it now covers every action in the program run rather than only the last eight, and `all_actions_ref` remains available. Receipts reuse the shared presentation projection, omitting only known duplicate metadata and repetitive passing-guard details while retaining all failures, unknowns, and new diagnostic fields.

Restricted Python subprocesses limit OpenCV, BLAS, and OpenMP compute threads to `1` by default while keeping the 1 GiB virtual address space cap. This prevents OpenCV from spawning thread pools sized to hundreds of host logical CPUs and exhausting the 1 GiB address space during thread stack allocation or library loading. Process exit status, vision algorithm outputs, and physical task success are still evaluated independently.

When the same model context polls the same job repeatedly, `stdout` or `stderr` payloads exceeding 256 bytes whose content, artifact reference, and excerpt completeness are unchanged are not re-inlined; instead, the reply reports `*_delivery.status="unchanged"`, the prior tool call ID, and the `detail="full"` handle (shorter texts remain inline so the marker is never longer than the text itself). Updated job status, exit code, stop confirmation, action facts, and pending review prompts are always returned in full. Any new log artifact, different `job_id`, context compaction, session reconnect, or uncertain transport delivery triggers full re-inlining. Explicit `detail="full"` reads never deduplicate, retransmitting the same call does not drop the body, and internal program reads never consume the actor's delivery baseline.

Forwarded supplementary images track a separate delivery baseline. Summary replies within the same model context may omit supplementary images whose content and provenance are unchanged since a prior delivery, providing `previous_tool_call_id` and a `program_status(job_id=..., detail="full")` recovery handle. Explicit `detail="full"` reads or model context resets re-deliver the supplementary images; this deduplication affects only repeated display and never modifies stored images, provenance records, or program/action state.

`stdout_ref` and `stderr_ref` always point to complete immutable output artifacts readable via `client.artifact(ref)`. Calling `program_status(job_id=..., detail="full")` returns all job fields and full `stdout`/`stderr` text (if a legacy archive only captured truncated text, `*_truncated=true` is still reported truthfully). A program `status="succeeded"` still means only that the process exited with code `0`; the summary never alters job status based on `stdout` or elevates arbitrary printed JSON into robot state.

Only when invoked through the legacy built-in `skill_run` entry point (removed in the 2026-10 refactor; this projection currently has no live callers) did [`program_result.py`](../../auriga/runtime/programs/program_result.py) parse the fixed-skill JSON protocol from complete `stdout` and tag it with `skill_output.source_kind="fixed_skill_program_output"`. That structure retained all skill fields, per-step receipt diagnostics, unknown extension fields, and pixel correspondences without stripping evidence via fixed allowlists or array summaries, while keeping the raw output in the `stdout` artifact. If skill output was incomplete or unrecognized, `execution_outcome="unknown_from_output"` was reported without assuming zero motion occurred. Standard `program_run` calls—even if they print identical JSON—are treated purely as uninterpreted program text.

## On-Demand Details for Status and Trajectories

The read-only tools below support a consistent `detail="summary" | "full"` parameter. Model calls default to `summary`, whereas `AurigaClient` sub-calls inside programs continue to receive the full original object when `detail` and pagination selectors are omitted, preserving stored evidence and full numerical precision.

| Tool | Default Summary (`detail="summary"`) | Full Read (`detail="full"`) |
| --- | --- | --- |
| `robot_action_status` | Retains original request, action outcome, errors, timing, partial execution, and unknown diagnostics; omits known storage duplicates and includes a `full_result` handle | Returns the complete original `{request, receipt}` |
| `history_list_actions` | Returns a chronological status index (`start=0`, `limit=25`) with `total` and `next_start`; each entry identifies the action, outcome, executed counts, stop status, and full-receipt handle | Returns the complete unpaginated list; cannot be combined with pagination parameters |
| `history_read_trace` | Returns small values verbatim; for sequences exceeding the window cap, returns the current contiguous window along with each window's JSON Pointer `path`, `total`, and `next_start` | Returns the complete original trace; cannot be combined with `path`, `start`, or `limit` |

In trace pagination, `start` is a zero-based index into the selected JSON array—**not a native step number**. For example, `path="/samples"` paginates dispatched samples and `path="/segments/0/q_rad"` paginates the planned joint sequence of segment `0`. Follow `windows[].path` and `next_start` to read subsequent pages (`limit` ranges from `1` to `100`, defaulting to `25`). Windowing slices only along the sample axis while keeping every individual sample intact—including 6-DOF joint vectors, 3D positions, 4D quaternions, and matrices (`limit=1` still returns one complete sample). Default windowing recognizes only top-level arrays and known trace time series rather than recursively truncating arbitrary nested arrays. Every sample retains its exact `sim_step`, timestamp, status, and floating-point values; windowing never relabels planned commands as measured trajectories, interpolates, or fabricates missing samples.

Passing `joint-travel-admission-v1` checks may omit duplicate joint vectors and passing periodic-check breakdowns in `summary` mode while preserving check order, compilation timestamps, phases, remaining budgets, and any skipped unknown checks. `motion_safety_check_summary` reports the total check count, passing count, final status, and full-action read handle. Rejected checks, witnesses, failure feedback, and unknown policies are always retained in full, and summary formatting never alters motion admission. Artifact access remains governed by existing scope, type, and observation permission checks.

## Action Timeout Must Fit Within Remaining Program Budget

The gateway admits an action from a program only when:

```text
program_deadline - time.monotonic() >= action.timeout_s + 5
```

This check uses the **remaining active-work time at the moment of admission**, not the initial `timeout_s` passed to `program_run` (wall-clock time spent waiting on auxiliary models or primary-model action review is excluded from active-work time). Starting Python, importing libraries, running prior tool calls, and IPC dispatch all consume active-work budget. Setting both the program timeout and an action timeout to 120 seconds will therefore always fail admission, and even a 115-second action leaves zero margin for startup overhead.

When a call inside a program omits `timeout_s` on `robot_action` (`preview` or `direct`) or `robot_wait`, the broker clamps the default timeout (120 s or 30 s) before plan generation to `min(tool_default, remaining_active_time - 5s_execution_margin - 1s_dispatch_margin)`. Explicit `timeout_s` values, already cached plans, and actor calls outside programs retain their exact semantics, and approving a review never silently shortens the deadline baked into a cached plan. Final action admission still re-verifies the margin, so slow planning can still cause an action to be safely rejected at dispatch time. If the broker has less margin than needed for a minimum 0.1-second action, `direct` and `wait` calls fail fast with a typed error (`reason="program_time_insufficient_for_action"`) without creating a plan or action record; if the margin runs out at final action admission, a `rejected` receipt with `action_not_executed` evidence is persisted. No physical motion occurs in either case. Read-only `preview` calls do not reserve execution time: even when remaining budget is low, a read-only plan can still be compiled with default timeouts, subject to final admission checks if executed later.

For example, in a 120-second program, an action with an explicit 30-second timeout is admitted only if at least 35 seconds of active-work budget remain when the request reaches the gateway. Subsequent actions must account for elapsed work and leave margin for startup and dispatch; a timer started inside the Python script does not know the gateway's exact monotonic deadline and cannot guarantee admission. When budget is insufficient, stop and return control to the primary model rather than looping on the same rejected action or inflating timeouts to mask failures.

## Implementation and Validation Entry Points

- [`harness.py`](../../auriga/runtime/harness.py): `program_run` contract, remaining-budget admission gate in `execute`, and action receipts/observations in `dispatch`.
- [`auriga_client.py`](../../auriga/runtime/programs/auriga_client.py): internal IPC client and native `dict` return values.
- [`runner.py`](../../auriga/runtime/programs/runner.py): exception replies, job status tracking, and stop confirmation.
- [`program_result.py`](../../auriga/runtime/programs/program_result.py), [`history_output.py`](../../auriga/core/presentation/history_output.py): model presentation and read-only on-demand details (never mutating program execution or `Store` evidence).
- [`robot_worker.py`](../../auriga/robot/backends/robodojo/robot_worker.py): action failure handling, partial execution tracking, and receipt fields.
- CPU regression tests: `tests.test_programs`, `tests.test_actor_payload`, and `tests.test_framework_candidates` (using isolated subprocesses or mock robots without requiring a live model or simulator).
- Summary and full-read tests: `tests.test_tool_output`, `tests.test_program_result`, and `tests.test_history_output` (covering log tails, full skill JSON, failed/unknown statuses, exact paginated reconstruction, passing-guard compaction vs. rejection-evidence preservation, and artifact scope enforcement).
