# Default Task Notes: Manipulation Heuristics and Planning Guidance

By default, new benchmark runs inject the `OPERATION_NOTES` manipulation guidance defined in [`actor_instructions.py`](../../auriga/cortex/context/actor_instructions.py) alongside the free-text `task_plan` tool. These notes serve as a reference menu of general physical heuristics to apply as appropriate to the scene, rather than a mandatory form to fill out on every turn; see [Unified Actions and Program Approval](unified-actions.md) for the current action interface. The sections below summarize the evolution of each version; resuming an archived run always uses the exact text saved in its manifest.

New runs use **`unified_operation_notes_v7`**. The startup guidance distills general physical principles across six areas:
1. **Task and geometric reasoning.**
2. **Grasping, transport, and bimanual coordination** (including fingertip-to-support clearance, recognizing that deeper finger insertion is not always more stable, and lifting clear of the support surface before lateral transport or folding).
3. **Cross-arm handoffs when a single arm cannot reach, and synchronized bimanual manipulation.**
4. **Placement and release.**
5. **Counter-evidence recovery after failures** (including using `actual_contact_position_m` and `approach_shortfall_m` after a `tracking_error` to adjust the contact height or contact segment rather than sweeping laterally at the obstructed height or repeating the same blocked descent).
6. **Parallelism and full-horizon step budgeting** (including avoiding unnecessary intermediate returns to home between subtasks).

When choosing grasp points and support strategies, the model is reminded to account for planned wrist rotations and final delivery orientations, recognizing that an initial test lift does not prove retention after wrist rotation and that repeated jaw closure commands do not increase grip force.

Once a plan has been submitted, routine non-oversized `/responses` requests neither rewrite conversation history nor strip-and-append reminders at the tail of the request, keeping each request a strict prefix extension of the previous one so upstream prompt caches remain valid. Only when context length triggers a `/responses/compact` compaction request does the egress hook strip prior Harness snapshots from the input and inject the current complete `plan`, `attention`, and `ledger` snapshot alongside a concise decision boundary. That snapshot remains framed as tentative assistant judgment rather than elevated to developer instructions. While `concern` / `check` / `when` entries in `attention` track immediate questions, task-stage prerequisites can be tracked in the optional `ledger` field (`id` / `expected` / `status` / `basis` / `next_check`, up to three entries) so that critical boundaries identified during planning are not lost across subsequent actions or context compactions.

## `task-evidence-boundary-v1`

The evidence `ledger` is an optional field on the existing `task_plan` state owner that leaves `attention` semantics unchanged. It holds at most 3 concise entries per episode:
- `status` must be one of `pending`, `supported`, `contradicted`, or `unknown`.
- `basis` cites permitted current RGB views, measured proprioceptive states, or typed action receipts (or `unknown` when no evidence exists yet).
- `next_check` states the next observable fact needed to disambiguate the prerequisite.

Entry IDs must be unique within the list; providing a `ledger` array atomically replaces the list, omitting the field preserves the existing list, and passing `[]` clears it (legacy records lacking a `ledger` field read as an empty list).

`CodexSession` injects the current `ledger` and a brief evidence-boundary reminder at the `/responses/compact` egress boundary for the primary actor, while leaving standard `/responses` requests untouched to preserve prefix caching. The reminder states that a `pending` item is not an established prerequisite, that `completed` motion receipts, preview successes, and closed jaws do not prove object-level outcomes, and that repeated failures require revising the underlying hypothesis or choosing an action that disambiguates the cause. The ledger provides structured context and auditability without gating `robot_action` or `robot_wait` or requiring extra probing actions solely to populate fields. Full observation and action references remain in the internal Store while model-facing projections omit hashes, evaluator state, and internal source metadata. The recovery-specific `tested-candidate` ledger remains separately managed by the recovery supervisor and is not merged into the task ledger.

This mechanism targets the failure mode where the model identifies a physical constraint during initial planning but forgets that prerequisite several actions later, while still allowing fluid exploration. CPU regression tests cover schema validation, atomic updates, legacy archive compatibility, prefix preservation on standard `/responses` calls, snapshot injection and retries on `/responses/compact`, and helper-thread exclusion (establishing software wiring and boundaries rather than guaranteeing model compliance or higher native task scores).

