# 程序回包、动作完成与原生任务结果

`program_run` 中的 Python 必须检查每次动作的内层回执。`client.call(...)` 返回的外层 `success=true` 只表示工具分发正常返回；动作被拒绝、规划失败、部分执行后停止或结果未知，都可能包含在这个正常返回中。程序退出码0也不能证明动作成功或原生任务成功。

这项要求来自实际运行中的失败：一个程序把总预算和各动作预算都设为120秒，25个动作全部因预算不足被拒绝，却因只检查外层 `success` 而报告程序成功。本文说明当前程序接口；历史冻结实验保留原接口和证据。

## Broker 内部证据

当前 `ProgramRunner` 在外部呈现之前读取 `ToolResponse.kind/data`：缓存计划使用 `plan`，原始动作回执使用 `action`，实时观察使用 `observation`。审核、动作统计、末帧观察不再通过反序列化 `contentItems` 获取。缺少内部动作值时只能记录未知结果；即使展示文字写着 completed 也不能升级。`AurigaClient.call()` 现在同时返回 `kind/data` 和兼容的 `success/contentItems`，程序直接读取结构化业务值，展示文本不再承担执行状态解析。数据先经过 producer 指定的元信息投影，再使用共享过滤与可逆短引用；不会公开原始归档。旧的 display-only 结果标为 `kind="untyped", data=null`，不能根据文本补造回执。幂等重放旧调用仍返回保存结果，不重新执行或把旧文本升级为 typed 证据。

程序通过冻结的 `ProgramServices` 回调调用工具；Runner 不持有完整 Harness、robot 或 registry。Harness 在服务端绑定 program role，调用者不能通过参数提升权限。程序实际调用和 actor 目录均使用同一 `ToolRegistry` 的 schema、角色和运行能力判定。默认物理动作继续要求审核；新增 physical 插件若没有内建审核流程，只有该次程序预先取得直接动作授权后才能调用。旧 motion/scene 名称与嵌套 program 调用仍不允许。详见[工具注册与扩展](tool-plugins.md)。

## 四层结果分别读取

| 层级 | 实际字段或返回形式 | 能说明什么 |
| --- | --- | --- |
| 程序通信 | `AurigaClient._request` 读取内部 `{ok, result}`；`ok=false` 抛异常 | 通信／broker是否交付结果；`client.call` 返回的是其中的 `result`，不要再读取 `r["result"]` |
| 工具分发 | `r = {success, kind, data, contentItems}` | `kind="error"` 的 `data` 保存诊断；`success=true` 仍要检查业务类型和回执，`output_contract_error` 表示交付/契约问题，不能自动重发 |
| 动作 | `kind="action"` 时读取 `r["data"]["receipt"]`，只有一层 receipt | 查看 `receipt.status`、`reason`／存在时的 `error`，以及执行量、停止确认、末帧观测；`completed` 仅代表该动作完成 |
| 程序及任务 | `program_run`／`program_status` 返回的作业 `status`、`returncode`、`stop_confirmed`；任务结果由原生环境决定 | 程序 `succeeded` 来自进程退出码0；`failed`、`cancelled`、`unknown` 不得改称成功。动作 `completed` 和程序 `succeeded` 均不能替代原生任务终态 |

`receipt.status` 不是布尔值。只有明确的 `completed` 才表示动作完成；`accepted`、`failed`、`stopped`、`rejected`、`unknown` 或缺失／未识别状态都应停止程序后续动作并返回控制。`stopped` 的原因可能是原生环境已经结束，也可能是取消等情况；不能自行把它转为任务成功。通信异常和未知结果也不能自动重放动作。

规划失败并不保证零执行：多路点动作可能已执行前几段才失败。保留完整回执，特别是 `executed_native_steps`、`completed_native_steps`、`executed_control_steps`、`execution_interval`、`failed_waypoint_index`、`waypoint_results`、`stop_confirmation` 及 trace引用。字段可能缺失，缺失不能当作零步或已确认停止。`executed_steps` 的具体含义见回执的 `step_semantics`，不能用程序调用次数代替仿真步数。

`path_infeasible` 的 `receipt.failure_feedback` 还提供失败点的原始抓取目标、转换后末端目标、求解器残差和可用的候选关节／碰撞诊断；程序应完整保留这些字段交还模型。具体字段、坐标系和证据边界见[运动规划失败反馈](motion-failure-feedback.md)。

## 处理一个已有动作回包的示例

