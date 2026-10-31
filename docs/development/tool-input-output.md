# Tool Cost Summaries, On-Demand Help, and Actionable Input Errors

The tool presentation layer never alters motion execution, admission checks, retries, or native task evaluation. Feasibility flags, budgets, executed step counts, and errors in a summary must come directly from the underlying return value, while complete evidence remains accessible on demand.

## Current Invocation Entry Point

Every public tool passes through `ToolRegistry.invoke` to validate caller role, single-source JSON Schema, disabled-tool configuration, observation profile, and backend/service availability before entering its handler. The catalog presented to the model is generated from the exact same declarations—it is not a loose advisory description. Extra fields, wrong types, and non-finite numbers are rejected before execution with a structured `invalid_tool_arguments` error and a JSON Pointer `field_path`. Unavailable tools return specific capability or configuration reasons. Downstream domain owners continue to verify current observations, action ownership, and budgets regardless of schema validation passing.

The current actor motion interface is `robot_action`. The `robot_preview_cost` and `manipulation_run` sections below—along with `motion_cost_output.py` and the template modules—describe legacy tools removed in the 2026-10 refactor. They are retained here solely to document historical reply formats; those tool names are no longer exposed to actors or programs. New read-only tools are registered via `ToolSpec` without maintaining separate allowlists across the catalog, dispatcher, and program broker; see [Tool Registration and Extensions](tool-plugins.md).

## Motion Cost Preview (Historical)

`robot_preview_cost` defaulted to `detail="summary"`. Each candidate retained `label`, `status`, `feasible`, `reason`, compiled native steps/duration, per-arm motion/gripper/wait costs, maximum and total joint travel, budgets, failing guards, and key trajectory evidence. A planning preview is not a future measurement and does not guarantee target arrival, contact, grasp retention, or continuous-time collision avoidance.

`motion_cost_output.py` (removed) compacted per-joint details for known passing guards, repetitive normal checks inside failing guards, duplicate provenance descriptions, and storage metadata. Every rejection witness, threshold, failure reason, over-budget breakdown, and ineligible periodic check was preserved value-for-value; unknown policies, unknown statuses, anomalous shapes, and new extension fields were retained conservatively. Trajectory samples, named joints, units, key poses, minimum model clearances, anomalies, and overlay metadata were never dropped by cost compaction. The full payload was stored in an actor JSON artifact referenced by `full_result_ref` and readable via `AurigaClient.artifact` without re-running preview or executing motion. Passing `detail="full"` explicitly returned the complete original object, and internal program sub-calls defaulted to `full` when `detail` was omitted.

In read-only static re-projection over historical delivered preview replies, total UTF-8 byte volume dropped to roughly 62% of the original size, every full artifact reconstructed the exact original result value-for-value, and passing vs. rejected guards produced the summaries described above. That figure reflects only cost projection over identical historical payloads; adding trajectory samples or overlay images changes actual reply sizes and does not establish token savings or task gains in live experiments.

## On-Demand Template Help (Historical)

Requesting help from `manipulation_run` neither launched a program nor executed robot motion:

```json
{"parameters": {"kind": "help"}}
```

This returned the permitted template names, their purposes, and per-template help handles. Selecting a specific template retrieved its parameter schema and a structural example:

```json
{"parameters": {"kind": "help", "template": "place_release_observe"}}
```

The response contained only the selected template's schema, one structural example, and its applicability semantics. Repeated pose and move schemas were deduplicated via local `$defs` / `$ref`; place and handoff templates omitted lift parameters, whereas grasp templates marked lift as required. Placeholders in examples had to be replaced with live observations, actor reasoning, and caller-chosen poses from the current turn rather than fixed task coordinates.

To retrieve the complete catalog explicitly:

```json
{"parameters": {"kind": "help", "detail": "full"}}
```

The legacy Python helper `catalog_for(...)` continued to return the full catalog. Disabled templates were never unlocked by querying help, and passing unknown fields alongside a read-only help request returned the exact offending `field_path` so callers would not mistakenly assume other parameters had been executed.

With airborne handoff enabled, UTF-8 serialized sizes were 1,002 bytes for the short catalog, 14,583 bytes for the full catalog, and 8,647 / 8,372 / 8,528 bytes for the single-template grasp, place, and handover schemas, respectively. Each single-template response retained full parameter constraints and semantics so callers never had to guess inputs from a template name alone.

## Actionable Input Errors

At the exception boundary of both model tool handlers and the program broker, [`tool_errors.py`](../../auriga/core/tool_errors.py) returns `success=false`, preserves the original `error` string and stable error code, and attaches:

- `field_path`: A JSON Pointer to the offending field, or `""` (indicating the parameter root) when evidence is insufficient to pinpoint a deeper path—never fabricating a narrower pointer.
- `actual`: The offending value, a missing-field marker, or a bounded shape summary (avoiding echoing full program source code or large objects).
- `suggestion`: A corrective hint derived from the existing validator.
- `minimal_example`: A local structural snippet or placeholder—never a pre-validated executable action or an automatic retry command.

For example, omitting position in a template `move` returned `invalid_manipulation_template_parameters` alongside `/parameters/move/grasp_position_m` and `actual={"present": false}`. When `paired` waypoint lists had unequal lengths, the error reported the actual left and right counts and explained either supplying matching synchronized targets or switching to `independent` when independent progression was intended, without auto-padding poses.

Both `released_unverified` and `retained` required an existing release observation ID, with `retained` additionally requiring visible evidence. When release evidence was missing, the corrective hint instructed keeping status as `unknown` with honest uncertainty rather than suggesting a fabricated ID or downgrading to `released_unverified` just to bypass validation.

Ordinary runtime exceptions likewise return `success=false`, which does not imply zero physical motion or task failure/success. When execution may have already started, the suggestion explicitly instructs checking live action status first; the presentation layer never replays an action. `Harness.dispatch` continues to raise exceptions per its legacy contract, and program call idempotency records persist the same failed reply.

CPU validation entry points: `tests.test_tool_errors` and `tests.test_tool_output_integration`, alongside existing program and Codex regression suites (`test_motion_cost_output`, `test_template_help`, and `test_motion_cost` were removed along with the legacy tools). No live model or simulator is required.
