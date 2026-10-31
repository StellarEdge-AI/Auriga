# Lean context: cache-preserving history projection for the actor thread

> **潜在性能影响说明（默认全部关闭）**
>
> 1. **范围**：当前代码包含上游请求历史投影能力（`features.lean_projection`、`lean_projection_block`、`lean_projection_k`、`lean_request_dump`）以及禁用 `operation_memory` 工具时同步省略其启动说明段，**所有开关默认关闭（`false` / `0`）**。早期实验中的 `features.lean_profile`（覆盖 Codex 原生 `baseInstructions`、关闭原生 `multi_agent`/`skills`、省略内联 URDF、对测量浮点数做 4 位舍入与字段裁剪）因违反仓库“保留原生 Codex 基底说明与测量精度、不另建平行 payload 裁剪”边界而未纳入。
> 2. **压缩原始信息的潜在性能风险**：开启 `features.lean_projection` 后，已闭合历史块中非锚点步的高频工具返回将被替换为简短摘要，内联图像被替换为文本占位符。虽然这能降低单次请求上下文长度，但**会丢失历史非锚点步的原始图像像素、完整轨迹采样数据、详细几何/接触字段及完整程序 `stdout`**（除非模型主动调用 `history_view_observation` 重新读取）。在长程精细操作、跨步误差对比或多步失败恢复场景中，压缩历史原始证据可能削弱模型的空间推理与归因能力，或诱导额外的回看调用；且下文第 1 节的对比仅基于小规模单轮运行，尚未在全量场景集上排除任务成功率退化风险。默认配置已通过 `ModelRequestProxy` 的超限请求图像裁剪（`images.trim_oversized_requests`，默认开启，「保首尾、压中段」）与 `reuse_exact_images` 解决大请求体 400 与缓存保活问题，各压缩手段的开关与实测见[上下文与图像压缩](development/image-pipeline.md#模型请求的上下文与图像压缩)，请保持 `lean_projection = false` 默认关闭，仅在明确评估信息压缩代价时按需开启。

With every switch below off the upstream request is unchanged byte for byte. One
independent change, active regardless of the switches: when the `operation_memory` tool
is listed in `features.disabled_tools`, its paragraph is also dropped from the developer
instructions.

| Key | Default | What it does |
|---|---|---|
| `features.lean_projection` | `false` | Block-wise projection of the upstream request: old steps become summaries, images become placeholders (section 2) |
| `features.lean_projection_block` | `8` | Block length B in tool steps; `0` = trailing mode |
| `features.lean_projection_k` | `1` | Image steps carried in the trailing message (trailing mode only) |
| `features.lean_request_dump` | `0` | Diagnostics: number of projected request bodies (image data replaced by digests) written to `<run-dir>/private/request-dump/`; keep `0` in production |

## 1. Measured effect

The projection was evaluated in small paired runs on RoboDojo (15 cases from three
tasks, single runs, block mode with `lean_projection_block = 8` and
`lean_projection_k = 1`, against both switches off). Those runs combined the projection
with the unmerged `lean_profile`, so they do not isolate the projection. Equivalent cost
below = uncached input + cached input / 20 (the rate card of the model used prices cached
input at 1/20).

- Input tokens per episode fell by about 30% and the equivalent cost per call by about a
  quarter; the per-call saving was stable across later reruns.
- Mean context per call fell from about 88k to about 68k tokens at the same ~91% cache
  hit rate, and Codex context compactions disappeared (about one per episode without the
  projection).
- The largest request body stayed at or below 2.3 MB, against 3.4–8 MB without the
  projection.
- The success rate on 15 cases was not distinguishable from the baseline; it has not
  been re-checked on a larger set.

Full `robot_action` replies were about half of the equivalent cost.

## 2. `lean_projection` (`auriga/core/presentation/lean_projection.py`)

**The problem it addresses.** The actor is a Codex thread, so every model call resends
the whole conversation: the static prefix, every tool reply so far (a `robot_action`
reply is about 8k tokens of JSON plus 3–4 images) and the model's own messages. Over
an episode of 60–80 calls the context grows from 35k to 150k+ tokens, Codex compacts it
once or twice (a 227k-token request that is almost entirely uncached), and on some
Responses relays the request body eventually exceeds the upload limit (section 3.2).
Most of that volume is old: the model acts on the latest feedback and rarely reads the
full JSON of a step it took twenty calls ago.

**Where it hooks in.** Codex does not expose its thread history, but every request it
sends goes through Auriga' local model proxy (`model_proxy.py` →
`codex.py:_present_model_request`). The projection is a pure function applied to that
outgoing request body: given the same history item it always produces the same bytes,
and it never changes Codex's stored thread. Switching it off restores the original
request byte for byte.

