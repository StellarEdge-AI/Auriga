# VGGT height / object-table service

HTTP service behind the Auriga switches `features.vggt_height`, `features.object_table`, `features.scene3d`,
`features.object_table_observe`, `features.scene3d_observe` and `features.held_perception` (client code: `auriga/perception/external/vggt_height.py`,
`auriga/perception/external/object_table.py`, `auriga/perception/external/scene3d.py`, `auriga/perception/external/obs3d.py`; method and results:
[docs/vggt_height.md](../../docs/vggt_height.md), [docs/object_table.md](../../docs/object_table.md),
[docs/scene3d.md](../../docs/scene3d.md), [docs/obs3d.md](../../docs/obs3d.md)).
These switches are on by default (master switch `features.external_perception`); with every switch off Auriga
never contacts the service. With a switch on and the service down the client degrades (object table status
`unknown`, obs3d attachment marked `unavailable`), it does not fail the episode.

Two processes, both bound to 127.0.0.1 by default:

| process | default port | model | endpoints | version strings (`/health`) |
|---|---|---|---|---|
| `vggt_height_service.py` | 8331 | VGGT-Omega 1B-512 + SAM 2.1 hiera-large | `GET /health`, `POST /height`, `POST /object_table`, `POST /points`, `POST /held` | `vggt-height-v2`, `object-table-v5-hp1`, `scene3d-points-v1`, `held-v1` |
| `sam31/sam31_service.py` | 8333 | SAM 3.1 (image detector of `sam3.1_multiplex.pt`) | `GET /health`, `POST /segment` | `sam31-text-v1` |

The VGGT process calls the SAM 3.1 process for `/object_table` (text prompts: the caller's `labels` plus the generic
phrase "object"). If the SAM 3.1 process is down or not installed, `/object_table` falls back to the SAM 2.1
point-prompt grid (`backend: sam21_auto`, per-label status `backend_unavailable`); `/height` and `/points` never use
SAM 3.1. The files here package the service that was used for the Auriga experiments: the only code changes are
that host, port and checkpoint paths are command-line arguments or environment variables (`VGGT_HOST`, `VGGT_PORT`, `VGGT_CKPT`,
`SAM2_MODEL`, `SAM31_ENDPOINT`; `SAM31_HOST`, `SAM31_PORT`, `SAM31_CKPT`) instead of fixed paths. Answers are identical
(see "Validation" below).

## Requirements

- Linux x86_64, NVIDIA GPU with bf16 support (compute capability 8.0 or newer: A100, RTX 30/40 series, L40, H100, ...)
  and a driver for CUDA 12.8 (R570 or newer). Older GPUs: see FAQ.
- GPU memory, measured on an RTX 4090 over about 15 000 requests: VGGT process 5.3 GB allocated
  after load, 7.4 GB reserved, per-request peak 7.3 GB; SAM 3.1 process 5.1 GB allocated, 7.0 GB reserved, peak 6.8 GB.
  Both on one GPU: plan for about **15 GB**. VGGT only (`WITH_SAM31=0`): about 8 GB.
- Disk: weights 9.0 GB (VGGT-Omega 4.6 GB, SAM 2.1 0.9 GB, SAM 3.1 3.5 GB; without SAM 3.1: 5.5 GB). The two venvs
  take about 10 GB (mostly torch and the CUDA libraries; uv hard-links them from `cache/uv`, so deleting the cache
  frees almost nothing), Python builds, sources and tools 0.5 GB, the torch wheels of the aliyun route 1.7 GB
  (`src/wheels`, deletable). Measured total of a fresh install with SAM 3.1: 21 GB.
- `git`, `curl`, `python3` (any version, only used to bootstrap `uv` if it is not on `PATH`), `ss` (iproute2).
- Network: GitHub (source checkouts, Python builds), PyPI, the PyTorch wheel index, Hugging Face (or the mirrors below).
- Hugging Face account with **approved access** to the gated repos `facebook/VGGT-Omega` and `facebook/sam3.1`
  (not needed for SAM 2.1; not needed for the ModelScope route, see "Weights").

