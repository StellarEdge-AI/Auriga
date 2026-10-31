# VGGT Height, Object-Table, and 3D Point-Cloud Service

Standalone local HTTP service backing the Auriga external-perception switches: `features.vggt_height`, `features.object_table`, `features.scene3d`, `features.object_table_observe`, `features.scene3d_observe`, and `features.held_perception`:

- **Client modules**: `auriga/perception/external/vggt_height.py`, `auriga/perception/external/object_table.py`, `auriga/perception/external/scene3d.py`, `auriga/perception/external/obs3d.py`, and `auriga/perception/external/held_perception.py`.
- **Design & evaluation docs**: [`docs/vggt_height.md`](../../docs/vggt_height.md), [`docs/object_table.md`](../../docs/object_table.md), [`docs/scene3d.md`](../../docs/scene3d.md), [`docs/obs3d.md`](../../docs/obs3d.md), and [`docs/held_perception.md`](../../docs/held_perception.md).

All switches above are enabled by default under the master switch `features.external_perception`. When every switch is set to `false`, Auriga never contacts this service. When enabled, if the service is unreachable at runtime, the client degrades gracefully (`object_table` and `scene3d` return `status: "unknown"`, and `obs3d` attachments report `"unavailable"`) without failing the episode.

## Service Architecture

The service runs as two local processes bound to `127.0.0.1` by default:

| Process | Default Port | Model Weights | HTTP Endpoints | Version Strings (`GET /health`) |
|---|---|---|---|---|
| `vggt_height_service.py` | `8331` | VGGT-Omega 1B-512 + SAM 2.1 `hiera-large` | `GET /health`, `POST /height`, `POST /object_table`, `POST /points`, `POST /held` | `vggt-height-v2`, `object-table-v5-hp1`, `scene3d-points-v1`, `held-v1` |
| `sam31/sam31_service.py` | `8333` | SAM 3.1 (image detector from `sam3.1_multiplex.pt`) | `GET /health`, `POST /segment` | `sam31-text-v1` |

`vggt_height_service.py` queries the SAM 3.1 sidecar during `POST /object_table` (and `POST /held`) using text prompts: any caller-supplied `labels` plus the generic class-agnostic prompt `"object"`. If the SAM 3.1 process is stopped or not installed, `/object_table` automatically falls back to the SAM 2.1 point-prompt grid (`backend: "sam21_auto"`, reporting `backend_unavailable` for text labels); `/height` and `/points` do not use SAM 3.1. Host addresses, ports, and checkpoint paths are configured via CLI flags or environment variables (`VGGT_HOST`, `VGGT_PORT`, `VGGT_CKPT`, `SAM2_MODEL`, `SAM31_ENDPOINT`; `SAM31_HOST`, `SAM31_PORT`, `SAM31_CKPT`).

## Hardware and System Requirements

