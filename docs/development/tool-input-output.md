# 工具成本摘要、按需帮助与输入错误

工具的展示层不改变运动执行、准入、重试或原生任务判定。摘要中的可行性、预算、实际执行量和错误必须来自原返回值；完整证据继续可读。

## 当前调用入口

所有公开工具通过 `ToolRegistry.invoke` 校验 role、同源 JSON Schema、禁用配置、观察 profile 与后端/服务可用性，再进入 handler。模型看到的目录使用相同声明；目录不是仅供提示的宽松参数说明。额外字段、错误类型和非有限数值都在执行前返回结构化 `invalid_tool_arguments`，并附 JSON Pointer `field_path`。不可用工具返回具体能力或配置原因。底层继续校验当前观察、动作所有权和预算，不因 schema 通过而跳过。

当前 actor 运动接口是 `robot_action`；下述 `robot_preview_cost`、`manipulation_run` 等章节描述的旧工具及 `motion_cost_output.py`、模板模块已在 2026-10 重构中删除，仅保留作历史回包解释，这些名称不重新开放给 actor/program。新增只读工具通过 `ToolSpec` 注册，目录、执行和程序权限不再维护三份名单，详见[工具注册与扩展](tool-plugins.md)。

## 运动成本预览

`robot_preview_cost`默认`detail="summary"`。每个候选保留`label`、`status`、`feasible`、`reason`、编译的native步数／时长、各臂运动／夹爪／等待成本、最大及总关节行程、预算、异常guard和轨迹关键证据。规划结果仍不是未来实测结果，也不保证到位、接触、保持或连续时间避碰。

`motion_cost_output.py`（已删除）精简已知通过的关节guard明细、拒绝guard中重复的正常检查、重复来源说明与存储摘要。拒绝的所有witness、阈值、原因、超预算明细及不合资格的周期检查逐值保留；未知策略、未知状态、异常形状和新扩展字段保守保留。轨迹采样、命名关节、单位、关键位姿、最小模型间距、异常及overlay元信息不能因成本压缩丢失。完整结果在`full_result_ref`指向的actor JSON artifact内，可用`AurigaClient.artifact`读取，既不重跑preview也不执行动作。显式`detail="full"`保留原完整对象；程序子调用没有显式detail时默认full。

对历史已交付的preview回包做只读静态重投影时，UTF-8总体积约降至原来的62%，所有完整artifact均逐值还原原结果，通过与拒绝的guard分别得到上述摘要。这只描述相同旧数据经过成本投影的大小；后续新增轨迹采样／图片会改变实际新回包成本，不能视为新实验token收益或任务效果。

## 模板帮助按需展开

`manipulation_run`的help不启动程序或动作：

```json
{"parameters":{"kind":"help"}}
```

返回当前允许的模板名、用途和逐模板help入口。选定后读取该模板的参数schema和结构例子：

```json
{"parameters":{"kind":"help","template":"place_release_observe"}}
```

返回值只包含选定模板的schema、一个例子及适用语义。重复的pose/move schema通过本地`$defs`／`$ref`复用；place／handover不列lift参数，grasp将lift列为必需。示例中的占位符必须用本回合真实观察、演员判断及调用者选择的姿态替换，不包含固定任务坐标。

需要原完整catalog时显式读取：

```json
{"parameters":{"kind":"help","detail":"full"}}
```

旧Python接口`catalog_for(...)`继续返回完整catalog。禁用的模板不会因help选择而解锁。只读help带未知字段时返回具体字段路径，避免调用者误以为其他参数已被执行。

启用airborne时，当前UTF-8序列化大小为：短catalog 1,002 bytes，完整catalog 14,583 bytes；单模板grasp／place／handover分别为8,647／8,372／8,528 bytes。单模板仍包含足够的参数约束和语义，避免只给模板名而迫使调用者猜测输入。

## 可操作的输入错误

[`tool_errors.py`](../../auriga/core/tool_errors.py)在模型tool handler和程序broker原有异常边界生成`success=false`，保留原`error`字符串及稳定code，并附：

- `field_path`：JSON Pointer字段路径；没有足够证据定位时为空字符串，表示参数根，不伪造更精确位置。
- `actual`：实际错误值、缺失标记或有界shape说明，避免回显完整程序源码／大对象。
- `suggestion`：基于已有validator的修正说明。
- `minimal_example`：局部结构例子或占位符，不是已验证可执行动作，也不是自动重试命令。

例如，模板`move`缺位置时，仍返回`invalid_manipulation_template_parameters`，同时指出`/parameters/move/grasp_position_m`及`actual={"present":false}`。paired路线数量不等时，返回实际左右数量，解释补充真实同步目标或在意图确为独立工作时使用independent；不会自动填充姿态。

`released_unverified`和`retained`均要求实际已存在的release观测ID，后者还需要可见证据支持。缺少release证据时，修正提示只能保留`unknown`及实际不确定性，不会建议伪造ID或仅改为`released_unverified`来绕过检查。

普通运行时异常也维持`success=false`，不能据此推断零运动或任务失败／成功。建议明确要求在执行可能已开始时先核对原状态；展示层不会重放动作。`Harness.dispatch`仍按原约定抛异常，程序调用幂等记录继续保存同一个失败回包。

CPU验证入口：`tests.test_motion_cost_output`、`tests.test_template_help`、`tests.test_tool_errors`、`tests.test_tool_output_integration`，以及原有`test_motion_cost`、模板、程序和Codex回归。其中 `test_motion_cost_output`、`test_template_help` 和 `test_motion_cost` 已随旧工具删除。无需模型或真实仿真。
