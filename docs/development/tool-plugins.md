# 工具注册、执行边界与扩展

模型目录、actor 实际调用和程序 broker 由同一个 `ToolRegistry` 读取 `ToolSpec`。新增工具直接注册 handler，不必在 `Harness.dispatch`、Workbench 名单或程序 allowlist 添加分支。现有声明、公开 handler 和旧适配器分别位于 `auriga/runtime/tools/definitions.py`、`handlers.py`、`legacy.py`；`builtins.py` 将内建工具组合成运行时 registry。

每项声明包含 `api_version`（默认 `"1.0"`）、实际输入 JSON Schema、`output_schemas`、handler、允许的 role、副作用分类和可选的不可用原因函数。`output_schemas` 将每个允许的 `ToolResponse.kind` 映射到验证其 `data` 的 JSON Schema；多种结果共用一个工具时逐 kind 声明，不从展示文本猜类型。新 actor/program 插件必须显式提供这份声明。注册时检查输入、输出 schema 及各自根级 `examples` 正例，调用时检查 handler 的实际 typed 结果。版本必须是正整数 major 与非负整数 minor 组成的 `major.minor`。

当前 `ToolSpec` 对应一个 Codex native function：声明必须包含 `type="function"`、字符串 `name`、字符串 `description` 和 `inputSchema`。按原生 0.160 的实际 admission 检查，name 必须为 1..128 个 ASCII 字母、数字、`_` 或 `-`，不能用 `mcp` 或 `mcp__` 前缀。不能混入 legacy `namespace` / `exposeToContext` 字段；可选 `deferLoading` 必须为布尔值，省略或 false 表示直接发布，true 表示由 Codex 适配器放入 `auriga` namespace 按需发现。registry 和程序调用仍使用原函数名，不增加另一套 handler 路由。根级 singleton null 输入类型也由 native 拒绝。注册边界检查整个声明的 JSON 可表示性、这些 native 字段和已知 admission 约束，再按本地 JSON Schema 验证实际输入；没有引入 Responses `strict` 或 MCP 专属字段要求。原生后端会对输入 schema 做 lowering/compaction，本地 schema 校验不声称全部关键词在模型请求中原样保留。注册保存声明、输入/输出/legacy schema 和 roles 的独立快照；宿主改动原 `ToolSpec`、`spec()` / `validate()` 返回值、目录、manifest 或 `_specs` 检查副本都不会改写 live 契约。`validator_for()` 也返回独立 validator，不把共享缓存的可变 schema 暴露给调用者。调整已注册工具的行为或声明，应构建新的 registry 和相应会话。

参数含义、单位、默认值、数值范围和适用条件都属于工具契约。类型与硬约束写入 `inputSchema`；字段 `description` 解释其业务含义及调用条件。公共工具从同一份输入声明生成顶层 `description` 中的 input field guide，保留原生转换可能删除的字段说明与约束，不再以 5000 字节阈值决定是否备份。条件分支的说明须带上适用条件，不能把互斥分支的 required、类型和范围无条件合并。示例仍只从原有示例来源发布。实际交付以真实 bundled app-server 的 loopback Responses 测试逐字段核对，不能用 Python 目录里存在字段代替模型已收到字段。

geometry 的正式几何请求和 `depth_query` 的测量/target 请求发布各自真实字段类型。`geometry_query` 原有 RGB-D 兼容转发只列明确支持的 depth kind 并指向 `depth_query` 的完整契约，不再复制一份深度参数手册；兼容 payload 仍由同一 `DepthRuntime` 验证，不能借这个分支传入任意未知 kind 或绕过 RGB-D 可用性限制。

