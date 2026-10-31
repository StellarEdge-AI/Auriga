# Contributing to Auriga

Thanks for your interest. Auriga controls physical (simulated) robots from a model, so changes are judged by two things: whether the software contract still holds, and whether task behaviour was actually measured. Read [AGENTS.md](AGENTS.md) before your first change — it is the authoritative development guide for both people and coding agents (in Chinese).

## Set up

```sh
mamba env create -f environment.yml      # one conda env named Auriga; no .venv
npm --prefix web ci
```

Use `mamba run -n Auriga python` (or the activated `Auriga` env) for everything: tests, scripts, the gateway and the queue. The RoboDojo worker and the optional OpenPI policy run in their own interpreters; see [RoboDojo environment](docs/development/robodojo-setup.md).

## Checks

The CPU checks need no GPU, simulator or model and are what CI runs:

```sh
mamba run -n Auriga python -m unittest discover -s tests
npm run check --prefix web
npm run build --prefix web
mamba run -n Auriga python scripts/build_package.py --output dist
mamba run -n Auriga python scripts/check_package.py dist/<new wheel>.whl
```

For a focused run: `PYTHONPATH=tests mamba run -n Auriga python -m unittest test_tool_registry`. The [validation guide](docs/development/validation.md) maps each kind of change to the checks it needs. Passing CPU checks says nothing about task success; claims about motion or task performance need native runs under frozen conditions.

## Rules that reviews enforce

- **One physical entry point.** Anything that moves the observed world goes through `Harness.execute`; plugins use `ToolContext.execute`.
- **Facts, not text.** Programs and tests consume `ToolResponse.kind/data`, never display text or exit codes, to decide whether an action happened. Keep unknown outcomes unknown.
- **Complete tool contracts.** Every new tool, parameter or helper documents its physical meaning, unit (m, rad, deg, s), default and when to use it, in the contract that also drives validation. No hidden heuristics, magic thresholds or silent overrides of what the model asked for.
- **Information boundary.** Models only read permitted observations. Hidden simulator state, evaluator internals, credentials and metadata such as hashes stay out of model-visible context.
- **Regression tests** for non-trivial logic, especially failures, partial execution, retries and compatibility with older archives.
- **Docs follow code.** When a public contract changes, update the relevant guide and the [documentation index](docs/README.md).

## Pull requests

Keep a pull request to one concern, describe what you changed and how you verified it (commands and results, including skips), and never commit credentials, run directories, recordings or model weights.
