# Pinned Native Codex Model Catalog

`auriga/cortex/session/codex_models.json` is an unmodified copy of OpenAI Codex's upstream model catalog at tag `rust-v0.160.0`:

- **Upstream URL**: `https://raw.githubusercontent.com/openai/codex/rust-v0.160.0/codex-rs/models-manager/models.json`
- **SHA-256**: `fd219bd9f061278275f528939f82f54d2eb97df4b25c23b022adbe48813d920b`
- **License**: Apache-2.0 (the upstream `LICENSE` and `NOTICE` files are retained in this directory).

## How Auriga Uses This Catalog

At session startup, `write_direct_model_catalog` creates a run-private copy of this catalog and sets each entry's `tool_mode` to `"direct"`. Because native model metadata takes precedence over code-mode feature flags, passing an explicit catalog through Codex's static model manager ensures that network model discovery cannot silently override the selected tool mode.

All model identifiers, reasoning settings, context window limits, system instructions, and capability metadata remain untouched. This guarantees reliable typed image delivery without substituting models or requiring the actor to parse image URLs from text.

## Updating the Snapshot

Update this snapshot in lockstep with the pinned Codex SDK/CLI dependencies and `auriga/cortex/session/codex_responses.json`. After any upgrade, re-run the native loopback transport tests and native preferences tests—a schema-only check is not sufficient to verify the model-facing tool catalog or image delivery payload.
