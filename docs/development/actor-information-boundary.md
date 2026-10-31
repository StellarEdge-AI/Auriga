# 正式观察与开发诊断边界

新 Harness / Workbench 运行默认使用 `rgb_proprio_calibrated_v1`，信息策略为 `robodojo-calibrated-rgb-proprio-no-task-truth-v1`。actor 可读取精确相机内参、外参、手眼变换及公开机器人模型，使用 geometry / scene 工具从许可 RGB 与机器人本体状态计算估计。该变更扩展了信息条件，不能与旧 `rgb_proprio_v1` 的成绩混算，也不宣称复现官方默认观测协议。

`--observation-profile rgb_proprio_v1` 保留旧策略 `robodojo-rgb-proprio-no-privilege-v1`，用于显式对照：无相机标定及其衍生工具。既有冻结源码、实验、报告和运行记录不重写。simple API 官方参考适配器继续显式采用旧 profile，以保留其声明的上游观测条件。

新增 `rgbd_proprio_calibrated_v1` 是必须显式选择的独立条件，**不改变上述默认配置**。仅该 profile 放行与 RGB/标定绑定的原生 float32 米制 z-depth artifact 引用；原始数组不进入模型回包。`depth_query`、`depth_move`（已删除）以及几何工具的深度查询别名都受同一 profile 检查。三个额外开关 `depth_auto_feedback`、`depth_skills`、`depth_event_payload` 均默认 false，也不能绕过 profile。普通 RGB 配置在原生任务覆盖完成后主动关闭深度采集，历史与程序 artifact 读取同样拒绝深度输入及明确标记的深度衍生结果。RGB-D 不开放隐藏对象姿态、接触或评分真值；契约与运行方法见 [RGB-D 数值工具与技能](rgbd-numeric-tools.md)。下表描述两个继续默认禁用深度的 RGB 条件。

## 可用数据

| 数据 | 默认标定 profile | 旧 RGB／本体 profile |
|---|---|---|
| 原始 RGB、图像尺寸、公开任务 instruction、带来源的 crop | 可用 | 可用 |
| 机械臂关节角、link6 末端位姿、自身动作及夹爪命令 | 可用 | 可用 |
| 关节名称、速度、实测手指位置 | 可用，属于机器人本体状态 | 不发布，维持旧投影 |
| 公开 ARX X5 几何、固定 grasp 偏移与完整运动学模型 | 可用 | 仅原先公开的接口尺寸与偏移 |
| 相机 K、畸变参数、外参、手眼变换、每帧相机位姿 | 可用；与对应 RGB、机器人状态及来源绑定 | 禁止 |
| 射线、投影、三角化、RGB 估计场景 | 可用；标注来源、误差、假设和退化条件 | 禁止 |
| 隐藏物体位姿、原生 depth、接触真值、未公开评分阈值和 evaluator 状态 | 禁止 | 禁止 |
| 原生 USD 位姿诊断、私有规划诊断、observer 快照 | 禁止 | 禁止 |

标定描述相机与机器人，不提供任务物体的真实深度、身份、姿态、质量、摩擦或接触状态。三角化仍需要正确的可见点对应与足够视差；场景估计不能由隐藏状态补齐。实际手指间隙、轨迹完成或 EE 抬高都不能证明物体被抓住。公开程序和技能不得保存某个 layout 的隐藏目标坐标、基于隐藏真值的任务 recipe 或未公开评分阈值。

benchmark 任务说明使用[官方任务原文与得分表](public-task-rules.md)：完整注入官网 Instruction、Description、可选 Comment 和 Scoring，包含官网明确公开的数值与得分条件，不做摘要、纠错或额外补充。静态公开说明不等于运行中的 evaluator 状态；每个 run 归档其实际使用的文本，旧 run 恢复不替换为新版。

## 工具与执行

