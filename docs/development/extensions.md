# 扩展技能、插件与工具

扩展从使用场景和权威边界出发，先确认已有工具是否能组合完成，再决定修改位置。[AGENTS.md](../../AGENTS.md)是开发规则入口，[架构](architecture.md)解释所有权，[ToolSpec 指南](tool-plugins.md)给出可执行模板。

## 先选择合适的层

| 要做的内容 | 推荐入口 | 必须保留的约束 |
| --- | --- | --- |
| 改进模型的操作经验、适用条件、失败恢复 | `task_notes.py`、`operation_memory.py` 和 [Task Notes](task-notes.md) | 标明条件、证据、反例；可复用方法不夹带评测答案或隐藏状态。跨任务共享记录使用已有 scope。 |
| 多步反馈或数值计算程序 | `program_run` / `auriga_client.py`，参照 [程序语义](program-result-semantics.md) | 通过 broker 调工具，默认物理审核；stdout 不作执行证据，不能嵌套启动程序绕过权限。 |
| 新的通用计算、查询或物理能力 | `auriga/runtime/tools/plugins/` 中的 ToolSpec，应用处显式注入 | 同一 schema/role/effect/availability、typed 输出及受限 context；见下方流程。 |
| 新动作表达、后端或规划实现 | [统一动作](unified-actions.md)与[机器人后端边界](robot-backend-boundaries.md) | 保持唯一执行入口、真实能力声明、缓存轨迹、身份/预算/停止/不确定性语义。 |
| 新机械臂、仿真平台或真机环境 | [机械臂与环境接入](robot-environment-integration.md) | 实现按职责分离，运行必须匹配已支持的环境/型号组合；资料注册不代替控制适配。 |
| 历史 manipulation/depth skill 维护 | `skills.py`（已删除）、`depth_skills.py`（仅保留 `depth_query` 的 `skill_help` 目录）、相应测试 | 这些是历史模板与动作辅助库，不是新 actor 的独立工具目录；不得顺便恢复旧公开入口。 |
| 给开发 agent 的说明或流程文档 | 根 `AGENTS.md`、本目录的专题文档 | 解释触发条件、读哪些契约、修改落点、验证与交付；主 Codex 不继承宿主 skills、MCP 或插件；默认不启用 OS 沙箱，开启与关闭隔离时的权限见[配置说明](configuration.md#代码执行沙箱)；它们与 Harness ToolSpec 插件分开管理。 |

仓库当前没有通用 `SKILL.md` 发现器，也没有外部插件自动安装框架。开发说明优先维护上述入口；确需随宿主发行 agent skill 时，单独明确宿主格式、资源路径和安装方式，并复用这里的契约，不在运行时臆造新 loader。

## 一个工具的完整开发流程

1. 搜索相似 handler、全部调用者和已有测试。确定输入、输出 kind、读取的正式证据、副作用与失败行为。纯计算优先留在现有领域模块，公开 handler 保持薄层。
2. 按[只读工具模板](tool-plugins.md#新增一个只读工具)写 ToolSpec，显式声明 `roles`、`effect`、`api_version` 和 `output_schemas`。输入拒绝多余字段、不合法类型和非有限数；跨字段或当前观察校验留给已有语义 owner。
3. 在组装处通过 `Workbench(..., tool_specs=[...])` 注册。新内建工具同步现有 definitions/handlers/builtins。不要在 Harness、程序 broker、目录分别加同名分支。工具缺能力时目录和调用使用相同不可用原因。
4. 每个模型可见字段都应有判断或下一次调用所需的用途；非必要元信息在 producer 处删除，哈希只留内部归档，必要对象引用交共享展示层生成短 ID。handler 返回 `ToolOutput(...).response()` 或明确的 `ToolResponse`。结构化事实放 `data`，可压缩的说明/图片放 `content`。业务 ID 使用已有 run 内引用，不能用字符串解析猜结果类型。
5. 补两端回归：真实 `Harness.handle_tool` 与程序 broker；broker 的权限和审核不随沙箱开关取消。断言非法输入不调用 handler，不产生动作；权限/禁用/能力不足在入口拒绝；输出失败保留真实执行事实。
6. 更新说明、正反例和测试指针，按[验证指南](validation.md)执行受影响检查。输入或结果语义不兼容要升级 API version；schema hash 只标结构，不能代替语义版本。

`tests/test_tool_registry.py` 已包含只读插件两端调用、物理授权、单写入者与幂等、输出违规和 actor provenance 过滤的可运行示例。优先扩充这些测试，不另写一个演示框架。

## 物理插件的额外要求

只有 `effect="physical"` 的调用拿到 `context.execute`。它绑定 correlation 并产生确定的 action ID；将符合已有动作契约的请求传给它，由 Harness 检查身份、观察版本、预算和不可变内容。不要自行保存 receipt、伪造 completed 或绕过后端的 stop acknowledgement。

程序中的额外物理插件需要本次 `program_run` 明确 `allow_direct_actions=true`；内建 `robot_action` / `robot_wait` 使用现有暂停审批流程。若扩展暂不支持程序审核，仅开放 actor role。可信 Python 宿主仍可捕获其他服务，因此 ToolContext 不是恶意 Python 沙箱；代码评审必须确认 effect 声明和实际行为一致。

先构造持久化执行结果对应的 typed response，再组装可选图片、图表或文本。已执行之后的交付错误应按既有 output-contract 路径返回原回执并禁止自动重放；程序证据中的 action ID 可来自 `request.action_id`，不能假定 receipt 总有重复 ID。对幂等复用测试“同 ID 同内容只执行一次”和“同 ID 不同内容拒绝”，对检查点测试旧回执不改变最新计划。

## 经验内容与文档的写法

一条可用经验说明：适用的视觉/机器人条件、动作意图、需要观察什么、失败怎样识别、恢复或停止条件，以及支持它的运行/动作/观察证据。泛化建议和单次案例分开；不把一次成功写成所有布局保证。参数单位、坐标系、TCP、姿态顺序和夹爪语义沿用共享契约。

面向开发 agent 的文档要有真实代码路径、可运行命令和明确证据边界。当前规则留在长期指南；日期化实验/审查报告保留当时 revision 与结果，后续修复添加链接，不重写旧失败为通过。修改入口后同步 README 与文档索引，避免同一规范散落成互相冲突的版本。