During normal execution, `task_plan` tool calls and replies remain visible in the natural conversation history, supplemented by full snapshot injection on `/responses/compact` and by the candidate ledger, counters, and counter-evidence rules in recovery v4 continuation messages. Loopback HTTP tests verify strict prefix extension across consecutive `/responses` calls, same-turn concern updates and subsequent clearing prior to `/responses/compact` delivery, retry consistency, and helper-thread isolation.

## Earlier Versions of the Unified Operation Notes

### `unified_operation_notes_v5`

`unified_operation_notes_v5` refined four judgment categories in the table:
1. Verifying object ownership and category assignment against the public task rules.
2. Using fixed scene landmarks to distinguish true object motion from wrist-camera motion.
3. Verifying task-required placement positions and orientations while keeping unresolved misalignments active.
4. Grounding item counts in observed object events—recognizing that a failed motion can still displace an object and a `completed` motion may fail to produce the target event.

Planning instructions also clarified that when overall task understanding, object assignment, or strategy changes, both `plan` and affected `attention` items should be updated (whereas updating `attention` alone preserves the existing `plan` text). Unresolved issues that still affect the task should not be marked resolved without evidence, though entries can be revised or removed based on new evidence, explicit reinterpretation of prior evidence, or changing relevance.

State management and prompt delivery never resurrect stale revisions or drop entries silently: `TaskPlanning.current` cross-checks the active episode against persisted records, `decision_context` reads from the authoritative owner, and `CodexSession` passes the snapshot through the shared metadata filter at egress. Provenance for partial updates is tracked per field via `plan_observation_id`, `attention_observation_id`, and `ledger_observation_id`, refreshing only the fields explicitly submitted in a call (passing `[]` records the observation at which the list was cleared, and calling `submit` without `attention` clears the prior list and its provenance). Re-submitting identical field values under the same observation returns `unchanged` to avoid redundant revisions and context growth, whereas repeating text under a newer observation updates provenance to reflect the re-verified observation. Legacy archives without per-field provenance return `null` without backfilling.

### `unified_operation_notes_v4`

`unified_operation_notes_v4` organized manipulation heuristics into a **"Situation — Visual Check — Counter-Evidence Adjustment"** table: at the start of an episode, the model selects only considerations relevant to the current scene, and can adopt additional considerations later if new observations, grasp shifts, contact failures, or state uncertainties arise. It imposes no fixed checklist order, mandatory per-step plan rewrites, or verbose reasoning requirements.

The initial plan states the model's scene understanding, intended approach, and completion criteria, while active questions, verifying evidence, planned adjustments, and check timing go into `task_plan.attention`. Before repeating a previously ineffective action pattern, the model is guided to state a brief hypothesis for the cause and an observation or strategy change that distinguishes it. Checks should piggyback on feedback from normal task-advancing actions wherever possible, reserving dedicated probing actions for cases where missing evidence directly blocks the next decision.

The table added guidance on short-lift co-motion and source-site clearance, distinguishing robot contact-overlay projections from physical object contact, checking object-in-hand orientation after wrist rotations, verifying destination retention after release and withdrawal, and handling unconfirmed process states or item counts—while retaining guidance on bimanual parallelism and pipelining, cross-arm staging when out of reach, two-arm collaboration on a single object, auxiliary wrist-camera viewpoints from an idle arm, full-arm and held-object swept clearance, full-horizon step budgeting, and required final return to home.

### `unified_operation_notes_v3`

`unified_operation_notes_v3` introduced guidance on using an idle arm's wrist camera for auxiliary viewpoints while retaining the bimanual coordination heuristics from v2 (all of which remain carried forward in v4–v7):

- **Auxiliary observation with an idle arm.** When one arm is performing a manipulation and the other arm is idle, the idle arm's wrist camera can be positioned to view the object and active fingers from the side or an oblique angle—especially when the head camera cannot clearly resolve relative height or contact clearance. The observing arm must stay clear of the active workspace and account for its own motion cost in the episode step budget.
- **Bimanual parallelism and pipelining.** When one arm is occupied with a primary or difficult manipulation, the other arm can execute independent, easier subtasks in parallel to make full use of simulation steps rather than sitting idle. Whether to run subtasks in parallel or sequentially should be decided from workspace reachability, full swept volumes of both arms and held objects, and intermediate feedback dependencies rather than task difficulty labels alone.
- **Cross-arm staging when out of reach.** When the primary working arm cannot reach an object that the other arm can reach, the reachable arm can place the object onto a mutually reachable, stable support surface for the working arm to pick up, weighing reachability, support stability, clearance, and total step cost.
- **Bimanual collaboration on a single difficult object.** When a single arm cannot reliably manipulate an irregularly shaped or unstable object alone, the second arm can assist—for example, stabilizing or supporting the object while the first arm rotates or re-grasps it, either on a support surface or in mid-air depending on object geometry, grip stability, reachability, and clearance.