## Install

```bash
git clone https://github.com/lose4578/Auriga.git && cd Auriga/services/vggt_height
export VGGT_HOME=/data/vggt                 # install root: venvs, sources, weights, logs (outside the repo)

# 1. environments (10-30 min; detached variant: setsid nohup bash setup.sh > "$VGGT_HOME/setup.log" 2>&1 < /dev/null &
#    then watch "$VGGT_HOME/run/setup.STATUS")
bash setup.sh
#    mainland China: TORCH_SOURCE=aliyun PYPI_INDEX=https://mirrors.aliyun.com/pypi/simple/ bash setup.sh

# 2. weights (gated repos need a token of an account whose access request was approved)
export HF_TOKEN_FILE=~/.cache/huggingface/token     # or: export HF_TOKEN=... (never commit it)
bash download_weights.sh
#    without Hugging Face access / from mainland China: VGGT_SOURCE=modelscope SAM31_SOURCE=modelscope SAM2_SOURCE=hf-mirror bash download_weights.sh

# 3. start (GPU chosen with CUDA_VISIBLE_DEVICES; 127.0.0.1:8331 and :8333)
CUDA_VISIBLE_DEVICES=0 bash start.sh

# 4. self-check (exit code 0 = pass)
python3 selfcheck.py
```

What `setup.sh` builds under `$VGGT_HOME`:

| path | content |
|---|---|
| `venv/` | Python 3.11 (uv-managed), torch 2.8.0+cu128, torchvision 0.23.0+cu128, `requirements-vggt.txt` (all pinned), vggt-omega editable |
| `vggt-omega/` | `git clone https://github.com/facebookresearch/vggt-omega.git`, checkout `48b23c8ce72c9a8fcf987f0d7f43d8e4870759f6` (2026-09-22, "eval code") |
| `venv_sam31/` | Python 3.12, torch 2.8.0+cu128, torchvision 0.23.0+cu128, `sam31/requirements-sam31.txt` (all pinned; numpy 1.26.4, setuptools < 81 for `pkg_resources`), sam3 editable |
| `sam3/` | `git clone https://github.com/facebookresearch/sam3.git`, checkout `7f473cfd983d5eb43040ff608f66bff0d7bdcb53` (2026-10-14) |
| `run/` | `setup.STATUS`, `freeze_vggt.txt`, `freeze_sam31.txt`; later the PID / STATUS / log files of `start.sh` |
| `cache/uv`, `uv_python/`, `tools/uv/`, `src/wheels/` | uv cache, uv-managed Python builds, bootstrap uv 0.13.2, torch wheels (aliyun route) |

At the end `setup.sh` compares both venvs with the reference freezes (`requirements-freeze.txt`,
`sam31/requirements-freeze.txt`) and prints any difference. Options: `WITH_SAM31=0` (VGGT venv only),
`TORCH_SOURCE=pytorch|aliyun`, `PYPI_INDEX=<url>`, `UV=<path to uv>`, and uv's own variables, e.g.
`UV_PYTHON_INSTALL_MIRROR` when GitHub downloads of the Python builds are slow. Re-running is safe (existing venvs
and checkouts are kept; packages are re-checked).

## Weights

`download_weights.sh` puts the files under `$VGGT_HOME/weights/` and checks every file against `weights.sha256`
(the sha256 of the official Hugging Face files; LFS ids read from the Hub API on 2026-10-22). Verified files are
skipped, interrupted downloads resume. `CHECK_URLS=1` only sends a 1 KB range request to every selected URL.

