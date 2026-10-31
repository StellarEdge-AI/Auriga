# Lean Context: Cache-Preserving History Projection for the Actor Thread

> [!WARNING]
> **Potential Performance Impact (All Switches Disabled by Default)**
>
> 1. **Scope**: The codebase includes an optional upstream request history projection (`features.lean_projection`, `lean_projection_block`, `lean_projection_k`, and `lean_request_dump`), and automatically omits the startup instruction paragraph for `operation_memory` when that tool is listed in `features.disabled_tools`. **All projection switches default to disabled (`false` / `0`).** An earlier experimental `features.lean_profile` (which overrode Codex's native `baseInstructions`, disabled native `multi_agent` and `skills`, omitted inline URDF data, and rounded measured floats to 4 decimal places while dropping fields) was rejected because it violated repository boundaries requiring preservation of native Codex base instructions, full measurement precision, and a single payload projection path.
> 2. **Risks of Compressing Historical Raw Evidence**: Enabling `features.lean_projection` replaces high-volume tool replies on non-anchor steps in closed history blocks with concise text summaries and replaces their inline images with text placeholders. While this reduces per-request token counts, **it discards raw image pixels, full trajectory samples, detailed geometric/contact fields, and full program `stdout` from non-anchor historical steps** (unless the model explicitly re-fetches an observation via `history_view_observation`). In long-horizon precision tasks, cross-step residual comparisons, or multi-step failure recovery, summarizing historical raw evidence can degrade spatial reasoning and error attribution or induce extra history-retrieval turns; moreover, the comparison in Section 1 below reflects only a small 15-case run and has not ruled out task success regressions on the full benchmark suite. Because the default configuration already prevents HTTP 400 oversized-request errors and preserves prompt cache hit rates via `ModelRequestProxy` image trimming (`images.trim_oversized_requests`, enabled by default with a "keep head & tail, trim middle" window) and `reuse_exact_images` (see [Context and Image Compression for Model Requests](development/image-pipeline.md#context-and-image-compression-for-model-requests)), keep `lean_projection = false` by default and enable it only when explicitly evaluating context-compression trade-offs.

When all switches in the table below are off, outbound upstream requests are unchanged byte for byte. Independently of these switches, whenever `operation_memory` is listed in `features.disabled_tools`, its descriptive paragraph is omitted from the developer instructions.

| Configuration Key | Default | Description |
|---|---|---|
| `features.lean_projection` | `false` | Enables block-wise history projection on outbound upstream requests: older non-anchor steps are summarized and inline images become placeholders (Section 2) |
| `features.lean_projection_block` | `8` | Block length `B` measured in tool steps (`0` selects trailing mode) |
| `features.lean_projection_k` | `1` | Number of recent image steps retained in the trailing message (trailing mode only) |
| `features.lean_request_dump` | `0` | Diagnostic limit: number of projected request payloads (with image bytes replaced by digests) written to `<run-dir>/private/request-dump/`; keep at `0` in production |

## 1. Measured Effect

The projection was evaluated in small paired runs on RoboDojo (15 cases across three tasks, single runs, block mode with `lean_projection_block = 8` and `lean_projection_k = 1` vs. all switches off). Note that those runs combined `lean_projection` with the unmerged `lean_profile`, so they do not isolate the effect of `lean_projection` alone. Equivalent cost below is defined as `uncached_input_tokens + cached_input_tokens / 20` (matching the 1/20 cached-input rate on the model's rate card):

- Total input tokens per episode fell by ~30% and equivalent cost per model call fell by ~25%, with per-call savings remaining consistent across reruns.
- Mean context length per call dropped from ~88k to ~68k tokens at the same ~91% prompt cache hit rate, and native Codex context compactions dropped to zero (compared to roughly one compaction per episode without projection).
- Maximum outbound request body size stayed at or below `2.3 MB`, compared to `3.4–8.0 MB` without projection.
- Task success rate across the 15 cases was indistinguishable from the baseline, though it has not yet been evaluated on a larger benchmark panel.

Full `robot_action` replies accounted for roughly half of the equivalent token cost.

## 2. How `lean_projection` Works (`auriga/core/presentation/lean_projection.py`)

### The Problem It Addresses

Because the actor runs as a single Codex thread, every model call resends the entire conversation history: the static developer prefix, every tool reply accumulated so far (a single `robot_action` reply carries ~8k tokens of JSON plus 3–4 camera images), and the model's own reasoning and tool-call items. Over a 60–80-call episode, the context grows from ~35k to 150k+ tokens, triggering one or two native Codex compactions (each sending a ~227k-token request that is almost entirely uncached) and eventually exceeding the upload size limit on some upstream Responses relays (Section 3.2). Most of that historical volume is stale: the model acts primarily on the latest observation feedback and rarely re-reads the full trajectory JSON from twenty turns earlier.

### Where It Hooks In

Codex does not expose its internal thread storage for mutation, but every outbound model request passes through Auriga's local HTTP proxy (`model_proxy.py` → `codex.py:_present_model_request`). `lean_projection` is implemented as a pure function over the outbound request body: given the same history item, it always emits the exact same bytes without mutating Codex's stored thread state. Disabling the flag restores the original outbound request byte for byte.

### How History Items Are Transformed

Each item in the outbound conversation history is classified by role and tool type:

| Thread History Item | Outbound Representation |
|---|---|
| Replies from the six high-volume tools (`robot_action`, `program_run`, `robot_observe`, `robot_wait`, `geometry_query`, `history_view_observation`) inside **closed** blocks (excluding the block's first anchor step) | Replaced by a deterministic single-paragraph summary retaining the outcome, key numeric measurements (terminal pose residuals, grasp contact, remaining step budget), and any anomalies (`program_run` retains up to 700 characters of `stdout`) |
| Inline images inside those summarized replies | Replaced by a fixed single-line text placeholder recording the camera ID and `observation_id` (`"pixels omitted from older history"`) |
| Replies from the same six tools inside the currently **open** block, plus the **anchor** (first step) of every closed block | Sent unchanged (full text and inline images preserved) |
| All other items—static developer prefix, model reasoning items, tool call arguments, replies from low-volume tools, and native Codex compaction summaries | Sent unchanged |

Here, one "step" comprises a model turn and the tool outputs it produced. Because summaries are formatted deterministically from the reply's own structured fields without calling a model, projecting the same historical item always yields identical bytes.

### Why Block Mode and Anchor Steps Are Used

Upstream prompt-cache matching rules dictate the block structure. Several Responses relays reuse a cached prefix from a prior request only if the new request **contains the prior request's prefix in its entirety**; modifying any item in the middle of the history—or even dropping the final item—invalidates the cache for everything after that position (Section 3.3). Summarizing every step as soon as it becomes one turn old would therefore cause a cache miss on every single call (reducing the measured cache hit rate to 68%). Instead, steps are grouped into fixed-size blocks of length `lean_projection_block` (default `B = 8`):

```text
block 1              block 2              block 3 (open)
┌───────┬─────────┐  ┌───────┬─────────┐  ┌───────┬─────┬─────┐
│anchor │ 7 steps │  │anchor │ 7 steps │  │anchor │ ... │ now │
│ full  │ summary │  │ full  │ summary │  │ full  │full │full │
└───────┴─────────┘  └───────┴─────────┘  └───────┴─────┬─────┘
```

- **Within an open block**, each request is a pure append extension of the previous request, so the upstream prompt cache hits on the entire history except for the newly appended step (~6k tokens).
- **When a block closes** (every 8 steps), its seven non-anchor steps are rewritten into compact summaries and image placeholders in a single batch. That rewrite request misses the cache from the start of the newly closed block onward: the seven new summaries (~1.2k tokens), the block's unchanged model reasoning and tool arguments (~1.9k tokens sitting after the modified items), and the newest step. Empirically, a block-rewrite request costs ~3.2k more uncached tokens than a pure extension—roughly 3.7% of an episode's total equivalent cost.
- **The anchor step** (the first step of each block) is never rewritten. Preserving the anchor ensures that when block `k` is rewritten, its anchor step still matches the end of the previous request's cached prefix; without anchors, every block rewrite would invalidate the cache all the way back to the static prefix (costing ~4.5% more in simulation).

Across recorded trajectories, projected requests carried an average of ~16k tokens of anchors, ~20k tokens in the open block, ~3.5k tokens of closed-block summaries, and ~8k tokens of model reasoning/tool calls atop the static prefix—maintaining a ~91% cache hit rate with zero native Codex compactions. A block length of `8–12` steps is empirically optimal (`B = 4` retains too many full anchor steps, while `B = 16` carries too large an open block).

### What the Model Is Told

Two concise lines are added to the developer instructions noting that older tool replies appear as summaries and that `history_view_observation` can retrieve any historical observation in full. No other prompt text is changed (adding detailed descriptions of reply formatting altered the model's tool-calling behavior; see Section 4).

### Trailing Mode (`lean_projection_block = 0`)

In trailing mode, all historical steps are summarized immediately from step 1, while the latest step's full reply and the inline images from the last `lean_projection_k` steps are placed into a single trailing user message that is replaced on every call. Each request is therefore `"the previous request minus its final item, plus new items"`. On upstream backends that cache prefixes up to the penultimate item, per-request context drops to ~43k tokens (eliminating both anchors and the open block) for an estimated additional 15–18% per-call cost reduction while keeping current-step feedback identical. However, on relays that enforce the strict containment rule (Section 3.3), dropping the trailing item invalidates the cache; keep block mode (`lean_projection_block = 8`) on such relays.

## 3. Upstream Relay Behavior to Account For in Comparisons

### 3.1 Provider Drift

Even with identical code, configuration, and benchmark cases, task success rates on one upstream endpoint dropped by roughly half within a 24-hour window—with failed episodes running for 100–170 calls without HTTP errors and exhibiting unchanged per-call latency and reasoning-token counts in proxy logs. Consequently, any ablation must compare configurations **side by side within the same time window** against a concurrent control group, evaluating both paired task success differences and per-call token cost (per-call cost is much more stable across upstream drift than per-episode cost, because failed episodes consume 1.6–2.0× as many calls as successful ones).

### 3.2 Request Body Size Limits

Some upstream relays reject HTTP POST bodies exceeding ~3.8–5.0 MB with HTTP 400 `image_compression_failed`. Retaining every full-resolution camera image across a long episode hits this ceiling mid-episode, whereas `lean_projection` kept request bodies at or below `2.3 MB`. In the default configuration (where `lean_projection` is off), `images.trim_oversized_requests` prevents this same HTTP 400 limit by pruning middle images only when the byte or image-count threshold is approached.

### 3.3 Cache Containment Rules and Usage Reporting

- Some relays reuse a cached prefix only when the new request contains the previous request's entire input array as an exact prefix. Any in-place modification—including removing a trailing message—loses the cache suffix from that index onward.
- Certain relays occasionally misreport token usage (for instance, reporting ~1.5× the actual input and output tokens on specific days); always cross-check comparisons using consistent local tokenizer counts (`o200k_base`).
- Codex's native background memory thread adds a small number of auxiliary requests per episode (~9% of equivalent episode cost in these runs) and remains enabled in both conditions.

## 4. Alternatives Tested and Rejected

Each of the following modifications reduced single-call token cost by 5–8%, but increased the average number of calls per episode in side-by-side runs, yielding no net episode savings:

- Dropping `key_poses`, `trajectory_feedback.measured`, and related diagnostic fields from successful `robot_action` replies.
- Omitting the trajectory overlay image after successful execution and returning only `cam_head` in the final observation of `program_run` (which reduced task success on one task).
- Condensing the `robot_action` tool schema description to 4.6k characters.
- Thinning trajectory samples and retaining `derived_motion` only on the final sample (which reduced uncached tokens per call by 8%, but increased mean calls per episode from 57 to 89 in paired runs).

The consistent lesson is that **summarizing old historical steps is benign, whereas thinning feedback on the current step forces the model to inspect and retry more often**.

Similarly, adding extra instructions describing compressed reply formats caused the model to stop using `program_run` and preview every motion individually; therefore, beyond the two-line note in the developer instructions, `lean_projection` signals itself solely through inline `[summary ...]` markers inside projected tool outputs.

## 5. Open Questions

- Evaluating `lean_projection` against the unprojected baseline across a larger paired benchmark panel ($\ge 30$ cases side by side) under a stable upstream provider.
- Comparing trailing mode (`lean_projection_block = 0`, `lean_projection_k = 1–2`) against block mode on an upstream backend that supports prefix caching after dropping the final input item (projected to reduce mean context from ~70k to ~40–45k tokens and cut per-call cost by another 25–30% while preserving full current-step feedback).

## 6. Decision-Boundary Reminder Placement in Block Mode

By default, the actor's decision-context hook (`CodexSession._task_decision_context`) strips any earlier `"Current task decision boundary"` assistant message and appends a fresh snapshot **only on `/responses/compact` requests**, keeping ordinary `/responses` calls pure prefix extensions. When `lean_projection` is active in block mode (`lean_projection_block > 0`), the hook also runs on ordinary `/responses` calls. However, if the reminder were appended as the very last input item of request $n$, request $n+1$ would strip it before appending new items—violating the strict containment rule of Section 3.3 on every call. Therefore, `lean_projection.place_block_reminders` keeps the reminder text unchanged but places it into fixed, immutable block slots:

- **Never end a request with an assistant reminder**: Relays enforcing the containment rule rarely reuse cache entries for requests that end with an assistant message (empirically reusing only 3 of 36 such entries, vs. 170 of 170 when the request ends with a tool output).
- **Slot 0**: Placed immediately before the initial user task message on request 1 (when the request has no tool steps yet), so request 1 still ends with the user task message.
- **One slot per block** (at anchor steps `1, B+1, 2B+1, ...`): Created on the first request in that block for which a `task_plan` exists, inserted immediately before the model turn of the newest step (i.e., right after the previous step's tool output). Because the previous request ended at that tool output, inserting the slot there remains a pure prefix extension and the new request still ends with the newest step's tool output.
- **Immutable once placed**: Each slot permanently retains the plan snapshot it was created with, and reminders in earlier slots remain at their original indices so that prior anchor prefixes remain bit-identical across block rewrites.
- **No-plan handling**: The `"no plan stored"` reminder variant is placed only in Slot 0. If a block has no stored plan yet, no slot is created for that block; once the first `task_plan` is submitted, it populates the next block slot created without rewriting any existing slot.
- **Compaction reset**: Any slot whose preceding item changed (due to native Codex compaction or a new thread) is dropped cleanly.

This slot strategy adds one reminder snapshot (`plan` + `attention` + `ledger` JSON, typically `0.3–1.0k` tokens) per 8-step block, served from the prompt cache on subsequent turns. Within an open block, the slotted reminder can be up to $B - 1$ steps older than the latest `task_plan` call, though the newer `task_plan` call and its reply remain directly visible in the open block's conversation history. Trailing mode (`lean_projection_block = 0`) and `lean_projection = false` retain the default behavior (injecting only on `/responses/compact`).