默认启用 `robot_wait`，`--no-enable-wait` 保留显式无等待对照。默认 `--motion-review` 开启运动预览、审查后执行计划及抓取规划／经验工具。`--no-motion-review` 关闭这组工具，保留相同相机标定及 geometry / scene 工具，用于比较工具干预。旧 profile 自动关闭依赖标定的工具。预览不推进仿真；计划仍需通过 Harness 唯一动作入口执行。碰撞模型范围和估计障碍的来源必须明确；机器人自身与静态桌面的规划通过不能保证持物、其他物体或另一只手无碰撞。

抓取经验默认持久化到 `~/.local/share/auriga/grasp-memory`，可用配置 `features.grasp_memory_dir` 或命令行 `--grasp-memory-dir` 指定独立库（仓库默认配置已设置该路径，`AURIGA_GRASP_MEMORY_DIR` 环境变量不会覆盖它）。经验保留对象类别／实例、适用条件和观测／动作来源，不作为旧场景坐标复用。配对评估通过 queue 每局的绝对路径 `grasp_memory_dir` 隔离读写经验，避免条件间相互污染。

Gateway CLI、Workbench 构造和 benchmark queue 均默认使用标定 profile，并在 manifest 中记录实际 profile、信息策略、motion review 开关和源码 hash。恢复会话只接受与归档相符的信息策略和 profile；缺策略标记的旧会话不能自动升级，须另建 run/thread。离线 replay 可查看原证据。

## 模型反馈呈现

上下文只放当前决策需要的信息。构建、来源、存储的校验哈希不发给模型；主模型、辅助模型、程序回包、文本 artifact 和恢复历史采用同一过滤。必要对象通过可逆短引用定位；动作状态、关键诊断、计划控制 revision 和实测数值保留。此规则独立于 summary/full 选择，完整查询也不公开非必要校验元信息。内部 typed 数据与 observer 归档仍保存原始校验值。

原生 Codex 自带说明、工具目录、通用编程与文件／记忆能力保留；不以节省上下文为理由覆盖默认基底或裁剪原生内容。模型可以在既有沙箱和许可观察范围内编写辅助工具。Harness 的精简落在自身 producer 与展示边界：按 `StructuredText.projection` 清理已知记录外壳，actor 和 program 共用纯元信息投影；程序回包不套用 actor 的证据摘要。几何结果省略算法实现版本、依赖版本和计算耗时，保留全部测量、诊断、不确定性与提案中的几何及控制字段。原始 `ScenePatch` 继续供可信 scene owner 使用；展示副本照常去除校验哈希，不作为可直接提交的完整原始协议对象。视觉评审的内部线程／turn、后端和上下文策略留在 observer 记录中。

阶段映射的键是业务标签，即使叫 `hash` 或 `sha256` 也保留；映射值内的校验元信息照常去除。带内部环境标识的时钟域使用稳定、可恢复的 `clock_N`，不同环境或 generation 不合并；步数和时序精度不变。动作 artifact 先检查原始权限与来源，再按 producer 声明的 kind 投影读取副本。未知业务记录和缺少投影声明的旧 wire artifact 只使用通用过滤，不按内容形状猜测字段含义。

