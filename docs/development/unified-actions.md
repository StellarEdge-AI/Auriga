# 统一动作接口与程序审批

新任务的模型入口由 `runtime/tools/definitions.py` / `builtins.py` 经 `ToolRegistry` 管理（原 `tool_catalog.py` 已删除）。任务规划、动作编译、物理执行、模型展示和经验存储各自独立；机器人状态和原始执行证据仍由现有 Harness / Store 保存。

## 模块职责

| 模块 | 职责 |
| --- | --- |
| `actor_instructions.py` / `definitions.py`、`builtins.py`（原 `tool_catalog.py`，已删除） | 集中管理启动约定、公开工具、简短说明；旧工具不进入模型目录。 |
| `task_planning.py` | 普通文本任务计划和可替换关注清单；首次物理动作的准入检查；修改计划不改变已编译轨迹。 |
| `codex.py` / `model_proxy.py` | 默认 `/responses` 请求保持对话历史前缀延伸（仅做公共信息过滤与确定性完全相同图像复用）；仅在主模型 actor `/responses/compact` 压缩请求时删除已有 Harness 快照并注入当前完整计划快照，辅助视觉模型不接收整局计划。 |
| `unified_actions.py` | `robot_action` 参数校验、短计划编号、预览/执行/查看、结果和图像交付。 |
| `batch_contract.py` / `batch_motion.py` | 原生批次协议、依赖图、整批编译、共享时间线、缓存轨迹执行与观察点恢复。 |
| `batch_geometry.py` | 用公开机器人模型、显式障碍与持物包围盒估算间距；不读取任务隐藏状态。 |
| `programs.py` / `program_review.py` | 隔离 Python 进程、主模型审批、取消与工作计时、可信动作结果归集。 |
| `stability.py` | 显式开启的有上限可见运动检测；通过普通等待动作消费真实步数。 |
| `actor_output.py` / `actor_presentation.py` | 模型侧字段精简、可恢复短编号与文件引用；原始证据不修改。 |
| `operation_memory.py` | 当前任务经验和跨任务经验分开保存、检索；动作事实读取实际历史。 |

## 从理解任务到执行

启动时自动交付机器人能力。模型可以先观察、查看几何或预览动作，再提交自然语言计划：

```json
{
  "plan": "任务理解：按公开要求转移目标。做法：选择可达抓取，搬运并放置。完成依据：目标在目的地得到支撑，撤手后仍保留。",
  "attention": [
    {
      "concern": "闭爪后是否真正带起同一目标仍不确定。",
      "check": "利用正常提离动作的反馈检查目标离开原支撑并随手移动；若未发生，调整接触位置或抓取方式。",
      "when": "提离之后、依赖持物状态继续运输之前。"
    }
  ]
}
```

`task_plan` 默认操作是 `submit`，返回短确认；`update` 修改整体认知或当前关注项，`read` 读取完整当前版本。首个物理动作（包括 `robot_wait`）前必须已提交。计划无需逐动作更新，也不是执行器的指令表。

`plan` 建议用短小的“任务理解、做法、完成依据”段落；`attention` 是可选列表，每项包含 `concern`（当前问题、假设或待选方案）、`check`（可见判断依据及不符时如何调整）、`when`（依赖该结论的动作或事件）。只保留当前有用条目，允许随新证据增加、改写、删除和重新选用经验。`update` 至少提供 `plan` 或 `attention`；省略的字段保留原值，`attention: []` 明确清空。相同观察下重复提交相同字段返回 `status=unchanged`，不制造新 revision 或重复快照；同一内容在新观察下重新提交仍记录为新的判断来源。更新是替换当前清单，旧版本仍可审计；保留的计划文字不会因为更新清单而变成已验证事实。每项文字最多 1600 字符，超限显式拒绝，不截断；不要求填满固定数量。

默认非超长的 `/responses` 请求不追加或删改决策提醒，使每个后续请求保持为上一请求的严格前缀延伸以复用 prompt cache；仅当上下文超限触发主线程 actor `/responses/compact` 压缩请求时，才重新读取同一 episode 的最新计划、revision、完整关注项、证据账本和依据观察并注入完整快照。`plan_observation_id`、`attention_observation_id` 与 `ledger_observation_id` 分别标记对应字段最近提交时的观察，部分更新保留未改字段的来源，旧档未知来源为 null；它们不证明判断成立。快照以 assistant 消息表示模型自己的暂定判断，再走共享元信息过滤；不提升为 developer 指令，不混入辅助视觉评审，不修改历史工具回执。压缩出口只删除已有的 Harness 快照并追加一份最新快照，保留普通 assistant 内容；重试复用同一份已投影请求，不重复调用 context hook。

