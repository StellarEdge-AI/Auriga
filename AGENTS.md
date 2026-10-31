# Auriga 开发约定

本文件面向参与仓库开发的 agent 和开发者。先理解变更的实际调用链，再完成实现、相关回归和文档；保持用户已有改动。用户明确授权的工作持续有效，常规可逆修复无需重复询问。

如果仓库根目录下存在 `lab/AGENTS.md`（单独克隆的私有配套仓库），一并阅读并遵守。

## 开始前阅读

1. [README](README.md)：产品目标、当前公开工具和运行方式。
2. [实现架构](docs/development/architecture.md)：模块职责、状态所有权与调用路径。
3. [工具和插件](docs/development/tool-plugins.md)、[扩展开发](docs/development/extensions.md)：新增工具、技能与后端的方法。
4. 按变更范围阅读[共享协议](docs/design/Astra具身Harness-共享接口与事件协议-v0.1.md)、[统一动作](docs/development/unified-actions.md)、[信息边界](docs/development/actor-information-boundary.md)和[验证指南](docs/development/validation.md)。历史设计描述扩展目标；当前实现以注册表、契约及测试为准。
5. 发起、续跑或整理实验前，阅读[实验库](docs/development/experiment-library.md)、[完成回调约定](docs/development/experiment-completion-hook.md)和下文“实验与评测”，先确认已有方案、baseline 和执行批次。

