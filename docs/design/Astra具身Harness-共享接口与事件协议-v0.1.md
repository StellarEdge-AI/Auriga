# Astra 具身 Harness 共享接口与事件协议

> 2026-10-07 当前接口说明：新运行默认 `rgb_proprio_calibrated_v1`，保留显式旧 RGB／本体 profile，并允许显式选择 RGB-D 条件；三者不能混作同一评测条件。此前“正式 actor 仅 RGB／本体”的声明只适用于当时的历史运行。当前观察边界以 [正式观察与开发诊断边界](../development/actor-information-boundary.md)、工具接口以 [统一动作与程序审批](../development/unified-actions.md) 为准。本文件保留设计版本，不覆盖当前公开 schema。

> 本文保留方案制订时（2026-09-30）的设计约定，描述扩展目标；其中“待实现”属于当时状态。当前支持范围以代码中的注册表、capabilities、工具契约及测试为准。

版本 v0.1，2026 年 9 月 30 日。状态：待实现的接口设计，供主 harness、局部场景服务和网页可视化分别开发。下列业务接口与事件类型由本项目定义，不是 Codex、RoboDojo 或 Isaac Sim 的原生 API。

**三个模块共享观察、动作和关联 ID；只有主 harness 的执行入口可以控制被测世界。** 局部场景服务输出测量和预测；网页展示事件并通过控制入口提交操作。两者都不能直接写 RoboDojo 的关节控制器。

配套设计：[主方案](./Astra具身Harness-RoboDojo实施方案与在线仿真探索-2026-09-30.md)、[局部场景独立开发方案](./局部场景与短时仿真-独立开发方案-2026-09-30.md)、[网页可视化方案](./Astra具身Harness-网页可视化与Codex接入方案-2026-09-30.md)。

## 1 所有权与传输

| 数据或行为 | 唯一写入者 | 消费者 |
|---|---|---|
| 被测环境观察与实际动作反馈 | RoboDojo adapter | 主 agent、许可的几何与场景服务、网页 |
| 被测环境动作 | 主 harness 执行网关 | 机器人运动后端 |
| Codex 运行事件 | app-server，网关原样存档 | 网页、研究者 |
| 事件接收序号与索引 | 主 harness 事件存储 | 网页、回放、离线工具 |
| 自建场景及版本 | 场景服务 | Astra、内部仿真 worker、网页 |
| 内部试验结果 | 独立仿真 worker | 场景服务、Astra、网页 |
| 权威任务分数与隐藏真值 | 隔离 evaluator | 研究者的评测视图，不进入 actor 工具 |

业务请求使用版本化 HTTP JSON；耗时请求返回 job，事件通过网关 SSE 推送网页。原始图片、视频、网格和轨迹数组走文件或对象存储引用，不放入每条事件 JSON。一个 run 可先用 SQLite WAL 加文件目录实现，无需引入消息中间件。

actor-facing 服务、observer-only 视图和 evaluator 日志的权限分别配置。研究者看过的全局视图不自动成为模型输入。相机标定是否可用由 run 的观察条件声明，不由某个工具自行追加。

## 2 标识与时间

| 字段 | 含义 |
|---|---|
| `run_id` | 一次实验运行或交互任务，包括配置与所有子记录 |
| `episode_id` | 一次环境初始化后的任务尝试 |
| `env_instance_id` | 承载该环境的实例；不同并行环境不同值 |
| `env_generation` | 实例重置或恢复时递增，防止旧请求作用于新场景 |
| `observation_id` | 一份不可变的观察包 |
| `action_id` | 一份不可变动作候选/请求的身份，贯穿其规划、试验和执行；是否提交到 observed 世界由独立提交记录及事件证明 |
| `scene_version` | 一份不可变的自建场景修订版本，不只是最新 head 名称 |
| `snapshot_id` | 一次内部试验所冻结的场景、机器人状态和参数组合 |
| `branch_id` | 内部仿真分支，拥有独立的内部时间轴 |
| `job_id` | 异步构建、预览、rollout 等工作 |
| `thread_id / turn_id / item_id` | 来自 Codex 的标识，在适配层由原生 camelCase 映射 |
| `tool_call_id` | 运行时调用关联值；没有原生值时由桥接层生成，并标明来源 |

