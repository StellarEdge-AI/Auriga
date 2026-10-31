# Completed-final progress supervision

`features.bounded_continuation=true` replaces prose matching with a bounded recovery policy at benchmark completed-turn boundaries. It prevents an unchanged episode from receiving unlimited automatic continuation prompts because the actor changes wording, says it has a new plan, or repeats read-only queries. It does not interrupt a running turn and does not infer object placement, reachability, task success, or failure from image contents or evaluator state.

The legacy `ActorStallTracker` and its five-consecutive-inability regex remain unchanged for `features.bounded_continuation=false`. New runs default to the bounded policy. Archived configurations that predate the feature resume with the legacy setting; the configuration/manifest layer owns that compatibility rule. A synthetic workbench without a config/feature also uses the legacy path.

`features.recovery_evidence=true` adds the evidence-aware policy below. Its independent switch supports same-source ablation; it has no effect while `bounded_continuation=false`. The original three/six-turn policy remains available with `recovery_evidence=false`. The first sections document that original policy; the final section describes the new two/six-turn policy.

## Exact budget and boundary semantics

The first completed final for a `(thread_id, episode_id)` establishes a baseline at the current `native_action_count`. Its tools seed the deduplication set; it does not consume a recovery opportunity. Every later unique completed turn at the same execution count increments `count`:

- Without a new eligible tool fingerprint, `idle_count` increments. The **third consecutive such final** stops the attempt. Pure final-to-final loops therefore receive **three continuation turns**, stopping at the fourth completed final including the baseline.
- With a new eligible fingerprint, `idle_count` resets, but `count` continues increasing. The **sixth completed recovery turn without native execution** stops even if every turn contains different tool calls. Such an episode receives at most six continuation turns, stopping at the seventh completed final including the baseline.
- An increase in actual `native_action_count` establishes a new baseline and resets both counters. This includes partial execution on a failed action; zero-step infeasible motion alone does not reset the execution budget.
- A different thread or episode establishes a new baseline. Reprocessing the same completed turn is ignored. Missing, invalid, or regressed execution counts produce an explicit unknown observation and cannot establish a stall; a proper episode reset must change the episode identity.

The native execution count is public robot-action accounting, not a task-object oracle. The policy deliberately does not claim that every executed step advances the task; a wait or ineffective executed motion resets this budget too. Existing native-step limits and other supervision still apply. Its purpose is to bound **automatic continuation after finals**, not to impose another timeout on normal long turns.

Constants and policy metadata live in `auriga/cortex/supervision/actor_stall.py`:

- `ACTOR_PROGRESS_IDLE_LIMIT = 3`
- `ACTOR_PROGRESS_RECOVERY_LIMIT = 6`
- `ACTOR_PROGRESS_POLICY.version = bounded_completed_final_progress_v1`
- `ACTOR_PROGRESS_REASON = actor_no_progress`

## What earns bounded recovery credit

`completed_turn_activity` reads only `tool_call` and `observation` records for the owning thread, turn and current episode. `recovery_fingerprints` canonicalizes the eligible public requests. Requests demonstrate attempted investigation, not that the tool succeeded, produced new knowledge, or established a reachable recovery route. Even invalid or endlessly varied requests cannot bypass the six-turn ceiling.

Eligible requests cover calibrated geometry, cost previews, synchronized trajectory sample inspection, contact overlays, historical image views/crops, disclosed capabilities, explicit motion attempts, manipulation templates, and actor Python computations. Optional grasp/motion review, `vision_review`, and `operation_verify` requests are also eligible (`operation_verify` was removed in the 2026-10 refactor; use `vision_review` with `evidence_mode="operation"`). Ordinary `robot_observe` calls and `task_plan` prose revisions earn no novelty credit. The native counter independently accounts for all executed motion.

Deduplication removes action/request/call IDs, preview labels, timeout and call-budget settings. Preview candidates and requested trajectory native-step samples are fingerprinted individually, so reordering or regrouping candidates does not make the same hypotheses new. Observation IDs are mapped to the referenced episode and execution step: another capture UUID at the same paused state is not new, while a view from a different historical step can be. Missing/foreign references share an unverified marker and cannot create unlimited ID-based novelty. Python source uses an AST fingerprint to ignore comments/formatting; observation UUIDs within source are normalized too. Fingerprints and bounded counters are audit evidence, not a semantic novelty detector or proof of grasp/retention.

