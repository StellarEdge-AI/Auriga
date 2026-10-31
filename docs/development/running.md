# 运行指南

本文集中说明环境准备、单局运行、会话恢复、回放与批量实验。框架结构见[主页](../../README.md)，字段与功能开关见[统一配置](configuration.md)，当前动作接口见[统一动作与程序审批](unified-actions.md)。

## 环境准备

Harness 的开发、测试、脚本和服务统一使用 Miniforge 的 `Auriga` 环境，不创建项目 `.venv`。未激活环境时使用 `mamba run -n Auriga python`；也可先明确激活 `Auriga` 后使用 `python`。机器人 worker 调用单独安装的 RoboDojo / Isaac Sim 环境，准备方法见 [RoboDojo 运行环境](robodojo-setup.md)。可选 OpenPI 策略也使用独立环境，不将仿真和策略依赖合并进 Harness。

运行前需要：

- 完整 Git checkout，以及 `environment.yml` / `requirements.lock.txt` 对应的依赖。
- 已安装 RoboDojo、任务资产和兼容的 Isaac / CUDA 运行环境（固定版本与补丁见 [RoboDojo 运行环境](robodojo-setup.md)），以及空闲 GPU。
- 已配置认证和 provider 的本地原生 Codex，能够访问所选模型；新实验默认读取 `~/.codex`，模型为 `gpt-6.1-sol`，推理档位另设为 `high`。供应商、认证和历史配置兼容见[配置说明](configuration.md)。
- OS 沙箱默认关闭（`features.sandbox_enabled=false`）。开启后需要支持 Landlock ABI ≥ 4、安装 libseccomp 的 Linux；隔离能力不满足时框架拒绝启动，不自动降级。
- 开启沙箱时，主 Codex 还需要 Linux、`bwrap`、系统 `python3`、C++ 运行库及 `ldd` / `ldconfig`；依赖从当前环境发现。优先使用非特权 user namespace，宿主禁用时才需要 `sudo -n` 与 `setpriv` 作为降权启动方式。Debian/Ubuntu 对应 `bubblewrap`、`util-linux`、`python3`、`libc-bin`、`libstdc++6` 包；`ripgrep` 为原生搜索工具提供 `rg`。沙箱使用当前 UID/GID、清除 capabilities，隔离不可用时终止启动。联网搜索关闭，Python 包只能经受控 PyPI 通道安装到私有目录；路径和 IPC 配置见[配置说明](configuration.md)。

