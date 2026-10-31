# 按动作控制运动速度

> 状态：下文提到的 `robot_move_grasp`、`robot_move_ee`、`robot_move_dual_grasp`、`robot_execute_ee`、`robot_gripper`、`robot_preview_motion`、`robot_execute_plan` 与 `depth_move` 已在 2026-10 重构中删除（只供 trusted_legacy 调用，模型与程序均不可达）。档位与数字倍率现在按 `robot_action` 的阶段设置：移动阶段用 `speed_preset` / `speed_scale` 与 `gripper_speed_preset` / `gripper_speed_scale`，夹爪阶段用后两者；逐路点速度列表由逐阶段设置代替。实际档位枚举以 `robot_action` 当前 schema 为准，见[统一动作](unified-actions.md)。

新运行默认使用 `features.motion_execution_mode="resampled"`。Astra 在现有动作工具中按请求指定速度，不需要先设置全局速度，也不沿用上一动作的速度。此接口仍执行原生规划器的路径，再调整下发的关节目标间距。

优先选择固定档位；需要更细的调整时，仍可直接指定数字倍率。档位是相对于原有重采样尺度的固定值，不是相对于本运行默认速度再乘一次。

| 控制对象 | 档位参数与固定映射 | 数字精调 | 两者均省略时 |
|---|---|---|---|
| 机械臂 | `speed_preset`: `slow` → 0.8，`normal` → 1，`fast` → 1.25，`faster` → 1.5 | `speed_scale`: 0.25–2 | 使用 `features.default_motion_speed_scale`，配置默认值为 1 |
| 夹爪 | `gripper_speed_preset`: `slow` → 0.8，`normal` → 1 | `gripper_speed_scale`: 0.25–1 | 1，保留原开合行为 |

同一控制对象不能同时传档位和数字倍率，即使二者表示相同数值也拒绝。机械臂和夹爪可分别选用不同形式，例如 `speed_preset="slow"` 与 `gripper_speed_scale=0.5` 可以配合使用。夹爪没有 `fast` 或 `faster` 档。

`robot_move_grasp`、`robot_move_ee`、`robot_preview_motion` 和 RGB-D 的 `depth_move` 接受这四个参数。`robot_gripper` 接受两个夹爪速度参数。`robot_execute_ee` 对应参数位于 `action` 顶层。夹爪速度要求同一请求包含夹爪目标 / 事件。

多路点请求可用 `waypoint_speed_presets` 或 `waypoint_speed_scales` 分段选速，两种列表互斥。列表按 `via_grasp_poses` 的顺序排列，最后一项对应最终目标，长度必须等于全部 via 点数加一。第 i 项控制**进入第 i 个路点的整段运动**，不是到点之后的速度。列表的 `null` 继承请求顶层的机械臂速度；顶层也省略时，才继承本运行默认。原有不带列表的请求仍让所有路点使用顶层速度。

`robot_move_dual_grasp` 在 `left`／`right` 内分别接受上述移动、路点和夹爪速度字段，左右臂可选择不同速度；两侧 via 列表长度必须相同。较短运动保持其终点等待另一臂，不改变较长运动的速度，每个共享原生行只计一步。底层 `robot_execute_ee` 对成对路点支持 `action.arm_options.left/right` 覆盖顶层共享速度；共享路点列表中的 `null` 继承各臂有效请求速度。到位后的夹爪序列在双臂实测终点均通过后开始。详见[双臂同步动作](dual-arm-motion.md)。

例如先到有充分净空的接触前位置，再短距离接近物体，可在一次 `robot_move_grasp` 中传一个 `via_grasp_poses`、最终接触目标以及：

```json
{
  "waypoint_speed_presets": [null, "slow"],
  "gripper_goal_normalized": 0,
  "gripper_speed_preset": "slow"
}
```

省略顶层机械臂速度时，在默认 1.25 的运行中解析成 `[1.25, 0.8]`，先按默认速度移动到安全接近点，再慢速接触，最后慢速闭爪。如果同时设置了顶层 `speed_preset="slow"`，则 `null` 也继承 slow；想覆盖首段可明确传 `["fast", "slow"]`。数字精调示例为 `waypoint_speed_scales=[1.3, 0.55]`，范围同顶层数字接口。

