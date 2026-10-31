# Pinned native model catalog

`auriga/codex_models.json` is an unmodified copy of OpenAI Codex's model
catalog at `rust-v0.160.0`:

https://raw.githubusercontent.com/openai/codex/rust-v0.160.0/codex-rs/models-manager/models.json

SHA-256: `fd219bd9f061278275f528939f82f54d2eb97df4b25c23b022adbe48813d920b`.
The upstream Apache-2.0 LICENSE and NOTICE are retained in this directory.

`write_direct_model_catalog` creates a run-private copy and changes only each
entry's `tool_mode` to `direct`. Native model metadata takes precedence over
code-mode feature flags. The explicit catalog uses Codex's static model manager,
so network model discovery cannot silently undo the selected tool mode.
Model identifiers, reasoning settings, context limits, instructions and other
capability metadata remain unchanged. This fixes typed image delivery without
substituting a model or requiring the actor to extract image URLs from text.

Update this snapshot together with the pinned SDK/CLI dependencies and
`codex_responses.json`. Re-run the real native loopback transport test and the
native preferences tests after any upgrade; a schema-only check does not prove
the model-facing tool catalog or image payload.
