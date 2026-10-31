# RoboDojo Runtime Environment

Auriga itself uses a single Python environment (Miniforge `Auriga`), while the robot worker runs in a separately installed RoboDojo / Isaac Sim interpreter so the two dependency trees remain isolated. This guide covers what needs to be prepared on the RoboDojo side, which patches are required, and how Auriga locates the installation. For setting up the Harness environment, building the web UI, and launching your first run, see the [Running Guide](running.md).

## Pinned Revisions

The currently validated hardware/simulation combination is RoboDojo with the dual-arm ARX X5. External checkouts are pinned to the following revisions:

| Repository | Location (relative to RoboDojo root) | Revision |
| --- | --- | --- |
| RoboDojo | `.` | `726e9aabfaa642203722eb126f5eaf0f37f3e1ad` |
| IsaacLab | `third_party/IsaacLab` (submodule) | `afca7b09d60d8beb9c1cb28b43066499940b969b` |
| cuRobo | `third_party/curobo` (submodule) | `d17b54ce32cba095c0b000c4c58777075d11de0e` |
| XPolicyLab | `XPolicyLab` | `bb9a0b5f5136a74503b679af830bfd0a3a837d5c` |

Follow the installation instructions in the [upstream RoboDojo repository](https://github.com/RoboDojo-Benchmark/RoboDojo) at that pinned revision to set up Isaac Sim 5.1, CUDA, and task assets until you have a Python environment capable of running standalone RoboDojo evaluations, then apply the patches below.

## Patches

All three patches live in [`docs/development/patches/`](patches/). They only address runtime compatibility—they contain no task solutions and do not alter scoring rules:

| Patch | Target Directory | Purpose |
| --- | --- | --- |
| [`robodojo.patch`](patches/robodojo.patch) | RoboDojo root | `src/eval_client/main.py`: decouples CUDA and Vulkan GPU index assignment. |
| [`isaaclab.patch`](patches/isaaclab.patch) | `third_party/IsaacLab` | GPU index handling in `app_launcher.py` and Starlette version compatibility for Isaac Sim 5.1 in `setup.py`. |
| [`native-runtime-compatibility.patch`](patches/native-runtime-compatibility.patch) | RoboDojo root | Aligns environment origin device/dtype with pose tensors for relative poses; registers PhysX before binding CUDA cloth views and preserves native positions and quaternions required by reward queries. |

```sh
export AURIGA_REPO_ROOT="$PWD"                     # Auriga checkout root
export AURIGA_ROBODOJO_ROOT="$HOME/code/robodojo"  # RoboDojo checkout

git -C "$AURIGA_ROBODOJO_ROOT" checkout 726e9aabfaa642203722eb126f5eaf0f37f3e1ad
git -C "$AURIGA_ROBODOJO_ROOT" submodule update --init --recursive

for patch in robodojo native-runtime-compatibility; do
  git -C "$AURIGA_ROBODOJO_ROOT" apply --check "$AURIGA_REPO_ROOT/docs/development/patches/$patch.patch" &&
  git -C "$AURIGA_ROBODOJO_ROOT" apply "$AURIGA_REPO_ROOT/docs/development/patches/$patch.patch"
done
git -C "$AURIGA_ROBODOJO_ROOT/third_party/IsaacLab" apply --check "$AURIGA_REPO_ROOT/docs/development/patches/isaaclab.patch" &&
git -C "$AURIGA_ROBODOJO_ROOT/third_party/IsaacLab" apply "$AURIGA_REPO_ROOT/docs/development/patches/isaaclab.patch"
```

If `apply --check` fails, the patch has typically already been applied or the checkout does not match the pinned revisions in the table above; do not force-apply it. On multi-GPU machines, verify that Vulkan and `nvidia-smi` device ordering match your setup.

## Pointing Auriga to RoboDojo

Backend paths are configured under the `backend` section of the unified configuration with the following defaults:

| Field | Default | Environment Variable |
| --- | --- | --- |
| `backend.root` | `~/code/robodojo` | `AURIGA_ROBODOJO_ROOT` |
| `backend.python` | `~/miniforge3/envs/RoboDojo/bin/python` | `AURIGA_ROBODOJO_PYTHON` |

If your installation lives elsewhere, override these paths in a Git-ignored `configs/auriga.local.json` or in a custom configuration file:

```json
{"version": 1, "backend": {"root": "/opt/robodojo", "python": "/opt/conda/envs/RoboDojo/bin/python"}}
```

Run `mamba run -n Auriga python -m auriga.apps.gateway --print-config` to verify the resolved values without starting the robot, model, or server. For the full configuration precedence rules, see [Configuration](configuration.md#usage-and-override-precedence).

## Verifying the Native Environment

The following tests read the configured backend paths and validate numerical behavior and interfaces without running a full task:

```sh
mamba run -n Auriga python -m unittest discover -s tests -p test_native_pose_devices.py
mamba run -n Auriga python -m unittest discover -s tests -p test_native_garment_runtime.py
```

Cross-device checks additionally require `AURIGA_NATIVE_TEST_CUDA=1` and a free GPU in `CUDA_VISIBLE_DEVICES`. Passing these unit tests does not validate end-to-end task performance; use a single-run task from the [Running Guide](running.md#launching-a-single-run) or `scripts/robot_validation.py` to test real robot motion.

## Optional Components

- **External Perception Service**: The default configuration enables `features.external_perception`, which queries the local VGGT service at `http://127.0.0.1:8331`. If the service is not running, the corresponding fields are marked `unavailable` and the episode continues normally. See [`services/vggt_height/README.md`](../../services/vggt_height/README.md) for installation and startup instructions, or set `"external_perception": false` in your configuration when not needed.
- **$\pi_{0.5}$ Local Policy**: Disabled by default (`features.pi05=false`). It uses OpenPI from `XPolicyLab` in a dedicated Python interpreter (`policy.python` / `AURIGA_PI05_PYTHON`) together with weights matching the ARX X5 (`policy.checkpoint` / `AURIGA_PI05_CHECKPOINT`). For weight provenance and checksums, see [`pi05_arx_manifest.json`](../../auriga/runtime/policy/policy_workers/pi05_arx_manifest.json); for the service interface, see [$\pi_{0.5}$ Inference Service](pi05-inference-service.md).
