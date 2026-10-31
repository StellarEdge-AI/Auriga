# RoboDojo 运行环境

Auriga 自身只占一个 Python 环境（Miniforge `Auriga`）；机器人 worker 在单独安装的 RoboDojo / Isaac Sim 解释器里运行，两套依赖不合并。本文说明 RoboDojo 侧需要准备什么、要打哪些补丁，以及 Auriga 如何找到它。Harness 环境、网页构建和首次启动见[运行指南](running.md)。

## 固定版本

当前验证过的组合是 RoboDojo + 双臂 ARX X5。外部 checkout 固定到以下 revision：

| 仓库 | 位置（相对 RoboDojo 根目录） | revision |
| --- | --- | --- |
| RoboDojo | `.` | `726e9aabfaa642203722eb126f5eaf0f37f3e1ad` |
| IsaacLab | `third_party/IsaacLab`（子模块） | `afca7b09d60d8beb9c1cb28b43066499940b969b` |
| cuRobo | `third_party/curobo`（子模块） | `d17b54ce32cba095c0b000c4c58777075d11de0e` |
| XPolicyLab | `XPolicyLab` | `bb9a0b5f5136a74503b679af830bfd0a3a837d5c` |

按 [RoboDojo 上游仓库](https://github.com/RoboDojo-Benchmark/RoboDojo) 在该 revision 的安装说明配置 Isaac Sim 5.1、CUDA 和任务资产，得到一个能独立运行 RoboDojo 评测的 Python 环境，再打下面的补丁。

## 补丁

三份补丁都在 [`docs/development/patches/`](patches/)，只改运行兼容性，不包含任务解法，也不修改评分规则：

| 补丁 | 应用位置 | 作用 |
| --- | --- | --- |
| [`robodojo.patch`](patches/robodojo.patch) | RoboDojo 根目录 | `src/eval_client/main.py`：CUDA 与 Vulkan 的 GPU 编号分离。 |
| [`isaaclab.patch`](patches/isaaclab.patch) | `third_party/IsaacLab` | `app_launcher.py` 的 GPU 编号处理与 `setup.py` 中 Isaac Sim 5.1 的 Starlette 兼容。 |
| [`native-runtime-compatibility.patch`](patches/native-runtime-compatibility.patch) | RoboDojo 根目录 | 相对位姿的环境原点跟随位姿 tensor 的 device/dtype；CUDA 布料在绑定 view 前注册 PhysX，并保留 reward 查询需要的原生位置与四元数。 |

```sh
export AURIGA_REPO_ROOT="$PWD"                     # Auriga checkout 根目录
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

`apply --check` 失败通常表示该补丁已经应用过，或 checkout 不在上表的 revision；不要强行合入。多 GPU 机器上需自行核对 Vulkan 与 `nvidia-smi` 的设备顺序。

## 让 Auriga 找到 RoboDojo

后端位置来自统一配置的 `backend` 段，默认值为：

| 字段 | 默认值 | 环境变量 |
| --- | --- | --- |
| `backend.root` | `~/code/robodojo` | `AURIGA_ROBODOJO_ROOT` |
| `backend.python` | `~/miniforge3/envs/RoboDojo/bin/python` | `AURIGA_ROBODOJO_PYTHON` |

路径不同时，在 Git 忽略的 `configs/auriga.local.json` 或自己的配置文件里覆盖，例如：

```json
{"version": 1, "backend": {"root": "/opt/robodojo", "python": "/opt/conda/envs/RoboDojo/bin/python"}}
```

用 `mamba run -n Auriga python -m auriga.apps.gateway --print-config` 检查生效值；它不启动机器人、模型或服务。合并顺序见[配置说明](configuration.md#使用与覆盖顺序)。

## 检查原生环境

以下测试读取当前配置里的真实 backend 路径，只检查数值和接口，不运行任务：

```sh
mamba run -n Auriga python -m unittest discover -s tests -p test_native_pose_devices.py
mamba run -n Auriga python -m unittest discover -s tests -p test_native_garment_runtime.py
```

跨设备检查另需设置 `AURIGA_NATIVE_TEST_CUDA=1` 和空闲的 `CUDA_VISIBLE_DEVICES`。这些检查通过不代表任务效果；真实运动请用[运行指南](running.md#启动单局任务)里的单局任务或 `scripts/robot_validation.py`。

## 可选组件

- **外部感知服务**：默认配置开启 `features.external_perception`，会访问本机 `http://127.0.0.1:8331` 的 VGGT 服务；服务不在时对应结果标为不可用，运行继续。安装和启动见 [`services/vggt_height/README.md`](../../services/vggt_height/README.md)，不需要时可在配置中设 `"external_perception": false`。
- **π0.5 局部策略**：默认关闭（`features.pi05=false`）。它使用 XPolicyLab 中的 OpenPI 和独立解释器（`policy.python` / `AURIGA_PI05_PYTHON`）以及匹配 ARX X5 的权重（`policy.checkpoint` / `AURIGA_PI05_CHECKPOINT`），权重来源和校验值见 [`pi05_arx_manifest.json`](../../auriga/runtime/policy/policy_workers/pi05_arx_manifest.json)，接口见 [π0.5 推理服务](pi05-inference-service.md)。