`world_kind` 只用 `observed` 和 `internal`。前者表示本实验中的被测世界，即当前 RoboDojo；不意味着真实硬件。后者表示由观察构建的内部场景。测试夹具另带 `data_origin=synthetic_fixture`，不能伪装成真实运行。

同一个动作候选可在多个 internal branch 中试验，运行实例用 `(branch_id, job_id, action_id)` 区分。它通过内部预演不产生 observed 的 `action.requested`。主 agent 决定提交时，仍需调用执行入口并检查当前状态；若修改观察绑定或目标等内容，就生成新的 action_id，同时记录其候选来源，不能复用原 ID 改内容。

保留四类时间：事件发生墙钟、网关接收时间、环境步与模拟时间、媒体 PTS。各值附时钟域；未同步的两个主机时间不能直接计算运动延迟。内部分支时间从自身快照起点计，不能和被测世界同名 `sim_time_s` 混合。

对齐优先用 ID、环境代次、动作执行区间和 frame/step 映射。墙钟相近只能作近似检索，不能建立唯一动作归属。一个 agent turn 可以对应多个动作，一个动作也可以有规划、执行和回看等多个 item。

`sim_step` 必须伴随 `step_domain`，明确它表示 adapter 控制步还是 physics 步，并记录对应 dt 或时间映射；policy turn、控制步、渲染帧和 PhysX 子步不能互换。RoboDojo 的 `take_action_cnt` 只按已核实的环境计数语义记录，不宣称它就是物理子步。未采集的计数和时间保持 null，不能从视频 fps 推算。下面示意选用 `step_domain=adapter_control_step`。

## 3 EventEnvelope

事件是追加记录，完成后不改旧事件。修正通过新的状态或 reconciliation 事件表达。除实时文本增量外，事件应能由消费者重复应用而不重复产生副作用。

```json
{
  "schema_version": "0.1",
  "event_id": "evt-0083",
  "run_id": "run-demo",
  "episode_id": "ep-001",
  "env_instance_id": "env-01",
  "env_generation": 3,
  "world_kind": "observed",
  "source": "execution_gateway",
  "source_instance_id": "gateway-boot-02",
  "source_seq": 19,
  "server_seq": 83,
  "type": "action.started",
  "occurred_at": "2026-09-30T08:00:00.000Z",
  "received_at": "2026-09-30T08:00:00.015Z",
  "clock_domain": "env-01/generation-3",
  "sim_step": 420,
  "step_domain": "adapter_control_step",
  "sim_time_s": 16.8,
  "correlation": {
    "thread_id": "thr-demo",
    "turn_id": "turn-04",
    "item_id": "item-12",
    "tool_call_id": "tool-07",
    "observation_id": "obs-031",
    "action_id": "action-009"
  },
  "payload": {
    "request_ref": "artifact:action-009-request",
    "planned_trace_ref": "artifact:action-009-plan"
  }
}
```

`server_seq` 是事件存储在一个 run 内分配的递增接收序号，不是物理事件发生次序。`source_seq` 只在 source 的同一实例生命周期内有序；source 重启增加 `source_instance_id`，不能复用旧序号空间。

没有对应动作、观察或 Codex item 时省略相应 correlation 字段，不能捏造映射。`occurred_at`、`sim_step` 等未知时允许 null，并附原因；不填零。大 payload 用 `payload_ref`，其字段结构由事件类型定义。

