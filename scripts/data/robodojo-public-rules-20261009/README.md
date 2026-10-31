# Official RoboDojo task text, 2026-10-09

The catalog is copied from 42 live public task pages at `https://robodojo-benchmark.com/doc/sim-tasks/`. All returned HTTP 200 between 2026-10-09 06:14:33.402581 and 06:14:33.731583 UTC. No archived fallback was used. All 42 downloaded HTML hashes also match the untouched 2026-10-04 snapshot.

`raw-pages.json.gz` stores each original UTF-8 HTML document, SHA-256 of the downloaded bytes, request URL, final URL, HTTP status, UTC retrieval timestamp, exact runtime base-task name, and the extracted task text. The `text` field contains the selected task sections with HTML converted to plain text and scoring tables formatted as Markdown. Downloads used Python standard-library `urllib.request.urlopen(url, timeout=45)` with eight concurrent workers; no authenticated source, evaluator, or recipe was consulted.

The catalog retains the existing 54 runtime task names and their source mapping: 42 base tasks plus 12 random-layout variants that reuse the exact base-page sections. Its public text consists of complete Instruction, Description, optional Comment, and Scoring sections, in source order with their original headings. Platform, Category, Data Source, Usage, navigation, and video UI are excluded. HTML entities are decoded, whitespace is normalized, inline text nodes are concatenated without invented spaces, and every scoring header and cell is retained. No task wording, object count, contradiction, or scoring condition is corrected or interpreted.

Reproduce or check from the repository root:

```sh
python scripts/extract_robodojo_task_rules.py --write
python scripts/extract_robodojo_task_rules.py
```

The check validates source hashes, section/table structure, source mapping, 42-page/54-entry coverage, and exact catalog equality. `verification.json` records a separate regex-based comparison of original HTML table cells against all emitted sections: 172 selected task-information cells and 378 scoring cells (189 scoring rows including 42 headers). Both original Comment rows are retained. This validates source transcription, not native task success or evaluator agreement. Website contradictions are intentionally preserved, including the four-number description and both four-/five-digit scoring cases for Arrange Largest Number.
