# 工具返回与按需详情

工具执行、原始回执和存储证据保持分离。常规模型交付先提供能决定下一步的结果，完整证据通过明确的详情参数、分页或 artifact 引用读取。输出投影不执行机器人动作、不增加仿真步数，也不判断抓持、接触或任务成功。

## 内部结果不依赖展示序列化

当前工具 handler 返回保留原 wire 键的 `ToolResponse`，通过内部 `kind/data` 属性向 broker 提供结构化计划、原始动作回执或 ObservationBundle。`ProgramRunner` 的审核、`action_summary` 与 `final_observation_id` 不解析 `contentItems`，也不信任程序 stdout。外部 JSON 仍只有原有 `success/contentItems`；typed 属性不会扩展客户端协议或泄露隐藏元数据。

`ToolOutput`、模型摘要、图片组合和 artifact 引用负责展示，物理执行原始结果独立保留。handler 已发生动作后不能因为可选展示/反馈失败隐藏回执或建议重放。具体扩展契约见[工具注册与扩展](tool-plugins.md)。

## 共用组件

`auriga/core/tool_output.py` 提供轻量 `ToolOutput(summary, details=None, extra_content=())`。`content_items()` 生成首个 JSON 文本、可选详情文本及后续图片；`response()` 生成原有 `{success, contentItems}`。没有在既有 `{request, receipt}` 外增加新的必需封装。`select_fields` 只复制已有字段，不填造缺失数值；`DETAIL_SCHEMA` / `resolve_detail` 共用详情选择；`validate_page` 验证整数分页；`bounded_text` 保留文本头尾并声明原始字节范围、遗漏量。

公开工具由`ToolSpec`声明输入、typed输出、角色及副作用，`ToolRegistry`统一生成目录并执行校验。各业务层拥有结果语义；下表同时列出保留的内部兼容模块，不表示其中的旧模板或skill重新向actor公开：

| 返回路径 | 维护入口 | 默认展示 |
| --- | --- | --- |
| 操作模板 | `manipulation_result.py`（已删除） | 阶段、动作状态/原因/执行量、观测编号及未验证物体状态；随后给完整回执和程序输出引用 |
| 普通动作 | `actor_payload.py` | 保留请求、执行结果及错误；已知成功 guard 的重复数值移到明确的完整回执读取入口 |
| 历史与状态 | `history_output.py` | 有界动作索引、状态摘要、可分页的精确 trace |
| 程序与固定 skill | `program_result.py` | 进程状态和日志头尾；已知固定 skill 可以投影结构化执行结果，任意 Python stdout 保持不透明 |
| 图像观察 | `observation_output.py` | 实测机器人状态、当帧相机世界位姿、当前/历史标识及当前预算；固定标定和关节约定首次交付，之后用版本引用 |
| 能力发现 | `capability_output.py` | 首次返回完整能力；同一上下文再次读取时返回版本和当前预算，变化后重新完整交付 |
| 动作轨迹 | `trajectory_feedback.py`、`feedback_delivery.py` | 默认 automatic：采样计划/实测关节、实测轨迹图、偏差及全文引用；异常与交接诊断补充计划及局部候选图；可选 diagnostic 保留常规成功摘要策略 |
| 候选轨迹 | `trajectory_evidence.py`、`manipulation_tools.py` | 关键姿态、最小模型间距与异常项；完整候选命令序列保存在 artifact 并可分页读取 |

## 输入约定与兼容

`robot_capabilities`、`robot_observe`、`history_view_observation`、`robot_action_status`、`history_list_actions`、`history_read_trace`、`program_run`、`program_status`支持可选 `detail="summary" | "full"`。这些是新增可选参数，旧参数仍合法。程序内部读取没有显式要求摘要/分页时保留完整结构，供已有脚本进行数值计算。通用 `ProgramRunner` 的内部状态接口和原始日志不变；模型展示层单独读取完整 artifact。

历史列表和 trace 使用 `start` / `limit`，默认25条，最大100条；返回总数及下一页位置。trace 的 `path` 是 JSON Pointer，例如 `/segments/0/q_rad`。分页不重排、不四舍五入、不把抽样冒充完整轨迹。`detail="full"` 不与分页选择器混用，可恢复原始完整结果。候选轨迹按原生步骤编号选取，保留它本身的 `native_steps` 选择和分页约定，详见[轨迹接口](motion-safety-and-trajectory-evidence.md)。

