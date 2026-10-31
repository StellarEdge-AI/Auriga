# Official Task Rules and Scoring Tables

New benchmark runs inject the **Instruction, Description, Comment (if present), and full Scoring table** verbatim from the official RoboDojo task pages. The source text is never summarized, rewritten, truncated, or supplemented: fixed object counts, per-tier point values, numeric thresholds, punctuation, and even contradictions present on the official website are preserved exactly as published. Converting HTML to plain text and Markdown tables normalizes whitespace, decodes HTML entities, and escapes Markdown table delimiters without altering the prose.

The current [catalog](../../auriga/cortex/context/robodojo_task_rules.json) is at **version 2**, covering all 42 base tasks and 12 `random` variants (each `random` variant inherits the text of its corresponding base task). In total, the catalog contains 42 Instructions, 42 Descriptions, 2 Comments, 42 Scoring tables, and 147 individual scoring conditions. Website navigation chrome, video player controls, and page metadata (such as Platform, Category, Data Source, and Usage) are not part of the task instructions and are excluded from model context.

## Provenance and Reconstruction

The catalog was extracted on `2026-10-09` from all 42 official task pages on `robodojo-benchmark.com` (all returning HTTP 200). Raw HTML files, capture timestamps, URLs, and cryptographic digests are archived in the [source snapshot](../../scripts/data/robodojo-public-rules-20261009/README.md). Every rule originates exclusively from `robodojo-benchmark.com`, with no additions from RoboProbe task recipes, evaluator source code, or layout ground truth.

To verify the current JSON catalog offline against the frozen HTML snapshot:

```sh
python scripts/extract_robodojo_task_rules.py
```

Pass `--write` to regenerate the JSON catalog from the local snapshot. The extractor reads only local frozen files and aborts with an error if any task fields, table structures, or item counts deviate from expectations—never silently dropping unrecognized sections. Although the upstream HTML pages on `2026-10-09` matched the earlier `2026-10-04` snapshot, the legacy summarized catalog and the version 2 verbatim catalog represent distinct model input conditions.

## Prompt Injection and Session Resume

[`public_task_rules()`](../../auriga/cortex/context/public_task_rules.py) loads the frozen catalog and concatenates the complete sections in their original order without appending framework commentary on object counts, execution order, or scoring logic. Public provenance metadata and digests are saved in the run manifest while the model receives only the body text.

[`Workbench.start`](../../auriga/apps/gateway/workbench.py) preserves the live native instruction emitted by the environment for the current episode and appends the verbatim official task rules alongside it. This task-specific rules block is independent of the general-purpose `Task notes`—disabling default operation notes does not disable the official task rules. Framework tool documentation, budgets, general operation notes, and execution semantics continue to follow the active run configuration without introducing non-official RoboProbe notes or task-specific execution scripts.

Resuming a run loads the archived `public_task_rules` directly from the original run manifest rather than re-reading the current catalog or re-sending the initial task prompt, so older runs are never upgraded to version 2 in place. Evaluations must distinguish between legacy and version 2 input conditions; updating the task rules catalog does not alter the task list, seeds, layouts, 15-case sampling, or four-way partitioning of the 42-task benchmark.

Whenever the official Description and Scoring table conflict on the website, both sections are preserved verbatim. For example, `press_by_number` no longer strips the disputed confirmation order, and `arrange_largest_number` no longer rewrites the stated fixed count to match the current layout's object count. The Harness never edits official text to resolve upstream inconsistencies, nor does it treat static scoring documentation as a live evaluator readout for the current episode.

## Validation

`test_public_task_rules` verifies exact catalog reconstruction against the frozen HTML snapshot, checking inline HTML text extraction, complete scoring table rows, rejection of malformed pages, startup manifest archiving, and legacy-version resume behavior. `test_model_proxy` verifies that all 54 task entries remain character-for-character identical after passing through initial prompt filtering and actual loopback HTTP `/responses` and `/responses/compact` egress. Finally, `scripts/check_package.py` verifies that out-of-checkout wheel builds package the version 2 catalog and complete scoring tables.

These checks verify content integrity and prompt delivery mechanics; they do not execute robot tasks or assert that the official website prose always matches the native Python evaluator implementation.
