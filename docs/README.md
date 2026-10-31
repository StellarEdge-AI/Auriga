# 文档索引

第一次了解项目先读[主页 README](../README.md)（[English](../README.md) / [中文](../README.zh-CN.md)）。下表按用途分组；开发规则以 [AGENTS.md](../AGENTS.md) 为准，当前接口以源码中的工具注册表和契约测试为准。`docs/development/` 与 `docs/design/` 以中文为主，`docs/*.md` 的感知功能说明为英文。

## 入门与运行

| 文档 | 适合查阅的问题 |
| --- | --- |
| [运行指南](development/running.md) | 如何准备环境、启动任务、交互与停止、恢复会话、回放和运行批量实验？ |
| [RoboDojo 运行环境](development/robodojo-setup.md) | RoboDojo / IsaacLab 固定哪个版本、要打哪些补丁，Auriga 怎样找到它？ |
| [统一配置](development/configuration.md) | 模型、机器路径、沙箱开关、观察条件、工具与录像如何配置？ |
| [实验库与常驻网页](development/experiment-library.md) | 运行如何归档、检索、直播与回放，可选的实验登记文件是什么格式？ |
| [高清离线重放](development/hd-replay.md) | 如何从归档动作重新仿真并导出高清视频？ |
| [实验完成回调](development/experiment-completion-hook.md) | 队列结束后如何通知调用方？ |
| [Benchmark OOM 重试](development/benchmark-oom-retries.md) | 队列何时为原生 GPU OOM 安排新 episode？ |
| [Token 与费用记录](development/token-accounting.md) | 用量怎样计量和换算费用？ |
| [网页开发与验证](../web/README.md) | 如何构建网页、联测终端、事件与视频回放？ |

## 参与开发

| 文档 | 用途 |
| --- | --- |
| [AGENTS.md](../AGENTS.md) | 开发者与 coding agent 必读：信息与动作边界、协作、实验与交付要求。 |
| [CONTRIBUTING.md](../CONTRIBUTING.md) | 环境、检查命令与 PR 要求（英文）。 |
| [实现架构](development/architecture.md) | 模块职责、调用路径、状态所有权与包边界。 |
| [扩展开发](development/extensions.md) | 选择技能、经验、插件、工具或后端的正确扩展层。 |
| [工具注册与扩展](development/tool-plugins.md) | schema、role/effect、窄 context、typed 输出和 actor/program 两端回归。 |
| [工具返回与按需详情](development/tool-output-contract.md)、[成本摘要与输入错误](development/tool-input-output.md) | 工具回包结构、按需帮助与可操作的输入诊断。 |
| [机械臂与环境接入](development/robot-environment-integration.md) | 新机械臂要补哪些资料、如何适配仿真/真机并校验支持组合。 |
| [机器人后端边界](development/robot-backend-boundaries.md) | 纯验证、动作编译、进程通信与原生执行分别放在哪里。 |
| [验证与发布](development/validation.md) | 按风险选择测试、软件与真实任务证据、版本发布流程。 |
| [版本记录](../CHANGELOG.md) | 各版本的软件变更。 |

## 接口与行为