主模型请求的任务关注提醒只读取当前 episode 的 actor 自写计划和关注清单，作为 assistant 消息中的暂定判断呈现，不作为新的任务要求或实测事实。每个 actor 请求都交付完整 plan、attention、ledger、revision 和依据观察；当前 provider 链没有可依赖的跨请求快照缓存，短提醒不能替代有价值内容。出口去除已有 Harness 快照后再追加最新版本，未知线程、辅助视觉模型和仅工具 schema 的投影不注入。详见[任务计划与关注清单](unified-actions.md#从理解任务到执行)。

新运行默认 `--actor-payload-profile lean_v1`。这只改变模型收到的常规动作／观察 JSON，不改变观察许可、图片字节、动作执行、程序接口或原始存储。它不舍入任何数值，不缩减实际执行 request，不删除 `step_semantics`、`trace_sampling`、错误、部分执行结果、时间／同步信息和重建警告。

| 位置 | lean_v1 呈现 |
|---|---|
| 动作 receipt 的 `versions` | 省略构建／依赖版本，后台完整保留 |
| 相机 `image_encoding`、图片视图 `encoding`，及同层 `sha256`／`source_sha256` | 省略存储编码元数据；图片、有效尺寸与像素变换保留 |
| receipt 的 `planned_trace_ref`、`dispatched_trace_ref`、`actual_trace_ref`、`before_state_ref` | 改为对应 `*_available`；以 `history_read_trace(action_id, kind)` 读取原始内容 |
| `robot_state_ref` | 同一反馈已展开机器人状态时改为 `robot_state_available` |
| 相机／裁剪视图的 `rgb_ref` 与裁剪视图 `original_rgb_ref` | 改为可用性标志；保留 observation／camera 标识、裁剪参数与像素变换，通过历史观察工具访问图片 |
| `calibration_ref`／`pose_sample_ref` | 仅同一反馈已展开对应内容、且引用与实际展开源一致时改为可用性标志；不同相机的独立引用保留 |

引用存在、显式 `null`、字段缺失分别保持为 `true`、`false`、字段缺失。未知格式或与已有可用性字段冲突的引用原样保留。未知字段和其他引用不泛删；`robot_model_ref`、运动预览 `trace_ref` 等仍可供程序读取。按需 `robot_action_status`／`history_list_actions` 返回完整记录。

`full_v1` 保留完整呈现；`compact_v1` 保持历史实验中的字段删除和六位小数舍入行为。profile 写入 manifest，恢复／重放沿用归档值，缺少该字段的旧归档回退 `full_v1`。精简接口测试验证数值与证据保留，不等于已验证机器人任务效果或整局 token 收益。

标定可用的默认运行在启动的 `robot_information` 简报中交付本局相机安装／基座标定，缺失或权限受限的内容以 `available`／`unavailable_fields` 为准；常规观察摘要保留当前 `camera_pose_sample.T_world_camera`，把重复的安装／基座／连杆重建矩阵留在完整观察引用中。相机内参、关节名称与坐标约定另由 `metadata_delivery.static_refs` 按模型上下文复用：首次观察交付后，未变化的值使用引用，内容变化、环境重置、上下文压缩或连接不确定时重新交付。程序内部读取不能替主模型建立交付基线；当前机器人测量、相机位姿和预算继续随观察更新。逐帧 `T_world_link` 可读取完整观察，启动简报的初始变换不能代替后续运动测量。

`robot_contact_overlay` 的默认回复同时保留叠图和完整 `points`、`arrows`、`approach_arrow_length_m` 数值。像素坐标和相机坐标深度来自机器人参考点的投影，遮挡未评估时仍明确保留 `not_evaluated`；不得将其解释为物体深度、接触或抓取真值。

## 其他运行入口

`configs/images.json` 只控制图片编码；pricing 配置和 `environment.yml` 不覆盖观察 profile 或工具开关。本机 experiment settings 读取器只接受 Codex home 和价卡路径，profile／review／wait 由当前 Gateway CLI 或每局 queue 配置决定。

固定机器人烟测、标定验证及原生控制器／π0.5 交接诊断使用标定观察。它们没有模型审查阶段，因此显式关闭 motion review，并记录这种执行角色；固定历史动作的重放不假称为新模型审查后的路线。`ReferenceRecorder` 是被动记录器，以启动时 backend capabilities 的明确 profile 绑定记录边界；未声明的通用新记录默认标定，一旦绑定不允许中途切换。调用方显式指定 profile 时必须与 capabilities 一致。

`reference_episode.py` 的官方 simple API 适配和 `reference_baseline.py` 的固定上游黄金参考仍显式使用 `rgb_proprio_v1`，保留其原先声明的参考输入条件。既有历史冻结源码／设置和恢复会话沿用归档条件。这些入口与新标定主线分别记录，不能混称为相同观察条件。

## 强制边界

- worker 输出许可标定和本体状态；Harness 对初始、动作前、progress、终态及历史观察再次检查 profile、递归禁止字段与状态白名单。`before_state` 与 `actual_trace.state` 同样按 profile 投影。
- actor 可读标定／模型 artifact 必须来自已核验的公开机器人和相机数据。`observer` artifact 不得经 `AurigaClient.artifact` 读取。`internal` 表示从许可输入计算的数据，不是绕过边界的诊断标签。
- 显式开启 `features.sandbox_enabled=true` 时，主 Codex、子代理、shell 和原生记忆运行在独立 mount/PID/IPC/network namespace。仅可读公开 Harness 源码副本与运行依赖，私有工作区可写；不挂载宿主、RoboDojo、evaluator、observer 数据或完整 checkout。联网搜索、宿主 MCP/插件和任意网络关闭，包安装仅经 PyPI 代理，认证留在宿主 relay。原生本地工具搜索仅发现已注册工具，不能绕过其角色、可用性或执行校验。`program_run` 的精确运行依赖、Landlock/seccomp、专属 FD 和 artifact scope 继续共同约束模型程序；evidence agent 使用独立 namespace 和选定证据。配置及迁移边界见[配置说明](configuration.md#代码执行沙箱)。
- `features.sandbox_enabled` 默认为 `false`，此时主/辅助 Codex namespace 与程序 Landlock/seccomp 一并关闭；工具投影、broker 权限、动作审核和预算保留，但代码拥有启动用户的宿主文件/网络权限。私有 `HOME` / `CODEX_HOME` 和占位认证不能阻止读取其他宿主路径，正式观察限制仅在应用层成立，不再有 OS 隔离保证。实验必须记录该条件，不能与隔离运行混同。
- 原生控制器与规划诊断保存在 observer-only 路径，不能进入 prompt、tool result 或 actor artifact。精确标定的许可不放开原生物体／接触／evaluator 诊断。

开发者仍可通过 observer 路由读取评分、隐藏快照和录像并做离线取证；报告应明确来源与使用时间。离线发现可用于改进通用工具和流程，不能将 layout 真值写入正式 prompt 或 skill。

## 验证

`tests/test_observation_policy.py` 覆盖两个 profile 的状态投影、嵌套隐藏字段、历史 profile 隔离和工具可用性；队列测试检查默认及显式对照参数。几何工具测试检查标定、投影和三角化的输入闭包。`tests/test_actor_isolation.py` 验证开启沙箱时主 Codex 原生启动、沙箱内文件能力和宿主文件／网络拒绝，并检查关闭沙箱时的直接执行及恢复模式限制；程序 broker 的权限与 artifact 边界在两种模式下均须保留；`tests/test_codex_native_preferences.py` 用真实原生工具验证主线程与记忆整理的文件读写。这些接口与边界测试不等于机器人任务成功率验证。

程序默认交付全部已保存的文字诊断及全部动作回执；不用固定字符数、最近动作窗口或字段白名单删除潜在决策证据。哈希和已知非功能元信息继续走共享投影；重复日志仅在同一上下文确认已交付后省去，压缩或重连后重送。信息边界拒绝的隐藏数据不能借完整输出放行，原档不完整时仍保留不完整标记。

动作阶段名称（例如 `depth`）只有在与已验证动作或 Store 中保存的实际请求匹配时，才按阶段映射的标签解释；映射值仍递归检查，不允许隐藏深度、接触或 evaluator 数据。执行后若隐私校验、序列化或观察发布失败，保留已校验的最小执行回执与步数、失效旧观察；不自动重发，也不把缺失停止确认或未知执行改成成功。

已确认的 `stopped`／观察检查点允许按协议继续。`execution_uncertain=true` 或明确停止未确认则保留原 typed status，同时关闭现有动作准入并保持评估阻断；补观察不能解除这一限制，恢复同一环境的回执时仍然有效。
