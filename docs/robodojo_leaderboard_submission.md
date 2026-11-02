# RoboDojo leaderboard 提交版：改动与验证

日期：2026-11-02。本文给复核人用：提交版相对公开版和 v5 私有版改了什么、做了哪些测试、哪些没有覆盖，以及怎么复核。

向 XPolicyLab 提交的 PR 正文草稿见 [robodojo_leaderboard_pr_draft.md](robodojo_leaderboard_pr_draft.md)（尚未提交）。

## 1. 三个版本

| 名称 | 位置 | 说明 |
|---|---|---|
| 公开版 | StellarEdge-AI/Auriga `main` @ `3e1be75` | 开源发布版。代码树等于内部仓库 Daedalus `9b3ad08`，差别只有目录和命名 |
| v5 私有版 | 内部仓库 Daedalus `94f5e45`（= tag `v0.0.3`）+ fill_pen_holder 任务文本 override，模型请求上限 170 | v5 三 seed 结果（6300 局）主体的模型可见版本 |
| 提交版 | Auriga：本分支 `robodojo-leaderboard` @ `ced0335`（= `3e1be75` + `1d219c7` + `5563f83` + `7fabe90` + `ced0335`，另有文档 commit）<br>XPolicyLab：StellarEdge-AI/XPolicyLab 分支 `policy/auriga` @ `9e1a580`（单个 commit；PR XPolicyLab/XPolicyLab#223），目录 `policy/Auriga/` | `policy/Auriga/deploy.yml` 的 `auriga_commit` 钉在 `ced0335` |

v5 主体以外的偏离格：G4 有 1000 局的记忆模型配成了 gpt-6-sol，G2 seed 0 有 380 局多带一个未合入的 PR，G2 有 4 格开了 self-briefing。这些格子任何单一冻结版本都复现不了，提交版也不去对齐它们。提交版对齐的是 v5 主体。

## 2. 相对公开版的改动（Auriga 仓库，3 个代码 commit）

### 2.1 `1d219c7`：冻结 leaderboard 配置

目的是让模型可见内容和评测配置回到 v5 主体的状态。

| 项 | 公开版 | 提交版 | 位置 |
|---|---|---|---|
| self-briefing | 默认开 | 关 | `configs/auriga.json`、`auriga/core/config.py` |
| 每局模型请求上限 | 180 | 170 | `auriga/cortex/session/codex.py`（3 处）、`auriga/apps/gateway/workbench.py`、`auriga/cortex/session/model_proxy.py` |
| 默认模型 | gpt-6.1-sol | gpt-6-astra（effort high） | `configs/auriga.json` |
| fill_pen_holder 任务文本 | 无 override | 加入 v5 该任务所用的 override entry，原样照搬 | `auriga/cortex/context/task_instruction_overrides.json`（catalog 版本号 `..._v3_fill_pen_holder_20261031`） |

- 其余任务的任务级文本不改。v5 每个任务实际用的规则文本已经等于公开版的规则文件，包括 tic-tac-toe 旧 prompt、make_kong / press_by_number 新 prompt；唯一缺的是 fill_pen_holder。
- 对应测试已同步更新：`test_config`、`test_self_briefing`、`test_codex_evidence_agent`、`test_model_proxy`、`test_task_instruction_overrides`（新增 2 个测试）。

### 2.2 `5563f83`：允许 RoboDojoAdapter 跑在宿主提供的环境上

- `auriga/robot/backends/robodojo/robot_worker.py`：`RoboDojoAdapter.__init__(arguments, environment=None)`。
  - 传入 `environment` 时，用宿主给的 env，跳过 Isaac 启动、原生 env 创建、evaluator 初始化。
  - `environment=None` 时逐行等于原代码，有回归测试锁住调用顺序。
- `auriga/apps/reference/reference_api.py`：`upstream()` 在进程里已有真正的 `XPolicyLab` 包时，临时让出 `XPolicyLab.*` 命名空间去加载 vendored 模块，加载完再恢复。
  - 原来遇到这种情况直接报错。模型中继的重试策略用到这些模块，不改的话在 XPolicyLab policy server 里会话一启动就失败。
  - 进程里没有 XPolicyLab 时，行为不变。
- `tests/test_injected_environment.py`：新增 7 个测试。

这两处都不改模型可见内容，prompt、developer instructions、工具定义、任务文本都不受影响。