Cross-arm handoffs retain the default `airborne_handoff=true, handoff_preference="surface"` policy: recommending a stable support surface when appropriate without prohibiting mid-air transfers, and keeping handoff preferences distinct from single-object bimanual stabilization.

## Legacy `operation-notes-*` Versions (`v2`–`v16`)

[`auriga/cortex/context/task_notes.py`](../../auriga/cortex/context/task_notes.py) preserves the versioned prose strings that preceded the unified operation-notes table: `DEFAULT_TASK_NOTES` (`operation-notes-v16`), `PREVIOUS_DEFAULT_TASK_NOTES` (`v14`), and `LEGACY_DEFAULT_TASK_NOTES` (`v11`). `resolve_task_notes` selects among them according to `features.manipulation_protocol`, `features.task_scheduling`, and `features.operation_candidates` (`manipulation_protocol=false` selects `v11`; disabling both `task_scheduling` and `operation_candidates` selects `v14`; otherwise `v16` is selected), after which new runs replace the default text via `actor_instructions.task_notes` with the current `unified_operation_notes_v7` table. None of these versions contain scene ground truth, hardcoded target coordinates, hidden object dimensions, undisclosed scoring thresholds, or task-specific recipes.

- **`operation-notes-v16`**: Added scheduling heuristics comparing mechanism-changing recovery against switching to an independent subtask, delivering an already held object before or alongside the other arm's recovery, using `independent` dual-arm advancement for unequal path lengths, yielding shared corridors, verifying that withdrawal and next entry do not disturb already placed objects, and omitting unnecessary intermediate home returns for idle arms. Current feature defaults are `task_scheduling=true, operation_candidates=false, preview_intent=false, recovery_evidence=true`.
- **`operation-notes-v15`**: Added two independently ablatable mechanisms:
  - **Scheduling (`task_scheduling`)**: Clarified that prompt listing order and `plan` array order do not imply execution dependencies (which must come from task rules, physical prerequisites, or required observations, optionally noted in `dependency_reason`), and introduced `scheduling_failure_advisory(receipt, plan_record, task_scheduling=...)` in `legacy_task_planning.py` to surface up to three dependency-free plan stages for re-evaluation after a `failed` or `rejected` action receipt.
  - **Full-operation candidates (`operation_candidates`)**: Guided the model to evaluate approach, contact, transport, placement, separation, withdrawal, verification, and required home return as a complete sequence rather than starting an operation merely because the initial approach is cheap.
