# Token and Cost Accounting

Token accounting is derived from the cumulative `total` field in native Codex `thread/tokenUsage/updated` notifications. Each notification replaces the current snapshot rather than summing repeated cumulative totals. Native `last` and `total` payloads, raw artifacts, and turn associations are preserved, while structured `token_accounting` records are written to runtime state, per-turn status, notification events, live snapshots, and the final summary.

## Accounting Definitions

- `input_tokens` includes cached reads; `cached_input_tokens` is the cache-hit portion, and `uncached_input_tokens = input_tokens - cached_input_tokens`.
- `cache_hit_rate = cached_input_tokens / input_tokens`, formatted as a decimal string in `[0, 1]`, or `null` when `input_tokens` is `0` or missing.
- `output_tokens` already includes `reasoning_output_tokens`; reasoning output tokens are displayed separately for visibility without double-counting them in cost calculations.
- `cache_write_input_tokens` is preserved verbatim (`null` when absent, never coerced to `0`). Whenever separate cache-write billing semantics are unknown or the count is non-zero, a complete total cost is omitted.
- Missing or invalid token counts, unknown model/provider identity, or mismatched price cards keep the cost status as `unknown`. Aggregated reports output known subtotals, complete totals, and counts of known vs. unknown runs side by side without dropping failed runs.

`rate_card_estimate` represents the estimated cost computed from the specified price card, whereas `provider_reported` represents the billed amount reported by the upstream provider. Because native notifications currently report token counts without billing fields, `provider_reported` remains `null`—estimates are never labeled as actual billed charges.

## Official Reference Price Card

The repository includes the public reference price card `configs/pricing/openai-gpt-6-astra-standard-2026-10-01.json`: OpenAI Standard tier at `$10` per 1M uncached input tokens, `$1` per 1M cached input tokens, and `$50` per 1M output tokens, applicable when single-request input does not exceed `272,000` tokens. Auriga uses native `model_context_window <= 272000` as upper-bound evidence that standard flat pricing applies, rather than mistaking multi-million cumulative thread input tokens for a single long-context request. If the context window is unknown or exceeds `272,000`, the estimated cost remains `null` so per-request tier pricing can be calculated separately.

Official pricing also lists `$12.50` per 1M tokens for cache writes; the reference card records this rate for completeness, but only outputs a complete total cost when native telemetry explicitly reports `0` cache-write tokens. Fast, Ultrafast, Batch/Flex, regional surcharges, and long-context tiers follow separate pricing rules; this file serves strictly as a Standard-tier reference and does not claim that custom providers bill at these rates or tiers.

When all supported conditions are met, the estimated cost is computed as:

```text
cost = (uncached_input_tokens * 10 + cached_input_tokens * 1 + output_tokens * 50) / 1,000,000
```

Amounts are computed using exact `Decimal` arithmetic without premature rounding to cents. Price cards match on exact `provider` and `model` identifiers and record the card version, source, currency, applied rates, billing notes, and SHA-256 digest of the price card file. Resuming the same cumulative thread requires keeping the same provider, model, and price card; to recompute costs under a different price card, run an offline report:

```sh
python scripts/benchmark_report.py /absolute/run1 /absolute/run2 \
  --price-card configs/pricing/openai-gpt-6-astra-standard-2026-10-01.json \
  --output /absolute/new-cost-report.json
```

Historical archives never need to be rewritten: offline reporting reconstructs model identity from the run's frozen `manifest.json` / configuration and recalculates estimates from saved token counts. The gateway accepts `--price-card` or `AURIGA_PRICE_CARD` and freezes the complete price card into `manifest.json`.

## Experiment-Specific Configuration and Scope

Before launching each case, the automated evaluation queue reads `~/.config/auriga/experiments.json` (or an alternate file specified via `AURIGA_EXPERIMENT_CONFIG`). Only two absolute path keys are permitted: `codex_home` and `price_card`. A dedicated experiment Codex home contains `config.toml` and `auth.json` with file permissions set to `0600` and directory permissions set to `0700` so API keys never enter the repository or experiment reports.

The queue sets `CODEX_HOME` and `AURIGA_PRICE_CARD` exclusively for child gateway processes without mutating global Codex authentication. This remains compatible with frozen legacy actor checkouts: existing credential preparation copies the necessary credentials from the selected home into the run's private directory, and while older checkouts do not emit the newer live `token_accounting` fields, their native token counts can still be priced offline.

Updated accounting logic takes effect in newly launched queue processes. An already running queue continues using its original code and environment—including for cases that have not yet started—without hot-patching frozen checkouts or restarting active experiments. Credentials for the interactive development controller session remain separate from benchmark actors.