默认关闭沙箱时，主/辅助 Codex 和 `program_run` 不使用 OS 隔离，代码拥有启动用户的宿主文件与网络权限；Harness 的观察投影、动作准入和程序审核照常执行。需要隔离时，用 JSON `{"version":1,"features":{"sandbox_enabled":true}}` 配合 `--config` 为新运行开启；完整说明见[代码执行沙箱](configuration.md#代码执行沙箱)。

在仓库根目录安装主环境并构建网页；已有环境可跳过创建：

```sh
mamba env create -f environment.yml
mamba run -n Auriga python -c "import sys; from importlib.metadata import version; print(sys.executable); print('SDK', version('openai-codex')); print('CLI', version('openai-codex-cli-bin'))"
npm --prefix web ci
npm --prefix web run build
```

运行使用 SDK 配套的原生 Codex 二进制，不依赖 PATH 中的包装脚本；实际 SDK / CLI 版本与路径随每局归档。启动和恢复前检查两包版本均与项目协议快照匹配，版本不符会在停止旧进程或启动新 app-server 前报 `codex_version_mismatch`。更新当前 Harness 环境的 `requirements.lock.txt` 依赖后再运行，不要只更新 PATH 中的 Codex。外部 RoboDojo 版本、补丁与可选 OpenPI 环境见 [RoboDojo 运行环境](robodojo-setup.md)。

需要安装到独立位置时，使用以下完整 wheel 构建。构建脚本在临时目录组装所有 Python 子包、网页、策略桥接脚本和固定版本参考资料；不会修改 checkout 或部署服务。只运行普通 `pip wheel .` 不包含尚未构建的网页资源，应使用此入口：

```sh
mamba run -n Auriga python scripts/build_package.py --output dist
mamba run -n Auriga python scripts/check_package.py dist/*.whl
mamba run -n Auriga python -m pip install dist/*.whl
```

`check_package.py` 用当前解释器执行 `pip install --no-deps --target`，将 wheel 安装到临时目录，再在 checkout 外用该解释器的 `-I` 隔离模式检查；不创建或使用项目 `.venv`。安装后在 `Auriga` 环境中可以使用 `auriga`、`auriga-queue`、`auriga-report`、`auriga-audit`。源码中的旧 `scripts/benchmark_queue.py`、`benchmark_report.py`、`audit_run.py` 继续转发到同一实现。RoboDojo、Isaac 和可选策略环境仍由配置指定；wheel 不打包模型权重、认证或外部仿真环境。固定实验组生成器及开发探针继续从完整 checkout 使用。

默认配置文件是 [`configs/auriga.json`](../../configs/auriga.json)。本机可以用 Git 忽略的 `configs/auriga.local.json` 覆盖默认值；供应商配置和 Key 放在仓库外的独立 Codex home，本地 JSON 只保存其路径，具体见[配置说明](configuration.md)。需要改变机器路径或模型时，从[完整示例](../../configs/auriga.example.json)选取所需字段，保存为自己的配置文件。默认后端路径是 `~/code/robodojo` 与 `~/miniforge3/envs/RoboDojo/bin/python`；也可通过 `AURIGA_ROBODOJO_ROOT` 和 `AURIGA_ROBODOJO_PYTHON` 覆盖。

```sh
# 只检查有效配置，不启动机器人、模型或服务。
mamba run -n Auriga python -m auriga.apps.gateway \
  --config configs/my-experiment.json --print-config
```

配置合并顺序为默认值 → 兼容环境设置 → 公共默认文件 → 本地默认文件（若存在）→ 显式配置文件 → 显式 CLI 参数。每次新运行保存有效配置；恢复与回放使用归档条件。模型认证与机器凭据不进入版本库。

## 启动单局任务

每局默认无人值守。模型自主决定策略、工具参数和程序动作审批，自行处理问题并继续推进，不要求人类确认。原生用户输入立即返回空 answers；当前会话所属的命令／文件审批自动批准并使用 acceptForSession。开启 `features.sandbox_enabled` 时，额外权限仅作用于外层 namespace 内，程序也受 Landlock 隔离。默认关闭时，原生命令可访问启动用户有权访问的宿主文件与网络；Harness 工具的观察投影、动作准入和程序审核仍保留。人工停止仍可用。

在以下命令中选择本机空闲 GPU，并为新运行使用新目录。该命令会启动真实模型和机器人仿真：

```sh
mamba run -n Auriga python -u -m auriga.apps.gateway \
  --benchmark --task general_pickup --seed 0 --layout-id 0 \
  --gpu 0 --port 8767 --run-dir runs/my-first-task
```

打开 `http://127.0.0.1:8767` 查看机械臂画面、原生终端、动作与运行记录。`--benchmark` 使用环境提供的原生公开任务指令，按原生动作步数与终态运行；结束后独立评分、导出录像并释放资源。

开发时可用自定义提示和有限预算进行接口验收：

```sh
mamba run -n Auriga python -u -m auriga.apps.gateway \
  --gpu 0 --port 8767 --run-dir runs/my-interface-check \
  --prompt '观察当前场景，提交计划，执行一次小幅末端动作并检查反馈。' \
  --max-actions 2 --wall-budget-s 900 --exit-after-turn
```

`--max-actions` 和 `--wall-budget-s` 只用于开发运行，正式 benchmark 不接受这些额外上限。当前 benchmark 保留模型返回预算和无进展保护，具体计数与终止语义见[配置说明](configuration.md)和[模型主导操作](model-led-operation.md)。

## 交互、停止与恢复

网页交互经 Harness 提交到同一原生 thread，写入控制权通过租约协调。网页终端用于显示原生输出；新提示和停止由网关处理。无人值守请求自动处理并保留记录，对已处理或过期请求的人工审批／问题回复会被拒绝。没有网页持有写入租约时，CLI 也可以提交提示：

```sh
curl -fsS http://127.0.0.1:8767/v1/control \
  -H 'Content-Type: application/json' \
  --data '{"request_id":"review-1","command":"prompt","prompt":"Review the previous request and measured feedback; do not move the robot."}'
```

`request_id` 必须唯一；同内容重发返回原响应，同 ID 改内容会被拒绝。新提示仅在上一 turn 和动作均结束、运行仍接收任务时提交。

网页停止或 `command: "stop"` 先关闭动作接收并请求取消，再中断模型；停止状态以实际回执为准。关闭网页不等于停止机器人。Ctrl-C / SIGTERM 用于结束整个服务。

```sh
# 无需 GPU 或模型的只读回放。
mamba run -n Auriga python -m auriga.apps.gateway \
  --replay --run-dir runs/my-first-task --port 8767

# 开发运行恢复原 thread；会重新创建物理 episode。
mamba run -n Auriga python -m auriga.apps.gateway \
  --resume --run-dir runs/my-interface-check --port 8767
```

恢复不重建原物理状态，重新加载的环境会使用新的 episode / generation；存活的旧网关或机器人 worker 会阻止恢复。`--benchmark` 不与 `--resume` 同时指定；仅用 `--resume` 可沿用归档的 benchmark 设置，但新 episode 不能视为原回合的连续物理执行。`features.sandbox_enabled` 必须沿用原会话模式，改变模式须新建 run。查看历史结果使用只读回放。

## 实验库与批量评估

省略 `--run-dir` 时，新运行默认保存到 `~/exp/auriga/runs/YYYY/MM/DD/category/task/run-id/`，日期采用 UTC+8。可以用 `--category`、`--title`、`--tag` 标记实验。实验库默认监听 `http://127.0.0.1:8780`，提供检索、直播入口与只读回放：

```sh
mamba run -n Auriga python -m auriga.apps.library
```

常驻服务、原始数据导入和远程访问说明见[实验库文档](experiment-library.md)。源码克隆不会复制运行数据、部署服务或访问权限。

固定任务组 [`gpt-as-policy-10`](../../benchmarks/gpt-as-policy-10/README.md) 包含 50 个 case。先离线生成新 manifest，再用队列启动；以下输出文件和目录须尚未使用：

```sh
mamba run -n Auriga python scripts/benchmark_group.py --group gpt-as-policy-10 \
  --run-root ~/exp/auriga/benchmark/gpt-as-policy-10/trial-001 \
  --output /tmp/gpt-as-policy-10-trial-001.json

mamba run -n Auriga python -m auriga.apps.evaluation.queue \
  /tmp/gpt-as-policy-10-trial-001.json \
  --output ~/exp/auriga/queues/gpt-as-policy-10-trial-001
```

组协议不覆盖本地模型和推理档位。队列按配置执行 GPU 准入、并发与故障处理；实际版本、seed、观察条件和协议差异随运行记录。该组用于同一 case 集重新评估，严格复现还需核对上游版本与全部条件。

离线统计已有运行，不启动机器人或模型：

```sh
mamba run -n Auriga python scripts/benchmark_report.py \
  /path/to/run1 /path/to/run2 --output /path/to/new-report.json
```

## 验证与独立模块

常规代码验证入口：

```sh
mamba run -n Auriga python -m unittest discover -s tests -v
npm --prefix web run check
npm --prefix web run build
```

运行证据审计使用 `mamba run -n Auriga python scripts/audit_run.py <运行目录>`，默认检查记录关联和 artifact 完整性，保留未确认动作，不要求特定模型或原生日志。`--profile native` 另检查原生调用、实际图像交付、动作及完成回合；`--require-continuity` 再要求多回合连续性。原图已归档而无法验证源字节时，native 审计明确失败。两种审计都不代替 evaluator 的任务评分。

`mamba run -n Auriga python scripts/benchmark_report.py <运行目录> ...` 保留每个坏目录的错误和总体可读覆盖率；缺失用量保持未知，不作为零成本或任务失败。队列启动与每个 case 启动前核对源码字节摘要，源码漂移会停止继续调度并清理自己持有的进程。正式运行应使用固定 checkout；每次启动前校验不能使仍可修改的目录成为操作系统层面的只读快照。

真实机器人矩阵使用 `mamba run -n Auriga python scripts/robot_validation.py --gpu <空闲 GPU> --run-dir <新目录>`，会占用 GPU 并实际运动；CPU 测试通过不能替代真实任务效果验证。CI 自动执行 CPU、schema、网页检查及完整 wheel 的独立安装验证；真实 GPU／模型验收另存实验记录。

独立场景服务可通过 `mamba run -n Auriga python -m auriga.perception.scene.scene_service --root <数据目录> --port 8770` 启动，接口说明为 `/v1/openapi.json`。已有能力包括许可观察导入、有限几何、版本与快照；完整 Isaac 物理 rollout 尚未实现。该模块不拥有被测世界的动作或复位权限，也不作为新任务的 `scene_*` 工具公开。设计与验收入口见[文档索引](../README.md)。
