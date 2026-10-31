# 统一配置与功能开关

`auriga.core.config.Config` 是新运行的配置入口。默认文件为仓库内的 `configs/auriga.json`；完整字段示例见 [`configs/auriga.example.json`](../../configs/auriga.example.json)。通常只写需要改变的字段，其余使用配置类的默认值。JSON 不支持注释，字段用途、依赖和旧入口对照在本文集中说明。

`scripts/build_package.py` 构建 wheel 时将同一份仓库默认文件复制为包内 `auriga/default_config.json`。安装版找不到 checkout 默认文件时读取该副本，因此新实验默认与源码版一致；显式 `--config` / `AURIGA_CONFIG` 仍优先，`Config.from_dict()` 的旧档解释不变。

## 使用与覆盖顺序

以下项目命令使用 Miniforge 的 `Auriga` 环境；创建环境及实际解释器、SDK/CLI 的核对见[运行指南](running.md#环境准备)。

```sh
# 使用仓库默认配置
mamba run -n Auriga python -m auriga.apps.gateway --run-dir runs/my-run --gpu 4

# 使用专门的实验配置；命令行参数只覆盖本次运行
mamba run -n Auriga python -m auriga.apps.gateway --config configs/my-experiment.json \
  --run-dir runs/my-run --gpu 4 --no-enable-wait

# 只检查并打印完整有效配置，不启动模型、GPU 或服务
mamba run -n Auriga python -m auriga.apps.gateway --config configs/my-experiment.json --print-config

# 所有支持统一配置的入口共用这个文件
export AURIGA_CONFIG=/absolute/path/auriga.json
```

配置按“配置类默认值 → 兼容的旧环境设置 → 仓库默认文件（安装版使用包内副本）→ 存在的仓库本地配置 `configs/auriga.local.json` → 显式 JSON 配置文件 → 显式命令行参数”合并。嵌套对象按字段合并，因此只覆盖 `features` 的实验配置仍继承当前默认模型和本地 Codex home；显式填写的完整旧模型设置仍可覆盖新默认。`--config` 比 `AURIGA_CONFIG` 优先；二者都省略时使用合并后的公共与本地默认。每份文件分别严格校验，相对路径分别以各自所在目录为基准。未显式传入的 CLI 默认值不会覆盖文件。拼错的字段、错误类型、非有限数字和不受支持的枚举会在启动服务、模型或机器人之前被拒绝。

文件中的相对路径以该配置文件所在目录为基准；CLI / 环境兼容路径以调用时的工作目录为基准，`~` 会展开。完整快照使用绝对路径。最小文件只需要 `{"version": 1}`；配置类本身也提供全部默认值。完整示例保留 `~`，复制到其他机器后应检查 `backend` 和 `policy` 路径。

列表按整体覆盖处理；例如显式 `--tag trial` 替换文件标签，`--clear-tags` 清空标签。JSON 中的 `null` 只用于 schema 允许的可选字段，不能用来关闭布尔开关；布尔开关使用 `false`。

例如，下面的文件保持原有默认观察条件，关闭等待和 π0.5，开启跟踪保护及紧凑回包：

```json
{
  "version": 1,
  "features": {
    "enable_wait": false,
    "pi05": false,
    "tracking_guard": true,
    "actor_payload_profile": "compact_v1"
  },
  "images": {
    "reuse_exact_images": false
  }
}
```

新运行默认仍然开启 `robot_wait`。关闭该工具只取消模型的等待能力，不会改变观测是否推进物理时间：`robot_observe`、模型思考和现实时间 sleep 都不推进暂停的世界。

Python 调用方使用同一个不可变配置对象，避免各模块再次读取环境变量或产生不同默认值：

```python
from auriga.core.config import Config

config = Config.load("configs/my-experiment.json").validate_launch()
assert config.features.enable_wait is False

# 返回新对象，不修改原配置；显式 False 不会被当作“未提供”。
candidate = config.with_overrides({"features": {"enable_wait": True}})
snapshot = candidate.to_dict()

# 读取归档时不读取当前环境变量或默认文件。
restored = Config.from_dict(snapshot)
```

`Workbench`、`Harness`、`RobotBackend`、`CodexSession` 和实验库接受 `config=`；保留已有显式构造参数兼容调用方。完整加载和 launch 校验不启动外部服务。

## 运行和模型

| 字段 | 默认值 / 取值 | 用途与约束 |
|---|---|---|
| `run.task` | `stack_bowls` | 原生任务名称 |
| `run.seed` / `run.layout_id` | `0` / `0` | 非负整数；实验初始化条件 |
| `run.gpu` | `4` | 机器人和本地策略使用的 GPU；队列会选择实际 GPU |
| `run.run_dir` | `null` | 指定新运行目录；省略则由实验库分配 |
| `run.experiment_root` | `~/exp/auriga` | 自动分配运行目录的归档根目录 |
| `run.category` / `run.title` / `run.tag` | `main` / `null` / `[]` | 归档分类、名称和标签 |
| `run.port` | `0` | 仅 loopback；`0` 自动选择空闲端口 |
| `run.prompt` | `null` | 省略时使用默认接口验收提示；benchmark 使用原生公共指令 |
| `run.benchmark` | `false` | 原生任务流程、同一 thread 续轮、评分及结束收尾 |
| `run.exit_after_turn` | `false` | 首轮结束后退出；benchmark 会自行运行至协议终止条件 |
| `run.resume` / `run.replay` | `false` / `false` | 互斥且要求已有 `run_dir`；benchmark 只允许新运行 |
| `run.max_actions` | `null` | 开发验收的高层动作数上限；正式 benchmark 禁止设置 |
| `run.wall_budget_s` | `null` | 开发运行整局墙钟预算；正式 benchmark 禁止设置 |
| `run.task_notes` / `run.task_notes_source` | `null` / `null` | 追加到默认操作经验后的 benchmark 公共说明及来源；二者必须成对提供且非空，自定义正文最多 8000 字符 |
| `model.name` / `model.effort` | 仓库新运行默认 `gpt-6.1-sol` / `high` | 主 actor 模型与推理档位；两个字段独立配置 |
| `model.codex_home` | 仓库新运行默认 `~/.codex` | 宿主读取本地 Codex provider 与认证；不继承宿主扩展，会话使用本 run 私有目录，OS 隔离由 `features.sandbox_enabled` 控制 |
| `model.price_card` | `null` | 显式价格表文件；估算费用不改变实际 token 证据 |
| `model.native_memories` | 仓库新运行默认 `true` | 控制私有 Codex 原生记忆读取和自动整理；不修改宿主配置或 Auriga `operation_memory` |
| `model.transport_recovery.max_rounds` | `10` | 12 次明确可重试错误的自动重试用尽后，或上游传输中断（`transport_incomplete`）后，冷却并重发同一请求的轮数上限，每轮一次 HTTP 尝试；`0` 关闭，最大 20 |
| `model.transport_recovery.initial_delay_s` / `backoff_multiplier` / `max_delay_s` | `60` / `2` / `600` | 冷却时长：60、120、240、480、600… 秒 |
| `model.transport_recovery.max_episode_delay_s` | `3600` | 整局（同一代理生命周期）恢复冷却总秒数上限；超过即按原逻辑终止 |
| `model.transport_recovery.transport_incomplete` | `true` | 是否也恢复无终态的上游传输中断（如 `IncompleteRead`）；`false` 只恢复明确可重试的 provider 错误 |

配置文件不承载 API token 或复制 provider 凭据。Provider、认证方式和上游地址仍由 `model.codex_home` 下的原生 Codex 配置 / 认证文件管理；环境中的 provider key 仍按原有方式读取。

本机可用 Git 忽略的 `configs/auriga.local.json` 选择独立 provider home，例如 `{"version":1,"model":{"codex_home":"~/.config/auriga/providers/my-provider"}}`。本地 JSON 只保存路径；将供应商的 `config.toml` 和含 Key 的 `auth.json` 放在该仓库外目录，目录权限设为 0700，两个文件与本地 JSON 均设为 0600。不要把 Key 写入统一 JSON，其有效配置会进入 manifest、实验归档和网页报告。独立 home 避免切换供应商影响日常 Codex 或旧队列；本地默认不进入 wheel。显式 `--config` 仍可覆盖本地默认，`Config.from_dict()` 与已冻结的队列不读取该本地文件，历史快照不因本地默认变化而重新解释。

仓库的 `configs/auriga.json` 和完整示例为新实验显式选择 `gpt-6.1-sol`、`high` 与本地 `~/.codex`；本地默认或显式配置可以选择其他独立 home。运行从选定目录的 `config.toml` 读取所选 provider，从 `auth.json` 或 provider 声明的认证设置读取凭据，真实认证只交给宿主 relay。actor 的私有 home 使用占位认证，不复制源目录的真实 Key、`.credentials.json` 或宿主扩展，也不写回源目录。不要将凭据写入跟踪配置、manifest、命令行或日志，也不要把源 Codex home 加入 Git。默认 `model.price_card = null`，避免沿用其他 provider/model 的价格表。

主 actor 的模型和 reasoning effort 以统一配置为准，源 Codex home 中同名偏好不会替代它们。harness 已固定生成 `web_search = "disabled"`、`service_tier = "default"` 和 Responses 协议；原生 provider 名 `auriga_model_proxy` 是本地计数代理，外部上游由所选本地 Codex provider 决定。可选视觉辅助模型复用主会话生成的 provider、代理及认证，在独立 home/thread 内运行 `gpt-6.1-sol` / `high`，不会再次读取源 Codex 配置。

新运行的默认模型选择只写在仓库配置文件中；`Config.from_dict()` 不读取当前默认文件，缺少模型字段的旧档仍按既有 `gpt-6-astra` / `~/.codex` 默认解释。已冻结 `auriga_config` 的队列继续使用原配置。未来重新使用旧 manifest 启动新实验时，应显式传入 `--config configs/auriga.json`，或重新生成 manifest。队列冻结的是 `codex_home` 路径，case 启动时仍会读取该目录的 provider 文件，因此切换未来默认时必须使用独立目录，不能改正在运行队列引用的 home。兼容的 `~/.config/auriga/experiments.json` 只支持 `codex_home` 和 `price_card` 路径；不使用价格表时应省略其中的 `price_card`，不能写 JSON null。

`manifest.auriga_config` 是有效配置快照；即使字段不完整，也按历史默认解释。新实验的局部配置覆盖请通过 `--config` 提供，生成器会先加载当前默认，再冻结完整快照。

## 代码执行沙箱

`features.sandbox_enabled` 是全局布尔开关，配置类和仓库 `configs/auriga.json` 均默认 `false`，开启隔离须显式设置为 `true`。它同时控制主 Codex（含原生终端、子代理、shell 和记忆整理）、辅助 evidence agent 的 bubblewrap namespace，以及 `program_run` 的 Landlock/seccomp。关闭时不探测或加载这些隔离能力，因此无需安装 `bwrap` 或 libseccomp，也无需 user namespace / Landlock 支持；其他运行依赖仍须满足。

例如，将以下内容保存为 `configs/sandbox.json`，只为新运行开启沙箱：

```json
{
  "version": 1,
  "features": {
    "sandbox_enabled": true
  }
}
```

```sh
mamba run -n Auriga python -m auriga.apps.gateway --config configs/sandbox.json --print-config
mamba run -n Auriga python -m auriga.apps.gateway --config configs/sandbox.json \
  --run-dir runs/sandbox-trial --gpu 4
```

关闭时（默认）仍使用每局独立的 `HOME` / `CODEX_HOME`、模型 relay 和占位认证，不继承宿主 MCP、插件或 skills；`program_run` 的 broker、工具权限、动作审核、预算与资源上限继续生效。但代码拥有启动用户的宿主文件和网络权限，私有目录和占位认证不构成 OS 安全边界，正式观察限制仅由应用层执行。此模式不能作为宿主文件、凭据、evaluator 或 observer 数据不可访问的证明；无沙箱运行与隔离运行须分别记录条件。

关闭时公共源码副本位于 `<run>/private/actor-source`，不再映射为 `/harness`。该开关保存到有效配置快照；修改配置不会热切换正在运行的实验或已冻结队列。恢复会话不能在两种模式之间切换，改变模式须新建 run；缺少隔离标记的旧 full-access 会话也不能原地迁移。

以下隔离、挂载与受控网络规则仅适用于显式开启的 `features.sandbox_enabled=true`。Harness 在 `CodexSession._prepare()` 中把整个原生 app-server 放进 Linux bubblewrap 沙箱。mount、PID、IPC 和 network namespace 覆盖主线程、子代理、shell 和记忆整理；进程使用当前用户的 UID/GID、清空 capabilities。`bwrap`、`sudo`、`setpriv` 从当前 PATH 和平台默认 PATH 发现，优先使用无需 sudo 的 user namespace；宿主禁用该能力时，才尝试 `sudo -n` 创建 namespace 并用 `setpriv` 降权。启动失败会终止，不回退到宿主直接运行。

运行目录、源码位置和原生 Codex executable 分别来自 run 配置、已安装的 Harness 包和固定版本的 CLI 包，不依赖用户名、checkout 或 conda 的安装位置。IPC 使用 `XDG_RUNTIME_DIR`，未设置时使用 Python 的平台临时目录；配置的 runtime 目录必须属于当前用户、权限为 0700 且足够短以容纳 Unix socket。系统 `python3` 从平台默认 PATH 发现，标准库位置由该解释器报告，ELF 动态库由 `ldd` 解析；不挂载整棵 `/usr`、Python/conda 环境或宿主 site/dist-packages。`/harness`、`/tmp`、`/usr/bin` 等是沙箱内部挂载点，不是宿主机器路径配置。

系统命令按 [`system_runtime.py`](../../auriga/cortex/session/system_runtime.py) 中的明确清单发布，包括 shell、文件操作、文本处理、压缩和本地 Git；`rg` 从当前环境单独发现。系统没有安装的可选命令不凭空提供。Python 可以通过私有 PyPI 通道安装计算依赖；其他宿主工具、编译工具链及系统包管理器不会因位于 `/usr` 就自动公开。

沙箱内部保留 `approval_policy="never"`、`sandbox_mode="danger-full-access"`，表示原生工具可在**外层沙箱内**自由工作。操作系统隔离不依赖 Codex 自觉遵守提示。默认功能如下：

```toml
web_search = "disabled"
approval_policy = "never"
sandbox_mode = "danger-full-access"
model_verbosity = "high"
service_tier = "default"

[features]
shell_tool = true
unified_exec = true
memories = true
multi_agent = true
multi_agent_v2 = true
hooks = true
goals = true
browser_use = false
browser_use_external = false
computer_use = false
apps = false
plugins = false
artifact = false
image_generation = false
network_proxy = false
code_mode_host = false
fast_mode = false
```

`HOME` 和 `CODEX_HOME` 是本 run 的 actor 工作区和私有会话目录；系统运行依赖只读，临时目录属于沙箱。`/harness` 是当前公共 Harness / ToolSpec / 动作与程序接口实现的只读副本，发布清单位于 `auriga/cortex/session/actor_source.py`。它不包含 RoboDojo、后端任务代码、评测器、参考策略、`.git`、历史补丁、实验数据库、宿主 home 或其他仓库。源码用于阅读接口；不会额外挂载完整 checkout 来满足 import。

网络 namespace 没有宿主或公网直连。仅通过本 run 的 Unix socket 转发到固定的计数模型 relay、必要的只读 account relay 和 Python 包代理。包代理仅允许 HTTPS 到 `pypi.org`、`files.pythonhosted.org`，支持 `python3 -m pip install 包名`，安装目标和缓存属于 actor 工作区；任意网站、宿主 loopback 服务、SSH、Git URL 和其他包镜像不可访问。联网搜索、浏览器及外部 app/MCP 入口关闭；模型 relay 也拒绝服务端搜索等远程工具请求。

宿主的 provider 路由和认证由 Harness 读取并留在宿主 relay；Codex 只收到本地 relay 地址和占位认证。支持本地 `auth.json` 的 API key / ChatGPT access token、provider `env_key` 或 bearer token；自定义认证 header 限于 `Authorization` / `ChatGPT-Account-Id`。不复制真实 `auth.json`、`.credentials.json`、MCP、插件、skills、宿主历史或记忆，不修改所选源 Codex home 或 CC Switch。ChatGPT 凭据在宿主启动时读取，沙箱不会刷新宿主登录；凭据过期需由宿主处理。

动态机器人工具继续采用 direct catalog 与 `code_mode=false` / `code_mode_host=false`，保证独立图片项进入 Responses。模型与 effort 来自统一运行配置；后台记忆和 title 等原生请求按实际角色、模型另行记录。`model.native_memories=true` 仍允许本 run 的原生文件记忆，显式 false 可关闭。Auriga `operation_memory` 和 grasp-memory 保持独立。

独立 evidence agent 使用自己的 namespace、空 workspace 和独立 IPC，不挂载主 actor 工作区、源码或装包入口；只接收选定证据并禁用工具。`program_run` 保留专属 broker、Landlock/seccomp；Python 仅允许 stdlib 和指定计算依赖，禁用宿主 `.pth`，不开放整个 conda 或 site-packages。机器人动作继续经 Harness 唯一入口。

## 功能开关

| 字段 | 新运行默认值 | 控制内容 |
|---|---|---|
| `features.sandbox_enabled` | `false` | 主/辅助 Codex namespace 与 `program_run` Landlock/seccomp 的全局开关，显式 `true` 才开启；关闭时使用启动用户的宿主文件/网络权限，保留应用层工具校验、动作审核与预算；见[代码执行沙箱](#代码执行沙箱) |
| `features.observation_profile` | `rgb_proprio_calibrated_v1` | 许可 RGB、本体状态、精确标定和公开机器人模型；另可选旧条件 `rgb_proprio_v1`，显式 `rgbd_proprio_calibrated_v1` 才许可原生深度及数值工具 |
| `features.motion_review` | `true` | 建议式运动预览、缓存计划执行、可选抓取规划与记忆工具；风险不作为执行许可；旧观察条件下自动关闭 |
| `features.default_task_notes` | `true` | 新 benchmark 注入版本化操作经验；关闭后仍可使用自定义 `run.task_notes`；普通 smoke 不注入默认内容 |
| `features.vision_compare` | `true` | RGB 特征比较及依赖比较的 RGB 技能；关闭不影响另行显式启用的 RGB-D 技能 |
| `features.robotic_view` | `false` | [腕部视角解算](robotic-view.md)：从调用者选择的视点计算 link6 姿态；要求许可标定，只返回预览参数，不移动机器人 |
| `features.stereo_guide` | `false` | [双目极线引导](stereo-guide.md)：在同期另一相机图像上标出源像素的极线；要求许可的零畸变针孔标定，不自动匹配或三角化 |
| `features.depth_auto_feedback` | `false` | 已登记目标的动作前后数值核验；要求 RGB-D profile，不增加 capture 或动作 |
| `features.depth_skills` | `false` | 有界抓取短抬与放置释放撤手技能；要求 RGB-D profile。执行入口 `skill_run` 已在 2026-10 重构中删除，当前仅影响 `depth_query` `skill_help` 目录的 `enabled` 字段 |
| `features.depth_event_payload` | `false` | 有可靠测量的普通中间动作可仅交付数值，完整图像始终归档；要求 RGB-D profile 和 depth_auto_feedback |
| `features.enable_wait` | `true` | `robot_wait`：显式推进 1–50 个原生步并返回时序图像 |
| `features.pi05` | `false` | 匹配 ARX X5 的 π0.5 短前缀策略工具 |
| `features.visual_review_model` | `null` | 默认关闭 Sol RGB/完整操作验证；显式 `gpt-6.1-sol` 开启，开启后计入同一模型预算 |
| `features.tracking_guard` | `false` | EE 动作持续关节跟踪偏差时停止，返回部分执行证据 |
| `features.joint_replan` | `true` | 尝试更近的合法等价关节终点，只选择已验证且更短的原生轨迹 |
| `features.motion_execution_mode` | `resampled` | 正式 actor/benchmark 使用 `resampled` 统一动作；`feedback` 仅用于可信旧入口的固定输入诊断，要求关闭 `motion_review` 和 `tracking_guard`，benchmark 启动前拒绝 |
| `features.default_motion_speed_scale` | `1.0` | 请求未选档位也未填写数值时的机械臂速度倍率，范围 0.25–2；显式选择覆盖默认，夹爪独立默认 1；feedback 仅允许默认 1 |
| `features.airborne_handoff` | `true` | 允许空中接手模板；`--no-airborne-handoff` 恢复 `surface_only` 硬限制，要求稳定支撑面中转。双臂同时移动、独立推进和队列始终保留 |
| `features.handoff_preference` | `surface` | `--handoff-preference surface/airborne` 选择允许空中交接时的偏好。新默认 `true + surface` 为 `prefer_surface`：模型自主选择，推荐物理支撑面，不强制落桌。`true + airborne` 为 `prefer_airborne`。显式旧bool未指定此字段时，true仍恢复旧偏好空中、false恢复硬限制；旧归档同样保留旧策略。禁止空中交接时不能同时偏好空中 |
| `features.actor_payload_profile` | `lean_v1` | 省略常规存储元数据和可查询的长引用，不舍入测量数值；另可选 `full_v1`、`compact_v1`，程序接口及原始证据始终完整 |
| `features.completion_guidance` | `hook` | benchmark 默认按实测归位状态选择提醒；`initial` 仅在初始 prompt 提醒；`both` 同时启用；显式 `none` 关闭归位检查，仅保留通用续接 |
| `features.grasp_memory_dir` | `~/.local/share/auriga/grasp-memory` | 跨运行抓取记忆目录；只有相关功能启用时使用 |
| `features.disabled_tools` | `[]` | 逐个禁用已知工具名称；关闭优先于功能组启用。`robot_capabilities` 不可禁用于 benchmark：gateway 启动时经注册表读取它；禁用 `operation_memory` 时同步省略其启动说明段 |
| `features.lean_projection` | `false` | 上游请求历史投影（[`auriga/core/presentation/lean_projection.py`](../../auriga/core/presentation/lean_projection.py)，默认关闭，只改发往上游的请求体，不改 Codex 线程历史）：将早期闭合块非锚点工具结果中的图片替换为占位文本，并将 6 个高频工具（`robot_action`、`program_run`、`robot_observe`、`robot_wait`、`geometry_query`、`history_view_observation`）的文本替换为确定性摘要；开启后观察回包不再引用此前交付的静态字段。**潜在性能影响**：压缩历史原始反馈与图像会丢弃闭合块内非锚点步的像素细节、完整轨迹采样与长 stdout（除非模型主动调 `history_view_observation` 重读），在长程空间推理或多步失败恢复中可能影响模型判断表现，故默认关闭 |
| `features.lean_projection_k` | `1` | 末尾块携带图片的最近工具步数，1–8（仅 `lean_projection_block=0` 生效） |
| `features.lean_projection_block` | `8` | 分块模式（`lean_projection` 开启时的默认策略）：不加末尾块；当前未闭合块内的工具步与每块第一步（锚点）原样发送，更早的步换摘要/占位；每 B 步改写一次，块内请求 n+1 = 请求 n + 新条目，改写时锚点请求仍是前缀，只重读被改写的一块。分块模式下决策边界提醒（`_task_decision_context`）不再每次放在末尾：文本不变，放在固定槽位且从不作为请求的最后一项（首个请求的任务消息之前一个；每块一个，在该块第一个有已存计划的请求上创建，位于最新一步的模型回合之前、即上一步工具输出之后），槽位保留创建时的快照，已闭合块的提醒原位保留；"无计划"提示只在首个槽位出现（见 `lean_projection.py` 文档串）。`0` = 末尾块模式（实测所用 Responses 兼容中转的上游缓存不复用去掉末尾块后的请求，命中停在最后一个无末尾块的请求） |
| `features.lean_request_dump` | `0` | 诊断：把前 N 个上游请求体（图片数据换成摘要）写入 `private/request-dump/`；`0` 关闭 |
| `features.external_perception` | `true` | 外部视觉模型（VGGT + SAM）总开关；配置类与仓库配置文件均默认开启 `vggt_height`（`vggt_height_mode="optional"`）、`object_table`、`scene3d`、`object_table_observe`、`scene3d_observe` 与 `held_perception`，服务地址为 `features.vggt_endpoint`（默认 `http://127.0.0.1:8331`）。服务未启动或请求失败时，相关结果标为 `unknown` / `unavailable` 并给出原因，观察与动作回执照常交付，不终止本局。设为 `false` 且子开关仍全部开启时全部关闭，不调用外部模型；全部子开关为 `false` 而总开关为 `true` 时全部开启；其他组合按各子开关取值。CLI 为 `--[no-]external-perception` 及各子开关的同名选项；见 `docs/vggt_height.md`、`docs/object_table.md`、`docs/scene3d.md`、`docs/obs3d.md` |
| `features.held_perception` | `true` | 持物与机械臂掩码感知（随 `external_perception` 默认开启，需 `object_table` 或 `object_table_observe`，服务支持 `object-table-v5-hp1`，对旧版 `object-table-v5` 自动降级回退）：物体表的机器人像素改用按实测关节 FK 渲染的机器人网格 mask（膨胀 3 px）代替 6 cm 连杆胶囊；夹爪闭合在物体上的臂得到一行持物行（quality `held_fk_depth`、support `held_<arm>`）：尺寸与长轴取抓取前该物体的物体表行并随 link6 刚性携带，中心为 FK 抓取中心（细长物体沿长轴按携带偏移平移，含 45° 轴冲突门禁）；无抓取前行时尺寸取 SAM mask 的 VGGT 点按 FK 抓取深度重标定；持物行不做悬空→桌面重标定；`value.held[id]` 给出方法、不确定度与来源行；developer 指令追加一句；关闭时目录、指令与物体表不变；见 `docs/held_perception.md` |
| `features.record_video` | `true` | 自动开始原生三相机录像 |
| `features.export_video` | `true` | 运行收尾时导出录像；关闭录制时不自动导出 |
| `features.terminal` | `true` | 启动原生只读 Codex 终端；可关闭用于无 TUI 运行 |

`feedback` 仅供 `scripts/probes/feedback_motion_probe.py --mode feedback` 等可信 Python 诊断使用；它不支持当前公开的 `robot_action`，不能用于 actor/program 的统一机械臂任务。`Config.validate_launch()` 和批量队列会在启动模型/worker 前拒绝 benchmark 的 feedback 配置。非 benchmark 的诊断工作台可指定 `--motion-execution-mode feedback --no-motion-review --no-tracking-guard`，但不会因此获得公开机械臂动作工具；历史配置仍可读取和回放。它每个 waypoint 规划一次，在限位内选择距当前实测关节最近的2π等价终点，并通过URDF全link等价性及目标位姿校验后固定该终点，通过同一个原生 `take_action` 入口反复下发；位置误差达到请求容差、姿态误差不超过 0.1 rad、所有活动臂关节速度不超过 0.1 rad/s，连续两个原生步满足后才完成移动。15 个原生步无足够最佳位置/姿态误差或关节目标误差改善时报告 `feedback_stalled`，不再依赖瞬时速度；位置已到但速度始终未稳定也受此判停约束；单动作仍受 10 秒原生时间（默认 250 步）、动作墙钟期限、取消和原生终态约束。它不改变整局官方步数上限。

该模式保留用户给出的顺序 waypoint 和原有 `after_motion` 夹爪停留，显式拒绝缓存轨迹 preview 与 `during_motion` 百分比时序。反馈执行追踪规划终点，不保证沿原规划轨迹运动；原规划的碰撞检查不能视为对实际路径的证明。`reference_resampled_steps` 仅是原轨迹行数的诊断估算，实际计步仍以回执 `executed_native_steps` 为准。`robot_wait` 与关节策略前缀的计步语义保持不变。

在 RoboDojo 瓶子任务上的对比中，反馈模式可减少短目标的步数，但没有改善完整任务结果；对比暴露的高瞬时速度下无净进展判停缺口及长绕转终点已修复。该模式保留为可信固定输入诊断，正式统一动作默认并要求 `resampled`。

配置工具是否可用和模型是否实际调用是两件事。禁用规则同时作用于模型目录、actor调用和`program_run`的broker调用。程序中的物理动作经同一`robot_action`执行边界并接受审批，不能通过历史动作名绕过。禁用程序入口或某个工具不能代替原生观察与执行协议验证。

深度默认在原生采集、模型工具、程序和历史访问各层关闭。三个独立显式配置、数值接口及技能参数见 [RGB-D 数值工具](rgbd-numeric-tools.md)。当前公开的`depth_query`仅在RGB-D profile下出现；旧`depth_move`已在 2026-10 重构中删除；禁用 `depth_query` 也会关闭 `geometry_query` 的深度别名。非 RGB-D profile 搭配任一深度功能开关会在配置校验时报错。

`completion_guidance=hook/both` 使用 [`HomePoseCheck`](../../auriga/runtime/actions/home_check.py) 组件。每局在 native reset 和场景稳定之后、首次 actor 动作之前，保存首个公开观测的双臂 measured `ee_pose`：`env_origin` 坐标系中的 link6 位置和 `quaternion_xyzw` 朝向。初始目标保留在观测历史和归位 reference 中，后续观测不覆盖它；显式环境 reset 或恢复时创建的新 episode 会重新记录。新建 benchmark 默认启用 `hook`，不额外注入初始归位提醒，也不会无条件发送归位提醒；普通开发运行不触发 benchmark 续接。历史恢复/回放继续采用存档模式，缺少该字段的旧存档按 `none` 解释，历史冻结实验仍按各自归档源码解释。

当主actor发出 `turn.completed`、环境仍未结束且原有调度保护允许续接时，比较当前完整观测与初始位姿。使用 RoboDojo 默认 `all_robot_back_to_origin` 的几何规则：两臂每个位置坐标轴的绝对差均不超过 **0.15 m**，最短四元数旋转夹角不超过 **20°**；边界相等通过。`q` 与 `-q` 表示同一朝向，关节圈数和夹爪开合不参与比较。这与 arrangement 任务的最终 home 条件一致，但不代表所有任务的完整成功规则；其他任务启用前应核对其归位约定。

| 检查结果 | 实际续接 |
|---|---|
| `away`：完整有效的双臂测量中至少一臂超出容差 | 明确提醒尚未归位，提供初始 observation 的历史查询引用；说明其他任务条件也可能未满足 |
| `home`：双臂均在容差内 | 原文 `任务还没完成，请继续。` |
| `unknown`：缺少初始/当前测量、观测过期、身份/坐标系不符或数据无效 | 同样发送 `任务还没完成，请继续。`，不把证据缺失诊断成没归位 |

组件只读许可的 robot-state artifact，不请求动作、不自动复位、不查询 evaluator 或物体真值。动作准入会使当前证据失效；中途 progress 观测不能替代结束观测，只有完整 terminal capture 或后续 `robot_observe` 才恢复可判断状态。世界暂停期间同一完整观测可以重复检查，history 查询不会改变当前证据。native terminal、模型累计预算、超时、未决动作和程序保护仍优先于 hook。

`completion_guidance.home_reference` 事件记录各 episode 的初始参考；manifest / task-prompt configuration 保存版本、容差、初始参考和三个提示分支。`benchmark.actor_turn_continuation` 保存该次检查结果、逐臂误差、两个观测引用、`continuation_kind` 和实际选中的 prompt。该事件表示发起续接，是否实际投递仍须核对后续 session 消息。状态感知模式的 `continuation_prompt` 为 `null`，应读取 `continuation_prompts` 和逐次事件，不再把静态 hook 文本当作所有回合的提示。

例如使用默认状态感知 hook，无需额外参数：

```bash
mamba run -n Auriga python -m auriga.apps.gateway --benchmark --task arrange_largest_number_random \
  --run-dir runs/arrangement-home-check
```

显式 `--completion-guidance none` 可关闭归位检查，`--completion-guidance both` 可同时添加初始归位提醒。队列 case 省略该字段时继承统一配置（默认 `hook`）；仍可用 `"completion_guidance": "none"` 或 `"both"` 做明确覆盖。

原有 CLI 保留，并补齐反向开关，例如 `--no-default-task-notes`、`--no-vision-compare`、`--no-enable-wait`、`--no-pi05`、`--no-tracking-guard`、`--no-joint-replan`、`--no-motion-review`、`--no-terminal`、`--no-record-video` 和 `--no-export-video`。`--no-visual-review-model` 关闭文件中启用的 Sol。`--disabled-tools robot_wait vision_review` 覆盖禁用列表，单独传 `--disabled-tools` 清空列表；它不会强制启用被功能组或观察条件关闭的工具。

## 当前 actor 工具及控制关系

当前目录由 `ToolRegistry` 的公开 `ToolSpec` 生成，再按配置、观察条件和后端能力过滤；`auriga.runtime.harness.TOOLS` 是兼容定义，不能作为当前 actor 工具名单。下表列出20项内建声明及2项可选视角工具，可选能力未启用时不进入实际目录。每项工具可以通过 `features.disabled_tools` 禁用；配置不会越过角色、观察权限或动作准入。

| 工具 | 基础可用性 / 依赖 | 功能 |
|---|---|---|
| `task_plan` | 默认可用 | 首次动作前提交任务计划，或修订、读取 |
| `robot_observe` | 默认可用 | 当前许可图像与同帧实测机器人状态 |
| `robot_capabilities` | 默认可用 | 当前能力、单位、frame、TCP及预算 |
| `robot_information` | 默认可用 | 启动资料、原生URDF、固定物理资料及许可标定 |
| `robot_contact_overlay` | 标定观察条件 | 在许可RGB上叠加机器人参考几何 |
| `robot_action` | 标定观察条件、`resampled`、后端支持统一动作 | 整批预览、同一缓存plan执行及轨迹检查；支持单双臂与观察点 |
| `robot_action_status` | 默认可用 | 读取已执行动作状态，不重复执行 |
| `robot_wait` | `enable_wait` | 保持双臂和夹爪，显式推进仿真 |
| `robot_policy_step` | `pi05` 且匹配策略服务已启动 | π0.5局部意图与有界关节前缀 |
| `history_list_actions` | 默认可用 | 分页读取不可变动作请求与回执 |
| `history_view_observation` | 默认可用 | 历史图像及机器人状态，可选相机、ROI及放大 |
| `history_read_trace` | 默认可用 | 许可的规划、下发与实测轨迹 |
| `history_annotate_action` | 默认可用 | 追加actor判断，不改测量与状态 |
| `geometry_query` | 标定观察条件；深度请求另需RGB-D | 像素射线、投影及许可数值几何 |
| `robotic_view` | `robotic_view=true`、许可标定及已公开固定安装链的腕部相机 | 选定相机视点到 link6 姿态的只读换算 |
| `stereo_guide` | `stereo_guide=true`、同一正式观察中的两个零畸变针孔相机 | 源像素与目标极线叠图，不自动寻找对应点 |
| `depth_query` | 显式RGB-D profile | 深度数值与候选假设；不直接执行动作 |
| `program_run` | 受控程序服务 | 有界Python，动作默认经主模型审批 |
| `program_status` | 受控程序服务 | 读取程序状态、历史源码及动作结果 |
| `vision_compare` | `vision_compare=true` | 两张许可RGB的像素特征运动 |
| `vision_review` | `visual_review_model`及辅助服务已启用 | 可选图像或operation证据复核，保留不确定性，不授予动作权限 |
| `operation_memory` | 默认可用 | 按任务/共享范围查询或追加有证据归属的操作经验 |

`robot_action` 默认先preview再执行同一缓存plan；`motion_review`控制历史单臂适配，并不单独关闭可用的统一批次接口。旧动作名、队列、模板、skill、`scene_*`、`operation_verify`和`depth_move`已在 2026-10 重构中删除，不进入新actor/program目录。旧Python适配和历史功能开关继续按各自兼容契约解释，详见[统一动作与程序审批](unified-actions.md)和[工具注册与扩展](tool-plugins.md)。

同步夹爪在`robot_action`的`kind="move"`阶段显式提供`gripper_goal_normalized`及可选`gripper_motion={"start_fraction":0.2,"end_fraction":0.8}`；必须满足`0 <= start_fraction < end_fraction <= 1`。没有时间比例时，该阶段到位后才下发夹爪目标；只改夹爪使用`kind="gripper"`与`goal_normalized`。这些都是本次请求参数，不是新增的全局策略。执行保持同一缓存轨迹，命令完成不证明真实接触、夹持或任务成功。

### 可选视角工具

`robotic_view` 与 `stereo_guide` 是独立布尔开关，均默认 `false`。可以只开启其中一个：

```json
{
  "version": 1,
  "features": {
    "robotic_view": true,
    "stereo_guide": false
  }
}
```

Gateway 的 `--robotic-view` / `--no-robotic-view` 和 `--stereo-guide` / `--no-stereo-guide` 分别覆盖文件配置。队列 manifest 的每个 case 可用 `"robotic_view": true`、`"stereo_guide": false` 显式覆盖；省略时继承统一配置，两个字段都不接受字符串或 `null`。恢复和回放使用归档开关，旧归档缺失字段时按关闭解释。

关闭时不装配对应插件，也不向 actor/program 交付该工具及其说明。开启后，`--disabled-tools robotic_view stereo_guide` 仍可禁用这两个名称。工具说明只随各自 ToolSpec 交付，不修改全局任务提示或 Task Notes；两者都不自动移动相机、捕获新观察或推断任务成功。调用参数及后续动作/测量示例见[腕部视角解算](robotic-view.md)与[双目极线引导](stereo-guide.md)。

## 机器路径和图像

| 配置字段 | 旧入口 / 默认 | 说明 |
|---|---|---|
| `backend.root` | `AURIGA_ROBODOJO_ROOT` / `~/code/robodojo` | 独立 RoboDojo 安装 |
| `backend.environment` | `robodojo` | 显式环境构造器；未知环境拒绝启动。 |
| `backend.robot_model_id` | `arx-x5` | 该环境已支持的机械臂绑定；当前 RoboDojo 只接受 ARX X5，不支持任意拼接。 |
| `backend.python` | `AURIGA_ROBODOJO_PYTHON` / `~/miniforge3/envs/RoboDojo/bin/python` | Isaac worker 解释器 |
| `policy.python` | `AURIGA_PI05_PYTHON` / `~/.local/share/auriga-openpi-arx/bin/python` | OpenPI 解释器 |
| `policy.checkpoint` | `AURIGA_PI05_CHECKPOINT` | 匹配 ARX X5 的官方权重；完整默认路径在示例文件 |
| `policy.server_url` | `AURIGA_PI05_SERVER_URL` / `null` | 可选复用的策略服务 |
| `features.grasp_memory_dir` | `AURIGA_GRASP_MEMORY_DIR` | 记忆路径 |
| `model.codex_home` | `CODEX_HOME` | Provider / 认证源目录 |
| `model.price_card` | `AURIGA_PRICE_CARD` | 可选价格表 |

| `images` 字段 | 默认 | 约束 / 行为 |
|---|---|---|
| `transport_format` | `webp` | `webp` 或 `jpeg`；WebP 编码不可用时回退 JPEG |
| `webp_quality` | `85` | 整数 1–100 |
| `webp_method` | `4` | 整数 0–6 |
| `jpeg_quality` | `95` | 模型传输 JPEG，整数 1–100 |
| `storage_jpeg_quality` | `95` | 已发布 RGB 归档 JPEG，整数 1–100 |
| `feedback_policy` | `all` | `all` 返回全部视角；`adaptive` 对普通动作及模板反馈的所有相机相对模型上次收到的原图去重；首次、压缩/换会话后、主动观察、失败与关键交接复核保留完整反馈；`--image-feedback-policy` 可覆盖 |
| `reuse_exact_images` | `true` | 同一模型请求内复用完全相同图片；不合并不同 detail，不改变压缩请求 |
| `trim_max_inline_images` | `60` | `trim_oversized_requests` 开启时，转发请求的内联图片数超过该值也按同一「保首尾、压中段」窗口裁剪，目标约为该值的 2/3（默认 40 张）；供应商单请求上限 64 张。`0` 关闭；非零值至少 24 |
| `trim_oversized_requests` | `true` | 模型代理在 `/responses` 请求体超过 4.5 MB 时保留开局与最近图片、把中段图片换成带 `observation_id` 的占位文本，每个原生压缩周期只触发一次；上游返回 `image_compression_failed` 时继续裁剪后重试。`false` 原样转发 Codex 历史，`image_compression_failed` 不再重试。与 `features.lean_projection` 无关，见[上下文与图像压缩](image-pipeline.md#模型请求的上下文与图像压缩) |

旧 `configs/images.json`、`AURIGA_IMAGE_CONFIG` 和 `AURIGA_IMAGE_SETTINGS` 作为兼容输入。新文件中的 `images` 优先。分辨率、相机顺序、采样与动作时序不是压缩配置；像素坐标契约保持不变。

## 队列、实验库与独立流程

`scripts/benchmark_queue.py --config ...` 使用同一个文件配置队列和每局运行。队列 manifest 仍定义要跑哪些 case，case 的既有字段仍能显式覆盖实验条件；省略的开关继承公共配置。队列启动时保存 `effective_config.json` 用于记录与重试，避免运行过程中配置文件变化改变后续条件。生成 case 组的 `scripts/benchmark_group.py --config ...` 也会把解析后的配置嵌入队列 manifest。

case 可用非空字符串 `experiment_condition` 标记实验条件；该字段保留在队列清单和状态中，仅供实验分组使用，不进入 gateway 参数、运行配置或模型上下文。

队列预检会静态读取每个 frozen runtime 的 `auriga/core/config.py`，不执行目标代码。已审查的兼容差异是旧 runtime 缺少后来增加的字段（`features.sandbox_enabled`、`robotic_view`、`stereo_guide`、`lean_compact`、运动阈值与外部感知字段，以及 `analysis` 节）：取值等于当前配置类默认值时（例如 `sandbox_enabled=false`），启动快照省略该字段，并在 `config.json` 的 `manifest.config_compatibility` 记录投影来源；取非默认值或出现未审查的字段差异会在 GPU admission 前拒绝队列。manifest 保留完整的统一配置快照，OOM recovery 会校验同一兼容投影。

| 配置字段 | 默认 | 用途 |
|---|---|---|
| `queue.max_parallel` | `60` | 新队列默认最多同时运行 60 个 case；显式 `null` 也使用该默认值，manifest 的显式上界优先 |
| `queue.memory_per_case_mib` | `11264` | 单局 GPU 显存预留，11 GiB |
| `queue.gpu_headroom_mib` | `0` | GPU 额外余量 |
| `queue.wait_timeout_s` | `1800` | GPU 可用资源等待超时 |
| `queue.oom_retry.max_retries` | `2` | 已确认原生 OOM 的新 episode 重试，`0` 关闭 |
| `queue.oom_retry.initial_delay_s` | `60` | 首次重试延迟 |
| `queue.oom_retry.backoff_multiplier` | `2` | 指数退避倍率 |
| `queue.oom_retry.max_delay_s` | `300` | 重试延迟上界 |
| `queue.wake_enabled` | `true` | 队列结束通知入口是否启用 |
| `queue.wake_thread` / `wake_codex` / `wake_remote` | `null` / `codex` / `null` | 通知目标会话 UUID、发送通知的 Codex 可执行文件和 app-server 端点；目标缺省时读取 `~/.config/auriga/completion-thread`（若存在），端点缺省为用户共享 socket，见[完成通知](experiment-completion-hook.md) |
| `library.root` | `~/exp/auriga` | 常驻库的扫描根目录 |
| `library.port` | `8780` | 实验库 loopback 端口 |
| `library.scan_interval` | `1` | 扫描间隔秒数 |
| `library.public_origin` | `null` | 受认证反向代理的准确 HTTPS origin |
| `library.task_source_root` | `null` | 可选任务审阅来源目录 |
| `scene.root` | `runs/local_scene` | 独立场景服务的目录；gateway 内嵌服务始终使用本局 `twin-service` |
| `scene.port` | `8770` | 独立场景服务 loopback 端口 |
| `scene.max_workers` | `2` | 有界 CPU job 同时执行数 |
| `scene.max_queue` | `4` | 场景 job 队列上界 |
| `scene.max_blob_store_bytes` | `536870912` | blob 总存储预算，默认 512 MiB |

`AURIGA_EXPERIMENT_CONFIG` 指向的旧实验设置文件仍作为兼容入口，已有 queue manifest / case 字段继续支持。新队列要求每个 `case.cwd` 指向支持统一配置的 checkout；不支持 `--config` 的历史冻结源码会在 GPU 准入和输出目录创建前报 `unified_config_requires_supported_gateway`。历史队列的 OOM 恢复沿用原始命令，不要求升级旧 checkout。正式 benchmark 的预算不由队列覆盖；OOM 只在原生证据及清理确认满足现有条件时重试，每次使用新目录，并保留失败尝试。

运行中的新队列可热修改并发上界：

```bash
mamba run -n Auriga python scripts/benchmark_queue.py --output /path/to/running-queue --set-max-parallel 15
```

该命令仅原子更新本队列的 `control.json`（`{"max_parallel": 15}`），不读取当前配置、不启动任务；只接受正整数，并拒绝已终止或不支持该控制入口的历史队列。队列每轮检查及每次准入前读取控制文件：增加上界会在下一次检查立即重新尝试准入，不等待 GPU 采样周期（默认 90 秒）；降低上界不停止已有任务，只暂停补位，直到在跑数低于新上界。显存和共享 GPU 租约准入规则仍适用。非法或不可读控制保留旧值，并对连续相同错误只记录一次 `queue_control_invalid`。

命令输出 `requested` 表示已写入请求，实际应用以 `state.json.scheduling.max_parallel` 与 `events.jsonl` 的 `max_parallel_changed`（旧值、新值、当时在跑数）为准。初始 manifest、`config.json` 与 `effective_config.json` 保留启动配置，运行过程中的调度变更由 state / events 记录。旧的在跑调度进程需要下次启动时使用新版入口才能支持热修改，仅修改磁盘源码不会使它获得该功能。

`scripts/motion_review_eval.py prepare --config ...` 继承统一配置的模型、推理强度、provider home、机器路径和图像设置。配对协议固定运行控制、功能条件与调度，将有效配置冻结到 protocol 和各队列 manifest；汇总只核对保存的配置，不读取当前默认值。已生成的旧 Astra 协议继续按其原模型与档位解释。

`scripts/reference_episode.py` 的 API 客户端和机器人后端共享一次解析得到的统一配置；`--config`、`--model`、`--effort`、`--codex-home` 可显式覆盖，`--print-config` 只显示有效配置，不读取认证或启动模型与机器人。新运行默认从本地 `~/.codex` 选择 provider，使用 `gpt-6.1-sol` / `high`，请求将模型名、`reasoning.effort` 与 `service_tier="default"` 分别发送。此直接 API 客户端认证仅从选定 provider 声明的 `env_key`、其 bearer 设置或选定 home 的 `auth.json` API key 获取，不支持 Codex 的 ChatGPT 登录；未声明 `env_key` 时不会借用环境中的 `OPENAI_API_KEY`。缺少选定的自定义 provider、端点或凭据会在模型请求前报错。

下面这些是独立操作或诊断命令，不是模型运行的全局布尔开关，仍使用对应 CLI / HTTP 请求：

| 流程 | 入口及控制 |
|---|---|
| 独立场景客户端重放 | `scene_client` 的 URL、delay、duplicate、reverse 是重放测试参数；服务本身支持统一配置 `scene` 节及 `--config` / `--print-config` |
| 固定 API 参考策略 | `scripts/reference_episode.py` 的 `--config` / 模型覆盖参数，以及 `--max-calls`（1–170）、`--action-timeout-s`（0.1–300）、任务 / seed / GPU；采用独立参考协议 |
| 原生策略诊断 | `native_policy_validate.py`、`pi05_atomic_handoff.py` 等的 prefix、prompt、noise seed、hold-limit、episode、ee-smoke；是特定实验输入 |
| 运动审查预检（脚本已于 2026-10 删除） | `motion_review_preflight.py --output ... --wait-timeout-s 600` 的等待是本次诊断参数；不继承正式队列 1800 秒等待默认值 |
| 几何验收 | `geometry_validate.py` 的 synthetic / raw-recording / ROI / heldout 等；决定本次验证的数据和操作 |
| 图片压缩归档 | `compact_observation_images.py --apply` 明确执行已校验原图移除；不作为每次运行的隐式清理 |
| 常驻服务安装 | `install_library_service.py --install` 执行 systemd 安装；默认只输出 unit |
| observer 操作 | `/v1/observer/record`、评分、隐藏场景快照、video export 等由操作者显式调用，不注册为 actor 工具 |
| 实验导入 / 报告 | `auriga.apps.library.experiments`、`benchmark_report.py` 等的输入输出路径与筛选是一次性操作参数 |

## 恢复、重放和可复现性

运行记录保存完整解析后的配置，同时保留已有 manifest 字段，便于旧分析脚本读取。配置保存的是有效运行条件，而不是仅保存用户写入文件的片段。

恢复或重放已有运行时，实验语义从归档恢复，不采用当前文件新默认值；查看服务的端口等本地启动参数仍可改变。旧 manifest 缺少 `enable_wait` 时保持关闭，缺少 `vision_compare` 时保持开启，缺少 payload profile 时使用 `full_v1`；缺少默认 Task Notes 配置时保持关闭。已有统一配置快照缺少后来增加的 `default_task_notes` 或 `vision_compare` 时，也按对应历史默认恢复。Task Notes 始终沿用保存的实际正文、来源和版本，不用当前默认文本重新生成或再次追加。旧图像设置缺少 `reuse_exact_images` 时保持关闭，缺少 `trim_max_inline_images` 时按 `0`（只按字节裁剪）恢复；已有模型请求账本未记录 `transport_recovery` 时恢复后不做自动恢复，记录过的按原策略。`resume` 会创建新的环境 episode / generation，不能恢复原来的物理状态；同一 thread 的预算和记账不重置。

这也意味着，“把默认设置改了”只影响新运行。做配对消融应创建两个新目录，保存各自解析后的配置，并检查 `manifest.json` 的实际工具 / 观察条件，而不是推断历史运行自动迁移到了新条件。

## 统一记录但不开放的协议与边界

这些常量 / 能力约束已一起梳理，但不转换为任意配置开关，以免仅改变 JSON 就无意改变 benchmark 协议或信息权限：

| 范围 | 当前固定规则 |
|---|---|
| 正式模型预算 | 180 次交付的有效 actor / helper / compaction 返回；单次请求无硬期限；明确可重试 API 错误由 harness 自动退避重试，默认最多 12 次，SDK 重试为 0；之后按 `model.transport_recovery` 有界冷却重发，见[模型主导操作](model-led-operation.md#模型请求)；失败/不完整尝试单独记账 |
| 正式监督 | 环境初始化期限 900 秒；运行中连续 1800 秒没有模型、代码、机器人或仿真实际活动才停止；正式 benchmark 无整局墙钟预算 |
| 决策边界 | 不设两次动作之间的只读查询次数门限；模型 turn 结束不等于原生 episode 结束 |
| 原生动作 | 每任务原生步数上限、成功条件、控制频率来自 RoboDojo；保持唯一动作写入者及幂等回执 |
| 当前动作 schema | `robot_action` 一批 1–32 个 stage，每臂至多 16 个；双臂按同一时间线执行，同臂顺序、跨臂依赖和 observe 检查点由批次契约约束。wait / policy prefix 最多 50 原生步；动作 timeout 0.1–300 秒 |
| Legacy 原子动作 | 旧单臂 `EEAction` 最多 8 个顺序 EE 路点，仅供可信兼容入口；该限制不描述当前 `robot_action` 的批次或双臂能力 |
| 运动审查接触诊断 | 最终连续接近区间为距终点 60 mm / 20° 内；放置接触最大 3 mm 几何穿透容差，默认 2 mm；仅解释数值风险，不禁止动作，原始交叠保留 |
| 跟踪保护 | 关节误差大于 0.5 rad 连续 5 个原生步触发；不自动修正 |
| 关节重规划 | 至少剩余 15 秒才尝试；只有合法且更短的完整轨迹可替换 |
| 程序资源与预算（两种沙箱模式均保留） | 1 GiB 地址空间、8 MiB 文件、128 KiB 源码；每程序工作墙钟预算默认 180 / 最大 300 秒，排除本程序辅助模型与主模型审批等待；不限制工具调用次数；数值库单线程、动作 admission 留 5 秒余量 |
| 场景单次请求 | 单 blob 最大 32 MiB；单 job 的 queue / wall budget 默认各 30 秒，由请求显式提供并按协议验证。进程数 / 队列 / 总 blob 空间用 `scene` 配置 |
| 原生 Codex 能力 | 原生动态工具直接返回图文，部分工具经本地工具搜索按需加载；shell / 文件 / 记忆可用，联网搜索和外部扩展关闭。namespace 与 `program_run` OS 隔离由 `features.sandbox_enabled` 控制；机器人工具的原生线程准入只允许主 actor |
| 自主执行 | 启动提示要求主模型自主决定计划、参数与程序动作审核，不向人提问；原生用户输入立即返回空 answers，当前会话所属的命令／文件审批自动 acceptForSession，额外权限按请求完整授予并使用 session scope，不等待人工。Harness 的观察投影与动作准入继续生效；Landlock 仅在 `features.sandbox_enabled=true` 时启用 |
| 观察权限 | 默认禁止原生 depth，显式 RGB-D profile 才许可数值深度接口；所有 profile 均禁止隐藏物体姿态、接触真值和 evaluator |
| 视频时序 | 三相机 5 Hz 仿真采样及观测边界帧，保留非均匀真实 frame mapping；不是原生满帧率 |
| 停止与评分 | 未确认停止、unknown 或基础设施失败按已有未评分规则保留；不可配置为伪造成功或隐藏失败 |

direct 工具模式通过每局私有 `model_catalog_json` 固定：沿用 SDK/CLI 0.160.0 的原生模型目录，仅将 `tool_mode` 设为 `direct`。原生模型元数据优先于 feature 开关，单独关闭 code mode 无法保证生效。原模型名称、推理档位、上下文限制和其它能力不变；目录来源、校验值和升级要求见 [Codex 快照说明](../../third_party/codex/README.md)。

若未来需要实验性调整这些协议，应显式新增版本化字段、校验、记录及相应测试，并区分正式 benchmark 与开发条件。当前统一配置不改变它们。

benchmark 和 `--exit-after-turn` 的运行监督从最近实际活动计时（普通交互工作台不因闲置自动退出）：模型请求发起和真实上游传输、代码执行事件与输出、机器人及仿真进展均可重置 30 分钟无活动期限。pending 请求、合成 keepalive、运行状态心跳或仅进程存活不会重置。任何来源仍有实际活动就不因整局耗时停止；全部静默达到期限后取消请求并进入停止与资源收尾流程，记录 `runtime_inactivity_timeout`，作为未评分的基础设施中断。明确可恢复 API 错误由 harness 自动重试；重试耗尽或上游传输中断后，在没有执行中/未确认动作和活动程序时按 `model.transport_recovery` 冷却重发同一请求；不可重试错误、恢复被拒绝或恢复用尽才终止。详见 [模型主导的操作与传输策略](model-led-operation.md)。

启动的 900 秒期限触发关闭动作准入与资源收尾，不保证整个 gateway 在 900 或 905 秒内退出。[启动监督](../../auriga/runtime/supervision.py)继续持有原启动任务，由 [Workbench](../../auriga/apps/gateway/workbench.py)以 50 ms 的轮询间隔检查已发布的 robot、SDK bridge、app-server 和 terminal 子进程句柄，包括停止请求后才创建的进程；先发 SIGTERM，5 秒后仍存活则发 SIGKILL，并保存信号及实际退出证据。

[worker 传输](../../auriga/robot/backends/robodojo/robot_process.py)的同一请求期限覆盖写锁、管道写入、取消发送和回包等待；部分写入或缺少可靠回执仍保留 unknown，不自动重发动作。pipe 已失败后的 robot close 各段计划等待合计最多 27 秒；普通 close 最长分支为 237 秒（含 close RPC、进程退出与 reader 等待）。这些时间不包含操作系统调度或不可中断 I/O 的额外延迟；无法安全终止的 Python/native 线程仍由原 owner 持有，不记录为资源已释放。整进程硬退出期限需要外部进程监督器。相关 CPU 子进程回归见 [管道期限](../../tests/test_robot_process_deadline.py)与[启动及关闭](../../tests/test_runtime_shutdown.py)。


`features.arm_queue` 与 `features.motion_cost_preview` 默认开启，分别用 `--[no-]arm-queue` / `--[no-]motion-cost-preview` 覆盖。两者限 resampled，成本预览还要求 calibrated profile。关闭队列同时禁止 raw ArmQueueAction；关闭成本预览同时禁止程序调用。基准 manifest 可用 `eligible_gpu_indices` 限定非负唯一GPU索引列表；不设置时保留全部GPU自动准入，设置后被排除GPU绝不会作为候选，仍遵守共享GPU租约与显存检查。


## 有限恢复、执行检查与操作证据

四个独立配置默认 true；CLI 使用 `--[no-]bounded-continuation`、`--[no-]motion-safety-guard`、`--[no-]trajectory-preview`、`--[no-]manipulation-protocol`，队列 case 使用同名下划线字段。恢复旧 manifest 时，缺失字段按 false 保留历史行为。

- `bounded_continuation`：首次完成 final 建立基线；之后没有动作进展时，连续 3 次无新工具证据的 final 或至多 6 次恢复 final 结束本局，原生终止优先。新的工具探索只给予有限恢复机会；不读取评测分数来判定。
- `motion_safety_guard`：执行前检查实际编译关节路径的异常绕行/回摆和剩余原生预算；拒绝时反馈证据，不修改关节命令、不自动重规划。保留已经执行的步数。
- `trajectory_preview`：成本预览可附候选关节、公开硬件 FK 和同步两臂模型间距；`robot_action(mode="inspect")`可按原生时间点读取，或可选投影到当前 RGB。只使用公开机器人模型、关节反馈、标定与候选指令，不使用任务物体真值。详见 [轨迹证据](motion-safety-and-trajectory-evidence.md)。
- `manipulation_protocol`：task notes、task plan 与有界模板采用显式接触位置、放置几何和观察证据。未知几何可写 unknown；释放完成只标为 released_unverified。交接许可与偏好分别由 `airborne_handoff`、`handoff_preference` 控制，双臂并行能力保留。