- **`operation-notes-v14` & `v13`**: Paired with `actor_task_plan_v7` / `v6` to evaluate grasp orientations relative to object shape, support posture, and graspable cross-section—preferring top-down or oblique approaches and vertical lifts over horizontal side sweeps through cluttered neighbors when grip stability and overhead clearance permit, and planning high-clearance placement/withdrawal paths over container rims and flaps.
- **`operation-notes-v12`**: Paired with manipulation templates v3 (removed in the 2026-10 refactor) to record structured contact regions, placement poses, rim clearances, and `released_unverified` vs. visually confirmed retention states, using explicit bounded `robot_wait` calls when physical settling must be observed.
- **`operation-notes-v11`**: Added guidance on adjusting object cross-section, finger insertion direction, and support posture together after a blocked approach (rather than merely switching `contact_region` to `finger_middle` and pushing down again), verifying placement retention while recovery is still affordable, and using cost previews at actual execution speeds to budget across all remaining objects. Also formalized the two-part handoff policy (`features.airborne_handoff` and `features.handoff_preference`, defaulting to `true + surface` = `prefer_surface`, with `true + airborne` = `prefer_airborne` and `false + surface` = `surface_only` preserved for backward compatibility).
- **`operation-notes-v10` & `v9`**: Condensed the ten numbered items below into seven thematic groups while separating **object grasp cross-section** from **finger contact region** (`tip_zone`, `pad_center`, `finger_middle`, or explicit `contact_depth_m`), clarifying that default `grasp_position_m` targets the center of the distal `15 mm` flat pad rather than the middle of the entire finger length, and keeping `slow` speed presets at `0.8` for both arms and jaws.
- **`operation-notes-v8` & `v7`**: Added finger contact-depth selection (weighing palm/fingertip clearance against deeper insertion) and removed the single-arm preference from `v6` so bimanual parallelism is scheduled purely by dependencies, reachability, clearance, and step cost.
- **`operation-notes-v6` & `v5`**: Consolidated earlier notes into 10 numbered items, introduced discrete speed presets (`speed_preset` and `gripper_speed_preset`, plus per-segment `waypoint_speed_presets` / `waypoint_speed_scales` with `slow = 0.8`), and advised against unnecessary mid-episode returns to home when the current pose neither obstructs the next stage nor blocks camera views.
- **`operation-notes-v4`, `v3`, & `v2`**: Incorporated early empirical lessons: ARX X5 nominal `88.8 mm` maximum interior jaw opening, centering two-finger grasps across a solid cross-section, comparing top-down vs. side grasps (and uprighting tilted objects on a support surface before re-grasping), aligning objects with container interior centers and clearing rims before slow jaw release, and slowing down during contact, grasp approach, initial lift, and placement.

## Default Injection, Custom Appending, and Disabling

- **Default behavior (no flags)**: New benchmark runs automatically inject the default operation notes.
- **`--task-notes '...' --task-notes-source '...'`**: Appends custom public instructions after the default operation notes without replacing them. Both flags must be non-empty when used, and custom text is capped at 8,000 characters (the built-in default notes do not count against this 8,000-character limit).
- **`--no-default-task-notes`**: Disables the built-in operation notes while still preserving any explicitly supplied `--task-notes`. If disabled without custom notes, no Task Notes section is injected.
- **Queue manifest**: Each case in a queue manifest accepts `"default_task_notes": false` (or `true`), inheriting the Gateway default when omitted.
- **Development smoke tests and resume/replay**: Standard development smoke runs do not inject Task Notes automatically, and `--resume` / `--replay` always honor the archived prompt from the run manifest (treating legacy manifests without the configuration field as having default notes disabled).
- **OOM retries and checkout pinning**: Automatic OOM retries of a newly launched episode re-use its frozen source copy and CLI arguments. Re-running a historical command line that omitted notes flags inside a newer mutable checkout will inherit the new default notes and must not be reported as an identical prompt condition; exact reproductions must either run from the frozen source checkout or explicitly pass `--no-default-task-notes` with the historical custom text.

For example, to disable default operation notes while supplying custom public instructions in a queue manifest case:

```json
{
  "default_task_notes": false,
  "task_notes": "Additional public operation or task instruction.",
  "task_notes_source": "public-document-or-experiment-protocol"
}
```

The Gateway assembles the initial prompt once at episode startup in the following order: complete `robot_information` briefing, Harness developer instructions, user task instruction (including official public task rules, initial step budget, and optional startup completion guidance), and finally the Task Notes block (default operation notes followed by any custom notes).

`task_notes_source` is stored solely as an audit provenance label in the archive—it is never sent to the model and never triggers network fetches or local file reads. The active `task_notes`, `task_notes_source`, and `task_notes_config` (recording the default switch, version identifier, custom text, and source label) are persisted to both `configuration/task_prompt-<digest>` and `manifest.json`, and `task_notes.py` is included in `source_sha256` and the run source archive. Offline benchmark reports read the archived configuration directly rather than reconstructing prompts from current code constants.

The independent `completion_guidance` setting defaults to `hook`: it sends an explicit return-to-home reminder at completed-turn boundaries only when measured dual-arm poses deviate from the home tolerance, using the standard continuation prompt when already at home or when pose evidence is unavailable. Startup prompt home reminders remain off by default and can be enabled via `completion_guidance="both"`; see the [Configuration Guide](configuration.md). References to returning home inside the operation notes describe trajectory planning for tasks that require homing, without imposing an extra homing requirement on tasks that do not.

## Summary of the Legacy Ten-Item Table (`v6`–`v9`)