以下函数只解析已经返回的动作结果，不选择目标、不执行动作，也不自动重试。它在非完成或缺少末帧引用时打印状态并以非零退出码返回控制；正常返回也只表示“动作完成且有观测引用”，下一步仍需检查实测反馈和RGB。

```python
import json


def inspect_action_reply(reply):
    def return_control(reason, evidence):
        print(json.dumps({"decision": "return_control", "reason": reason,
                          "evidence": evidence}, ensure_ascii=False))
        raise SystemExit(1)

    try:
        result = reply.get("data")
        if reply.get("kind") != "action" or not isinstance(result, dict):
            return_control("missing_action_evidence", reply)
        receipt = result.get("receipt")
        if not isinstance(receipt, dict):
            return_control("missing_receipt", result)
        if receipt.get("status") != "completed" or receipt.get("error"):
            return_control("action_not_completed", result)
        if reply.get("success") is not True or reply.get("output_contract_error"):
            return_control("delivery_or_contract_error_do_not_replay", result)
        if receipt.get("execution_uncertain") or receipt.get("stop_confirmed") is False:
            return_control("execution_not_confirmed", result)
        after = result.get("current_observation_id") or receipt.get("result_observation_id")
        if not after or result.get("result_observation_is_current") is False:
            return_control("current_observation_unavailable", result)
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        return_control("unrecognized_reply", {"error": str(error)})
    return receipt, after
```

调用方式是 `receipt, observation_id = inspect_action_reply(reply)`，其中 `reply` 是一次已完成的 `client.call(action_tool, arguments)` 的返回值。将 `client.call` 的异常同样交还主actor处理，不捕获后继续执行下一动作。示例不会把失败或未知回执重新标记为成功；`SystemExit(1)` 使程序作业显示为 `failed`，具体动作状态仍保存在输出和Store中。

完整回包的后续 `inputText` 项包含 `{bundle, robot_state}`，RGB在 `inputImage` 项中。按 `receipt.result_observation_id` 匹配动作末帧，不要把wait过程中的 `temporal_sample` 当最终观测，也不要沿用动作前的 `observation_id`。动作回执对应的末帧只证明捕获时的状态：若其他操作后来推进环境，它可能已过时；提交下一动作时仍以网关当前观测校验为准。

程序子调用先把图像交给 `actor_program`。`program_run` 和 `program_status` 会自动将程序内只读或预览工具返回的 `inputImage` 作为独立图像项交给主actor，包括历史观察、预览和几何投影；待审核预览图与程序末帧观察也通过各自的回包流程交付。程序只需调用相应工具，无须把 `imageUrl` 或 base64 打印到 stdout 来显示图片。

补充图像的 `visual_feedback` 记录原工具、子调用标识、参数和保存的 `reply_ref`，让actor知道图像来自哪次操作及如何读取完整回包。转发保留工具返回的 `imageUrl`、ROI和label，不再重编码；图像保留此前采集、存储和工具交付已有的编码质量。历史图像的观测ID、裁剪坐标与捕获时刻仍对应原图，不会因为再次交付而变成当前观察。需要判断新状态时，actor仍须核对停止／执行状态，再按需调用 `robot_observe`；历史图像不能确认仍在运行的动作已停止。新鲜RGB、机械到位和夹爪命令也不能单独证明抓持、接触或原生任务完成。

## 面向模型的摘要与完整读取

公共 [`tool_output.py`](../../auriga/core/tool_output.py) 只统一输出构造、`detail`选择、文本片段与分页校验：第一个`inputText`仍是原有结果对象；存在详细证据时，第二个`inputText`给出详情；图像保持独立内容项及原顺序。它不新增执行器、动作门槛、自动重试或任务判断。

`program_run`、`skill_run`（已删除）及`program_status`的默认模型回包保留`job_id`、程序状态、退出码、停止确认、调用次数、时间／预算和完整日志引用。仅去掉重复的源码摘要与调用关联。普通Python stdout/stderr默认保留全部已保存文字，不按字节预算删去中间诊断。`*_excerpt.byte_size`、`byte_ranges`、`omitted_bytes`和`source_complete`继续报告交付范围与来源完整性；若旧归档只有截断片段，不能凭展示层恢复缺失原文，也不会标为完整。

summary只清理明确的内联图片`data:image/...;base64,...`，以显式占位符替换载荷，原文保留在artifact和full读取中。发生替换时，excerpt分别报告原始字节大小、被移除媒体的原始字节范围，并明确byte_ranges针对变换后的presentation UTF-8文本；不会把变换后的偏移冒充原文偏移。普通JSON字段、数值数组、失败诊断和日志文本不按内容猜测删除。