| 文档 | 内容 |
| --- | --- |
| [统一动作与程序审批](development/unified-actions.md) | 任务计划、动作批次、双臂依赖、程序审批和操作经验。 |
| [程序回包与任务结果](development/program-result-semantics.md) | 程序状态、动作完成与原生任务结果各自的含义与预算。 |
| [机器人资料接口](development/robot-information.md) | 机器人结构、URDF、原生 mesh、标定和物理资料。 |
| [正式观察与开发诊断边界](development/actor-information-boundary.md) | 模型和辅助工具可以读取哪些数据。 |
| [图像传输与存储](development/image-pipeline.md) | 模型图片、归档图片、压缩、复用和视频。 |
| [自适应相机反馈](development/adaptive-view-selection.md) | 动作反馈附带哪些相机视角。 |
| [双臂同步动作](development/dual-arm-motion.md)、[移动中的夹爪命令](development/synchronized-gripper.md) | 双臂并行、交接与随移动执行的夹爪动作。 |
| [按动作控制速度](development/motion-speed.md) | 速度比例的含义、范围与默认值。 |
| [运动规划失败反馈](development/motion-failure-feedback.md) | 规划失败、候选轨迹诊断和部分执行反馈。 |
| [关节行程准入与轨迹证据](development/motion-safety-and-trajectory-evidence.md) | 轨迹约束、检查范围和执行证据的含义。 |
| [运动成本与接触参考叠加图](development/motion-cost-and-contact-overlay.md) | 预览成本摘要与 `robot_contact_overlay`。 |
| [模型主导操作](development/model-led-operation.md) | 模型请求传输、风险参考与辅助核验的分工。 |
| [回合完成后的进度监督](development/actor-progress.md) | benchmark 回合结束时的有界续跑策略。 |
| [腕部相机视角解算](development/robotic-view.md)、[双目极线引导](development/stereo-guide.md) | 两个默认关闭的可选几何工具。 |
| [RGB-D 数值工具](development/rgbd-numeric-tools.md) | 显式深度观察条件下的数据、限制和配置。 |
| [π0.5 推理服务](development/pi05-inference-service.md) | 可选局部策略的接口与边界（默认关闭）。 |
| [官方任务说明与得分表](development/public-task-rules.md) | 42 个官方任务页面原文的冻结、注入方式与旧运行兼容。 |
| [默认 Task Notes](development/task-notes.md) | 默认注入的通用操作经验、版本与开关。 |

## 感知功能说明

| 文档 | 内容 |
| --- | --- |
| [obs3d](obs3d.md) | 默认观察中附带的三维场景信息。 |
| [object_table](object_table.md)、[scene3d](scene3d.md)、[vggt_height](vggt_height.md) | 对象表、点云与 `pixel_plane` 物体高度估计；服务端见 [VGGT 服务](../services/vggt_height/README.md)。 |
| [held_perception](held_perception.md) | 持物状态感知。 |
| [pose_skill](pose_skill.md) | 姿态目标与抓取几何参考。 |
| [lean_context](lean_context.md) | 保持提示缓存的历史投影（默认关闭）。 |
| [scene_truth](scene_truth.md) | 只用于离线几何评分的真值记录，不进入模型。 |

## 设计文档

设计文档保留最初的设计版本，部分内容是扩展目标；当前接口已收敛为统一动作与程序审批，较早文档中的独立运动、模板、skill 及 `scene_*` 工具名不代表当前公开接口。

| 文档 | 内容 |
| --- | --- |
| [主体设计方案](design/Astra具身Harness-RoboDojo实施方案与在线仿真探索-2026-09-30.md) | 设计理念、模型与执行器分工、观察边界、程序与扩展方向。 |
| [共享接口与事件协议](design/Astra具身Harness-共享接口与事件协议-v0.1.md) | 运行与动作身份、ObservationBundle、EEAction、事件、快照和媒体引用。 |
| [网页与 Codex 接入方案](design/Astra具身Harness-网页可视化与Codex接入方案-2026-09-30.md) | 原生会话、终端、机械臂画面及历史检查的关联。 |
| [独立场景与短时仿真设计](design/局部场景与短时仿真-独立开发方案-2026-09-30.md) | 从许可观察构建场景、查询与快照；完整物理 rollout 尚未实现。 |

当前 Cortex 工具目录与调用校验来自 [`runtime/tools/builtins.py`](../auriga/runtime/tools/builtins.py) 装配的注册表，输入 schema 来自 [`runtime/tools/definitions.py`](../auriga/runtime/tools/definitions.py) 及其引用的领域声明；实际可用集合受配置、观察条件、后端能力和服务就绪状态约束。模型可在支持的查询工具中使用 `request={"kind":"help"}` 获取字段说明。
