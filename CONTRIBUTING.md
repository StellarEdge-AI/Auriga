# Contributing to Auriga

Thank you for your interest in contributing to **Auriga**! Because Auriga controls physical (simulated) robots from a frontier model in a closed loop, every change is evaluated on two criteria:
1. **Software contract integrity**: Do schemas, boundaries, and CPU/Web/package regression checks hold?
2. **Measured physical behavior**: For changes affecting motion, perception, or prompts, was task behavior verified under frozen benchmark conditions?

Please read [AGENTS.md](AGENTS.md) before making your first change—it is the authoritative architectural and development guide for both human contributors and coding agents.

---

## 1. Development Environment Setup

```bash
# Create the single conda environment named Auriga (Python 3.12); do not create a .venv
mamba env create -f environment.yml

# Install frontend dependencies
npm --prefix web ci
```

Always use `mamba run -n Auriga python` (or activate the `Auriga` conda environment explicitly) for tests, scripts, the gateway, and the evaluation queue. The RoboDojo simulator worker and the optional OpenPI policy service run in their own isolated interpreters; see [RoboDojo Environment Setup](docs/development/robodojo-setup.md).

---

## 2. Verification & Test Suite

The CPU, Web, and package checks require no GPU, simulator, or model credentials and match what CI runs:

```bash
# Run the full Python CPU contract and regression test suite
mamba run -n Auriga python -m unittest discover -s tests

# Run frontend unit checks and build the production web bundle
npm run check --prefix web
npm run build --prefix web

# Build and verify the distributable Python wheel in a clean environment
mamba run -n Auriga python scripts/build_package.py --output dist
mamba run -n Auriga python scripts/check_package.py dist/auriga_harness-*.whl
```

To run a single test module during development:

```bash
PYTHONPATH=tests mamba run -n Auriga python -m unittest test_tool_registry
```

See the [Validation & Release Guide](docs/development/validation.md) for a complete mapping from change scope to required checks. Note that passing CPU/Web checks verifies the software contract only; claims about motion planning or manipulation success rates require native runs under frozen benchmark conditions.

---

## 3. Core Architectural Invariants Enforced in Review

- **One Physical Entry Point**: Any operation that advances or mutates the observed world must flow through [`Harness.execute`](auriga/runtime/harness.py); tool plugins must call `ToolContext.execute`.
- **Facts Separated from Presentation**: Helper programs and tests must inspect `ToolResponse.kind` and `ToolResponse.data`—never parsing human-readable display strings or process exit codes to infer whether a robot action succeeded. Keep unknown outcomes explicitly `unknown`.
- **Complete, Transparent Tool Contracts**: Every new tool, stage parameter, or helper function must document its physical meaning, unit (`m`, `rad`, `deg`, `s`), default behavior, and valid bounds directly in the contract that drives schema generation and validation. Never introduce hidden black-box heuristics, magic thresholds, or silent overrides of model-specified parameters.
- **Strict Information Boundary**: Models may read only permitted observations. Hidden simulator state, evaluator internals, credentials, and storage metadata (such as SHA-256 hashes) must never enter model-visible context.
- **Targeted Regression Tests**: Every non-trivial fix or feature must include regression tests covering failure modes, partial execution, retries, permissions, and backward compatibility with existing run archives.
- **Documentation Sync**: Whenever a public contract, configuration key, or workflow changes, update the corresponding guide under [`docs/`](docs/README.md) as well as the [Documentation Index](docs/README.md).

---

## 4. Submitting a Pull Request

- Keep each pull request focused on a single cohesive concern.
- Clearly describe **what** changed, **why** it changed, and **how** you verified it (exact commands run, results, and any skipped checks).
- Never commit API keys, credentials, `configs/auriga.local.json`, run directories (`runs/`), large video recordings, or model weights.