这两个列表参数也可用于 `robot_execute_ee` 的 action 顶层，按 `path.waypoints` 对齐。单目标的 `robot_move_ee`、`robot_preview_motion` 和 `depth_move` 接受长度为一的列表；预览仍只支持一个目标。不支持给 wait、独立夹爪或 policy prefix 添加路点速度；feedback 模式不支持路点速度列表。

接触前路点必须留出整臂和持物的净空，不会自动根据距离或接触检测替模型选速。同一请求中路点之间没有新的模型决策；需要根据新图像调整目标时仍应分开请求。避免仅因最终需要接触，就把长距离空旷接近整段设为 slow。以下是阶段参数示例，位置和姿态必须由当前观测确定：

| 阶段 | 工具 | 参数示例 |
|---|---|---|
| 有充分净空的普通移动 | `robot_action` 移动阶段 | 省略移动速度，使用本运行默认值；或显式 `speed_preset: "normal"` |
| 有充分净空且适合加快的移动 | `robot_action` 移动阶段 | `speed_preset: "fast"`；进一步加快可选 `"faster"` |
| 接触前靠近、初始短抬、容器内下放 | `robot_action` 移动阶段 | `speed_preset: "slow"` |
| 慢速夹取 | `robot_action` 夹爪阶段 | `goal_normalized: 0, gripper_speed_preset: "slow"` |
| 确认支撑后的慢速释放 | `robot_action` 夹爪阶段 | `goal_normalized: 1, gripper_speed_preset: "slow"` |

机械臂减速不会自动减慢夹爪；两者需要独立指定。`goal_normalized` 为目标开度，0 闭合、1 打开。夹爪减速改变实际下发的中间目标，不只是延长一次性开合后的等待。它不是闭环握持力控制，也不保证物体不会滑落。

## 命令和预算语义

- 机械臂的有效倍率为 `1` 时，保留历史重采样命令行；夹爪省略速度、显式 `normal` 或数字 `1` 时保留历史夹爪命令行。机械臂省略速度而运行默认值不为 `1` 时，会按该默认值执行。其他机械臂倍率采用有界路径重采样：保留显著转折及精确终点，允许每关节最多 0.001 rad 的路径简化误差，并限制相邻目标差（包括实测起点到首目标）。这不是新的碰撞检测。
- 档位解析后的机械臂倍率及 `speed_scale` 表达关节命令间距，不是末端米/秒，也不保证实际运动时长严格按倍率变化。整步取整、路径转折、原生关节速度上限和物理跟踪误差都会影响结果。不同规划调用还可能生成不同路径。
- 夹爪档位解析后的倍率或 `gripper_speed_scale < 1` 时，渐变目标的最大归一化开度增量为 `0.18 * gripper_speed_scale`。夹爪物理响应由原生驱动器和接触决定。夹爪目标完成不等于实际开度到位或已保持抓取。
- 普通 `after_motion` 先检查机械臂实际到位，再根据此刻实测开度生成慢夹爪序列，保留速度、单步开度变化和请求剩余预算限制。规划时的夹爪行数是估计，到位后可能增减；剩余预算不够时在派发新夹爪行前停止。显式 `preview_plan_id` 执行保持精确缓存行、不重新编译；其旧首行因开度变化越界时仍返回 `gripper_start_state_changed`。普通动作不再要求模型因微小开度漂移额外拆一次夹爪调用。
- `during_motion` 继续使用最终运动段的比例区间；慢速夹爪从实测开度起算，必要时细分机械臂命令以满足同步步幅界限。
- 每条实际下发的原生 `take_action` 仍计一步。慢速通常增加步数，快速通常减少移动步数；夹爪终值停留仍计步。单次动作的 10 秒 / 250 原生步预算和任务官方预算不变。瓶子入垃圾桶任务官方上限为 700 原生步。不存在不计步的后台仿真推进。
- `robot_preview_motion` 缓存包含速度参数及精确动作行。`robot_execute_plan` 只能指定 `plan_id` 和可选 `action_id`；修改速度必须重新预览。
- `robot_capabilities.action_speed` 公布可用性、范围和语义。实验性 `feedback` 不支持非默认速度，wait 和 policy prefix 不接受速度字段。