独立 `inputImage` 与stdout/stderr文字分别交付。图片从工具回包转发，不从任意stdout文本解析；打印的data URL在summary中仍是占位符，full和artifact读取保留原文。`visual_feedback` 的可信出处来自broker保存的工具回包，普通程序打印即使长得像观察或图像记录，也仍是程序作者的文本。

程序的 `action_summary.recent_actions` 从 broker 收到的 typed 回执直接保留 `stage_results`，包括各阶段的状态、执行量、`endpoint_error` 和 `grasp_contact_feedback`，并保留失败阶段／机械臂／阶段类型，以及首步关节跳变量、上限和起点漂移诊断。这些数值不依赖 stdout 打印，日志过长或程序打印伪造成功结果都不会覆盖它们；机器人几何推导的接触参考位置仍不证明与物体实际接触。`recent_actions` 为兼容保留字段名，内容现在覆盖该程序的所有动作，不再只留最后八次；已有 `all_actions_ref` 仍可回查。回执复用共享的展示投影，仅省略已知重复元信息和成功检查的重复细节；失败、unknown及新增诊断字段全部保留。

受限 Python 子程序默认将 OpenCV、BLAS 和 OpenMP 的计算线程限制为 1；不改变 1 GiB 地址空间限制。这样避免 OpenCV 按宿主的数百个逻辑 CPU 创建线程池，在线程栈与库加载挤占地址空间时出现线程创建或后续内存分配失败。进程成功、图像算法输出与任务成功仍分别判断。

同一模型上下文对同一job再次读取时，超过256字节且内容/引用/摘录完整性均未变化的stdout或stderr不重复内联，返回`*_delivery.status="unchanged"`、前次调用ID和full读取入口；短文本直接保留，避免标记比正文更长。新的状态、退出码、停止确认、动作事实及待审批信息仍返回。新的日志artifact、另一个job、上下文压缩、会话重连或不确定的发送都会重新内联。显式full读取不去重，同一次调用的重传不因该调用先前准备过结果而省掉正文；程序内部读取不消耗actor的交付基线。

补充图像使用独立的交付基线。同一模型上下文的summary回包可以省去内容与来源均未变化的补充图像，同时给出 `previous_tool_call_id` 和 `program_status(job_id=..., detail="full")` 恢复入口。显式full读取或模型上下文重置会再次交付补充图像；这只改变重复显示，不修改保存的图像、来源记录或程序／动作状态。

`stdout_ref`／`stderr_ref`始终指向完整不可变输出，可用`client.artifact(ref)`读取。`program_status(job_id=..., detail="full")`返回完整作业字段及完整stdout/stderr文本。其完整性来自artifact读取；若只有已截断文本，则仍显示`*_truncated=true`，不会宣称完整。程序`status="succeeded"`仍仅表示进程退出码0；摘要不依据stdout修改它，也不把任意JSON打印提升为机器人状态。

仅当调用入口是已选择的内置`skill_run`版本时（该入口已在 2026-10 重构中删除，此投影当前没有调用方），[`program_result.py`](../../auriga/runtime/programs/program_result.py) 才从完整stdout读取固定技能JSON协议，并以`skill_output.source_kind="fixed_skill_program_output"`标注来源。该结构保留完整技能字段、每步回执诊断、未知扩展字段及像素对应点，不再用固定白名单或数组摘要移除有用证据。回执复用共享元信息投影；每步仍有兼容的 `receipt_ref`，整个原始输出保存在stdout artifact中。没有完整或可识别的技能输出时，`execution_outcome="unknown_from_output"`，不能推断没有动作发生。普通`program_run`即使打印完全相同的JSON，也仍是没有自动解释的程序文本。

## 状态和轨迹的按需详情

以下只读工具使用一致的`detail="summary"|"full"`。模型默认摘要；`AurigaClient`的程序子调用在未显式给出`detail`或分页选择时继续返回原有完整对象，保存证据及数值精度不变。

| 工具 | 默认摘要 | 完整读取 |
| --- | --- | --- |
| `robot_action_status` | 保留原请求、动作结果、错误、时序、部分执行及未知诊断；省略已知存储重复项，并附`full_result` | `detail="full"`返回原`{request,receipt}` |
| `history_list_actions` | 按记录顺序返回状态索引，`start=0`、`limit=25`，含`total`和`next_start`；每项标识动作、结果、执行量、停止状态及完整回执入口 | `detail="full"`返回原完整列表；不能与分页参数并用 |
| `history_read_trace` | 小值原样；大于窗口上限的序列保留当前连续窗口，并列出每个窗口的JSON Pointer路径、总数和`next_start` | `detail="full"`返回原完整trace；不能与`path/start/limit`并用 |