检查优先结合正常操作反馈：一次有用的接近、提离或撤手也可以检验假设。只有现有证据不足且答案会改变下一步时才额外探测；长批次仅在后段依赖未知前段结果时设置观察点。默认程序审批返回主模型时也收到最新清单；显式 `allow_direct_actions=true` 的程序内部没有逐步模型推理，须自行处理可计算的证据条件，或在需要判断时返回主模型。该清单不自动观测、打勾或触发恢复动作。

当前固定的 Codex SDK/CLI 0.160.0 有模型工具 `update_plan` 和 `turn/plan/updated` 进度通知，但生成的客户端 RPC schema 没有计划读写接口，`thread/read` / `thread/resume` 也不提供可恢复的结构化最新清单。因此复用 Harness 的 `task_plan` 作为唯一状态来源，不维护第二份原生待办镜像。参考：[App Server 备用文档](https://www.codex-docs.com/docs/app-server)、[Hooks 备用文档](https://www.codex-docs.com/docs/hooks)；结论同时核对了本地固定版本。回归见 `test_task_planning`、`test_codex_requests`、`test_model_proxy`。

下面示例中的坐标必须根据当前观察填写。`move` 可使用 `grasp_position_m + angles_deg` 或 link6 的 `position_m + quaternion_xyzw`，两种方式不能混用。

阶段参数按 `kind` 分别声明，并与编译器共用字段定义。`move` 必须给出 `arm` 和完整的一套姿态；contact 只适用于 grasp 姿态，夹爪速度或同步时间必须同时提供 `gripper_goal_normalized`。`gripper` 必须提供 `arm` 和 `goal_normalized`。`reason` 只用于 `observe` 检查点，并且必须包含非空白文字。不同阶段的字段不能混用；actor 与程序在调用 handler 前使用同一 schema 检查。四元数、依赖图、后端能力、缓存状态和执行预算仍由相应业务边界检查。

```python
preview = robot_action(
    observation_id=current_observation,
    stages=[
        dict(id="left_approach", kind="move", arm="left",
             position_m=left_target, quaternion_xyzw=left_orientation),
        dict(id="right_prepare", kind="move", arm="right",
             position_m=right_target, quaternion_xyzw=right_orientation),
        dict(id="check", kind="observe", reason="检查双臂到位和物体位置"),
        dict(id="close", kind="gripper", arm="left", goal_normalized=0),
    ],
)
# 模型检查预览中的成本、轨迹、图像和未知项后：
result = robot_action(mode="execute", plan_id=preview_plan_id)
# 观察点暂停后，检查实际反馈；原方案仍有效时继续同一计划：
result = robot_action(mode="execute", plan_id=preview_plan_id)
```

这些示例展示主模型直接调用原生动态工具的参数；Python 程序使用 `AurigaClient.call`，返回 `success/kind/data/contentItems`；按 `kind` 读取 `data`，动作回执位于 `data.receipt`，无需从展示文本解析状态。主模型直接收到独立图像项，无需 JavaScript wrapper 或手动提取 base64。程序子调用的预览／历史图像由 `program_run`／`program_status` 转交，不要将图像 base64 当文字打印。

同一臂的段隐式有序，`depends_on` 增加跨臂依赖；独立双臂段可重叠。没有 `arm` 的 `observe` 等待双臂前序完成，有 `arm` 时可在该臂到位后暂停两臂。每段可独立设置速度；移动段的 `gripper_motion` 用比例指定夹爪动作时间，没有该字段时夹爪动作排在到位之后。

双臂同时移动需要把左右段放进**同一次** `robot_action` 的 `stages`；即使先列完左臂的多段再列右臂，两臂仍会并行推进。启动说明中的“顺序等待工具调用”约束的是不同动作调用，不是批次内的两臂。把左右动作拆成两次执行调用会串行；给右段加上左段的完成依赖，或在左右段之间放一个未指定 `arm` 的 `observe`，也会有意形成先左后右的顺序。只有实际存在前置条件时才增加这些依赖。

`preview` 是默认模式，不推进仿真。整批编译成功才返回可执行计划，任何后段规划失败都不会执行前段。`execute` 使用缓存中的同一轨迹，检查环境、实测状态、后续预算与首步变化；不暗中重新规划。观察点恢复还核对执行位置与检查点状态。请求重放不会重复执行。改变目标、路线或计划失效时必须重新预览。显式 `direct` 在一次调用内完成规划和执行。

`robot_action`（行为版本 `1.1`）除显式 `observe` 阶段外，支持在含夹爪命令的 `move`/`gripper` 阶段或批次顶层按需声明可选布尔参数 `checkpoint_after_gripper`（默认 `false`）。当显式启用且该夹爪命令窗口改变了夹爪目标、并有后续 `move` 阶段通过同臂顺序或 `depends_on` 依赖它时，编译器在该窗口末尾的既有 native cursor 自动增加观察检查点（窗口末阶段显式设为 `false` 可覆盖批次级 `true`；同 cursor 已有显式检查点时合并）。预览 `checkpoints` 和执行 `checkpoint_stage_ids` 使用触发的真实阶段 ID。返回仍是 `stopped / observation_checkpoint`、正式末帧与 typed receipt，随后显式执行同一 `plan_id` 零重规划继续。默认省略时整批连续执行，不在常规开爪或释放撤退时强行打断。

检查点暂停双臂，因此另一臂独立执行中的阶段也会暂时停在同一共享 cursor，恢复后继续原轨迹；单阶段内同步闭爪后继续移动的部分仍会完整执行到阶段末尾。最终夹爪阶段、未改变目标的夹爪命令、没有后续直接物理依赖的夹爪阶段不增加检查点；已有 `observe` 隔开的顺序继续按显式观察边界执行。

当前批次执行基于 `resampled` 原生模式；最多 32 段、每臂最多 16 段，整批（包括跨观察点继续的部分）最多 250 个原生步。缓存容量为 16 个计划；缓存淘汰或原生进程重建后不能执行旧计划。实际执行证据仍可通过 `mode="inspect", action_id=...` 或保存的 `trajectory_ref` 读取。默认分页，显式 `detail="full"` 可读取完整证据。

预览中的关节/EE/夹爪是计划命令；执行后返回实际测量、失败/部分执行状态及同帧图像。障碍框与持物框来自调用方估计；持物计算假定刚性附着。几何不可用会明确报告未知，不作无碰撞保证。接触声明仅解释几何估计，不能证明真实接触、夹持或支撑。

默认动作反馈保留失败时的逐关节首步跳变量和允许上限（`first_joint_step_rad`、`max_joint_step_rad`），以及轨迹段起点偏差（`max_joint_start_error_rad`）。程序 broker 的可信动作摘要同样保留这些诊断及各阶段的末端误差、接触参考位置反馈，保留完整阶段诊断及未知扩展字段；程序汇总覆盖全部动作，避免早期失败随最近动作窗口消失。

失败预览的自碰撞模型诊断先通过信息边界校验，然后完整交付已采样见证；不再仅展示最深的 3 条。已有 `planner_collision_witnesses` JSON artifact 与 `collision_model_witnesses_ref` 保留供回查。关节位置／速度／加速度／jerk约束也保留每个关节的最严重违规，不能因排在前三之外而丢失。它们只来自候选轨迹和公开机器人模型，不是已执行碰撞或环境接触真值。归档失败仍交付已通过边界校验的完整见证并说明归档错误；未通过权限／序列化校验的部分不会交付。此更改不补齐历史已丢弃条目，也不恢复其他未捕获 solver seed 或候选中间关节路径。

失败预览同样写入 `action.batch_preview`，保留公开失败原因、安全见证、原观察绑定与当时预算。恢复账本将 actor 工具请求及程序 broker 的 `program.call_started` 公开请求与对应预览关联，限定同一 episode、thread、turn 和调用身份；程序内预览使用同一证据校验，不从 stdout 或 evaluator 补造请求。仅在明确零物理推进且观察／预算相符时采用；只有已知成本超出剩余预算并有一致预算拒绝见证，才记作该候选的预算拒绝。未知成本或 IK 失败不代表预算不足；相同候选的重复结果去重，状态、成本或证据改变则保留。任何单个候选拒绝都不证明整个任务无解。回归见 `test_unified_actions`、`test_recovery_evidence`。

## 程序审查与计时

`program_run(operation="start", source=...)` 默认不授予直接动作权限。程序中的 `client.action(...)` 或 `client.call("robot_action", ...)` 请求执行时，broker 先生成预览，冻结程序进程组，把 `awaiting_review`、审批编号、预览和图像交回主模型。

```json
{"operation":"resume","job_id":"返回的程序编号","review_id":"返回的审批编号","decision":"approve"}
```

批准执行已审查的缓存计划，拒绝则返回拒绝结果供程序处理。审批期间其他调用改变计划状态时，旧审批不能继续推进。程序不能调用 `resume` 自批，也不能通过旧动作名绕过审查。`robot_wait` 同样推进物理，因此也要经过审批。

主模型可在 **start** 时明确设置 `allow_direct_actions=true`，授权仅本次程序直接执行；审批恢复时不能修改权限、源码或动作参数。审查逻辑集中在 broker，可扩展审查方；当前仅由主模型批准。

默认 180 秒，最大 300 秒；不限制工具调用次数。工作计时扣除该程序等待辅助模型和主模型审批的时间。这里的审批由主模型自主完成，不等待人类。等待期间仍支持取消，取消必须解冻进程并回收。程序退出码与 `action_summary` 分开：动作结果只从 broker 的真实回包收集，不将 stdout 或退出码当作动作成功证据。末帧图像通过原生动态图像项返回。

`direct` 也可能在规划阶段失败。只有 typed `plan` 明确记录 `status=failed`、`physics_advanced=false` 和整数零步时，程序动作摘要才记为 `not_executed` / `phase=planning`，保留失败原因和此前观察；它不生成动作回执。缺少这些证据或执行通道中断时，继续保留 `unknown` 与未知步数，不能根据展示文字或外层 `success` 补零。

## 可选等待与经验

动作默认结束即返回。显式 `settle={roi:[x,y,w,h], max_native_steps:25}` 才启动可见运动检测；最多 50 步，使用普通 `robot_wait`，计入原生预算。连续可追踪低位移只表示 ROI 可见运动较小；镜头移动、纹理不足等返回未知。检测结果与原动作回执分开，当前观察编号指向最后一次等待后的状态。独立 `robot_wait` 继续保留。

`operation_memory` 支持任何操作阶段的经验。`scope="task"` 与 `scope="shared"` 查询互不混合；跨任务经验必须带适用条件。记录时核对动作与观察归属、关联，并从历史提取执行事实。历史经验中的绝对位置和旧任务成功判断不能代替当前观察。

## 兼容边界

旧动作、独立预览/成本、队列、模板、skill、抓取表格和 `scene_*` 不再向新任务模型公开。可选操作视觉复核并入 `vision_review(evidence_mode="operation")`。旧原生执行/历史解析适配器保留，便于读取归档和测试底层机制；它们不因此获得模型或程序执行入口。旧程序模板测试通过测试专用适配器运行，生产不存在放行开关。

展示层删除版本、哈希、来源地址和重复请求，只保留可执行/可回看的短编号及必要测量信息。原始后台证据和完整轨迹仍保存。图片选择策略、高频反馈决策和辅助模型审批策略不由展示层改变。

## 注册和内部边界

`robot_action` 的声明由 `ToolSpec` 同时用于模型目录、actor 调用和程序调用。`feedback` 后端模式、`unified_action.available=false` 或不允许校准的观察 profile 会使该工具不可用，直接调用也在 handler 前被拒绝。旧 `motion_review` 开关控制旧单臂预览，不单独禁止可用的 `resampled` 批次接口。默认预览、执行和检查的 effect 分别为 `plan`、`physical`、`read`。

批次编译直接调用 `arx_actions.compile_action(kind, arguments, identity, capabilities)`，不再借旧工具名或 Harness 的私有转换方法。Harness 保留身份、当前观察、动作准入、唯一物理写入和不可变回执；后端通过自己的 `validate_action` port 执行原生约束。当前观察不可用时不能计划新动作，已经确认的 action_id 重放仍返回原始记录。

审核和执行返回内部 `ToolResponse(kind="plan"|"action", data=...)`；程序 broker 和生成程序均读取结构化值；客户端的 `kind/data` 副本经过相同的信息过滤与短引用，兼容保留 `contentItems`。主模型原生工具展示仍为 `{success, contentItems}`。新增工具方法见[工具注册与扩展](tool-plugins.md)。

`grasp_position_m` 是所选接触截面上两指之间的中心，不是某一根手指的接触面；`contact_region` 只选择沿手指长度的截面，不能保证触及物体。`position_tolerance_m` 检查实测 link6 位置，`orientation_tolerance_rad` 检查实测朝向；省略时取默认 `tolerance_preset="normal"` 的 10 mm 与 0.15 rad。跟踪失败会停止后续阶段，包括尚未下发的夹爪命令；放宽容差不等于已经接触。夹爪命令结束或测得开口变化也不证明物体被持住，应根据任务选择可观察的验证。


## Explicit motion and held-evidence contract

Grasp-angle planning preserves the requested orientation. Infeasible poses return planning failure; the actor may explicitly choose new angles and preview again. Joint-space replanning remains available without changing the Cartesian target. Vertical compliance never relaxes the declared orientation tolerance; downward error remains bounded by the declared position tolerance. Contact-stop only changes stopping behavior and the documented upward window, not declared horizontal or angular tolerances.

Held metadata reports `retention_status="unverified"` (no retention assertion), `attachment_assumption="rigid_since_grasp"` (conditional kinematic model), `geometry_evidence="current_mask"|"prediction_only"` (current usable segmentation availability), and `current_mask_views` (head/wrist views with usable masks). These categorical fields have no physical units. They are present for both carried and fallback rows; a current mask does not establish co-motion. Prediction-only rows remain available during occlusion but cannot establish a successful grasp or continued retention. No additional observation or model call is required.
