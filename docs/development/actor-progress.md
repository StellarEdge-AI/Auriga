# Completed-Final Progress Supervision

Setting `features.bounded_continuation=true` replaces prose matching with a bounded recovery policy at benchmark completed-turn boundaries. It prevents an unchanged episode from receiving unlimited automatic continuation prompts simply because the actor changes its wording, announces a new plan, or repeats read-only queries. It does not interrupt a running turn and does not infer object placement, reachability, task success, or failure from image contents or evaluator state.

The legacy `ActorStallTracker` and its five-consecutive-inability regex remain unchanged when `features.bounded_continuation=false`. New runs default to the bounded policy. Archived configurations that predate the feature resume with the legacy setting, a compatibility rule owned by the configuration and manifest layer. A synthetic workbench without a configuration or feature block also uses the legacy path.

Setting `features.recovery_evidence=true` enables the evidence-aware policy described below. Its independent switch supports same-source ablations and has no effect while `bounded_continuation=false`. The original three/six-turn policy remains available with `recovery_evidence=false`. The first sections below document the original policy, and the final section describes the evidence-aware two/six-turn policy.

## Exact Budget and Boundary Semantics

The first completed final turn for a `(thread_id, episode_id)` pair establishes a baseline at the current `native_action_count`. Its tool calls seed the deduplication set without consuming a recovery opportunity. Every subsequent unique completed turn at the same execution count increments `count`:

- Without a new eligible tool fingerprint, `idle_count` increments. The **third consecutive such final** stops the attempt. Pure final-to-final loops therefore receive **three continuation turns**, stopping at the fourth completed final (including the baseline).
- With a new eligible fingerprint, `idle_count` resets to zero while `count` continues to increase. The **sixth completed recovery turn without native execution** stops the episode even if every turn contains novel tool calls. Such an episode receives at most six continuation turns, stopping at the seventh completed final (including the baseline).
- Any increase in actual `native_action_count` establishes a new baseline and resets both counters. This includes partial execution on a failed action; a zero-step infeasible motion attempt alone does not reset the execution budget.
- Switching to a different thread or episode establishes a new baseline. Reprocessing an already evaluated completed turn is ignored. Missing, invalid, or regressed execution counts produce an explicit `unknown` observation and cannot trigger a stall; a proper episode reset must change the episode identity.

The native execution count is public robot-action accounting rather than a task-object oracle. The policy deliberately does not require every executed step to advance the task—a wait or an ineffective executed motion also resets this recovery budget. Existing native-step limits and other runtime supervision rules continue to apply. Its sole purpose is to bound **automatic continuation after completed finals**, not to impose an extra timeout on normal long-running turns.

Constants and policy metadata are defined in `auriga/cortex/supervision/actor_stall.py`:

- `ACTOR_PROGRESS_IDLE_LIMIT = 3`
- `ACTOR_PROGRESS_RECOVERY_LIMIT = 6`
- `ACTOR_PROGRESS_POLICY.version = bounded_completed_final_progress_v1`
- `ACTOR_PROGRESS_REASON = actor_no_progress`

## What Earns Bounded Recovery Credit

`completed_turn_activity` inspects only `tool_call` and `observation` records belonging to the owning thread, turn, and current episode, while `recovery_fingerprints` canonicalizes eligible public requests. Eligible requests demonstrate attempted investigation, not that a tool succeeded, yielded new knowledge, or established a viable recovery route. Even invalid or endlessly varied requests cannot bypass the six-turn ceiling.

Eligible requests include calibrated geometry queries, cost previews, synchronized trajectory sample inspections, contact overlays, historical image views and crops, disclosed capability queries, explicit motion attempts, manipulation templates, and actor Python computations. Optional grasp/motion review, `vision_review`, and `operation_verify` requests are also eligible (`operation_verify` was removed in the 2026-10 refactor; use `vision_review` with `evidence_mode="operation"`). Ordinary `robot_observe` calls and `task_plan` prose updates earn no novelty credit. The native step counter independently accounts for all executed physical motion.

Deduplication strips action, request, and call IDs, preview labels, timeouts, and call-budget settings. Preview candidates and requested trajectory native-step samples are fingerprinted individually, so reordering or regrouping candidates does not make existing hypotheses appear novel. Observation IDs are normalized to their referenced episode and execution step: a fresh capture UUID at the same paused state is not considered new, whereas viewing a different historical step is. Missing or foreign references share a single unverified marker and cannot generate unlimited ID-based novelty. Python source code is fingerprinted via its AST to ignore comments and formatting, with embedded observation UUIDs normalized as well. Fingerprints and bounded counters serve as audit evidence rather than a semantic novelty detector or proof of object grasp/retention.

