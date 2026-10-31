# 运动规划失败反馈

`robot_action` 的移动阶段（原 `robot_move_grasp`，已删除）和同一末端执行后端在 `path_infeasible` 时返回
`receipt.failure_feedback`。预览失败也提供相同字段。反馈随原回执保存并直接进入
full、lean、compact 和程序调用返回，不需要模型读取私有规划日志。

原有 `planner_failure_stage`、`failed_waypoint_index`、执行步数、执行区间、前段
`waypoint_results`、轨迹和终态观测继续保留。规划失败可能发生在已执行的前段之后，
因此不能从 `path_infeasible` 推断机器人没有移动。

## 实机信息来源边界

失败反馈只允许请求参数、实机规划器也能返回的数值结果，以及配置的机器人
运动学/碰撞模型。没有实机传感器、标定或用户提供的环境几何来源时，不输出环境
障碍位置、桌面真值、实际接触、仿真基座坐标或它们推导出的世界坐标诊断。
`information_sources` 明确列出这些来源和不可用项。

## 模型能看到什么

| 字段 | 含义 |
| --- | --- |
| `waypoint_index`、`waypoint_role` | 从 0 开始的失败点，区分中间途经点和最终目标 |
| `requested_grasp_pose` | 该点原始的夹持中心位置和 `angles_deg`，仅抓取接口提供 |
| `requested_ee_pose` | 工具偏移转换后的实际 link6 目标；坐标系见 `frame_id` |
| `solver_summary` | 各阶段调用数、原生求解状态码、成功/可行候选数、返回候选中的最小位置与姿态误差；这些是求解器结果，不是实测接触 |
| `candidates` | 每个 IK/轨迹优化阶段保留一个候选，列出阶段调用索引、seed、同一候选的误差及末端关节值 |
| `constraint_witness` | 原生配置中的关节限位、动态限位及机器人自碰撞模型检查结果；诊断不可用时明确标注 |
| `cause_status` | 当前为 `undetermined`；阶段及候选观察不足以证明唯一根因 |

例如抓取中心为 `[-0.22,-0.08,1.08]`、`angles_deg=[0,0,0]` 的中间点，
反馈会明确标为 `intermediate`，并显示 link6 目标高度 `1.2301 m`。
这能提醒模型修改实际失败的中间点，而不是误认为最终抓取点已经测试失败。

候选从每次 solver 返回时立即复制，避免 CUDA graph 后续覆盖结果。选择标准是返回
seed 中最小的最大归一化末端残差；不同残差的独立最小值可能属于不同 seed，只有
`candidates` 内的位置、姿态、关节及 witness 才是同一个候选。`stage_call_index`
是从 0 开始的该阶段调用编号，不能作为跨阶段共用的规划重试编号。
`selection_normalization` 区分原生 solver 容差和仅用于排序的诊断参考值。

原生 `js_solution` 可以包含已锁定的夹爪关节，并重新排列列顺序。反馈按 native
active joint names 严格映射，排除米制夹爪值；名称缺失或形状不匹配时只报告可确认的
残差，不构造关节诊断。速度、加速度和 jerk 同样取自该 seed 的原生衍生状态，
不对位置做差分推测。

## 关节和碰撞诊断

关节位置超限记录给出名称、轨迹采样点、位置、上下限、最近限位和余量。离限位
不超过 `0.05 rad` 的关节单独报告为 `joints_near_limits`，邻限本身不是失败原因。
动态超限给出对应关节、采样点、原生速度/加速度/jerk 数值及限值。

自碰撞检查只使用配置的机器人碰撞球、pair/padding。记录给出两条连杆、球索引、
球中心和球模型重叠量，每类保留至多三项最严重观察，并报告省略数量。
诊断只做候选 FK 和自碰撞几何计算，不重规划、不推进物理、不读取环境模型。

所有候选 FK 与碰撞位置使用 `planner_base`。当前没有实机世界坐标标定来源，
因此不读取 `robot.entity_origin_pose`，不提供 `T_world_planner_base`、候选世界
末端位姿或世界残差向量。请求的 `requested_ee_pose` 仍保留调用者指定的原坐标系，
不能与 `planner_base` 的候选坐标直接相减。

同样，不读取 `planner.scene_model` 中由仿真配置生成的静态桌面，不输出静态障碍
ID、表面点、碰撞位置或穿透量。`environment_geometry_unavailable_reason` 与
`world_frame_transform_unavailable_reason` 说明当前缺少实机环境/标定来源。
未来环境碰撞反馈必须来自实机可获取的感知、标定或用户明确提供的几何信息；
不能用仿真配置替代该来源。

球模型重叠不是实际接触；没有找到离散样本重叠也不能证明整条路径可行。
`solver_feasible=false` 是规划器约束检查的数值返回，可能受规划器内部场景模型
影响，不能单凭该布尔值断言实机碰撞或关节超限。原始 `planning_diagnostics`、
任务物体真值和评价器状态仍不进入模型返回。

## 运动预览的同一边界

原生运动预览也不输出 `planner_scene_model`，不读取仿真基座配置生成公开几何。
在公开测量契约充分时，由 named joint measurements、测得的 EE pose 和机器人
运动学模型推导 `T_world_base = T_world_ee @ inverse(T_base_ee)`，并标注
`derived_from_public_proprioception_and_robot_model`。这只重用已经公开的机器人
信息；不增加环境几何。公共测量或模型不足时 `preview_geometry.available=false`，
不提供基座变换，不用仿真数据回退。可执行计划仍可缓存，几何审查返回不可用。
环境风险估计只使用调用者提供的有观测依据的代理几何或重建场景。

## 验证

CPU 回归涵盖 solver hooks、buffer 复制、seed/name 对齐、实机来源边界、部分执行、
关节与球模型检查、不可用数据降级及各返回 profile 的反馈保存。

使用空闲 GPU 和新的目录运行原生端到端探针（`scripts/motion_failure_probe.py` 驱动已删除的 `robot_move_grasp`，已于 2026-10 删除，以下命令仅作历史记录）：

```bash
python scripts/motion_failure_probe.py \
  --config /path/to/working-config.json \
  --run-dir /path/to/fresh-validation-directory \
  --gpu 1
```

探针请求原记录的中间点、直接目标和一个远距离不可达目标。前两个请求可能因
随机规划产生不同结果；最后一个必须失败、零执行并返回原生候选诊断。
它检查模型实际收到的回执及 Store 持久化，不调用模型，也不评估任务成功率。