核心业务事件：`observation.available`、`action.requested`、`action.accepted`、`action.rejected`、`action.started`、`action.progress`、`action.completed`、`action.failed`、`action.stop_requested`、`action.stopped`、`action.status_unknown`、`scene.updated`、`scene.invalidated`、`job.started`、`job.progress`、`job.completed`、`job.failed`、`job.cancelled`、`job.expired`。`job.queued` 等细化事件可增补，但不能改变终态含义。`action.status_unknown` 是待对账状态，不是已确认终态；恢复后追加状态更新及对账依据。

Codex 原生通知和 server request 保存在独立原始流；标准事件仅增加索引和关联，不删除原始 method、params、JSON-RPC request ID 或未知事件字段。原生事件中的 `item/completed` 不自动转成业务事件 `action.completed`。

## 4 ObservationBundle

这是场景团队可离线消费的输入单位。完整数据、索引与媒体引用不可变；后续校正产生新 observation 或 calibration 版本。

```json
{
  "schema_version": "0.1",
  "run_id": "run-demo",
  "episode_id": "ep-001",
  "env_instance_id": "env-01",
  "env_generation": 3,
  "world_kind": "observed",
  "observation_id": "obs-031",
  "sim_step": 419,
  "step_domain": "adapter_control_step",
  "sim_time_s": 16.76,
  "clock_domain": "env-01/generation-3",
  "observation_profile": "rgb_proprio_calibrated_v1",
  "robot_model_id": "arx-x5-pinned",
  "calibration_id": "cal-002",
  "cameras": [
    {
      "camera_id": "left_wrist",
      "frame_id": "left-wrist-419",
      "rgb_ref": "artifact:rgb-left-wrist-419",
      "width": 640,
      "height": 480,
      "pixel_transform": "identity",
      "calibration_ref": "artifact:cal-left-wrist-002",
      "pose_sample_ref": "artifact:camera-pose-419",
      "capture_time_ref": "artifact:capture-left-wrist-419"
    }
  ],
  "robot_state_ref": "artifact:robot-state-419",
  "last_action_id": "action-008"
}
```

上例是协议示意，不声称 RoboDojo 采用该分辨率或已经输出这些字段。一个包可以包含多相机，逐相机保存采样关系和是否对齐；不同时间帧不能假装同步双目。

“模型所见”由真正发往模型请求的 image ref/hash 及预处理变换证明。展示用 JPEG 可以单独派生并引用原始图像，不能因视觉相似就称两者字节一致。中间控制帧另带 `delivered_to_actor=false`；被记录不代表已经进入模型上下文。

`robot_state_ref` 保存关节名字与 q、可用速度、EE 实际反馈或 FK 结果，以及夹爪命令和可用反馈。每类值带 `source_kind=measurement|fk_from_measurement|command|estimate`。当前 RoboDojo 某些夹爪字段来自先前命令，按 command 记录，不转成接触反馈。

标定文件包含 K、畸变模型、图像尺寸、optical frame 轴约定、外参方向和来源。定义 `T_A_B` 为将 B 系变换到 A 系。米与弧度为内部单位；四元数用 `xyzw`，归一化。裁剪与缩放携带对应像素变换。

主参考条件不允许的标定、深度或物体状态字段必须缺省，而不是传了字段后仅要求模型“不要用”。输入校验发现与观察条件不符时拒绝导入并报告字段；合成测试与 oracle 测试使用不同 manifest。

## 5 EEAction 与动作回执

模型工具可接受便利姿态表示，工具服务先转换为下面的共享表示并存档。原始模型参数同时保留，便于审查坐标转换。