`geometry_query` 的 `pixel_plane` 分支接受单个 `observation_id`、`camera_id`、原图像素 `pixel:[u,v]`，以及必填的 `plane_origin_m:[x,y,z]` 和 `plane_normal:[nx,ny,nz]`。它返回条件交点 `point_m`（`status=conditional`，非实测深度）、沿平面法向 +1 m 偏移的三维敏感度向量 `d_point_d_normal_offset_m`，以及同帧其他已标定相机的跨相机重投影 `cross_camera_projections`。可选 `plane_origin_alt_m:[x,y,z]` 计算第二个同法向平面交点并返回 `alternate_point_m`、`alternate_ray_distance_m` 与 `assumption_delta_m`；可选 `target_camera_id` 在同一次调用中返回目标相机的投影叠加图。当双视角可见且可匹配时优先使用 `triangulate`，当单视角遮挡或目标位于已知支撑面时按需使用 `pixel_plane` 并核对跨相机投影。射线与单位法向量点积绝对值小于 0.01，或交点位于相机原点及后方时，返回 `unknown` 和 `value=null`。相关回归见 `tests/test_geometry.py`。

Input guide 随工具自身定义交付，不是额外系统消息或每轮注入的 prompt。`task_plan`、`robot_action`、`robot_observe`、`program_run`、`operation_memory`，以及历史使用频繁的 `geometry_query`、`history_view_observation`、`robot_wait`、`robot_contact_overlay`、`history_list_actions` 共 10 个工具直接可见；其余 10 个内建工具在各自声明中设置 `deferLoading=true`，由原生 `tool_search` 搜索后加载完整定义。实际目录仍按 role 和运行配置筛选；默认加载不使禁用或不可用工具重新出现。直接可见的工具按历史运行中的主模型调用频率及 case/task 覆盖选择，程序内部调用单独统计。搜索返回的 schema 仍经历原生简化，因此按需加载不能替代说明/约束保全。搜索不推进物理环境，但会带来工具交互和后续模型请求，模型请求沿用现有计数。调用继续由同一 registry 检查 role、availability、输入和输出；原生网络工具限制不变，没有新增搜索服务或配置开关。

同一冻结目录支持 native thread resume；改变 `deferLoading` 也属于目录变化，既有 `resume_tool_registry_conflict` 会在启动前拒绝混用。旧 flat 线程可用原目录恢复，不能将新版目录静默套用到旧线程。当前原生 SDK 的 `thread/resume` 不提供替换 `dynamicTools` 的参数。

`history_list_actions` 可选 `anchor="events"|"failures"|"gripper_commands"|"all"` 按需检索本局交互片段：返回顶层概览（`selected_count`、`episode_action_total`、`status_counts`、`execution_issue_count`、`gripper_command_count`、`evidence_gap_count`、`next_start`）及紧凑交互块（含 action identity、typed request/receipt 证据、`observation_ids.decision/execution_start/result`、从执行起点到结果帧的 `measured_delta`、夹爪命令位置与显式 `evidence_gaps`），并提供 `full_result` 入口按动作 ID 读取原始 request/receipt。省略 `anchor` 保留原分页索引与 `detail="full"` 完整列表语义；`detail="full"` 不允许同时传 `anchor` 或分页参数。实现见 [`history_output.py`](../../auriga/core/presentation/history_output.py)，回归见 `test_history_output` 和 `test_tool_registry`。

