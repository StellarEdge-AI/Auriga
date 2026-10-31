# Changelog

Version numbers correspond to `pyproject.toml` and matching annotated Git tags. The project was renamed from **Daedalus** to **Auriga** in `0.2.0`; entries for earlier releases retain the historical name where appropriate. This file tracks software releases—task performance claims always require independent benchmark runs under frozen source, configuration, and model conditions.

---

## 0.2.0 — 2026-10-31

- **Project renamed to Auriga**: Python package `auriga`, distribution package `auriga-harness`, CLI commands `auriga` / `auriga-queue` / `auriga-report` / `auriga-audit`, environment variable prefix `AURIGA_`, conda environment `Auriga`, default paths `~/exp/auriga`, `~/.config/auriga`, `~/.local/share/auriga`, `~/.cache/auriga`, in-program client `AurigaClient` (`auriga_client.py`), and model tool namespace `auriga`.
- **Full backward compatibility for pre-rename archives**: `daedalus_config` in manifests, `daedalus.robodojo_trajectory.v1` trajectory bundles, worker and continuity markers in legacy logs, and resume commands in older queue states remain readable. The evaluation queue can still launch frozen checkouts from before the rename or package restructuring, passing settings with the legacy `DAEDALUS_` prefix. Legacy identifiers are centralized in `auriga/core/legacy_names.py`. To resume a session started before the rename, use its original checkout.
- Removed the internal module-path migration table.

---

## 0.1.0 — 2026-10-31

- **Six-layer package restructuring**: Moved flat package modules into six strict layers (`core/`, `robot/`, `perception/`, `cortex/`, `runtime/`, `apps/`); split the gateway into `cli` / `server` / `routes` / `workbench`; extracted `robot_action` schemas and worker-side native state recovery into dedicated modules. Function bodies remain unchanged apart from imports and path resolution, and the model-facing tool catalogue, descriptions, and responses are byte-for-byte identical. Updated CLI module entry points (`python -m auriga.apps.gateway`, etc.) while keeping queue support for frozen pre-restructure checkouts.
- **Removed 21 legacy `trusted_legacy`-only tools**: Removed 21 deprecated tools that were unreachable by the model or helper programs under any configuration, along with their dedicated modules, tests, and probe scripts. Their names remain accepted in `disabled_tools` for configuration compatibility.
- **Open-source release preparation**: Removed internal experiment logs, research notes, `analysis/` dumps, internal deployment configs, and team score sheets; moved official task page snapshots to `scripts/data/` and RoboDojo/IsaacLab patches to `docs/development/patches/` alongside the [RoboDojo Environment Setup](docs/development/robodojo-setup.md) guide; added English and Chinese READMEs and [CONTRIBUTING.md](CONTRIBUTING.md); cleared packaged experiment-group relations and removed machine-local paths from tests; released under the MIT License.

---

## 0.0.4 — 2026-10-30

- **Optional self-briefing (`features.self_briefing=true`, enabled by default)**: Generates task-agnostic operating reminders before the first physical action and appends them after the initial task instruction.
- Increased the formal model request budget from `170` to `180` responses per episode.
- **Model proxy**: Client disconnects after a complete response has been delivered no longer abort the active episode.

---

## 0.0.3 — 2026-10-30

*(This release does not include self-briefing.)*

- **Optional OS sandbox (`features.sandbox_enabled`, disabled by default)**: When enabled on Linux, runs the main Codex session, sub-agents, native tools, and terminals inside a Linux namespace/Landlock/seccomp sandbox with read-only mounted Harness sources, disabled host extensions, controlled PyPI download relay, and host-side authentication relay.
- Narrowed standalone program Python runtime dependencies, separated program workspaces from host logs, and blocked symlink-based host file reads through program output files (legacy full-access sessions require a fresh run directory).
- **Lean, untainted model context**: Stripped non-essential verification metadata and content hashes across the main Codex session, auxiliary visual verification, program responses/history caches, text artifacts, and $\pi_{0.5}$ prompts.
- Standardized object references on reversible short IDs (`short_id`) while preserving full internal execution facts and archive verification; added scrubbing for resumed session histories at the Responses/compact egress.
- Updated development guidelines and added HTTP, sandboxed program, and policy-input regression tests enforcing the actor information boundary.
- Decoupled manipulator definitions from environment adapters, binding deployments strictly to validated robot/environment pairs.
- Added the `robodojo-42-15` benchmark suite (42 official tasks × 15 scenes with unified evaluation seeds) and verbatim injection of official task descriptions and scoring tables, plus optional public task instruction clarifications (`features.task_instruction_overrides`, enabled by default).
- Integrated external 3D perception (enabled by default): VGGT object height estimation, `object_table`, `scene3d` point clouds, inline `obs3d` observation attachments, and `held_perception`; gracefully degrades when the perception service is unreachable.
- Made `robot_action` geometry, history, and gripper checkpoints on-demand; added ROI crop zoom to `robot_observe`; exposed stage-level vertical compliance (`max_upward_dz_m`) and `effective_tolerances` directly to the model.
- Added optional prompt-cache-preserving history projection (`lean_projection`, disabled by default) and one-shot mid-history image compression in the model proxy when request payloads exceed 4.5 MB.
- **Infrastructure hardening**: Bounded model transport recovery (up to 10 cooldown retries), connection-phase retry handling, single simulator restart on CUDA faults, planner OOM classification as infrastructure failure, and default queue concurrency limit of 60.
- Pinned the native Codex memory model to match the episode's active model.

---

## 0.0.2 — 2026-10-09

Merged `refactor/systematic-20261007` while preserving `main` capabilities for HD archive replay, trajectory ZIP downloads, GPU admission/shared-device options, and custom domain configuration.

- Consolidated tool registration onto `ToolSpec` / `ToolRegistry` with unified input/output schemas, roles/effects, availability predicates, and manifests; plugins connect through a narrow `ToolContext`.
- Separated typed tool facts from actor presentation, program brokering, and wire adaptation; established explicit module boundaries for worker IPC protocols, lifecycle management, read-only HTTP APIs, the evaluation queue, and packaged web assets.
- Fixed edge cases where image/presentation errors after a completed action dropped the action receipt, full status queries truncated diagnostics, replaying an older checkpoint rolled back plan revisions, terminal state sampling failures lost execution facts, or program-initiated action IDs were omitted.
- Fixed sealed WAL scoring reads creating `-shm` sidecars, archive imports dropping score metadata, corrupt summaries blocking catalog scans, integrity audits missing dangling references, completion hooks missing the new queue entry point, and entity freshness borrowing timestamps from unrelated observations.
- Migrated HD archive loading and reference image reads to the read-only `Store` helper so trajectory downloads and replay reads never create `-shm` files in sealed archive directories.
- Added contributor guidelines, architecture docs, extension guides, and validation guides. See the [Documentation Index](docs/README.md).

**Known boundaries**: Trusted host plugins are not a sandbox against malicious Python code; standalone replay operates on a copy of the archive; integrity auditing verifies reconstructed RGB frames while reporting raw image byte integrity separately.

---

## 0.0.1 — 2026-10-08

Preserved the pre-refactor `main` baseline, including HD replay, trajectory downloads, GPU scheduling, model defaults, and configuration override logic. Corrected the package version to `0.0.1` at commit `0a59e0e6b0a6f475fa5b88e7638bec92d8801ef3`.