```json
{
  "schema_version": "0.1",
  "action_id": "action-009",
  "run_id": "run-demo",
  "episode_id": "ep-001",
  "env_instance_id": "env-01",
  "env_generation": 3,
  "based_on_observation_id": "obs-031",
  "frame_id": "robot_base",
  "tcp_config_id": "arx-grasp-center-v1",
  "mode": "absolute",
  "path": {
    "kind": "linear",
    "waypoints": [
      {"left": {"position_m": [0.35, 0.12, 0.22], "quaternion_xyzw": [0, 1, 0, 0]}}
    ],
    "orientation": "slerp",
    "blend_radius_m": 0.0,
    "position_tolerance_m": 0.005
  },
  "gripper_events": [],
  "limits_profile": "simulation-slow-v1",
  "timeout_s": 10.0
}
```

此处坐标、阈值和时间只是格式例子。动作起点必须来自提交时反馈，不能使用请求中的臆造起点。增量工具在接收时按照明确的 frame 规则转换，保存转换前后参数。

`path.kind` 可为 `backend_free`、`linear`、`polyline`、`arc`、`spline`。能力清单声明当前 adapter 实际支持什么；不支持的类型返回 `unsupported_capability`，不静默改成自由规划。夹爪事件定义阶段或路径位置，但只有后端支持时才允许该模式。原参考手臂后夹爪的时序需准确表达，不宣称连续同步已实现。

本协议 v0.1 重点支持有限 EE 路径。调用外部 action model 的 native chunk 或生成控制程序时，增加带版本的独立 action 类型；不得把任意 joint chunk 填入 EE waypoints。内部仿真若接受程序，使用不可变程序包及同样的受控 EE client，不赋予被测世界凭据。

回执分开记录请求、规划、下发和实际反馈轨迹，保留各自时间轴、部分执行范围、终态和新 observation 引用。不存在真实反馈时明确 unavailable。一次 motion 完成不是抓取成功，也不是任务成功。

每个动作终态应给出 `result_observation_id` 或明确的采集失败原因；episode 的最后一个动作也需要终态观察，不能依赖下一轮输入。逐步反馈按实际采样保存，只有边界状态时不把两点插值称为完整实测轨迹。

幂等键为 `(run_id, episode_id, env_generation, action_id)`。相同键和相同内容返回原状态，相同键但内容不同返回冲突；网络重试不能另造动作。若后端崩溃无法判断是否执行，返回 unknown，先对账，不自动重发。

## 6 SceneSnapshot 与内部试验引用

场景服务拥有 scene head，消费者只引用不可变版本与快照。最低字段如下：

| 字段 | 必须说明的语义 |
|---|---|
| 场景身份 | run、episode、env instance、generation、scene_version、snapshot_id |
| 证据边界 | 使用的 observation IDs、calibration ID、robot model ID、观察条件 |
| 场景资产 | 自建几何、碰撞体、物体 pose、未知区域、物理参数及其来源 |
| 初始状态 | 从许可观察得到的 q、qdot 可用性、夹爪语义、对象速度假设 |
| 执行设置 | adapter、controller、IK/planner、物理引擎与参数版本 |
| 内容摘要 | manifest 与所引用资产的 digest，禁止通过可变 latest URL 替换内容 |
| 质量与局限 | 退化、缺失信息、初始化不一致、接触状态假设 |

Snapshot 是从观察构建的初始条件，不是被测世界全状态克隆。隐藏接触力、求解器 warm-start、物体速度等不可由 RGB 唯一恢复；其缺失和初始化方式必须进入 manifest。

RolloutRequest 至少包含 snapshot_id、候选 EEAction 或不可变程序引用、参数假设集、seed、时间与步数预算、需要保存的媒体。执行只作用于 internal 分支，不能接受“顺便提交到 observed 世界”的选项。

RolloutResult 带 branch_id、job_id、所用 snapshot 和候选 digest、实际完成区间、内部轨迹与媒体、查询量、错误和限制。在参数集合中的完成比例只描述该集合，不能直接写成现实成功概率。未来执行某个候选时，由主 agent 单独调用主执行入口，并重新核对 observation 与 env_generation。

## 7 ArtifactRef 与媒体同步