- **OS & GPU**: Linux `x86_64`, NVIDIA GPU with native `bfloat16` support (compute capability 8.0 or newer: Ampere A100 / RTX 30 series, Ada RTX 40 series / L40, Hopper H100, etc.) and an NVIDIA driver supporting CUDA 12.8 (`R570` or newer). See the [FAQ](#faq) regarding older GPUs.
- **GPU VRAM** (measured on an RTX 4090 over ~15,000 requests):
  - `vggt_height_service.py`: `5.3 GB` allocated after model load (`7.4 GB` reserved, `7.3 GB` per-request peak).
  - `sam31_service.py`: `5.1 GB` allocated after model load (`7.0 GB` reserved, `6.8 GB` per-request peak).
  - **Total VRAM on a single GPU**: ~**15 GB** for both processes together, or ~**8 GB** for VGGT + SAM 2.1 alone (`WITH_SAM31=0`).
- **Disk Space**:
  - Model weights: `9.0 GB` (`4.6 GB` VGGT-Omega + `0.9 GB` SAM 2.1 + `3.5 GB` SAM 3.1; `5.5 GB` without SAM 3.1).
  - Virtual environments (`venv/` and `venv_sam31/`): ~`10 GB` (PyTorch + CUDA 12.8 libraries hard-linked from `cache/uv`), plus `0.5 GB` for Python builds/sources and `1.7 GB` if caching wheel files via the Aliyun mirror (`src/wheels`, removable after setup). Total fresh install with SAM 3.1: ~**21 GB**.
- **System tools**: `git`, `curl`, `python3` (used only to bootstrap `uv` if not already on `PATH`), and `ss` (`iproute2`).
- **Hugging Face access**: A Hugging Face account with **approved access** to the gated repositories [`facebook/VGGT-Omega`](https://huggingface.co/facebook/VGGT-Omega) and [`facebook/sam3.1`](https://huggingface.co/facebook/sam3.1) (not needed for SAM 2.1 or when using the ModelScope download route; see [Weights](#weights)).

## Quick Start Installation

```bash
git clone https://github.com/lose4578/Auriga.git && cd Auriga/services/vggt_height
export VGGT_HOME=/data/vggt                 # Install root for venvs, sources, weights, and logs (outside the repo)

# 1. Build virtual environments (10-30 min)
#    Optional detached run: setsid nohup bash setup.sh > "$VGGT_HOME/setup.log" 2>&1 < /dev/null &
#    Then monitor "$VGGT_HOME/run/setup.STATUS"
bash setup.sh
#    Mirror option for mainland China:
#    TORCH_SOURCE=aliyun PYPI_INDEX=https://mirrors.aliyun.com/pypi/simple/ bash setup.sh

# 2. Download and verify model weights (gated HF repos require a read token from an approved account)
export HF_TOKEN_FILE=~/.cache/huggingface/token     # Or: export HF_TOKEN=... (never commit tokens to Git)
bash download_weights.sh
#    Mirror option without Hugging Face token / for mainland China:
#    VGGT_SOURCE=modelscope SAM31_SOURCE=modelscope SAM2_SOURCE=hf-mirror bash download_weights.sh

# 3. Start both services (binds to 127.0.0.1:8331 and 127.0.0.1:8333 on the selected GPU)
CUDA_VISIBLE_DEVICES=0 bash start.sh

# 4. Run end-to-end self-check (exit code 0 = pass)
python3 selfcheck.py
```

### Directory Layout Created Under `$VGGT_HOME`

| Path | Contents |
|---|---|
| `venv/` | Python 3.11 (`uv`-managed), `torch==2.8.0+cu128`, `torchvision==0.23.0+cu128`, pinned `requirements-vggt.txt`, and editable `vggt-omega` |
| `vggt-omega/` | Clone of `https://github.com/facebookresearch/vggt-omega.git` pinned at `48b23c8ce72c9a8fcf987f0d7f43d8e4870759f6` (`2026-09-22`) |
| `venv_sam31/` | Python 3.12 (`uv`-managed), `torch==2.8.0+cu128`, `torchvision==0.23.0+cu128`, pinned `sam31/requirements-sam31.txt` (`numpy==1.26.4`, `setuptools<81`), and editable `sam3` |
| `sam3/` | Clone of `https://github.com/facebookresearch/sam3.git` pinned at `7f473cfd983d5eb43040ff608f66bff0d7bdcb53` (`2026-10-14`) |
| `run/` | `setup.STATUS`, `freeze_vggt.txt`, `freeze_sam31.txt`, and runtime `PID` / `STATUS` / `log` files written by `start.sh` |
| `cache/uv`, `uv_python/`, `tools/uv/`, `src/wheels/` | `uv` package cache, managed Python builds, bootstrap `uv 0.13.2`, and downloaded wheel files |

At completion, `setup.sh` diffs both virtual environments against `requirements-freeze.txt` and `sam31/requirements-freeze.txt`. Useful environment variables: `WITH_SAM31=0` (builds only the VGGT environment), `TORCH_SOURCE=pytorch|aliyun`, `PYPI_INDEX=<url>`, `UV=<path>`, and `UV_PYTHON_INSTALL_MIRROR`. Re-running `setup.sh` is idempotent.

## Weights

`download_weights.sh` places all checkpoints under `$VGGT_HOME/weights/` and verifies every file against `weights.sha256` (exact SHA-256 digests of the official Hugging Face LFS blobs verified on `2026-10-22`). Already verified files are skipped, and partial downloads resume automatically. Setting `CHECK_URLS=1` performs a fast 1 KB HTTP Range check against each configured URL without downloading full weights.

| Checkpoint File | Official Upstream Repository (Pinned Revision) | SHA-256 Digest | License |
|---|---|---|---|
| `vggt_omega_1b_512.pt` (`4,576,706,117 B`) | [`facebook/VGGT-Omega`](https://huggingface.co/facebook/VGGT-Omega) @ `1041e80f` (**gated**) | `c02da418b18bb01d0392598d3f6147366bcde1bb70fd08a5e3bf7925b0667934` | FAIR Noncommercial Research License v1 |
| `sam2.1-hiera-large/` (`model.safetensors` `897,897,416 B` + 4 JSON configs) | [`facebook/sam2.1-hiera-large`](https://huggingface.co/facebook/sam2.1-hiera-large) @ `665f8e2a` (open) | `dc407dce...7230` (all 5 files verified in `weights.sha256`) | Apache-2.0 |
| `sam3.1/sam3.1_multiplex.pt` (`3,502,755,717 B`) | [`facebook/sam3.1`](https://huggingface.co/facebook/sam3.1) @ `daa63191` (**gated**) | `0567debeec80ba4ac6369540c6c248025283cb3ff2b92827509e57e2b3541cb6` | SAM License (Meta, `2025-11-19`) |

### Download Sources (`VGGT_SOURCE`, `SAM2_SOURCE`, `SAM31_SOURCE`)

- **`hf` (default)**: Downloads directly from `https://huggingface.co/<repo>/resolve/<revision>/<file>`.
- **`hf-mirror`**: Uses `https://hf-mirror.com` (gated repositories still require your `HF_TOKEN`).
- **`modelscope` (supported for VGGT-Omega and SAM 3.1)**: Downloads from `https://www.modelscope.cn/models/facebook/VGGT-Omega/resolve/master/...` and `.../facebook/sam3.1/...` without requiring a token, and verifies that the downloaded files match the exact official SHA-256 checksums in `weights.sha256`. All upstream license terms continue to apply.
- **Local pre-downloaded files**: Pass `LOCAL_VGGT_PT=...`, `LOCAL_SAM2_DIR=...`, and/or `LOCAL_SAM31_PT=...` to hard-link (or copy) existing files into `$VGGT_HOME/weights/` and run SHA-256 verification. Pass `ONLY=vggt,sam2` or `WITH_SAM31=0` to skip SAM 3.1.

To request gated access on Hugging Face, log in to `huggingface.co`, accept the license form on the model page, create a read token under **Settings → Access Tokens**, and pass it via `HF_TOKEN_FILE` (default `~/.cache/huggingface/token`) or `HF_TOKEN`. `download_weights.sh` pipes the token to `curl` over `stdin` so it never appears in process argument lists (`ps`) or under `$VGGT_HOME`.

## Managing the Service

```bash
export VGGT_HOME=/data/vggt
CUDA_VISIBLE_DEVICES=0 bash start.sh          # Starts SAM 3.1 then VGGT and waits for /health (~20 s load)
bash status.sh                                # Prints STATUS files, PID liveness, and /health summary (exit 0 = healthy)
bash stop.sh                                  # Stops both processes (or pass: bash stop.sh vggt | sam31)

# Launching a second instance on another GPU and port pair:
VGGT_PORT=18331 SAM31_PORT=18333 CUDA_VISIBLE_DEVICES=1 bash start.sh
```

- **Runtime files** in `$VGGT_RUN_DIR` (default `$VGGT_HOME/run`): `vggt.<port>.{PID,STATUS,log}` and `sam31.<port>.{PID,STATUS,log}`.
- **Multi-GPU placement**: Set `SAM31_CUDA_VISIBLE_DEVICES` to place SAM 3.1 on a separate GPU from VGGT, or `WITH_SAM31=0` to run VGGT only.
- **In-memory caches**: `VGGT_CACHE` (default `48` images on CPU) and `SAM31_CACHE` (default `16` image embeddings on GPU). Each process runs a single GPU worker thread served by a 64-entry FIFO queue (`~0.35 s` for `/height` and `~0.50 s` for `/object_table` on an RTX 4090 for uncached frames). `HF_HUB_OFFLINE=1` is enforced at startup so no runtime network calls occur.
- **Network security**: Keep `VGGT_HOST=127.0.0.1` because the HTTP service has no authentication layer. To use a remote GPU server, forward the loopback port over SSH (`ssh -N -L 127.0.0.1:8331:127.0.0.1:8331 gpuhost`) rather than binding to `0.0.0.0`.

## Auriga Configuration

Shipped defaults in `configs/auriga.json`:

```json
{
  "features": {
    "external_perception": true,
    "vggt_height": true,
    "vggt_endpoint": "http://127.0.0.1:8331",
    "vggt_support_z_m": 0.765,
    "vggt_timeout_s": 60,
    "object_table": true,
    "scene3d": true,
    "object_table_observe": true,
    "scene3d_observe": true,
    "held_perception": true,
    "obs3d_timeout_s": 60
  }
}
```

Setting `"external_perception": false` disables the entire stack at once, while individual sub-switches can still be toggled selectively.

## End-to-End Self-Check

```bash
python3 selfcheck.py [--url http://127.0.0.1:8331] [--no-sam31] [--compare-url http://other:8331] [--json-out out.json]
```

Uses only the Python standard library and sends the bundled RoboDojo head-camera test frame (`testdata/organize_table_head.jpg` and calibration `testdata/organize_table_head.json`) to verify:

1. `GET /health`: confirms version strings `vggt-height-v2`, `object-table-v5` / `object-table-v5-hp1`, and `scene3d-points-v1`, plus SAM 3.1 reachability (or `sam21_auto` fallback when `--no-sam31` is passed).
2. `POST /height` at 4 object pixels: checks status, method, finite heights within `0–35 cm`, and support-plane fit.
3. `POST /object_table` without and with text labels (`"computer mouse"`, `"alarm clock"`, `"keyboard"`): checks returned object rows, 3D centers, heights, bounding boxes, SAM 3.1 text backend usage, and label matching.
4. `POST /points`: verifies the compressed NPZ point cloud (`xyz`, `conf`, `uv`).
5. **Reference accuracy check** against `testdata/reference_v5.json` (recorded on an RTX 4090): verifies `/height` top surfaces within `1 cm` and at least 80% of reference objects within `2 cm`.

Passing `--compare-url` sends identical requests to a second service instance and checks that all numeric outputs match within `1e-3`.

## Held-Object Endpoints (`object-table-v5-hp1`, for `features.held_perception`)

Requests that omit the optional held-object fields follow the exact `object-table-v5` code path:

- **`POST /object_table` with `robot_mask_b64` and `robot_mask_shape: [H, W]`**: Replaces the `6 cm` link-capsule filter with the caller's FK-rendered robot silhouette mask (`np.packbits` of a row-major `bool[H, W]` mask). Candidate masks with $\ge$ `robot_frac` pixels on the robot are discarded, and robot pixels are subtracted from all remaining masks before computing 3D geometry (`answer.robot_mask = "pixels"`).
- **`POST /object_table` with `held: [{arm, grasp_centre_m, phrases?}]` (up to 2 arms)**: Segments the held object near the projected FK grasp center using SAM 3.1 text/point prompts with robot pixels removed, anchors the ray depth to the FK grasp-center depth (`centre_m`), rescales the mask's VGGT points to that depth (`gk`: length, width, top/low $z$, yaw, elongation), and suppresses floating-to-table re-anchoring.
- **`POST /held`**: Computes the same per-arm `held` results on a single camera image without building a full tabletop object table (used for the holding arm's wrist camera).

## Package Validation Summary

A clean installation into an empty `VGGT_HOME` was compared request-for-request against the original research service on the same RTX 4090 host across 18 RoboDojo head-camera frames × `{PNG, JPEG 95}` × `{/height, /object_table (unlabelled, labelled, and sam21), /points}` (180 request pairs total): both virtual environments matched `requirements-freeze.txt` identically, all 7 checkpoint files matched `weights.sha256`, and all 180 request pairs produced bit-identical numeric answers (maximum absolute difference `0`) and identical `/points` NPZ bytes.

## FAQ

- **Hugging Face access request rejected or files still inaccessible after approval**: Both `facebook/VGGT-Omega` and `facebook/sam3.1` use Meta's automated gating system ([`vggt-omega#7`](https://github.com/facebookresearch/vggt-omega/issues/7), [`vggt-omega#59`](https://github.com/facebookresearch/vggt-omega/issues/59), [`facebook/sam3.1#85`](https://huggingface.co/facebook/sam3.1/discussions/85)). If your usage complies with the upstream licenses, you can use `VGGT_SOURCE=modelscope SAM31_SOURCE=modelscope`, which verifies the exact same official SHA-256 digests via `weights.sha256`.
- **`pip install -e .` of `vggt-omega` fails with `setup.py not found`**: Caused by older `pip` versions lacking PEP 660 editable-install support ([`vggt-omega#18`](https://github.com/facebookresearch/vggt-omega/issues/18)); `setup.sh` uses `uv pip install -e`, which handles `pyproject.toml` natively.
- **GPUs older than Ampere (e.g., Turing T4 or Volta V100)**: VGGT-Omega selects `bfloat16` whenever `torch.cuda.is_bf16_supported()` returns true; on `sm_75` this falls back to emulated `bfloat16` without memory-efficient attention and runs out of VRAM ([`vggt-omega#66`](https://github.com/facebookresearch/vggt-omega/issues/66)). Use an Ampere (`sm_80`+) or newer GPU with native `bfloat16`.
- **SAM 3 `CUDA error: no kernel image is available`**: Caused by installing `cu126` wheels on newer GPU architectures ([`sam3#374`](https://github.com/facebookresearch/sam3/issues/374)); keep the `torch==2.8.0+cu128` wheels installed by `setup.sh`.
- **SAM 3.1 returns no match for a label**: SAM 3 text prompting works best with concise singular noun phrases rather than plurals ([`sam3#353`](https://github.com/facebookresearch/sam3/issues/353)).
- **`/object_table` reports `backend: "sam21_auto"` and `backend_unavailable`**: The SAM 3.1 sidecar is not reachable at `SAM31_ENDPOINT`; check `bash status.sh` and restart it with `bash start.sh sam31`.
- **SAM 3.1 startup logs `"4 missing keys"`**: Expected behavior when loading the image detector (`detector.*`) from `sam3.1_multiplex.pt` (`backbone.vision_backbone.convs.3.*` is pruned by `scalp=1`, and the 36 unexpected keys belong to video tracker necks).

## Version History

| Date (2026) | Version | Summary of Changes |
|---|---|---|
| `10-20` | `vggt-height-v1` | Initial `POST /height`: VGGT-Omega depth, RANSAC tabletop plane scaled to `support_z_m`, SAM 2.1 point mask, 98th-percentile top height |
| `10-20` | `vggt-height-v2` + `object-table-v1` | Added `POST /object_table` (automatic SAM 2.1 grid masks → 3D object geometry) and quality flags on `/height` |
| `10-20` | `object-table-v2` | Restricted floating-object ray re-anchoring to cases where the vertical gap exceeds visible object thickness (`reanchor_mode: "thickness"`) |
| `10-20` | `scene3d-points-v1` | Added `POST /points` (voxel-downsampled 3D point cloud NPZ for `features.scene3d`) |
| `10-20` | `object-table-v4` | Point prompts reuse an existing object row only inside its largest connected component with point-mask IoU $\ge 0.5$ |
| `10-20` | `object-table-v5` | Added SAM 3.1 text-prompt sidecar (`sam31_service.py`) and optional `labels` (`params.seg_backend: "sam21"` reproduces v4) |
| `10-22` | Standalone packaging | Parameterized host/port/checkpoint paths via CLI flags and environment variables with bit-identical outputs |
| `10-26` | `object-table-v5-hp1` + `held-v1` | Added `robot_mask_b64`/`robot_mask_shape` and `held` on `/object_table` plus `POST /held` for `features.held_perception` |

## Licenses

- **VGGT-Omega** ([`facebookresearch/vggt-omega`](https://github.com/facebookresearch/vggt-omega) code and weights): **FAIR Noncommercial Research License v1** (`2024-10-16`) and FAIR Acceptable Use Policy (**non-commercial research use only**; see [`LICENSE.txt`](https://huggingface.co/facebook/VGGT-Omega/blob/main/LICENSE.txt)).
- **SAM 2.1** ([`facebook/sam2.1-hiera-large`](https://huggingface.co/facebook/sam2.1-hiera-large)): **Apache-2.0**.
- **SAM 3 / SAM 3.1** ([`facebookresearch/sam3`](https://github.com/facebookresearch/sam3) code and `sam3.1_multiplex.pt` checkpoint): **SAM License** (Meta, `2025-11-19`; see [`LICENSE`](https://huggingface.co/facebook/sam3.1/blob/main/LICENSE)).
- The service wrapper scripts in this directory are part of Auriga; any deployment loading the weights above is governed by their respective upstream licenses (in particular, VGGT-Omega restricts the combined service to non-commercial research use).