**What it does to each history item.** Items are classified by type:

| Item in the thread | Sent as |
|---|---|
| Replies of the six high-volume tools (`robot_action`, `program_run`, `robot_observe`, `robot_wait`, `geometry_query`, `history_view_observation`) from *closed* blocks (except the block's first step) | a deterministic one-paragraph summary: outcome, the numbers the model is likely to need (end-pose errors, grasp contact, budget), anomalies; `program_run` keeps up to 700 characters of stdout |
| Images inside those replies | a fixed placeholder line naming the camera and observation id ("pixels omitted from older history") |
| Replies of the same tools in the *open* block, and the first step of every block | unchanged, text and images |
| Everything else — the static prefix, the model's reasoning items and tool arguments, replies of other tools, Codex compaction summaries | unchanged |

A "step" is one model reply and the tool outputs it produced. A summary is written from
the reply's own fields only (no model call), so it is identical every time the item is
projected.

**Why blocks, and why anchors.** The upstream cache rules decide the shape. Some
Responses relays reuse a cached request only if the new request *contains it entirely*;
changing any item in the middle, or even dropping the last one, loses the cache for
everything after that point (section 3.3). Summarising each step as soon as it is one
step old would therefore cost a cache miss on every call (measured hit rate: 68%). So
steps are grouped into blocks of `lean_projection_block` (default 8):

```
block 1              block 2              block 3 (open)
┌───────┬─────────┐  ┌───────┬─────────┐  ┌───────┬─────┬─────┐
│anchor │ 7 steps │  │anchor │ 7 steps │  │anchor │ ... │ now │
│ full  │ summary │  │ full  │ summary │  │ full  │full │full │
└───────┴─────────┘  └───────┴─────────┘  └───────┴─────┴─────┘
```

- Inside a block, each request is the previous request plus new items — a pure
  extension, so the cache hits on everything but the new step (≈6k tokens).
- When a block closes (every 8 steps) its seven non-anchor steps are rewritten into
  summaries and placeholders in one go. That request misses the cache from the start of
  the rewritten block onward: the summaries (≈1.2k), the block's reasoning and tool
  arguments (≈1.9k, unchanged but behind the changed items) and the new step. Measured:
  a rewrite request costs about 3.2k tokens more than an extension, i.e. 3.7% of the
  episode's equivalent cost.
- The anchor (first step of each block) is never rewritten. It is what the rewritten
  request still shares with the previous one: without it the cache would have to restart
  from the static prefix at every rewrite (simulated: 4.5% more expensive).

On the recorded trajectories a request then carried on average about 16k tokens of
anchors, 20k of open block, 3.5k of summaries and 8k of the model's own history on top
of the static prefix, with the same ~91% hit rate and no Codex compaction. Block length
8–12 is the optimum on the recorded trajectories; 4 keeps too many anchors, 16 carries
too much open block.

**What the model is told.** Two short lines in the developer instructions explain that
older tool replies appear as summaries and that `history_view_observation` can fetch an
old observation in full. Nothing else in the prompts changes (adding sentences that
describe reply formats changed the model's tool use, see section 4).

**Trailing mode** (`lean_projection_block = 0`). History is summaries from the start;
the latest step's full reply and the images of the last `lean_projection_k` steps go
into one trailing user message that is replaced every call. Every request is then
"the previous request minus its last item, plus new items", which some backends still
serve from cache but relays with the containment rule of section 3.3 do not. On a backend that allows it, the per-request context drops to
roughly 43k (no anchors, no open block), an estimated further −15–18% per call on top of
block mode, with the current step's feedback unchanged. Keep block mode on relays with
the containment rule.

## 3. Upstream behaviour to know before comparing runs

### 3.1 Provider drift

With the same code and configuration on the same cases, success on one upstream
endpoint dropped by about half within a day, with failed episodes running to 100–170
calls without errors and with unchanged per-call latency and reasoning tokens, so proxy
logs showed nothing. Configurations must therefore be compared side by side in the same
time window with a control, judged by the paired success difference and the per-call
cost. The per-call cost is the most stable metric across upstream states, because
failed episodes are 1.6–2× more expensive than successful ones.

### 3.2 Request-body size limits

Some relays reject requests of roughly 3.8–5 MB or more with HTTP 400
`image_compression_failed` (not retryable; the episode is terminated unscored). Keeping
every image in history reaches that size mid-episode; with the projection the body
stayed at or below 2.3 MB. The default oversized-request trimming
(`images.trim_oversized_requests`, see the image-pipeline link above) addresses the
same limit without the projection.

### 3.3 Cache rule and usage reporting

- Some relays reuse a previous request only if the new request contains it entirely.
  Any in-place change, including dropping the trailing message, loses the suffix.
  That is why block mode exists and why trailing mode needs a backend that serves
  "previous request minus its last item" from cache.
- A relay's reported token usage can differ from the actual request on some days (one
  relay reported input and output about ×1.5 on some requests); compare days only with
  a consistent token count.
- The native-memory thread adds a few requests per episode (about 9% of the episode's
  equivalent cost in these runs); it stays on.

## 4. Tried and not adopted

All of these reduce the cost of a single call by 5–8% but made episodes longer in the
side-by-side runs, so the per-episode cost did not fall:

- dropping `key_poses`, `trajectory_feedback.measured` and similar evidence from
  successful `robot_action` replies;
- no trajectory overlay after successful execution and `cam_head` only in the
  `program_run` final observation (success fell on one task);
- a 4.6k-character `robot_action` description;
- thinning trajectory samples and keeping `derived_motion` only on the last sample:
  uncached per call −8%, but 89 vs 57 calls per episode in the paired run.

The common pattern: summarising old steps is fine; thinning the feedback of the current
step makes the model inspect and retry more.

Adding sentences to the developer instructions that describe reply formats changed the
model's tool use (it stopped using `program_run` and previewed every action); beyond a
two-line context-format note in the developer instructions, the projection therefore
announces itself only through markers inside the replies.

## 5. Open questions

- Success rate of the projection against the baseline on a larger set (≥30 cases side
  by side) under a stable upstream.
- Trailing mode (`lean_projection_block = 0`, `lean_projection_k = 1–2`) vs block mode
  on a backend that serves trailing requests from cache. Expected mean context
  70k → 40–45k and another −25–30% per call, with the same full feedback for the
  current step.

## 6. Decision-boundary reminder under block mode

The actor's decision-context hook (`CodexSession._task_decision_context`) removes any
earlier "Current task decision boundary" assistant message and appends a fresh one. By
default it does so only on compaction requests (`/responses/compact`), so ordinary
`/responses` calls stay pure prefix extensions. With `lean_projection` in block mode
(`lean_projection_block` > 0) it also runs on every actor `/responses` request; appended
as the last input item, request n+1 would then lack the last item of request n, and
under the containment rule of section 3.3 no request would be a prefix of the next one.
The hook's text is therefore kept unchanged and only where and how often it is placed
changes (`lean_projection.place_block_reminders`):

- a reminder is never the last input item: relays with the containment rule rarely reuse
  the cache entry of a request that ends with an assistant message (measured: 3 of 36
  such entries reused, against 170 of 170 for requests ending with a tool output);
- slot 0 directly before the task message of the first request (the last item of a
  request without tool steps, when it is a user message), so request 1 ends with the
  task message as without the hook;
- one slot per block (anchor steps 1, B+1, 2B+1, ...), created on the first request of
  the block for which a plan is stored, directly before the model turn of the newest
  step, i.e. after the previous step's output. The previous request ended there, so
  the creating request extends it and ends with a tool output;
- each slot keeps the snapshot it was created with; reminders of earlier slots stay
  where they were sent, so the anchor request of the previous block is still a prefix
  of the rewrite request;
- the "no plan stored" variant is carried only by slot 0. A block without a stored plan
  gets no slot; the first plan appears in the next slot created, so no slot is ever
  rewritten (an earlier variant re-took the open slot once after the first `task_plan`,
  one extra re-read per episode when the plan landed inside block 1);
- a slot whose preceding item changed (Codex compaction, new thread) is dropped.

Cost: one reminder (plan + attention + ledger JSON, typically 0.3-1k tokens) per block
stays in the context, read from cache. Behavioural difference: inside a block the
reminder can be up to B-1 steps older than the latest `task_plan` update; that update is
itself visible as the `task_plan` call and reply. Trailing mode (`lean_projection_block = 0`)
and `lean_projection = false` keep the default placement (compaction requests only).

In request dumps of block-mode runs with the slot rule no request ended with a
reminder, and most requests were exact extensions of the previous one; the breaks were
the block rewrites.
