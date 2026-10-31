# Official RoboDojo Task Text Snapshot (2026-10-09)

This directory contains the verbatim snapshot of the 42 public RoboDojo task pages retrieved from `https://robodojo-benchmark.com/doc/sim-tasks/`. All 42 pages returned `HTTP 200` between `2026-10-09 06:14:33.402581` and `06:14:33.731583 UTC` without using any archived fallback, and all 42 downloaded HTML SHA-256 digests match the earlier untouched `2026-10-04` snapshot.

## Contents & Structure

- **`raw-pages.json.gz`**: Stores each original UTF-8 HTML document, the SHA-256 digest of the downloaded bytes, request URL, final URL, HTTP status, UTC retrieval timestamp, exact runtime base-task name, and extracted task text. The `text` field contains the selected task sections with HTML converted to plain text and scoring tables formatted as Markdown. Downloads used Python's standard library (`urllib.request.urlopen(url, timeout=45)`) with eight concurrent workers; no authenticated source, evaluator code, or reference recipe was consulted.
- **Task Coverage**: The catalog covers all 54 runtime task names and their source mappings—42 base tasks plus 12 random-layout variants that reuse the exact base-page sections.
- **Included Sections**: Complete `Instruction`, `Description`, optional `Comment`, and `Scoring` sections in source order with their original headings. (`Platform`, `Category`, `Data Source`, `Usage`, navigation menus, and video UI elements are excluded.)
- **Fidelity**: HTML entities are decoded, whitespace is normalized, inline text nodes are concatenated without invented spaces, and every scoring header and cell is preserved verbatim. No task wording, object count, upstream contradiction, or scoring condition is altered or interpreted.

## Reproducing & Verifying

Run from the repository root using the `Auriga` environment:

```bash
# Re-extract the catalog from the archived HTML snapshot
mamba run -n Auriga python scripts/extract_robodojo_task_rules.py --write

# Verify hashes, section/table structure, and exact catalog equality
mamba run -n Auriga python scripts/extract_robodojo_task_rules.py
```

The verification check validates source hashes, section/table structure, source mappings, 42-page / 54-entry coverage, and byte-exact catalog equality. `verification.json` records an independent regex-based comparison of original HTML table cells against all emitted sections: 172 selected task-information cells and 378 scoring cells (189 scoring rows including 42 headers), retaining both original `Comment` rows. Website contradictions (such as the four-number description alongside both four- and five-digit scoring rows in *Arrange Largest Number*) are intentionally preserved verbatim.