| file | official source (pinned revision) | sha256 | licence |
|---|---|---|---|
| `vggt_omega_1b_512.pt` (4 576 706 117 B) | [huggingface.co/facebook/VGGT-Omega](https://huggingface.co/facebook/VGGT-Omega) @ `1041e80f`, **gated** | `c02da418b18bb01d0392598d3f6147366bcde1bb70fd08a5e3bf7925b0667934` | FAIR Noncommercial Research License v1 |
| `sam2.1-hiera-large/` (`model.safetensors` 897 897 416 B + 4 json) | [huggingface.co/facebook/sam2.1-hiera-large](https://huggingface.co/facebook/sam2.1-hiera-large) @ `665f8e2a`, open | `dc407dce...7230` (model.safetensors; all in `weights.sha256`) | Apache-2.0 |
| `sam3.1/sam3.1_multiplex.pt` (3 502 755 717 B) | [huggingface.co/facebook/sam3.1](https://huggingface.co/facebook/sam3.1) @ `daa63191`, **gated** | `0567debeec80ba4ac6369540c6c248025283cb3ff2b92827509e57e2b3541cb6` | SAM License (Meta, 2025-11-19) |

Sources (`VGGT_SOURCE`, `SAM2_SOURCE`, `SAM31_SOURCE`):

- `hf` (default): `https://huggingface.co/<repo>/resolve/<revision>/<file>`.
- `hf-mirror`: the same paths on `https://hf-mirror.com` (third-party mirror; gated files still need your token).
- `modelscope` (VGGT-Omega and SAM 3.1 only): `https://www.modelscope.cn/models/facebook/VGGT-Omega/resolve/master/...`
  and `.../facebook/sam3.1/...`, no token. These ModelScope pages are uploaded by a ModelScope account under the
  display name "AI at Meta", not by Meta's Hugging Face account; their files have the same sha256 as the official
  Hugging Face files (checked 2026-10-22 through the ModelScope file API), and the script verifies the bytes anyway.
  The licence terms of the original repos apply unchanged.
- Already have the files: `LOCAL_VGGT_PT=...`, `LOCAL_SAM2_DIR=...`, `LOCAL_SAM31_PT=...` hard-link (or copy) them; the
  sha256 check still runs. `ONLY=vggt,sam2` restricts the set (`WITH_SAM31=0` drops SAM 3.1).

Gated access (VGGT-Omega and SAM 3.1): log in on huggingface.co, open the model page, fill the form and accept the
licence. Approval is automatic (Meta's system; the authors cannot approve or reset requests, see FAQ). Create a
read token (Settings -> Access Tokens) and give it to the script as `HF_TOKEN` or a file `HF_TOKEN_FILE`
(default `~/.cache/huggingface/token`, as written by `huggingface-cli login`). The script passes the token to curl on
stdin, never on the command line, and never stores it under `$VGGT_HOME`. Do not commit tokens.

## Run

```bash
export VGGT_HOME=/data/vggt
CUDA_VISIBLE_DEVICES=0 bash start.sh          # SAM 3.1 first, then VGGT; waits for /health (load ~20 s)
bash status.sh                                # STATUS files, PID liveness, /health summary; exit 0 = healthy
bash stop.sh                                  # stop both (only the PIDs start.sh wrote); stop.sh vggt | sam31
VGGT_PORT=18331 SAM31_PORT=18333 CUDA_VISIBLE_DEVICES=1 bash start.sh   # a second instance (same for stop/status)
```

- Files in `$VGGT_RUN_DIR` (default `$VGGT_HOME/run`): `vggt.<port>.{PID,STATUS,log}`, `sam31.<port>.{PID,STATUS,log}`.
  STATUS is `STARTING ...`, `UP pid=... port=... gpu=... load_s=... allocated_mb=...`, `FAILED ...` or `STOPPED ...`.
- Processes are started with `setsid nohup` and survive the shell; they are not restarted automatically.
- `SAM31_CUDA_VISIBLE_DEVICES` puts SAM 3.1 on another GPU. `WITH_SAM31=0` never starts it. `VGGT_CACHE` (48) and
  `SAM31_CACHE` (16) set the per-image caches (reconstruction on CPU, SAM embeddings on GPU).
- One GPU worker thread per process, FIFO queue (64); new image ~0.35 s for `/height` and ~0.5 s for `/object_table`
  on an RTX 4090, cached image less. `HF_HUB_OFFLINE=1` is set: nothing is downloaded at start.
- Binding: keep 127.0.0.1. Auriga normally runs on the same host (a container with host networking sees
  127.0.0.1 of the host). For a remote GPU host, use an SSH tunnel
  (`ssh -N -L 127.0.0.1:8331:127.0.0.1:8331 gpuhost`) rather than `VGGT_HOST=0.0.0.0`: the service has no
  authentication.

## Auriga configuration

```json
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
```

These are the shipped defaults. `"external_perception": false` turns the whole stack off (switches that are not
given explicitly follow the master switch); individual switches can still be set on their own. All of them share
`vggt_endpoint`, `vggt_support_z_m` (RoboDojo table top, the disclosed arm-mount height) and `vggt_timeout_s`. The other keys (`object_table_max_objects`,
`object_table_head_exclude_px`, `scene3d_voxel_m`, ...) are described in the docs linked at the top.

## Self-check

```bash
python3 selfcheck.py [--url http://127.0.0.1:8331] [--no-sam31] [--compare-url http://other:8331] [--json-out out.json]
```

Standard library only. It sends the bundled RoboDojo head-camera frame (`testdata/organize_table_head.jpg`,
calibration in `testdata/organize_table_head.json`) and checks:

1. `/health`: versions `vggt-height-v2` / `object-table-v5` / `scene3d-points-v1`, SAM 3.1 backend reachable
   (`--no-sam31` expects the SAM 2.1 fallback instead);
2. `/height` at 4 object pixels: status, method, finite heights in 0-35 cm, support-plane fit;
3. `/object_table` without and with labels (`computer mouse`, `alarm clock`, `keyboard`): objects with centre, height,
   bounding box, flags; SAM 3.1 text backend; every label reported, at least one matched;
4. `/points`: npz payload with `xyz`, `conf`, `uv`;
5. the answers against `testdata/reference_v5.json` (recorded on an RTX 4090): `/height` top within 1 cm,
   80 % of the reference objects within 2 cm. A failure here usually means wrong weights, a different package
   set or a different service version.

`--compare-url` sends the same requests to a second instance and reports the largest numeric difference over all
answer fields except timing, cache and memory fields (default tolerance 1e-3). Exit code 0 = pass, 1 = a check
failed, 2 = service unreachable.

## Held-object fields (object-table-v5-hp1, for `features.held_perception`)

Optional additions; a request without them takes the object-table-v5 code path and gets the same answer (the
self-check accepts both version strings and compares against the same reference file).

- `POST /object_table` with `robot_mask_b64` (np.packbits of a bool H x W mask, row-major) and `robot_mask_shape`
  `[H, W]`: the caller's robot pixel mask (Auriga renders the robot meshes with forward kinematics and dilates the
  silhouette) replaces the link-capsule test (`robot_segments_m` / `robot_radius_m`). A mask with >= `robot_frac` of
  its pixels on robot pixels is dropped as robot; every other mask has the robot pixels cut out before its geometry
  is computed. The answer carries `robot_mask: "pixels"`.
- `POST /object_table` with `held: [{arm, grasp_centre_m, phrases?}]` (at most 2): per item the held-object mask (SAM
  3.1 text instance of the phrases plus "object" that contains the projected grasp centre, else the nearest within
  40 px; robot pixels removed; SAM 3.1 point prompt at the nearest non-robot pixel as a fallback), the mask-centroid
  ray at the grasp-centre camera depth (`centre_m`), the VGGT points of the mask rescaled along the camera rays so
  that the points next to the grasp centre sit at that depth (`gk`: length, width, top/low z, yaw, elongation), and
  `object_index` / `object_iou` of the returned object row that is the same mask. The answer carries `held` and
  `held_s`. Held items never use the floating -> table re-anchoring.
- `POST /held {image_b64, K, T_world_camera, support_z_m, robot_mask_b64?, robot_mask_shape?, held: [...]}`: the same
  held results for one image without an object table (used for the holding arm's wrist camera); a missing support
  plane is tolerated (points left unscaled, they are rescaled to the grasp depth anyway).

Client: `auriga/perception/external/held_perception.py`; method and measurements: [docs/held_perception.md](../../docs/held_perception.md).

## Validation of this package

The package was installed from scratch into an empty `VGGT_HOME`, started on other ports (18331 / 18333) and another
GPU, and compared request by request with the unpackaged service on the same RTX 4090 host:

- `setup.sh` (aliyun torch / PyPI route): about 11 minutes, of which about 3 minutes bootstrapping uv with an old
  system pip; vggt-omega and sam3 cloned at the pinned commits; both venvs equal the reference freezes
  (`compare_freeze` step: no difference).
- `download_weights.sh`: all 7 files matched `weights.sha256`; a re-run skipped everything. `CHECK_URLS=1`: range
  requests answered 206 for the gated files on huggingface.co and hf-mirror.com with a token and for both ModelScope
  files without a token; hf-mirror answered 429 (rate limit) to a burst of range requests but served the full
  download right after.
- `start.sh`: both processes healthy after 26 s; 9.3 GB GPU memory after load for both.
- `selfcheck.py --compare-url <unpackaged service>`: all checks passed with every answer field identical (max abs
  difference 0). With SAM 3.1 stopped: `--no-sam31` passes (fallback `sam21_auto`, labels `backend_unavailable`),
  the default run fails as intended.
- 18 RoboDojo head-camera frames x {PNG, JPEG q95} x {`/height` at all pointed pixels, `/object_table` without
  labels, with labels, with `seg_backend: sam21`, `/points`}: 180 request pairs, all answers identical (max abs
  numeric difference 0, identical `/points` npz bytes).

## FAQ

- **Access request rejected / "not accessible" although accepted.** Both gated repos use Meta's automated review;
  the authors cannot approve, reject or reset requests
  ([vggt-omega #7](https://github.com/facebookresearch/vggt-omega/issues/7), 2026-05-17, author reply 2026-05-18;
  [#59](https://github.com/facebookresearch/vggt-omega/issues/59), 2026-08-09: status "accepted" but files not
  accessible; for SAM 3.1 the Hub discussion page has dozens of reset requests, e.g.
  [facebook/sam3.1 discussion #85](https://huggingface.co/facebook/sam3.1/discussions/85), 2026-10-12). Use the
  ModelScope route (`VGGT_SOURCE=modelscope SAM31_SOURCE=modelscope`) if your use is covered by the licences;
  the sha256 check guarantees the same bytes as the official files.
- **`pip install -e .` of vggt-omega fails with "setup.py not found".** Old pip without PEP 660 support
  ([vggt-omega #18](https://github.com/facebookresearch/vggt-omega/issues/18), 2026-05-19). `setup.sh` uses
  `uv pip install -e`, which builds from `pyproject.toml`.
- **GPU older than Ampere (T4, V100).** VGGT-Omega picks bf16 whenever `torch.cuda.is_bf16_supported()` is true; on
  sm75 that is emulated bf16 without memory-efficient attention and runs out of memory
  ([vggt-omega #66](https://github.com/facebookresearch/vggt-omega/issues/66), 2026-10-12). The service is only
  validated on sm89 (RTX 4090); use a GPU with native bf16.
- **SAM 3 "CUDA error: no kernel image is available".** torch build without kernels for the GPU (cu126 wheels on
  a newer GPU, [sam3 #374](https://github.com/facebookresearch/sam3/issues/374), 2025-12-22). Keep the cu128 wheels
  that `setup.sh` installs; sam3 itself asks for Python >= 3.12, torch >= 2.7, CUDA >= 12.6 (its README at
  `7f473cfd`; it suggests torch 2.10, we pin 2.8.0 for both venvs, which is what was validated).
- **SAM 3.1 finds nothing for a phrase.** SAM 3 is sensitive to the phrasing; singular nouns work better than plurals
  ([sam3 #353](https://github.com/facebookresearch/sam3/issues/353)). Phrases come from the caller (the model);
  the service never names objects.
- **`/object_table` reports `backend: sam21_auto` and labels `backend_unavailable`.** The SAM 3.1 process is not
  reachable at `SAM31_ENDPOINT`; `bash status.sh`, then `bash start.sh sam31`.
- **Loading prints "4 missing keys" for SAM 3.1.** Expected: the image detector is loaded from the `detector.*` keys
  of the multiplex checkpoint; the 4 missing keys are `backbone.vision_backbone.convs.3.*` (a neck level discarded by
  `scalp=1`), the 36 unexpected keys are tracker necks (`/health` of sam31 lists them).
- **Downloads from mainland China.** `download.pytorch.org` and `huggingface.co` time out from some networks:
  `TORCH_SOURCE=aliyun`, `PYPI_INDEX=https://mirrors.aliyun.com/pypi/simple/`, `SAM2_SOURCE=hf-mirror`,
  `VGGT_SOURCE=modelscope`, `SAM31_SOURCE=modelscope`; for the uv-managed Python builds set
  `UV_PYTHON_INSTALL_MIRROR`, or a proxy with `HTTPS_PROXY`.
- **HTTP 429 from hf-mirror.com.** Rate limit of the mirror; wait a minute and re-run (downloads resume).
- **Port busy.** `start.sh` refuses to start on a port in use; pick other ports with `VGGT_PORT` / `SAM31_PORT`
  (the VGGT process is told the SAM 3.1 port automatically) and point `features.vggt_endpoint` at the new port.

## Version history

| date (2026) | version | change |
|---|---|---|
| 10-20 | vggt-height-v1 | `/height`: VGGT-Omega depth, RANSAC table plane scaled to the support height, SAM 2.1 point mask, p98 top |
| 10-20 | v2 = vggt-height-v2 + object-table-v1 | `POST /object_table` (automatic SAM 2.1 masks -> per-object geometry); plausibility flags on `/height` (values unchanged) |
| 10-20 | object-table-v2 | floating-object re-anchoring only when the gap exceeds the visible thickness (`reanchor_mode: thickness`) |
| 10-20 | + scene3d-points-v1 | `POST /points` (voxel point cloud npz for `features.scene3d`); `/height`, `/object_table` unchanged |
| 10-20 | object-table-v4 | a requested pixel reuses an object only inside its main component and with point-mask IoU >= 0.5 |
| 10-20 | object-table-v5 | SAM 3.1 text backend (second process), optional `labels`; `params.seg_backend: "sam21"` = v4 behaviour |
| 10-22 | (packaging) | this directory; host/port/paths as arguments or environment variables, answers unchanged |
| 10-26 | object-table-v5-hp1 + held-v1 | optional `robot_mask_b64`/`robot_mask_shape` and `held` on `/object_table`, `POST /held` (for `features.held_perception`); requests without them unchanged |

## Licences

- **VGGT-Omega** code (github.com/facebookresearch/vggt-omega) and weights: **FAIR Noncommercial Research License
  v1** (last updated 2024-10-16) plus the FAIR Acceptable Use Policy: **non-commercial research use only**; read the
  terms (including those on redistribution) before sharing weights. Licence text: the repo's `LICENSE` and
  <https://huggingface.co/facebook/VGGT-Omega/blob/main/LICENSE.txt>.
- **SAM 2.1** (facebook/sam2.1-hiera-large, via Hugging Face transformers `Sam2Model`): Apache-2.0.
- **SAM 3 / SAM 3.1** code (github.com/facebookresearch/sam3) and the `sam3.1_multiplex.pt` checkpoint: **SAM License**
  (Meta, last updated 2025-11-19; custom licence with use restrictions, read it before use):
  <https://huggingface.co/facebook/sam3.1/blob/main/LICENSE> and the repo's `LICENSE`.
- The service code in this directory is part of Auriga. Using it loads the models above, so their terms govern any
  deployment; in particular the VGGT-Omega licence rules out commercial use of the service as a whole.
- `testdata/organize_table_head.jpg` is a frame rendered by the RoboDojo simulator (task `organize_table`).
