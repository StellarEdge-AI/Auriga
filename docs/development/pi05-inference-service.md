# Auriga 接入 π0.5 推理服务

π0.5 在 Auriga 中有两条互相独立的路径，推理都放在 Harness 之外的独立环境里：Harness 继续使用 `Auriga` 环境，机器人 worker 继续使用 `RoboDojo` 环境，策略推理另用自己的环境，各自管理依赖。RoboDojo 与可选 OpenPI 环境的准备见 [RoboDojo 运行环境](robodojo-setup.md)。

## ARX X5 局部策略（`robot_policy_step`）

`features.pi05=true` 时，`robot_policy_step` 使用 RoboDojo 官方 ARX X5 checkpoint（train config `pi05_base_aloha_full_sim_arx-x5_seed_0`，来源与逐文件校验值见 [`pi05_arx_manifest.json`](../../auriga/runtime/policy/policy_workers/pi05_arx_manifest.json)），默认关闭。实现位于 [`action_policy.py`](../../auriga/runtime/policy/action_policy.py)：

- 策略进程只读取已保存的正式 actor 观察：三台相机（`cam_head`、`cam_left_wrist`、`cam_right_wrist`）的同帧原始 RGB、两臂实测 6 关节角及最近一次归一化夹爪命令（共 14 维），以及本局公共 instruction。观察、相机与状态的步数和时钟域必须一致。
- 返回值必须是后处理后的 50 × 14 有限关节动作块；Harness 只执行调用指定的前缀（`prefix` 为 1–50 行，默认 5 行，25 Hz 下为 0.20 秒仿真时间，每行消耗一个原生步），动作仍经唯一的 `Harness.execute` 入口。
- 未配置 `policy.server_url` 时，Harness 在 `policy.python`（`AURIGA_PI05_PYTHON`）指定的 OpenPI 解释器中启动 [`pi05_arx_inference.py`](../../auriga/runtime/policy/policy_workers/pi05_arx_inference.py)，加载 `policy.checkpoint`（`AURIGA_PI05_CHECKPOINT`）。配置 `policy.server_url`（`ws://` / `wss://`）时改用 [`pi05_arx_client.py`](../../auriga/runtime/policy/policy_workers/pi05_arx_client.py) 连接已有的 OpenPI WebSocket 服务；该客户端不加载模型、不检查权重、不执行动作，也不重试推理。

这条路径不使用下文通用 base 服务的 32 维 gRPC 输出。

## 通用 LeRobot π0.5 base 服务

另一种用法是单独运行 LeRobot 原生 gRPC 推理进程，供 Auriga 或其他本机 Python 客户端调用。推理环境（例如 mamba 环境 `lerobot-inference`）安装官方 PyPI LeRobot 0.6.1 的 `pi` 和 `async` extras；权重为 Hugging Face 的 `lerobot/pi05_base`，tokenizer 来自 PaliGemma（`paligemma-3b-pt-224`）。部署时应固定权重 revision 并核对 SHA-256；服务缓存与日志不放进 Git。

### Auriga 接入边界

- 本服务的客户端需要 `grpc`、LeRobot protobuf 和 Python 数据类。
  客户端应运行在推理环境中；不要把整套 LeRobot/Torch 依赖装回
  `Auriga`。需要连接 Harness 时可由该环境中的专用客户端进程调用服务，
  再与 Harness 交换已定义的观测和动作数据；通用 base gRPC 这条桥接尚未实现，
  当前 ARX 工具使用上文单独的 OpenPI 路径。
- 受控 actor 程序禁止建立网络连接。gRPC 调用应放在受信任的 Harness
  接入层或专用 worker，遵守已有隔离与控制边界。
- π0.5 base 输出是 32 维策略动作，不能直接作为本项目的 `EEAction`
  下发。先确认机器人的关节顺序、夹爪约定、动作单位、绝对/增量语义、
  归一化统计、动作块执行步数和控制频率，再按实际语义实现适配。
  所有真实动作仍应经过 Harness 的唯一动作写入入口。
- 相机和状态必须来自同一份许可观测，保留 observation/frame/环境身份；
  任务文字取本次公共 instruction，不引入隐藏评分或场景真值。

项目边界以 [共享接口与事件协议](../design/Astra具身Harness-共享接口与事件协议-v0.1.md) 为准。

### 调用协议

仓库不附带该服务的客户端示例。客户端使用 `grpc` 和 `lerobot.transport.services_pb2_grpc.AsyncInferenceStub`，原生协议顺序：

1. `Ready(Empty)`：开始一次客户端会话并清理队列。
2. `SendPolicyInstructions(PolicySetup)`：发送 pickle 编码的
   `lerobot.async_inference.helpers.RemotePolicyConfig`，指定权重路径、
   device、feature schema 和每块动作数；这一步加载模型。
3. `SendObservations(stream Observation)`：发送 pickle 编码的
   `TimedObservation`，用 `lerobot.transport.utils.send_bytes_in_chunks` 分块。
4. `GetActions(Empty)`：解码 `response.data`，得到 `list[TimedAction]`。

`lerobot/pi05_base` 的输入约定如下：

| 策略 feature | 原始观测字段 | 格式 |
| --- | --- | --- |
| `observation.images.base_0_rgb` | `base_0_rgb` | 224×224×3，HWC uint8 RGB |
| `observation.images.left_wrist_0_rgb` | `left_wrist_0_rgb` | 224×224×3，HWC uint8 RGB |
| `observation.images.right_wrist_0_rgb` | `right_wrist_0_rgb` | 224×224×3，HWC uint8 RGB |
| `observation.state` | feature schema 中 `names` 指定的 32 个标量字段 | 32 维 float32 |
| 任务文字 | `task` | 本次公共 instruction 字符串 |

状态字段可命名为 `state_0` 到 `state_31`。原生服务按 schema 的 `names`
从 raw observation 组装状态，不直接接收训练用的 batched tensor 字典。
相机名应与策略 feature 对齐，不能依赖 `rename_map` 修复任意相机名，因为
服务在 rename processor 之前就会查找策略图像配置。

该权重每块返回 50 个 32 维动作（去噪步数 10，模型 dtype float32）。
base checkpoint 的 normalizer/unnormalizer `features={}`，不含 RoboDojo
机器人的状态和动作统计。32 维只是该权重的输入输出配置，不证明已经适配
本项目机械臂的真实状态与动作空间。

一个服务实例按一个活动客户端使用，原生实现不隔离多个客户端的状态。
每次 `SendPolicyInstructions` 都会重新加载模型；同一配置只 setup 一次，
后续循环发送观测和获取动作。服务进程重启后，客户端需要重新 setup，模型才会加载。
RPC 使用 Python pickle，服务只应监听本机回环地址（例如 `127.0.0.1:50051`）；这里没有 HTTP/REST 接口。
`GetActions` 在上游捕获推理异常后可能返回空消息，客户端应检查
`response.data` 非空，并验证动作维度与有限值。新 episode 的首条观测应
显式设置 `must_go=True`，避免被上一个 episode 的相似状态过滤。

用零状态、黑图和合成提示词的调用只证明加载与推理可用，不构成 RoboDojo 的性能结果；单次延迟也不是吞吐量基准。任何模型性能比较还需要对齐 RoboDojo 任务、机器人接口、初始状态、执行步数与评分协议。