No final-answer words are inspected by the bounded path. The existing task-plan schema has no structural blocked-status field, so this change adds no tool or plan status and does not scrape natural-language observation gates.

## Integration and scoring

`gateway.wait_benchmark` runs this check only after the existing completed-turn, native terminal, model budget, shutdown, unresolved action, active action and active program checks, including the recheck after acquiring the control lock. Before a triggered stall actually becomes the stop reason, it checks terminal/budget/unresolved work again to cover callbacks during evidence collection. Native terminal results and unconfirmed execution retain priority.

Each accepted observation is emitted as `benchmark.actor_progress`, with version, context, current native count, baseline flag, total and idle recovery counts, fingerprint novelty and trigger reason. The final observation is also exposed in `harness.actor_stall`/the run summary. An unknown count is labelled `status=unknown`, not silently called no progress.

A clean `actor_no_progress` stop remains a harness task-limit failure with the existing valid native process score. It never manufactures native success. Unknown/inflight execution or invalid evaluation retains the existing exclusion/unknown handling. Legacy runs retain `benchmark.actor_inability` and `actor_repeated_inability`.

## Offline validation

Replayed over a recorded RoboDojo episode in which the actor produced 92 completed finals and 92 automatic continuations while the native count stayed nearly unchanged, the bounded policy stops at **final 7 / continuation 6** and preserves the turns that introduced new recovery fingerprints. This is a replay of the policy over saved evidence with no model, robot, physics, or evaluator call, not a counterfactual physical-success claim.

`tests/test_actor_progress_stall.py` includes a compact 93-event activity/native-count sequence of that shape (92 completed finals plus one failed turn, which is excluded) as a self-contained regression, plus request-normalization and live wait-loop fakes. Existing legacy and benchmark-terminal tests remain relevant.

Validation command:

```bash
PYTHONPATH=tests mamba run -n Auriga python -m unittest \
  test_actor_progress_stall test_actor_stall test_benchmark_terminal_policy -q
```

These CPU tests cover the exact off-by-one boundaries, varied exploration ceiling, duplicate turns, repeated requests, actual execution resets, unknown/regressed counters, episode/thread changes, the legacy switch, normal running turns, and terminal/unknown/inflight priority. They do not measure robot task success rates.

## Recovery guidance v1

When `bounded_continuation` is enabled, a continuation now appends the current actor observation ID, known native budget and measured home status. It asks for one observable recovery objective, the changed assumption/approach and a budget for approach, correction/manipulation, release/verification, withdrawal, necessary final return and uncertainty. A cheap approach alone is not a complete recovery. Unknown counters stay null. This helper reads no evaluator, scene-object or contact state and performs no robot calls. The existing stall thresholds, native-execution counter semantics, termination priorities and scoring remain unchanged; guidance does not infer useful task progress. Disabled bounded continuation retains the prior exact prompt. Startup archives the guidance version/text and each continuation records the actual context and delivered prompt.

## Evidence-aware recovery v2

On hard RoboDojo layouts, different endpoints alone were observed to keep the original investigation counter alive although their previewed motion already exceeds the unchanged episode budget. The new policy distinguishes those nominal candidates from genuinely missing information. It does not infer that all grasps, routes or the task are infeasible.

`completed_turn_evidence` in `auriga/cortex/supervision/recovery_evidence.py` uses only current-thread/current-turn public tool requests, observation metadata, actually delivered RGB hashes and the `motion.cost_preview` event type. It selects that event type in SQL before decoding and never scans evaluator events or reads artifacts, objects, contacts or simulation state. A preview must be zero-step, verify an unchanged measured state, and match the current native count. An over-budget classification additionally requires a known nonnegative integer remaining budget and matching preview accounting. A missing, stale, boolean or mismatched budget never establishes a rejection. The cost is the exact count of one nominal compilation, not a lower bound for every route or an estimate of a whole recovery operation.

Each candidate is keyed by its normalized requested motion, measured-state hash and remaining budget. Label changes, generated action IDs and new observation UUIDs at the same state do not create a new candidate. The completed-turn trace and continuation carry a compact ledger of the most recent six tested candidates, their nominal costs, outcomes and budget scope, with a count of earlier entries. The exact underlying requests and full cost evidence remain in the existing recorded events.

With valid evidence collection:

- The baseline still grants recovery without immediately stopping. **Two consecutive completed recovery turns with no new eligible information** stop automatic continuation. This is two additional opportunities after a baseline, not a stop after one failed candidate.
- A new candidate whose nominal cost is already above the same known episode budget does not refresh idle credit. Budget-only rounds get the explicit `budget_rejection_final_limit` reason; other unchanged rounds get `no_new_evidence_final_limit`.
- A genuinely different affordable or unresolved candidate keeps bounded investigation credit. An affordable approach alone is not declared a viable whole operation. Changed outcomes for the same candidate can also add credit.
- Actually delivered RGB uses `(camera_id, source_sha256)`, so fresh IDs without changed pixels add no credit. A newly obtained missing trajectory for an already tested candidate can add credit once. Default plots attached to endlessly varied unaffordable endpoints do not refresh the budget-only loop.
- Other public investigation requests retain the original bounded request credit because there is no general, sound way to prove their semantic usefulness. All varied investigation remains limited to **six completed recovery turns without native execution**.
- Actual native execution, new thread or new episode still resets the plateau and ledger. Unknown/regressed native counters still cannot establish a stop. If the evidence reader is unavailable or its native binding loses a race with execution, the tracker falls back to the original request-credit policy and three/six limits.

`ActorProgressTracker(evidence_aware=True)` exposes `recovery_context()` for `recovery_guidance(..., evidence=...)`. Guidance v4's continuation message supplies the tested-candidate ledger and recovery-specific counter-evidence rules. The main request reminder supplies the short decision boundary alongside the current plan: choose a useful bounded next step and verifying feedback, budgeting necessary release, withdrawal, required return and uncertainty. Each owned actor request receives the complete current snapshot because the provider path does not retain a dependable cross-request assistant-message cache; duplicate Harness reminders are removed before the latest snapshot is appended. Remaining task cost matters without requiring a complete route to success before useful action. If an expected event is absent, retain the unresolved prerequisite and change the hypothesis or choose useful work that neither depends on nor interferes with it. Unknown later outcomes stay unknown; material changes update the plan and affected attention.

V3 clarified v2's “complete attempt” wording, but only after a completed actor turn. V4 adds the prerequisite distinction and delivers the decision boundary through `CodexSession._task_decision_context` once a plan exists, for both ongoing execution and recovery. It is not repeated in the continuation user message. Every owned actor request carries the current plan snapshot; the projection removes only prior Harness reminders and leaves ordinary assistant content unchanged. Developer instructions remain unchanged. This planning reminder is independent of continuation flags. It does not copy recovery counters, the candidate ledger or the full operation-notes table into every request, and does not monitor inner actions of a direct-action program.

Guidance still forbids treating a single candidate rejection as global infeasibility, repeating unchanged candidates, or adding idle motion solely to refresh accounting. The actor need not consume all remaining steps or preview calls. Native terminal priority, unresolved-work handling, official limits, execution admission, success detection and scoring are unchanged. The v1 continuation text remains unchanged when recovery evidence is disabled. Existing integration tests cover actual continuation text/version, a composed HTTP request containing the core once, and same-turn delivery of updated concerns, clearing/compaction, identical retries and helper-thread exclusion. These checks establish delivery, not improved native task outcomes.

### Validation and replay

Focused CPU tests cover budget/measurement binding, same-candidate deduplication, alternative affordable routes, changed results, real delivered RGB, missing trajectory evidence, unknown budget fallback, execution races, native resets, preserved feature-off behavior and compact guidance. Run:

```bash
PYTHONPATH=tests mamba run -n Auriga python -m unittest \
  test_recovery_evidence test_recovery_guidance test_actor_progress_stall \
  test_actor_stall test_benchmark_terminal_policy test_recovery_gateway_integration -q
```

The independent gateway integration checks cover callbacks during baseline evidence collection, stall-event recording and recovery-guidance preparation. Native termination, model limits, unresolved actions/programs and shutdown retain priority before a new actor turn; native accounting callbacks reset the plateau. Turning the evidence feature off retains the original bounded policy, and turning bounded continuation off retains the legacy declaration policy.

Read-only replay over recorded hard cases reduces the accepted completed finals in some episodes. Where an episode keeps producing different affordable approach candidates, the policy conservatively retains investigation credit; guidance and planning behavior must be measured live. These are saved-evidence policy boundaries, not counterfactual model-cost, task-success or physical-performance claims.