查询 Codex 能力时优先使用 OpenAI 官方文档；官方入口无法访问或检索不到所需内容时，可查阅 [Codex Docs 备用文档站](https://www.codex-docs.com/docs)。涉及 API、hook、任务规划和版本支持的结论，须与本项目固定版本的 SDK/CLI、生成 schema 或实际调用核对，并区分文档描述与本地已验证能力。

**项目默认解释器是 Miniforge 的 `Auriga` 环境。** 开发、测试、网关、队列和离线脚本使用 `mamba run -n Auriga python`（或明确激活该环境后使用 `python`）。不要创建或重建项目 `.venv`，也不要用系统 Python 或 PATH 中的 Codex 代替。SDK/CLI 的实际版本与路径须由该环境核对并随运行记录。wheel 检查的临时安装目录仅用于验证 checkout 外安装，不是项目运行环境；RoboDojo/OpenPI 继续使用各自配置的独立解释器。

## 不可破坏的边界

**上下文只承载必要信息。** 不把非必要内容，尤其是构建、来源和存储元信息，交给 Codex 占用上下文。设计每个模型可见字段时，说明它如何帮助当前判断或下一次工具调用；没有用途就从 producer 和展示副本删掉。哈希值不得进入模型可读上下文，不以冗长占位说明替代。对象定位确需的 ID 使用可逆短引用；控制所需的计划 revision、执行状态、错误诊断、测量精度与证据不确定性保留。

保留原生 Codex 自带的说明、工具目录与通用编程能力，不为了缩短上下文而覆盖其默认基底或裁剪原生内容。鼓励模型在现有沙箱与观察权限内自行写代码、构建辅助工具解决问题；上下文精简聚焦 Harness 添加的非必要元信息与重复说明。

- **一个物理动作入口。** 观察世界中的动作必须经 `Harness.execute`。新插件使用 `ToolContext.execute`，不直接调用 robot/worker、写动作回执或推进仿真。actor/program 使用同一 ToolSpec；`trusted_legacy` 只服务已有可信 Python 集成。
- **事实与展示分离。** 程序消费 `ToolResponse.kind/data`；不能解析展示文本、stdout 或 HTTP 成功码推断动作成功。动作完成后展示/图像失败须保留 typed receipt，说明交付错误；不得自动重发动作。缺少可靠回执、停止确认或计数时保留 unknown/null。
- **完整的工具契约。** 声明输入/输出 schema、role、effect、availability 和 API version，验证 actor 与 program 两个入口。参数含义、单位、默认值、上下界和适用条件属于工具定义；**新增任何工具、阶段参数或辅助功能，必须在契约描述中逐字段写清物理含义、量纲单位（如 m、rad、deg、s）、缺省行为与适用场景，严禁出现仅有字段名而无说明的“裸参数”；新增随路辅助函数（如 `scene3d_helpers`）必须向模型提供清晰签名、入参与返回值说明及调用示例，禁止让模型凭空盲猜。**从同一契约生成模型说明与校验，不通过额外任务 prompt 补参数文档，并检查原生转换后的实际交付。复用 registry/context，不扩充平行 allowlist、名称路由或隐式模块扫描。
- **信息来源受限。** actor 和辅助模型只读取许可的正式观察。隐藏环境状态、evaluator、原始 observer artifact、凭据和开发诊断不进入模型。主模型、辅助模型、程序回包及文本 artifact 共用元信息过滤与短引用；恢复历史在实际 Responses 请求出口再次过滤。原始 typed 数据和 observer 归档保留校验事实，不能把 raw archive 当作模型绕行接口。
- **状态由其 owner 修改。** 幂等重放返回历史回执，不回滚当前计划；可能推进物理状态的动作缺末帧时旧观察失效；补观察不能解除 unknown/stop 门禁。场景实体按其实际引用的观察集合检查时钟和时效。
- **归档与评分可追溯。** 封存数据用 `open_read_store`，含 WAL 时读取私有副本，无 WAL 时 immutable 只读；活动 WAL 用既有 live reader。导入保留 native summary，导入生命周期单独记录。原图、重建图、任务分数和完整性检查分别报告；未知 case 留在固定分母中。

## 实现与协作

先用搜索找所有调用者、测试、配置和导出。优先修共享根因、复用现有 helper；不为未来需求新增框架、依赖、开关或复制契约。新非平凡逻辑必须有能暴露原问题的回归，特别是异常、部分执行、重试、权限和旧归档兼容。

可按文件所有权并行委派互不冲突的工作；集成者负责交叉复核、最终检查与交付。不要覆盖其他 worktree 的未提交改动，不改写无关历史，不触碰正在运行的实验或共享环境。远程运行、同步、部署和归档迁移应在用户任务范围内执行，不能由本地测试隐式触发。

### 实验与评测

实验库可读取 `<实验根目录>/service/experiment-groups.json` 中的可选登记，描述每组实验的名称、目的、迭代来源与 baseline，格式见[实验库](docs/development/experiment-library.md)。登记只维护定义与关系；分数、状态和录像始终以原始归档为准。

- **先确定比较口径。** 比较实验启动前选定 baseline 并写明依据。受控比较对齐 task、variant、seed、layout、预算及研究干预以外的条件；源码、模型、供应商、SDK、提示或记忆同时变化时逐项说明。缺少可靠基线时只做探索，不据此宣称单因素收益。配置模型与实际 served model 分别验证。
- **固定协议与分母。** 在 manifest 中写明每个条件的 case 身份，seed/layout 使用整数。准备、未启动、取消、无效和未评分 case 保留在固定分母中。补跑只能是原协议子集；合并时按执行顺序逐场景保留首个正式评分（包含 0 分和正式任务失败），只有此前没有正式评分时才使用恢复结果，绝不取最好分。
- **身份与执行副本分开。** baseline/candidate 等不同条件分组统计，不混合。新执行及重试都使用新的 run directory，不复用旧批次的路径；原始目录和录像不为整理而移动或改名。
- **记录真实来源与时间。** 记录实际执行的源码 revision；有未提交变更时保存可核验的补丁或快照引用，不能事后用当前 worktree 的 HEAD 反填。实际开始与结束时间取自身份匹配的运行事件和原始 summary，统一存 UTC；登记、导入和准备时间不得冒充实际启动，缺证据保持 null。这些审计元信息不得注入实验 actor 的上下文。

### 长实验、任务调度与完成回调

需要等待模型、native worker 或 GPU 的 benchmark 必须通过项目内的 `auriga.apps.evaluation.queue`（`scripts/benchmark_queue.py` 仅是兼容入口）启动。多个相互独立的 case 必须放在同一个 manifest 中，由这个队列用用户要求的 `max_parallel` 并行调度；每个 case 仍须使用独立 run directory 和独立 memory；同一源码 revision 的 case 共用一个固定 checkout。不要把 case 拆成多个手工后台进程、逐个串行启动，或在 agent 回合中直接并行启动 gateway；也不要用 shell 自己拼一套后台监视器。manifest、源码绑定、并发上限和完成 hook 都必须在提交队列前固定并写入证据。

队列启动成功且已配置 `--wake-thread`、`--wake-codex` 和 `--wake-enabled` 后，立即结束当前 agent 回合并等待队列自己的完成通知。等待期间禁止手工轮询 `state.json`、tail 日志、查询 PID/GPU、sleep 后重复读取、刷新网页或用新 agent 监视；这些动作不能替代完成回调。不要因为队列处于 `running`、`finished` 或 hook 回执为 `sent` 就自行宣布实验成功，也不要自动取消、重启或追加实验。用户没有要求停止时，也不能因为当前回合即将结束而取消队列；让调度器继续运行并由 hook 唤醒后续回合。

收到完成回调后，才重新读取 queue 终态、每个 case 的 `summary.json`/`evaluation.json`、request identity、native cleanup 和失败证据。`queue=finished` 只证明调度器收尾；成功率和 score 必须来自官方 evaluator，未收到回调或回调内容不完整时报告状态未知并停止，不伪造结果。具体 hook 约定见 [`docs/development/experiment-completion-hook.md`](docs/development/experiment-completion-hook.md)。

## 交付

按[验证指南](docs/development/validation.md)完成受影响检查；改公共边界或发布版本时执行全套 CPU、Web 和 wheel 检查。记录实际 revision、命令、结果、skip 及环境限制。CPU/包/浏览器通过只证明对应软件层；原生运动语义或任务效果须有冻结条件下的独立 native 证据。

提交前检查 diff 和敏感信息。配置示例不写密钥，Git 不保存完整运行目录、模型权重、大录像或机器凭据。文档链接到真实实现与测试；变更公共契约同时更新相关指南及 README/文档索引。发布时核对 package version、tag、分支祖先与工作区状态，保留旧版本，正常推送，避免改写已有 tag。

## 具身控制第一性原理与防坑哲学

本节总结开发中反复出现的设计错误及其教训，修改 Harness 行为前先对照。

### 最高设计纲领：消除写死逻辑与黑箱，把自主权彻底交还模型

**我们迄今为止所有的泛化性下降、所有的跨轮次反复与性能波动，根源都在于同一个错误：把太多逻辑、规则和数值写死在 Harness 内部，且这些逻辑对模型是黑箱。**

当开发者看到某个具体任务失败时，最容易犯的致命错误就是在底层代码（如 `batch_motion.py`、`action_state_ledger.py`、`vggt_height_service.py`）里写死新的启发式规则、魔法阈值或隐藏状态机。这种做法必然导致灾难性后果：
1. **剥夺模型自主权**：逻辑和数值被写死在后台甚至从工具 schema 中隐藏，导致模型根本无法根据当前视觉与物理情境**自主选择、自主判断、自主设置和自主调用**。
2. **后台黑箱引发认知错位**：Harness 在后台暗中猜测阶段意图（如偷偷区分预抓取/预释放、暗中维护跨阶段支撑面缓存 `compliant_support_z`、静默篡改容差或夹爪门禁），模型既不知道这些潜规则的存在，也不知道为什么有的阶段差 6cm 被判成功、有的阶段差 6mm 被判失败，或者为什么捏住薄布后每段移动都会莫名暂停。
3. **跨任务 Overfitting 与跷跷板效应**：具身操作涵盖刚体精密插孔、柔体折衣、顺应套娃、原位拧螺母等截然不同的物理约束。任何写死的单一数值或启发式规则都只能迎合部分 task，一旦写死就会对其他 task 产生反向过拟合（修好 A 必然搞挂 B），导致性能永远在震荡中停滞不前。

**【每次修改代码前必须执行的“四问自检红线”】**
- **第一问（是否写死）**：我是不是又在为刚失败的某个 task 写死魔法数值（如 `0.075`、`0.0055`、`+0.002`、`-0.02`）、写死形状/步数阈值、或增加特定情况的 `if-else` 补丁？**若是，立即禁止！**
- **第二问（是否黑箱）**：我写的这段逻辑、状态转移或实际生效的容差/门禁，对模型是不是不可见的黑箱？**若是，必须拆除黑箱，改为无隐式状态的透明机制！**
- **第三问（模型能否自主）**：模型能否通过公开、正交的工具参数（如阶段级 `position_tolerance_m`、`orientation_tolerance_rad`、`max_upward_dz_m`、`stall_guard_preset`、`grip_guard_preset/min_aperture_m`）自主表达物理意图？如果模型缺少控制旋钮，**正确做法是在工具契约中公开该参数并写清物理含义与默认值，绝不是删掉参数由后台替模型瞎猜！**
- **第四问（回执是否诚实透明）**：模型显式设置的参数是否被 100% 尊重而未被后台静默覆写？`preview` 与 `execute` 回执是否透明返回了实际生效的阈值（如 `effective_tolerances`）与真实物理测量残差？

### 核心四大铁律（Harness 第一性原理）

1. **不做任何隐式的假设判断与后台静默覆写。**
   - 严禁在后台背着模型篡改模型显式传入的参数或几何假设，也严禁把原本公开的参数从工具 schema 中删掉改成后台启发式黑箱（**反面教训 1**：将 `vggt_height_mode` 强设为 `"default"`，静默替换模型显式传入的 `plane_origin_m=[0,0,0.765]`，直接导致 `sweep_blocks` 与 `plug_in_charger` 射线偏移失败；**反面教训 2**：曾从工具 schema 移除 `max_upward_dz_m`，改为在 `batch_motion.py` 中写死 `_is_open_jaw_pre_grasp`、`_is_closed_jaw_pre_release`、`compliant_support_z` 隐藏状态机以及 `0.075m / 0.0055m` 魔法数值，导致模型失去阶段级顺应控制权并在多任务间剧烈震荡）。
   - 严禁在 `preview` 或执行前用基于单目噪声边界框或动作计数的启发式规则充当“盲人裁判”训斥模型（**反面教训**：在 `grasp_preview_3d` 中用单目斜视角 2D 投影长宽比弹出假阳性警告，或在 `action_state_ledger.py` 中写死三级认知升级话术与连续两步零位移告警 `no_scene_progress_warning`，把 `fasten_screws` 正常的原位连续拧螺母误判为死锁并强行诱导模型放弃当前目标）。
2. **不对任何单一 task 进行 Overfitting。**
   - 任何机制必须建立在通用的刚体/柔体物理接触定律、机器人运动学（如关节 $[-\pi, \pi]$ 主分支等价类、平行夹爪对称性、本体开度零点分布）与公开透明的参数契约之上。
   - 严禁为特定 task 写死固定流程、启发式形状阈值、专属规则分支或为了迎合个别任务而修改全局固定阈值（**反面教训**：为检出个别薄物体而人为压低 `vggt_height_service` 的 `OT` 全局过滤阈值，或为个别插管滑脱将 `active_arm_grip_guard` 改为跨所有后续移动阶段常驻且硬编码 `ap_after <= 0.0005`，直接导致 `fold_clothes` 捏住薄布后每一段移动都被当成空抓拦截）。所有改动必须在同时包含正例与反例的跨任务全集上验证帕累托收益。
3. **把决策权和控制权完整交还给模型（自主选择、自主判断、自主设置、自主调用）。**
   - 感知与执行模块只报告客观几何测量/估计事实、动作前后 3D 状态差分（`object_state_delta`）、物理受阻归因（`contact_object_3d`）、真实测量残差与当前生效的参数配置（`effective_tolerances`），绝不替模型命名物体、强行指定抓取目标、注入死板公式或替模型决定下一步策略（**反面教训**：在提示词中写死 `grasp_yaw + (target_yaw - yaw_deg)` 公式，导致模型把单目斜视角噪声 `yaw_deg` 代入公式拧歪立柱）。
   - 工具契约与参数说明的作用是提供正交、透明的控制旋钮及通用物理/坐标系说明，让模型根据视觉观察自主选择预设或显式设置数值参数，保持模型对极端场景、意外遮挡与受阻反馈的随机应变能力。
4. **构建易于模型进行操作输出的工具，辅助模型完成任务。**
   - **零额外回合税的随路信息与按需深挖工具结合**：将高价值、低噪声的客观 3D 几何参考（如随路 `object_table`、主分支优先 `across_yaw_deg`、平躺立起参考 `stand_upright_angles_deg`、贴桌薄物体与叠放物体的真实 `pad_z_m`、`scene3d` 点云句柄）零回合税随路交付；同时提供高容错、签名宽容的 `scene3d_helpers`（支持多种参数顺序、双目射线三角化 `triangulate_rays`、指尖持物主轴分析 `in_hand_axis`、局部无截断高程极值 `surface_extrema`）与按需显式调用的 `obstacle_object_ids`，降低模型调用工具的摩擦成本，避免强制模型每步额外调用独立规划工具产生回合税。
   - **工具与功能可解释性红线**：新增工具、新增参数或新增辅助 helper 时，必须在契约（`ToolSpec` schema / field `description`）或模型可见说明中逐字段注明**物理含义、量纲单位（如 m、rad、deg、s）、默认行为、典型取值与调用示例**；严禁引入让模型无法理解入参作用的“裸参数”或“黑箱函数”。

### 运动控制与闭环门禁防坑准则

5. **严守阶段终点真实性与参数透明性：尊重模型显式容差，诚实报告物理残差，绝不用后台黑箱放宽容差制造“虚假成功 (`completed`)”。**
   - 在多阶段批次（`[approach -> grasp -> lift -> align -> insert -> release]`）中，中间阶段的终点到位校验是阻断误差级联放大的物理防火墙。
   - 必须把容差与竖直顺应控制权（`tolerance_preset`、`position_tolerance_m`、`orientation_tolerance_rad`、`max_upward_dz_m`、`stall_guard_preset`）公开给模型按阶段自主设置，并在 `preview` 和 `execute` 回执中透明展示 `effective_tolerances`。当模型显式指定 `max_upward_dz_m` 或 `position_tolerance_m` 时严格按模型设定校验；绝不在后台写死跨阶段状态机或暗中篡改阈值（如把 `3mm/5mm` 偷偷放宽到 `5.5mm` 或把未声明顺应的阶段偷偷放宽到 `75mm`），避免中间阶段带着偏位被谎报为 `completed` 后在开环下盲目执行后续阶段。
6. **物理门禁对齐真实物理零点，且门禁配置对模型完全开放可控。**
   - **区分“未声明顺应的预抓取下探受阻”与“显式顺应/接触放置”**：在默认未开启大范围竖直顺应的开爪预抓取下探中，若因指尖撞在物体顶部停高，应诚实以 `tracking_error` 拦截并原样交付顶层 `contact_block_summary`（`blocked_above_target` 与 `approach_shortfall_m`），避免悬空合拢夹爪并丢失撞顶诊断。若模型需要贴合支撑面或顺应压入，由模型显式设置 `stall_guard_preset="contact_stop"`、`tolerance_preset="compliant"` 或阶段级 `max_upward_dz_m`。
   - **`grip_guard` 阈值透明且支持模型按物体特性自主选择**：真实空抓的闭爪开度为 `0.0000 m`，薄壁、杯沿、细柄的正常夹持开度落在 `2–13 mm`，而极薄柔体（如衣物布料）捏合后开度接近 `0.0000 m`。模型可通过 `grip_guard_preset`（`none`、`thin_object`、`non_empty`、`rigid_object`、`wide_object`）或显式 `min_aperture_m / max_aperture_m` 自主控制门禁；闭爪后的抬升滑脱检测仅在紧随闭爪的第一段抬升移动且发生明显滑脱闭合（`jaw_delta <= -0.08` 且 `ap_after <= 0.002m`）时触发一次，严禁在后续移动阶段常驻拦截薄物体操作。
7. **警惕“观察检查点税（Checkpoint Tax）”与多机制叠加耦合。**
   - **异常触发暂停优于无差别每步暂停**：在需连续操作多个物体的长序列任务中，过度诱导模型插入显式 `observe / checkpoint_after_gripper` 会导致全场暂停次数翻倍，迅速耗尽模型回合与原生步数预算（`stopped` 激增）。应让正常抓握依靠硬件级 `grip_guard` 在异常（抓空/滑脱）时自动暂停，正常执行一气呵成。
   - **受控单变量消融与主分支防缠绕**：多方向合并出现 `1 + 1 < 2` 时，必须通过代码级 diff 与逐阶段遥测（`tracking_error`、`grip_guard`、`vertical_compliant`、$|q_0| > \pi$）做受控单变量剥离；等价旋转关节重规划必须优先约束在 $[-\pi, \pi]$ 主分支内，防止跨过 $\pm\pi$ 后向 $\pm 2\pi$ 软限位单向棘轮缠绕死锁。
   - **LPT 耗时降序并行调度**：大规模基准实验应按历史实测加权墙钟耗时降序（Longest Processing Time First）排列 case 并配合高并发队列与多 key 负载均衡，避免长尾慢任务拖慢整轮实验周期。