trace分页的`start`是所选JSON序列的零起始索引，**不是native step**。例如`path="/samples"`读取分发采样，`path="/segments/0/q_rad"`读取第0段规划关节序列。使用返回的`windows[].path`及`next_start`继续读取；`limit`范围1..100，默认25。窗口只切样本序列，每个样本完整保留，包括六维关节向量、三维位置、四维四元数和矩阵；`limit=1`仍返回完整的一条样本。默认窗口只识别顶层序列及已知trace时间序列，不递归截短任意数组。每条样本的真实`sim_step`、时间、状态和精确数值保持不变；窗口不会把规划命令改称实测轨迹，也不会插值或推断缺失样本。

正常的`joint-travel-admission-v1`通过项可以省略重复关节向量和已通过的periodic检查明细，但仍保留检查顺序、编译时刻、阶段、剩余预算和被跳过的未知检查。`motion_safety_check_summary`注明总数、通过数、最后状态及完整动作读取入口。拒绝项、witness、失败反馈和未知策略保持完整；任何摘要都不更改运动准入。artifact访问仍由原有scope、类型及观察权限检查决定。

## 动作预算小于程序剩余预算

网关接纳动作的实际条件是：

```text
program_deadline - time.monotonic() >= action.timeout_s + 5
```

这里是**接纳当时的剩余工作时间**，不是最初传给 `program_run` 的总 `timeout_s`；程序自己的模型和动作审核等待不计入工作时间。启动Python、导入模块、之前的工具调用和分发延迟都会消耗程序预算。显式设置总程序120秒、动作120秒必然不满足条件；动作115秒也没有启动余量。

程序内部省略 `robot_action`（新 preview/direct）或 `robot_wait` 的 `timeout_s` 时，broker 在生成计划之前取该工具默认值（120／30秒）与 `剩余工作时间 - 5秒执行余量 - 1秒分发余量` 的较小值。显式值、已缓存计划和程序外 actor 调用保持原语义；审核通过后不会偷偷缩短计划中的截止时间。最终动作准入仍重新检查余量，耗时规划可能使动作被安全拒绝。若 broker 已无最小0.1秒动作的余量，direct／wait 返回 typed error、原因 `program_time_insufficient_for_action`，不创建计划或动作；若在动作准入时不足，保留 rejected 回执和 `action_not_executed` 证据。两种情况都不执行物理动作。只读 preview 无需预留执行时间：余量不足时仍可按原默认值生成只读计划，后续执行仍受最终准入约束。

例如总程序120秒、某次动作显式设置30秒，只有到达网关时至少还剩35秒才能被接纳。这是预算关系示例，不是通用动作时长或动作配方。后续动作必须重新考虑已消耗时间，并额外留出启动／分发余量；程序内自行开始的计时器不知道网关的精确截止时间，不能保证接纳。预算不足就停止并返回控制，不循环提交相同动作，不提高总预算来掩盖既有失败。

## 实现与验证入口

- [`harness.py`](../../auriga/runtime/harness.py)：`program_run`说明、`execute`的剩余预算门槛，以及`dispatch`的动作回执和观测内容。
- [`auriga_client.py`](../../auriga/runtime/programs/auriga_client.py)：内部通信封装与原生dict返回。
- [`programs.py`](../../auriga/runtime/programs/runner.py)：异常回包、程序状态与停止确认。
- [`program_result.py`](../../auriga/runtime/programs/program_result.py)、[`history_output.py`](../../auriga/core/presentation/history_output.py)：模型展示和只读按需详情；不改程序执行及Store证据。
- [`robot_worker.py`](../../auriga/robot/backends/robodojo/robot_worker.py)：动作失败、部分执行和回执字段。
- 现有CPU检查：`tests.test_programs`、`tests.test_actor_payload`、`tests.test_framework_candidates`。这些检查使用隔离子进程或模拟机器人，不运行模型或仿真环境。
- 摘要与完整读取检查：`tests.test_tool_output`、`tests.test_program_result`、`tests.test_history_output`覆盖日志末端、完整技能JSON、失败／未知状态、精确分页还原、成功guard明细与拒绝证据边界，以及artifact scope拒绝。