### 2.3 `7fabe90` + `ced0335`：感知服务点选 mask 扣除机器人像素

- `services/vggt_height/vggt_height_service.py`：`/object_table` 的点选路径在客户端同时发送 `robot_mask_b64` 和 `robot_segments_m` 时，原来不使用像素 mask（只在没有 segments 时用），而 `screen()` 的 3D+2D segment 检查也不在这条路径上，所以点在夹爪上或旁边时，会把手臂当作被点的物体返回。现在只要有像素 mask，就从点选 mask 里扣掉机器人像素，剩余不足 `min_area`（40 px）时回 `no_object_mask`。这是 v5 hp 版的做法。文本 mask 和自动 mask 路径不变，health 版本字符串不变。
- 问题由 Auriga `b1adc51` 引入，v5 G4 的 1500 局带着这个问题跑。
- `ced0335`：扣除后如果点击本身落在机器人 mask 上、且点选 mask 只剩不到 10%（`robot_click_cut` = 0.90），回 `no_object_mask`。针对 pointfix2 发现的 4 个夹爪周边残片；按该次数据离线推算，`no_object_mask` 为 27（v5 31，配对 7 对 3，p = 0.34）。提交后用服务全量重跑验证过（见 §5）。

## 3. 相对 v5 私有版的差异

v5 私有版 `94f5e45` 和公开版代码树 `9b3ad08` 逐函数比较，结果是 59 个函数改了、222 个删了、92 个新增。逐项审查后，在 v5 配置下会影响运行的只有下表几项：

| # | 差异 | 影响 | 处理 |
|---|---|---|---|
| D1 | 模型中继：模型响应已完整送达、之后客户端才断开时，不再把这局判为传输失败（Daedalus `1c27e92`，2026-10-31 的修复） | 只在"完整送达后断开"这一种情形下改变一局的结局。v5 G3 的 570 个 run 里只有 1 次断开，而且记录为未完整送达，不属于这种情形 | 保留（bug 修复） |
| D2 | 请求上限 180、self-briefing 默认开、默认模型、缺 fill_pen_holder override | 会影响 | `1d219c7` 已改回 |
| D3 | 改名：工具 namespace `daedalus`→`auriga`，`DaedalusClient`→`AurigaClient`，`/harness` 源码快照的目录结构 | 只是名字变化，语义不变 | 维持 |
| D4 | `grasp_memory_dir` 默认是持久共享目录 | 跨局共享会引入跨局记忆 | adapter 每局新建一个空目录 |

函数级差异的分类如下：

- **59 个改过的函数**：除 D1、D2 外，都是改名、搬位置，或者只在 legacy 工具、队列、web、reference agent 里生效。
- **222 个删掉的函数**：
  - 64 个是搬到别处，内容不变；
  - 147 个只服务 legacy 工具，这些工具在 v5 里也不对模型和 program 开放；
  - 11 个在 v5 里就是死代码；
  - v5 运行时会执行到、现在却没有了的：0 个。
- **92 个新增函数**：v5 局里会执行到的只有一项禁用工具检查。v5 的禁用列表是空的，结果不变。

另外补查了 AST 比较覆盖不到的三类内容：class 字段、import 来源、非 `.py` 数据文件。没有新发现。

## 4. XPolicyLab adapter（`policy/Auriga/`，公开版和 v5 都没有）

v5 是 Auriga 在 Isaac 进程旁边起一个 worker 直接驱动仿真。提交版把整个 harness 搬进 XPolicyLab 的 policy server，通过 websocket 和 RoboDojo 的 env client 通信。