回执及执行 trace 记录本次有效速度、规划移动 / 夹爪步数和实际完成步数，保留原有终点误差、取消、原生终止与部分执行信息。混合速度时，`waypoint_speed_scales` 给出完整有效列表，逐段 trace 的 `speed_scale` 给出该段实际倍率，顶层倍率只是继承默认。列表在保存 action 和编译预览前统一解析，原始选择保留在 `action.speed_resolved.input`；预览摘要绑定数值列表，执行时不能换速。单次 250 步预算在全部路点和夹爪之间累计，不随分段重置。

## 运行默认速度与消融

`features.default_motion_speed_scale` 控制未指定机械臂档位和数字倍率的移动；配置默认值为 `1`。它只改变本运行的省略值，不改变固定档位，也不影响夹爪默认速度。例如配置为 `1.25` 时：

| 请求 | 实际机械臂倍率 |
|---|---:|
| 不指定移动速度 | 1.25 |
| `speed_preset="normal"` | 1 |
| `speed_preset="slow"` | 0.8 |
| `speed_preset="fast"` | 1.25 |
| `speed_preset="faster"` | 1.5 |
| `speed_scale=0.8` | 0.8 |

较快的运行默认值（如 `1.25`、`1.5`）只应在明确的实验配置中使用；显式选择数字或其他档位也覆盖运行默认。生产配置默认仍为 `1`，不会因准备实验而全局加速。历史运行只有在工具、提示及其余条件相容时才能作同条件对照；否则只能作背景参照。分析需要核对实际默认值使用次数、显式档位／数字选择、任务结果和真实原生步数，不能将配置倍率等同整局速度倍率或必然省步。当前接口和提示尚未证明全任务收益；更快失败不算效率改善。

机械臂与夹爪的 `slow` 当前均为 `0.8`；早期版本为 `0.6`。请求级 `speed_preset` 和逐路点 `waypoint_speed_presets` 使用同一机械臂映射。数字 `speed_scale=0.6` 或路点数字 `0.6` 仍精确保持该值。旧实验快照不改写。

## 配合操作经验

历史默认 Task Notes `operation-notes-v8` 包含以下与速度配合的经验：容器内中心放置；接触、夹取、初抬、下放与释放减速；根据形状选择顶部或侧面抓取，必要时先有支撑地摆正；根据形状、接触面、估计重心和净空选择爪尖或内侧夹持面中部接触，调整伸入深度而不改变工具中心约定；跨越容器边缘时检查物体底部、整体包络、夹爪与容器壁的三维关系。使用逐路点速度区分快的空旷搬运与短慢接触，需要中间反馈时才拆请求。任务顺序结合依赖、现场可达性、遮挡和后续成本，适合时优先完成容易的子任务，不规定单臂优先；阶段结束不妨碍后续操作与视角时不必归位，任务明确要求的最终归位仍须做。经验不固定目标坐标或自动代替模型选择路线。

版本化 Task Notes 仅影响启用默认经验的新 benchmark，历史运行沿用归档正文；当前默认操作经验表见 [Task Notes](task-notes.md)。

## 速度与关节绕行重规划

更大的步幅不能消除原生规划中的整圈绕行。可选 `features.joint_replan` 在原生末端规划成功后，检查是否存在限位内更近的整数 2π 等价关节终点；验证所有连杆的 FK 等价和原始末端目标，再调用一次原生 `plan_joint`。只有候选合法且重采样行数、总关节行程都严格下降才选用，不直接修改原轨迹角度。

候选与原计划现在使用**当前路段的实际速度**比较成本：执行与预览均传入逐路点解析后的倍率，使用同一 `_resample_action_plan`，不再用统一倍率 1 估算。比较的是移臂行数，夹爪同步或附加阶段仍由后续编译计算。该优化只对 resampled 的无额外路径约束动作启用；feedback 只命令固定端点，不额外重规划一条不会执行的路径。

该功能默认开启（`features.joint_replan=true`），可用 `--joint-replan` / `--no-joint-replan` 覆盖。原生规划失败、候选无收益或剩余动作时间不足 15 秒时保留原路径；原生异常向上传播，超时或取消阻止继续下发。它不保证所有绕行都被消除，也不覆盖普通规划器未建模的任务物体、持物和另一机械臂碰撞。

所有观察模式下执行动作的私有 `planning_diagnostics` 都会归档，用于确认重规划实际采用次数；calibrated 只保存必要元数据、不进入 actor 回包。归档不改变动作和计步，也不能追溯补回早期运行。preview 自身的规划诊断不在此范围。