引用记录至少包含 `artifact_id`、`kind`、`mime_type`、`byte_size`、内容 hash、逻辑存储键、访问范围。URL 由网关按当前权限解析，文档中的 `artifact:` 是逻辑引用，不是可直接访问的地址。

图像映射为 camera_id、frame_id、observation_id、env_generation、sim_step 与 capture 时钟。视频另存 frame/PTS 到这些字段的映射；对齐使用映射表，不能假设录制视频第一帧就是环境第零步。变帧率、丢帧、暂停录制都保留。

网页点击 action 时，默认选择该 action 所引用的模型输入观察，以及实际执行区间内可用的媒体。最近但不完全对应的帧标明时间或步差。找不到媒体时显示缺失，不沿用上一动作画面假装匹配。

内部场景、被测世界和特权诊断各有不同访问标记。Rerun 录制、视频和日志下载继承该标记，不能因导出到网页而绕过 actor 的观察边界。

## 8 网页事件恢复与控制语义

本项目拟提供 `GET /v1/runs/{run_id}/events?after={server_seq}` 的 SSE 与持久回放接口，SSE ID 使用 server_seq。连接恢复从游标读取，客户端按 event_id 去重。游标早于保留范围时返回明确的 gap/snapshot 提示，再加载最新 run snapshot，不无限重试旧游标。

Codex 原生连接断开和网页 SSE 断开是两种故障。网关可以从自己的日志恢复网页；若网关漏收上游 delta，需要重新读取 Codex 保存的 thread/item 并标记恢复范围，不能宣称所有逐 token 过程完整可重放。

展示暂停只暂停浏览；停止 agent turn、停止机器人动作、暂停仿真时钟是三种不同操作。“停止运行”由后端先禁止新动作，再请求中断 agent 和当前执行，等待动作停止确认后报告结果；未确认时显示 unknown。任何 UI 断线、关闭标签或取消模型请求都不自动证明机器人已停止。

审批和用户输入使用原生待决请求身份，经网关定向回复。重复点击幂等，已过期请求不可当新请求重发。同一 run 的写入控制权由服务端持有，不能只禁用浏览器按钮解决多窗口竞争。

原生终端显示另用 PTY 字节流，不取代 EventEnvelope。run manifest 绑定同一 app-server 实例、thread_id 与 terminal_session_id；terminal session 重建不自动创建新 thread。网页刷新仅重新附着已有 PTY，并恢复终端画面/scrollback。终端只读由网关禁止转发输入实现，不把 CLI 的 `--remote` 当作只读权限。运行协调器与 TUI 不能同时发起 turn、回复审批或取消；手动接管须明确移交输入权。历史节点选择只改变检查器/媒体回放，不能重写实时终端显示。

## 9 独立开发所需夹具和兼容性验收

提供一份无模型调用的夹具包：run manifest、数个 ObservationBundle、标定、机器人状态、两个 EEAction、一份 scene snapshot、raw Codex 事件样例、业务事件、图像与轨迹。所有数据带 synthetic_fixture 标记及 hash。

场景团队只用观察和动作夹具完成构建与 rollout adapter；网页团队只用事件与媒体夹具完成节点联动、回放、断线恢复；主 harness 团队用 mock 工具先验证 correlation 链。最后替换成真实 RoboDojo 与 Codex 输出，不改公共字段含义。

必须覆盖：reset 后旧动作拒绝、重复 action 不重复执行、来源缺失明确显示、图像延迟、不支持路径拒绝、scene head 更新不改变已运行 snapshot、internal 结果不会触发 observed 动作、Codex turn 结束与机器人运动结束不同时发生、SSE 断线去重以及上游丢事件后的不完整回放。

增加字段采用可选字段；删除字段、改变单位、坐标或状态含义必须提升协议版本。项目固定一组可协同的版本，而非不同团队各自追随 latest。此文定义接口与验收目标，不代表上述夹具、服务和测试已经交付。