| 组件 | 做法 | 与 v5 的关系 |
|---|---|---|
| `deploy.py` | 以 demo_policy 为基础，改了三处：<br>① 模型思考时回 WAIT，客户端不推进仿真，物理保持暂停；<br>② 回空动作表示结束，交给 RoboDojo 自己做 final check；<br>③ `additional_info["auriga"]` 透传实测夹爪开度、关节速度、native step 和上限 | 不读、也不发送任何物体、layout、seed 或 evaluator 状态 |
| 运行时 `auriga_xpl/runtime.py` | 等价于 Auriga 的 `serve --benchmark`：官方任务文本、override、completion hook 全部注入。每局串行执行，每局开始都新建空 grasp memory、复位 planner | 配置三层叠加：Auriga 默认值、所钉 commit 的 `configs/auriga.json`、`auriga_eval.json`（只放部署项：sandbox、视频、PTY、scene_truth 关闭） |
| 规划器 `vendor/robodojo_planner/` | vendored RoboDojo 自己的 `CuroboPlanner` 和 ARX X5 机器人描述，cuRobo 用 RoboDojo 钉的 `d17b54c`。加了一个 NumPy 2 兼容子类，按 NumPy 1 的类型提升规则重算目标四元数 | 与仿真内 planner 逐点相同（见 §5） |
| 标定 `assets/arx_x5_calibration.json` + `auriga_xpl/calibration.py` | 头相机用冻结常量；腕相机用常量加 URDF 正运动学，输入是实测关节角 | 与 worker 输出的 `calibration_id` 相同 |
| 感知 `auriga_xpl/perception.py` | policy server 自己启动、做 health 检查、停止 VGGT-Omega 和 SAM 3.1 服务。服务代码是所钉 Auriga commit 里的 `services/vggt_height`（点选和文本 mask 都走 SAM 3.1），启动参数与它的 `start.sh` 相同；adapter 里不放服务代码 | 在 v5 G4（1500 局）所用版本上加了点选扣除机器人像素的修复（§2.3）；v5 G1–G3 用的是 hp 版（点选走 SAM 2.1），对比见 §5。hp 版提交分支（`b59af89`）不在公开仓库，内部存档为 git bundle |
| 模型 API | 每个 server 写一份私有 Codex provider 配置，走 Responses API，key 只从环境变量或 0600 文件读，不写进任何文件 | endpoint 和 key 由我们提供，仓库里留空 |
| 安装 | `install.sh` 依次完成：clone 钉好的 Auriga，安装 XPolicyLab（editable），装 planner，从 HF 固定 revision 下载机器人 mesh，安装感知环境，下载 8.1 GB 权重（VGGT-Omega + SAM 3.1）；所有下载都做 SHA-256 校验 | — |

## 5. 测试