For historical traceability, the table below summarizes the ten numbered items used in `operation-notes-v6` through `v9` before `v10` reorganized them into seven thematic groups and moved parameter syntax into tool schemas:

| Item | Manipulation Guidance Summary |
|---|---|
| **1. Task ordering & grasp selection** | Order subtasks by dependencies, current arm/object poses, reachability, occlusion/clearance, and downstream cost (without single-arm bias). Center jaws across a solid cross-section within the `88.8 mm` opening; explicitly choose distal pad, mid-finger, or fingertip contact relative to object shape, center of mass, and clearance; compare top vs. side grasps and adjust after empty lifts based on measured residuals. |
| **2. Gripper state** | Distinguish commanded goal, measured aperture, and physical object retention; neither a `completed` motion nor closed jaws prove a successful grasp. Select `slow` jaw speed independently for contact closure and release. |
| **3. Segment speeds, test lifts & handoffs** | Use `waypoint_speed_presets` / `scales` to combine normal open-space transit with `slow` (`0.8`) short contact segments; split into separate actions whenever intermediate visual feedback is needed. Verify retention after short lifts, wrist rotations, and cross-arm handoffs. |
| **4. Placement & release** | Target the usable interior center of containers while accounting for rim height, wall height, held-object orientation, bottom clearance relative to fingers, and full 3D swept volume; descend and release slowly after clearing the rim, and verify physical separation and stable retention from RGB rather than assuming disappearance implies success. |
| **5. Swept clearance & protecting placed items** | Maintain clearance along the entire path—especially during rotational wrist sweeps—for the held object, both arms, open jaws, and already placed items; inspect for dragging or tipping after close passes and update downstream targets if objects shift. |
| **6. Recovery from tracking & planning failures** | Inspect inner action receipts and measured poses; after a tracking error, correct the approach from the measured residual and confirm grasp alignment before closing jaws. Stop program loops on repeated rejections or zero progress, and adjust both position and orientation when targets are infeasible. |
| **7. View selection & displacement verification** | Reuse views already returned with action receipts before requesting extra captures; use wrist views, head views, ROI crops, or viewpoint adjustments to resolve occlusions. Judge object displacement against fixed background landmarks rather than end-effector motion alone. |
| **8. Stage transitions & final homing** | Preserve frame and TCP conventions across stages; omit intermediate returns to home or large empty-hand reorientations when the current pose does not obstruct the next stage or required camera views, while still completing and verifying any task-mandated final return to home. |
| **9. Observing dynamic processes** | When waiting for external motion, demonstrations, or physical settling, use `robot_wait` to advance simulation time and inspect temporal samples rather than sleeping in wall-clock time or re-reading static observations. |
| **10. Local policy delegation (`robot_policy_step`)** | When delegating to a local learned policy, state the local intent, execute a bounded step prefix, and inspect both arms and grippers afterward—recognizing that text instructions are not hard constraints and short prefixes bound duration rather than displacement. |

## Maintenance and Validation

Task notes must never hardcode fixed yaw angles, heights, step counts, task-specific object orderings, hidden evaluator thresholds, automatic jaw-release gates, mandatory multi-view captures on every step, or assumptions that a failed episode only needs to return home. Plausible-sounding heuristics do not establish benchmark improvements without controlled empirical comparisons.

1. Before adding a new heuristic, document the observed failure trigger, concrete action/observation evidence, potential counterexamples, and scope of applicability, then distill it into concise, general guidance.
2. Whenever `OPERATION_NOTES` in `actor_instructions.py` is modified, increment the version and source tag returned by `task_notes()` (currently `unified_operation_notes_v7`) and document the change here while keeping historical `operation-notes-*` constants intact in `task_notes.py`. Preserve the custom `--task-notes` append behavior.
3. Version updates apply only to newly launched episodes; existing sessions, run manifests, frozen benchmark queues, and historical reports are never rewritten.
4. CPU unit tests verify Workbench startup prompt assembly, default/disabled/appended modes, CLI and queue forwarding, manifest archiving, legacy resume/replay fidelity, and single-injection behavior across completion hooks:

```sh
PYTHONPATH=.:tests mamba run -n Auriga python -m unittest \
  test_task_notes test_completion_guidance test_benchmark_queue \
  test_benchmark_group test_benchmark test_gateway_native_failure \
  test_gateway test_gateway_expanded test_benchmark_terminal_policy
```