写法参考 [Codex 自带 shell 工具](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/handlers/shell_spec.rs)：`yield_time_ms` 的参数说明同时写明默认值、单位和范围；本项目安装版本的实际请求也保留这段文字。[MCP SDK 的字段说明测试](https://github.com/modelcontextprotocol/python-sdk/blob/main/tests/server/mcpserver/test_parameter_descriptions.py)检查 `list_tools` 返回的参数 schema 确实包含 description；其[约束参数测试](https://github.com/modelcontextprotocol/python-sdk/blob/main/tests/interaction/mcpserver/test_tools.py)由带 `ge`/`le` 的参数定义同时生成 schema 和执行校验。本项目复用现有 JSON Schema/registry 达到同样目的，不为此引入 MCP 服务或更换校验框架。动态接口另参考 [app-server 备用文档](https://www.codex-docs.com/docs/app-server)；当前版本差异以安装 SDK 和原生回环为准。

`catalog(runtime)` 与 `invoke(..., role=...)` 使用同一可用性判定；入口先拒绝禁用/不允许的工具，再做完整输入 schema 和请求相关能力检查，之后才调用 handler。未知参数、布尔冒充数字、非有限数值均不能进入公开 handler。底层语义校验仍负责观察版本、原生动作预算及缓存状态等跨字段约束。原生后端明确声明 `unified_action.available`；旧 adapter 缺该字段时保留 resampled 配置的兼容解释。新增后端应声明准确的 capability 并实现对应 port，不能把省略字段当作后端功能已验收。

`ProgramRunner(harness.program_services())` 只接收冻结的 `ProgramServices`：服务端绑定的工具校验/调用/effect、观察快照、活动动作与取消回调，以及该运行 Store；Runner 不持有 Harness、robot 或 registry。`actor` role 由 `Harness.handle_tool` 指定，`program` role 由 `ProgramRunner` 的服务端回调指定；调用者不能在 JSON 参数里提升 role。`Harness.dispatch` 是已有 Python 集成的 `trusted_legacy` 适配入口（旧动作和 scene 工具已在 2026-10 重构中删除）。旧适配器保留原语义校验，不能被 actor 或程序借来绕过统一动作及主模型审核。历史 `from auriga.runtime.harness import TOOLS` 兼容别名已随旧工具删除。

## 新增一个只读工具

默认关闭、通过 owner 窄回调读取正式观察的现有插件示例见[腕部视角解算](robotic-view.md)和[双目极线引导](stereo-guide.md)；两者复用同一 actor/program 注册表，使用说明随各自 ToolSpec 交付。

把下面声明放在 `auriga/runtime/tools/plugins/` 下（例如 `example_status.py`），在应用组装处导入并传给 `Workbench(..., tool_specs=[make_status_tool()])`；直接使用 Harness 的宿主同样支持 `tool_specs`。handler 获得 `ToolContext`，只读 action port 返回本运行的原始动作记录；不提供 robot 或原始 Store。

```python
from auriga.core.tool_output import ToolOutput
from auriga.runtime.tools.registry import ToolSpec


def make_status_tool():
    def read_status(context, arguments, correlation):
        # 固定只读 port，不提供任意路径、观察者 artifact 或数据库访问。
        receipt = context.read_action(arguments["action_id"])["receipt"]
        return ToolOutput({
            "action_id": arguments["action_id"],
            "status": receipt["status"],
            "reason": receipt.get("reason"),
        }).response()

    return ToolSpec(
        definition={
            "type": "function",
            "name": "example_action_status",
            "description": "Read a saved action status without executing it.",
            "inputSchema": {
                "type": "object",
                "properties": {"action_id": {"type": "string", "minLength": 1}},
                "required": ["action_id"],
                "additionalProperties": False,
            },
        },
        handler=read_status,
        roles=frozenset({"actor", "program"}),
        effect="read",
        api_version="1.0",
        output_schemas={
            "data": {
                "type": "object",
                "properties": {
                    "action_id": {"type": "string", "minLength": 1},
                    "status": {"type": "string", "minLength": 1},
                    "reason": {"type": ["string", "null"]},
                },
                "required": ["action_id", "status", "reason"],
                "additionalProperties": False,
                "examples": [{
                    "action_id": "action-example",
                    "status": "completed",
                    "reason": None,
                }],
            },
        },
    )
```

该模板的输出正反例可以直接使用同一 validator 检查；注册时也会检查 `examples`，不会另维护一份示例参数表：

```python
from auriga.runtime.tools.registry import ToolRegistry, validator_for

spec = make_status_tool()
ToolRegistry([spec])
check = validator_for(spec.output_schemas["data"])
assert check.is_valid({"action_id": "action-example", "status": "completed", "reason": None})
assert not check.is_valid({"action_id": "action-example", "status": True, "reason": None})
```

`scene_` 是旧适配器保留的名称前缀，不能注册为 actor/program 插件；注册时拒绝，避免目录广告一个入口必拒绝的名称。`tool_specs` 在 Harness 初始化前加入 registry，因此 `features.disabled_tools` 可以包含这个名称；重名立即失败。Workbench 的运行源代码归档递归收录包内 Python 文件，保留 `runtime/tools/plugins/example_status.py` 这样的包内相对路径，Harness 对应键为 `runtime/harness.py`。启动 manifest 的 `tool_catalog` 保存目录 hash、各项 `api_version`、输入 schema hash、`result_kinds`、完整 `output_schemas` 与其 hash、effect、可用性及不可用原因。`api_version` 是作者对工具行为/结果语义的显式兼容版本；schema hash 标识具体结构，schema 不变也可能需要提升行为版本，二者不能互相替代。目录 hash 对应实际交付的名称/描述/输入 schema；输出契约 hash 单独冻结，不扩展 Codex 的输入工具目录。旧 manifest 缺少 API version 或输出契约时保留未知，不追溯填成当前默认值。外部包的源代码不会自动进入 Auriga 包源归档，应由宿主另行记录；把随项目交付的声明放在包内即可。

不需要模块扫描、动态下载安装或新的插件服务。对已有 Harness，启动 actor 会话前也可显式 `harness.tool_registry.register(spec)`；会话目录已经交付后再添加工具不会自动更新客户端。

## 副作用与程序授权

`effect` 可以是固定字符串，或按已校验参数返回分类的函数。`robot_action` 的 `preview` 是 `plan`，`inspect` 是 `read`，`execute/direct` 是 `physical`；`task_plan` 的读取是 `read`、保存是 `record`；经验库区分读取、当前运行的 `record` 和跨任务 `shared_memory`。分类描述业务副作用，常规日志或只读查询产生的证据 artifact 不等于物理动作。

`program_run` 默认审核物理动作。内建 `robot_action` 和 `robot_wait` 使用现有暂停/恢复审核；额外声明为 `physical` 的程序工具只有在本次程序启动已经获得 `allow_direct_actions=true` 时才可执行，不能通过一个新名称绕过审核。需要自定义审核流程的物理插件应先只开放 actor role。`program_run`、`program_status` 和策略动作不开放 program role，程序不能嵌套启动其他程序或借旧入口扩权。

插件默认只得到 `ToolContext` 的 identity、当前观察/能力/预算副本及 `read_action`。只有 `physical` 调用获得 `execute(action)`；它固定传递 broker correlation，并按同一 tool call 内的顺序分配确定的 action_id，实际执行仍通过 `Harness.execute` 的状态、预算、不可变内容和幂等校验。没有原生 robot 或原始数据库句柄。内建历史 handler 在组装处显式绑定自己的运行依赖。可用性回调另收冻结的 `ToolAvailability`，只有配置/能力与服务就绪事实。

可信 Python 代码仍不是恶意代码沙箱：宿主自己闭包绑定的额外服务由宿主负责。插件必须真实遵守副作用声明，不能伪造执行回执；正常扩展路径使用上述窄读取/执行 port。

## 内部类型与外部回包

外部格式继续为 `{success, contentItems}`。内部 `ToolResponse` 是明确的非映射类型，持有 `kind`、原始 `data`、`success` 和独立的 `content` 展示项；不在读取时隐式生成 JSON。`ToolOutput(summary).response()` 保存 `StructuredText(summary, projection="data")`。有独立展示与执行事实时使用 `ToolResponse([StructuredText(display, "action_display"), ...], data=original, kind="action")`，不能把执行事实直接当成展示替代品。动作后附带的观察是另一个展示项，程序状态与程序原始 job、观察 bundle 与实际交付的 metadata/images 也分别保存。

普通插件默认 `projection="data"`，不把 JSON 形状猜成业务结果类型。actor 最终交付会将合法 ID/artifact 引用换成同一运行的短引用，并递归省略 provenance：hash/digest/checksum/fingerprint 字段、来源 URL/文件/仓库/提交、文档来源集合，以及 hash 形态的 version/calibration_id；普通数值 revision、solver_version 等业务数据仍保留，image_url/audio_url 仍用于媒体交付。该规则同时适用于插件的根字段和嵌套字段；不要依赖这些被过滤的字段向 actor 传递必要业务数据。program 展示与旧结果重放使用同一过滤；typed data 和 observer 原始归档保留全部事实。需要内建摘要的 producer 显式选择 `capabilities`、`action_result`、`action_display`、`observation`、`operation_review`、`contact_overlay` 等具名投影；`action_result` 保留业务 owner 的 full/summary，不再对 receipt 做第二次字段白名单截断。投影名属于展示语义，不改变 `ToolResponse.kind/data` 的可信执行含义。

actor 在 `ActorOutput.present` 的最终交付边界投影和序列化；program FD 在 `ActorPresentation.present_response` 使用相同的元信息过滤和短引用。原始 wire 回复的 artifact 保存、日志和显式 legacy 调用通过 `wire_response` / `wire_content` 生成普通 JSON。`ToolOutput.content_items()` 仅是显式 wire 兼容适配器。历史 wire/artifact/log 读取可以保留旧展示清理；它们不能用于反推内部执行事实。直接 Python 调用需要 wire 时显式适配，不要求调用方依赖内部 dataclass 序列化行为。

内建工具的输出声明先约束已存在的结果包络：`action` 必须有对象类型的 `receipt` 和字符串 `receipt.status`，完整动作、轨迹、执行量及状态转移仍由原业务 owner 校验；注册表不复制第二套 physical 契约，也不重新推断执行是否完成。`plan` 校验状态字段，失败预览允许没有 `plan_id`；程序审批继续要求实际可执行的缓存计划。`observation` 校验已由共享观察契约验证的 bundle 交付包络。`history_list_actions` 保留分页对象与完整列表两种返回；`history_read_trace` 保留归档 JSON 的原形状（可为数组、标量或 null），仅要求合法 JSON，不另限制历史 trace 的内容。其余 `data` 内建结果目前只校验对象容器，各工具的深层可选字段仍由已有 owner/回归负责，这一限制不等于已发布全部字段级输出 schema。

展示-only 兼容明确保留 `null`：`history_view_observation`、`robot_contact_overlay` 的 data 为 null；`robot_action` 的 action/trajectory inspect 也可能只有展示项，data 为 null。这些返回不能被 broker 当成动作或新观察证据。`robot_action` 其余模式可以返回 `plan` 或 `action`，分别按该 kind 的 schema 校验。只有 role 完全为 `trusted_legacy` 的旧适配器允许 `output_schemas=None`；manifest 将其标为 `trusted_legacy_wire_unvalidated`，actor/program 新插件不能利用这一豁免。

输出不合契约发生在 handler 已返回之后。registry 不抛成输入错误，而保留原 `kind/data` 与可传输展示项，返回 `success=false` 并追加 `output_contract_error`，其中说明 `phase=after_handler` 和禁止自动重放。已保存的 completed/partial/stopped 回执保持原样；缺少可信 typed receipt 时结果才是 unknown。程序 broker 记录输出错误次数，同时继续从原回执记录真实执行事实。`success=false` 在这里说明回包契约失败，不说明动作没有执行。非 JSON 或不符合 native 内容项形状的展示值会被隔离，其他合法项继续交付，不调用其 repr 或尝试重放；动作类型另附可传输的 typed receipt 事实摘要。展示项允许 `StructuredText`，或 native `inputText/text:string`、`inputImage/imageUrl:string`、`inputAudio/audioUrl:string`，不把普通 JSON 标量/对象当作 native 内容项。非法 native 项报告 `invalid_native_content_item` 和具体 `/content/<index>/<field>`；其他输出错误也会移除非法项。actor 投影失败保持同一后执行错误含义。外层 `success` 必须是实际布尔值，整数、字符串、None和非JSON对象都返回 `/success` 的 after-handler 诊断；不能把这类回包错误降成执行unknown。注册表检查外层类型、JSON 可表示性、native 内容项及声明的 schema，不提前产生 JSON 文本。

handler 在已经执行之后组装可选观察、图像或详情 artifact 时，也要保护交付异常：先保留 typed action 事实，再复用 `output_contract_error` 返回 `action_feedback_unavailable`，不能让异常变成无回执的普通工具失败。现有统一动作、wait 和 policy handler 均覆盖该路径。对旧检查点的幂等重放不能把历史 revision/cursor 写回当前计划。

程序 broker 的审核、动作汇总和最新观察分别读取 `kind="plan"` 的缓存计划、`kind="action"` 的原始 `{receipt, ...}`、`kind="observation"` 的 ObservationBundle。它不再从 `inputText` 解析内部执行事实；展示文本损坏、字段压缩或伪造 stdout 都不能改变可信状态。只有外部 wire dict 而没有内部动作结果时，执行量和结果为未知，不补零、不升级为成功。动作 ID 优先来自 receipt，缺少重复字段时可来自原始 request；不能从展示文本补造。完整 wire reply 仍保存 observer artifact 供回放，不作为 actor 可读的未过滤 fallback。

回归见 `tests/test_tool_registry.py`：真实 actor 入口及隔离 Python 程序调用新只读工具、非法输入的零 handler/零动作副作用、输出声明/正反例、输出 schema hash、动作完成后输出违规仍保留原回执且不重放、缺失 receipt 保持 unknown、配置/profile/后端可用性矩阵、伪造展示文本不改变 broker 证据、递归源归档。`tests/test_program_review.py` 另覆盖真实进程暂停、审核、缓存推进失效与未知执行结果。

定向回归还见 `tests/test_typed_actor_output.py`：内部展示不解析或提前序列化、具名混合回复、业务字段保留与 actor provenance 递归过滤、metadata fallback 同步、已执行动作展示失败与保存后 program replay。

上下文预算是接口设计约束：非必要的元信息应在 producer 处不提供。展示层还会删除哈希字段、未命名的裸哈希值及自由文本中的校验片段，不追加冗长占位文本。必要的 action/job/review/observation/plan/artifact ID 使用同一 Store 的稳定短引用，包括复合 ID、字典键和程序源码中的引用，两端参数会恢复为原始身份。JSON Pointer 提取后的裸哈希、stdout/stderr、错误和 text artifact 也受此规则约束；图片/音频字节保留。输出 schema 的属性与 required 不得被字段过滤损坏，需要回传的哈希型枚举使用可逆引用，内部校验仍使用原值。

程序执行入口只将源码中的必要对象引用统一为回包使用的短引用，保证源码字面量与工具回包可直接比较；不对可执行源码应用自由文本清理。普通代码、校验字面量和数值保持原样，幂等判断使用原始调用参数，源码归档记录实际执行内容。参见 `tests/test_tool_registry.py` 中真实 `program_run` 的引用、源码和重放回归。

辅助视觉核验在结构化证据序列化前过滤；CodexSession 对实际 Responses/compact 请求的可读文本再过滤，覆盖恢复历史。原生 call/item ID、媒体 URL/数据与加密 reasoning 保持协议原值，不作为展示文本展开。内部校验哈希、幂等 digest、来源审计不因展示规则而删除。测试应捕获真实 loopback HTTP / helper turn 参数，而非只检查一个格式化 helper。

开发入口与检查要求见[AGENTS.md](../../AGENTS.md)、[实现架构](architecture.md)、[扩展开发](extensions.md)和[验证指南](validation.md)。

## 生成程序的结构化结果

`AurigaClient.call()` 返回 `success`、`kind`、`data` 与兼容展示 `contentItems`。生成程序按 `kind` 消费 `data`；动作回执在 `data.receipt`，计划与规划拒绝在 `kind="plan"` 的 `data`。`ToolOutput.projection` 同时选择业务数据投影；直接构造 `ToolResponse` 的 producer 用 `data_projection` 明确选择对应元信息投影，不从展示文字或工具名称猜测。随后统一经过 `ActorPresentation` 过滤元信息、解析短引用。公开输出 data 仍必须遵守观察权限和输出 schema；不得把隐藏内部状态放入业务结果。

`kind="untyped", data=null` 保留旧 display-only 结果的不确定性；异常的 `kind="error"` 只报告调用诊断，不推断是否执行。已执行动作的 `output_contract_error` 不改变原回执，不能触发重发。旧缓存结果幂等重放不解析文本补造业务事实。示例与回归见[程序回包语义](program-result-semantics.md)、`tests/test_program_review.py`、`tests/test_actor_presentation.py`。