| 测试 | 方法 | 结果 |
|---|---|---|
| Auriga 单测 | 在 `5563f83` 和 `1d219c7` 上跑全量 unittest，逐模块对比 | 除新增模块（7 个测试全过）外，结果与基线相同。基线 202 个模块 2061 个测试，其中有环境性失败（缺 bubblewrap 等），两边一样 |
| 标定 | 10 个任务 11 次仿真 dump，覆盖 6 个 `*_random`、conveyor、带 support arm、另一个 layout 和另一个 seed；另用 6044 个历史观测 | 三台相机的标定跨任务逐位相同；腕相机 FK 误差最大平移 0.00063 mm、旋转 9.6e-5°；复算 446 个历史 geometry_query，最大差 1.5e-11 mm |
| 规划器 | 814 个真实请求（39 个任务，plan_path、plan_joint、带约束、不可达目标），vendored 对仿真内 planner | 成功与否 814/814 一致，成功轨迹逐点相同，最大关节差 0。每局复位 sampler 后，与新建 planner 614/614 一致 |
| 感知复现 | 新装 hp 版重放 v5 记录里的请求；10 帧真实观测 × 5 种请求，与生产服务对比 | 与 v5 记录、生产服务逐位相同 |
| 执行层等价性 | 不调用模型，把 12 个任务（覆盖 5 个维度）的 v5 真实工具调用按原顺序分别送进 v5 私有版和提交版 | 闭源重放 12/12 复现原局的评分和动作数（说明重放方法可靠）。提交版对 v5 私有版：12/12 评分和动作数相同，机器人测量状态逐步相同（最大差 0），native step 逐步相同。初始 prompt 和 20 个工具定义逐字相同。工具返回只在感知字段有出入，v5 私有版自己跑两遍也一样有 |
| 感知两版离线精度 | 230 帧初始布局图，按仿真真值对比 hp 版和 Auriga 仓库版 | 两版总体持平。hp 版错检更少、点选长尾误差更小；仓库版按物体名查找的召回更高、mask 略贴合 |
| 操作中途点选对比 | v5 G3 中途帧上模型实际点击的约 1200 个像素，按 FK 投影的机械臂轮廓判定返回 mask 是否落在机械臂上 | 中途结果（580 个配对点击）：返回机械臂 mask 的比例 hp 版 0%、仓库版 2.8%（16 个，全部单向，p≈3e-5）。只出现在离机械臂 10 px 以内的点击（这类点击占 10%）且都在头相机；点在机械臂上时 hp 版多回 no_object，仓库版多返回夹爪 mask，报出的高度与夹爪顶差约 3 cm。其余情况两版持平。据此选用 3.1-only |
| 点选修复验证 | 同一批 800 个中途点击、530 个请求，用 `7fabe90` 的服务全量重跑，与未修 repo 版、v5 hp 版逐点击对比；`ced0335` 再全量重跑一次 | 返回机械臂 mask：21 → 2（v5 为 0；剩下 2 个点击落在已由文本或自动 mask 找到的物体上，走复用路径，不经过点选检查）。`no_object_mask`：5 → 23（v5 为 31，对 v5 配对 11 对 3，p = 0.057）。另有 108 个点击保留物体但扣掉了部分机器人像素。selfcheck 通过。先试的整块拒绝方案把 `no_object_mask` 推到 40，弃用 `ced0335` 全量重跑：相对 `7fabe90` 只有那 4 个残片变为 `no_object_mask`，其余 795 个点击逐字段相同；`no_object_mask` 27（对 v5 配对 7 对 3，p = 0.34），返回机械臂仍为 2。selfcheck 对 `reference_v5.json` 29/29 |
| XPolicyLab debug 闭环 | `EVAL_ENV_TYPE=debug`，plain 和 `DEBUG_OBS_ENCODED=1` 各一次 | 都到 `[MAIN] eval finished`，0 traceback |
| 真实闭环 smoke | 标准 `eval.sh`（`EVAL_ENV_TYPE=sim`）+ 真实模型，跑 4 局 | 4/4 跑通。同一 server 连续多局后无进程、显存、fd 泄漏 |
| 端到端验证 | 未改动的上游 RoboDojo main（`f57e163`）+ XPolicyLab `0075cc9`，从全新 `install.sh` 开始，5 个任务 × 2 layout × 2 种感知，共 20 局 | 20/20 局经标准 `eval.sh` 正常结束，成败以 RoboDojo `_result.json` 为准，适配层没有报错；两种感知整局表现无可分辨差异。全新 `install.sh` 一次通过（23.7 min，含本次修复的 conda 服务条款步骤）。这批只验证功能，不作为成绩 |
| 静态检查 | `check_adapter.sh`：CONTRIBUTING 规定的检查，加 key 和私有引用扫描、CRLF、文件大小、README 模板 | 0 FAIL。剩一条人工判定的提示：Isaac 版本号 `5.1.0.0` 被识别成 IP 形字符串 |

## 6. 没有覆盖到的

- 模型传输层没有重放。D1 只做了代码审查和数据计数。
- 等价性重放不调用模型，所以续轮提示这类由模型侧决定的逻辑没有触发。
- 感知精度只测了初始布局帧，没有覆盖抓取后、物体被移动、手里拿着物体的情况。
- 显存数据只在 RTX 4090 上测过（数字见 adapter README）。
- 3.1-only 分支没有再跑真实评测局：评测主流程未改，端到端验证里 repo 版 10 局走的就是同一套感知代码；改动后只做了安装脚本、服务启动、health、真实请求、显存、debug 闭环与静态检查。

## 7. 复核方法

1. **Auriga 改动**：`git diff 3e1be75..ced0335 -- . ':!docs'`，应只涉及 §2 列出的文件。
2. **adapter**：看 StellarEdge-AI/XPolicyLab 分支 `policy/auriga` 相对 upstream `0075cc9` 的 diff，应只新增 `policy/Auriga/`。
3. **静态检查**：在 XPolicyLab checkout 里运行
   `bash check_adapter.sh <XPolicyLab checkout> Auriga upstream/main`。
4. **不需要模型的闭环**：
   `EVAL_ENV_TYPE=debug bash eval.sh RoboDojo stack_bowls no-checkpoint arx_x5 joint 0 0 0 Auriga base`
5. **真实闭环**：在 `config.env` 里填好 endpoint 和 key，然后运行
   `bash eval.sh RoboDojo stack_bowls no-checkpoint arx_x5 joint 0 0 1 Auriga robodojo`
   运行记录在 `policy/Auriga/results/runs/<task>/`。