The bounded path never inspects natural-language words in final answers. Because the task-plan schema has no structural blocked-status field, this policy adds no new tool or plan status fields and does not scrape natural-language observation gates.

## Integration and Scoring

`gateway.wait_benchmark` runs this check only after passing existing checks for completed turns, native termination, model budgets, shutdown signals, unresolved actions, active actions, and active programs—including the recheck performed after acquiring the control lock. Before a triggered stall becomes the official stop reason, the gateway re-verifies terminal, budget, and unresolved-work states to account for callbacks that arrived during evidence collection. Native terminal results and unconfirmed executions always take precedence.

Each accepted progress observation is emitted as a `benchmark.actor_progress` event containing the policy version, context, current native step count, baseline flag, total and idle recovery counts, fingerprint novelty, and trigger reason. The final observation is also recorded under `harness.actor_stall` in the run summary. An unknown step count is labeled `status=unknown` rather than silently treated as a lack of progress.

A clean `actor_no_progress` stop is recorded as a Harness task-limit failure while preserving the valid native process score; it never fabricates native task success. Unknown or in-flight executions and invalid evaluations retain their existing exclusion and unknown-state handling. Legacy runs preserve `benchmark.actor_inability` and `actor_repeated_inability`.

## Offline Validation

When replayed against a recorded RoboDojo episode in which the actor produced 92 completed finals and 92 automatic continuations while the native step count remained nearly static, the bounded policy stops at **final 7 / continuation 6** while preserving the turns that introduced novel recovery fingerprints. This replay evaluates the supervision policy over archived evidence without invoking a model, robot, physics engine, or evaluator, and makes no counterfactual claim of physical task success.

`tests/test_actor_progress_stall.py` includes a compact 93-event activity and native-count sequence of that shape (92 completed finals plus one failed turn, which is excluded) as a self-contained regression test, alongside request-normalization tests and live wait-loop fakes. Existing legacy stall and benchmark-terminal tests remain active.

Run the validation suite with:

```bash
PYTHONPATH=tests mamba run -n Auriga python -m unittest \
  test_actor_progress_stall test_actor_stall test_benchmark_terminal_policy -q
```

These CPU unit tests verify exact off-by-one boundaries, the six-turn exploration ceiling, duplicate turns, repeated requests, resets upon actual execution, unknown or regressed counters, episode and thread transitions, the legacy fallback switch, normal running turns, and terminal/unknown/in-flight precedence. They do not measure robot task success rates.

## Recovery Guidance v1

When `bounded_continuation` is enabled, continuation prompts append the current actor observation ID, known remaining native step budget, and measured home status. The prompt asks the actor to specify one observable recovery objective, the changed assumption or approach, and a step budget covering approach, correction/manipulation, release/verification, withdrawal, any required final return to home, and uncertainty margin—emphasizing that a cheap approach phase alone is not a complete recovery. Unknown counters remain `null`. This helper reads no evaluator, scene-object, or contact state and makes no robot calls. Existing stall thresholds, native-execution counter semantics, termination priorities, and scoring rules remain unchanged, and the guidance makes no inference about task progress. Disabling `bounded_continuation` restores the exact legacy continuation prompt. Startup archives the guidance version and template text, and each continuation logs the actual context and delivered prompt.

## Evidence-Aware Recovery v2

On challenging RoboDojo layouts, varying target endpoints alone was observed to keep the original investigation counter alive even when the previewed motion already exceeded the remaining episode step budget. Evidence-aware recovery distinguishes such unaffordable nominal candidates from genuinely new information without assuming that all grasps, routes, or the overall task are infeasible.

`completed_turn_evidence` in `auriga/cortex/supervision/recovery_evidence.py` inspects only current-thread, current-turn public tool requests, observation metadata, delivered RGB hashes, and `motion.cost_preview` events. It filters for `motion.cost_preview` in SQL prior to JSON decoding and never scans evaluator events or reads artifacts, scene objects, contacts, or simulator state. To qualify, a preview must execute zero physical steps, verify an unchanged measured robot state, and match the current native step count. Classifying a candidate as over-budget additionally requires a known non-negative integer remaining budget and matching preview cost accounting; a missing, stale, boolean, or mismatched budget never triggers a budget rejection. The previewed cost represents the exact step count of a single nominal trajectory compilation, not a lower bound across all possible routes or a full-operation cost estimate.

Each candidate is keyed by its normalized requested motion, measured-state hash, and remaining budget. Changing labels, generated action IDs, or observation UUIDs at the same physical state does not create a distinct candidate. The completed-turn trace and continuation prompt carry a compact ledger of the six most recent tested candidates—recording their nominal costs, outcomes, and budget scope—along with a count of earlier entries, while the full underlying requests and cost records remain in the event store.

When valid evidence collection is active:

- The baseline turn still grants recovery without stopping immediately. **Two consecutive completed recovery turns with no new eligible information** stop automatic continuation—granting two additional recovery opportunities after the baseline rather than stopping after a single failed candidate.
- A new candidate whose nominal preview cost exceeds the known remaining episode budget does not refresh idle credit. Rounds containing only over-budget candidates stop with `budget_rejection_final_limit`, while other unchanged rounds stop with `no_new_evidence_final_limit`.
- A genuinely distinct affordable or unresolved candidate earns bounded investigation credit (though an affordable approach alone is not declared a viable end-to-end operation). A changed outcome for a previously tested candidate also earns credit.
- Delivered RGB images are deduplicated by `(camera_id, source_sha256)`, so fresh observation IDs with identical pixels earn no credit. Obtaining a previously missing trajectory sample for an already tested candidate can earn credit once. Default trajectory plots attached to endlessly varied unaffordable endpoints do not refresh budget-only loops.
- Other public investigation requests retain their original bounded request credit because semantic usefulness cannot be proven in general. All varied investigation remains capped at **six completed recovery turns without native execution**.
- Actual native execution, a new thread, or a new episode resets both the stall plateau and the candidate ledger. Unknown or regressed native step counters never trigger a stop. If the evidence reader is unavailable or its native step binding loses a race with physical execution, the tracker falls back to the original request-credit policy and three/six-turn limits.

`ActorProgressTracker(evidence_aware=True)` exposes `recovery_context()` for `recovery_guidance(..., evidence=...)`. Guidance v4's continuation message provides the tested-candidate ledger and recovery-specific counter-evidence rules, while the primary request reminder delivers a concise decision boundary alongside the current plan: choose a useful, bounded next step and verifying feedback while budgeting for necessary release, withdrawal, required return, and uncertainty. Every owned actor request receives the complete current snapshot (removing prior Harness reminders before appending the latest snapshot) because the upstream provider path does not guarantee a persistent cross-request assistant-message cache. Remaining task cost is weighed without requiring a complete start-to-finish route before taking a useful action. When an expected event fails to occur, the actor is guided to retain the unresolved prerequisite and either revise its hypothesis or perform useful independent work that neither relies on nor interferes with it. Unknown future outcomes remain `unknown`, and material changes update the plan and affected attention items.

Whereas v3 clarified v2's "complete attempt" wording only after a completed actor turn, v4 introduces the prerequisite distinction and delivers the decision boundary via `CodexSession._task_decision_context` as soon as a plan exists—covering both ongoing execution and recovery without repeating the boundary in the continuation user message. Every owned actor request carries the current plan snapshot; the projection removes only prior Harness reminders while leaving ordinary assistant messages and developer instructions untouched. This planning reminder operates independently of continuation flags, does not copy recovery counters, candidate ledgers, or the full operation-notes table into every request, and does not monitor inner actions inside a direct-action program.

Guidance continues to prohibit treating a single candidate rejection as global task infeasibility, repeating unchanged candidates, or executing idle motion solely to reset progress counters. The actor is not required to exhaust all remaining steps or preview calls. Native terminal precedence, unresolved-work handling, official step limits, execution admission, success detection, and scoring remain unchanged, and disabling `recovery_evidence` restores the exact v1 continuation text. Integration tests verify the delivered continuation text and version, single-injection composition in outbound HTTP requests, same-turn delivery of updated concerns, clearing and compaction behavior, identical retries, and helper-thread exclusion. These checks verify message delivery mechanics rather than native task success rates.

### Validation and Replay

Focused CPU unit tests cover budget and measurement binding, same-candidate deduplication, alternative affordable routes, changed candidate outcomes, delivered RGB verification, missing trajectory evidence, unknown-budget fallbacks, execution races, native execution resets, feature-off compatibility, and compact guidance formatting:

```bash
PYTHONPATH=tests mamba run -n Auriga python -m unittest \
  test_recovery_evidence test_recovery_guidance test_actor_progress_stall \
  test_actor_stall test_benchmark_terminal_policy test_recovery_gateway_integration -q
```

Independent gateway integration tests cover callbacks arriving during baseline evidence collection, stall-event persistence, and recovery-guidance generation. Native termination, model call limits, unresolved actions or programs, and shutdown signals retain priority before starting a new actor turn, and native accounting callbacks reset the stall plateau. Disabling `recovery_evidence` restores the original bounded policy, and disabling `bounded_continuation` restores the legacy prose-matching policy.

Read-only replays over recorded hard episodes reduce the number of accepted completed finals in several cases. Where an episode continues to generate distinct, affordable approach candidates, the policy conservatively preserves investigation credit; the live impact of guidance and planning must be evaluated in full closed-loop runs. These replay results establish policy boundaries over archived evidence rather than counterfactual model-cost, task-success, or physical-performance claims.