例如先调用 `history_list_actions({"start":0,"limit":25})`，根据 `next_start` 继续，或用条目内的 `full_result` 精确读取单个动作；不必每次展开整局所有回执。读取完整 artifact 可在 `program_run` 内使用 `AurigaClient().artifact(ref)`；只读操作不消耗 action step。完整图片依然可通过 `history_view_observation` 按相机取回，图片筛选与文本摘要是独立机制。

## 会话内复用与轨迹交付

`MetadataDelivery.prepare/commit` 只复用已实际组成模型回复的已知静态字段。固定内参、畸变参数、关节顺序和坐标约定按精确值生成版本；值变化、环境重置、actor thread 更换、原生上下文压缩或模型 transport 变化后重新交付。未知字段、来源标记、动态相机矩阵、关节/夹爪测量、历史及当前预算持续内联。`metadata_delivery.static_refs` 指向此前的观测与版本，当前观察的 `detail="full"` 可恢复全部字段。显式全文、程序内部观察和被覆盖的 wait 时间采样文本不消耗模型的首次交付记录。回复组装途中上下文失效时回退到完整内联元数据。

`static_refs.bundle` 是一组共享观测约定的版本引用，涵盖明确列出的运行/环境标识、固定相机尺寸与像素约定、时钟单位，以及跨字段重复的预算和几何说明。它不只对应 `bundle` 子对象。相机按 ID 关联，列表顺序与相机 ID 持续内联；实际时间戳、同步和可用性标记、测量及预算数值仍逐次发送。只有连同引用本身计算后能减少字节时才启用该组复用；未知说明或值变化会重新内联。

`features.action_trajectory_delivery="automatic"` 默认随动作返回采样计划/实测关节和实测轨迹图。常规成功只画一份实测路径，计划关节和跟踪偏差仍在返回中，计划图按需获取；原图面分辨率不缩小。`"diagnostic"` 是兼容的可选模式，其中无图常规摘要只适用于执行完成、原生时间轴同步、计划和实测采样完整、几何及结果观察可用、无剩余候选或异常的回执。两种模式都保留精确 `trajectory_feedback_ref`，完整关节序列与公开 FK 保存在该 artifact。失败、部分执行、未知结果、采样或几何缺失，以及交接验证继续返回详细诊断和可用轨迹图。动作完成仍不证明抓持、释放后稳定性或任务成功；必须检查当前 RGB 和物体状态。

`robot_action({"mode":"inspect","trajectory_ref":"<trajectory_feedback_ref>","path_kind":"measured","camera_id":"cam_head"})` 可以按需重画已执行动作的轨迹；`path_kind="planned"` 查看存档命令，`local_N` 查看 `available_paths` 列出的局部候选。返回精确保留实测夹爪、`sim_step`、各路径时间含义及原生边界是否完整对齐，支持现有采样选择、分页和 `detail="full"`。图像来自执行开始时的观察和相机位姿，并显式标为历史图；它不重建执行后场景。读取候选预览时省略 `path_kind`，候选覆盖图仍要求该预览基于当前观察。所有这些检查均不执行机器人动作。

## 不能压掉的语义

- 程序 `succeeded` 只代表进程退出0；模板可能仍是 `stopped`。动作 `failed` 或 `stopped` 可能已经执行若干步，未知执行量不能填0。
- 原生步数、物理/控制步数和模型请求次数不混用。双臂共享时间线上，一臂可能已完成开爪，另一臂随后失败；没有夹爪执行报告时明确未知。
- `arm_results` 可能是先前路点最后一次检查，必须保留相应阶段、时间步、路点和 `endpoint_result_semantics`。命令完成不等于夹爪物理到位或物体被保持。
- 模板只有完整正常阶段报告才返回 `released_unverified`。缺失/损坏输出、失败或未知状态不升级为释放成功；`physical_claims_verified` 仍为 false。给出新图片也不自动升级为 `retained`。
- `bundle.action_budget` 是拍摄时历史快照。`current_action_budget` 是现在的控制器预算；`observation_is_current` 和 `current_observation_id` 区分旧图与当前状态。存储的旧观察不被改写。
- 候选轨迹不是未来实测；已执行反馈区分计划与测量。模型球体间距不是物体碰撞证明。相机输出保留当前世界位姿、像素变换及来源，固定内参保持可解析的已交付版本引用，不新增物体/接触/评分真值。
- 摘要必须有真正可读取的全文入口。错误、未知字段及尚未识别的异常保守保留；不能只删除信息并留下无法解析的 `available=true`。

体积回放只能证明交付量变化；任务表现、模型请求次数、用量和墙钟收益需要通过固定输入实验及轨迹/视频复核分别验证。
