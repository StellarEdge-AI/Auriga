# 统一机器人资料接口

`robot_information` 默认返回当前机器人的**完整物理简报、原始 URDF 文本、补充参数 JSON、可直接读取的原生几何文件引用，以及本局最新已发布观察的相机和基座标定**。JSON 不再扩充部件形状文字，也不默认发送图片。URDF 原文默认直接交给模型，完整表面几何通过它引用的 mesh 文件读取。

资料读取不重新观察、不移动机器人、不推进时间、不联网。当前注册 ID 为 `arx-x5`；未知机器人明确返回不可用，不回退到 X5。静态资料可用与 `calibration.available` 分别判断。

## 初始化与任务顺序

gateway 在机器人、运行能力和初始观察建立后，直接调用与工具共用的默认读取方法。`developerInstructions` 最前面是完整简报、补充参数 JSON（含几何文件引用与本局标定）以及独立的 URDF 原文块；随后保留既有 Harness 指令，再发送用户任务，Task Notes 位于任务正文最末。`task_notes_source` 仅归档，不发给模型。

URDF 从注册 ZIP 的装配入口读取，保留原始数值、空白和换行；初始化放入独立文本块一次，不在 JSON 中再转义复制。工具默认结果的 `urdf` 字段返回同一份原文，供程序解析。

ZIP 二进制不编码进 prompt；`reference.geometry.resource_ref` 是模型可通过 `client.artifact` 直接读取的文件地址。初始化不新增模型回合，也不伪造模型工具调用。工具表兼容的 resume 会重新注入资料而不重发初始任务；旧线程若因工具表不兼容报 `resume_tool_registry_conflict`，应新建 run。replay 保持只读。

## 默认内容与按需查询

关节链、固定变换、局部轴、声明限位、逐连杆惯量及 mesh 装配位置由 URDF 原文提供，精简 JSON 不再重复这些数据。JSON 仅补充末端接口约定、接触参考偏移、夹爪命令映射与有效控制参数，并提供 mesh 文件引用。原始 USD 字段、碰撞球及其他数值明细按需查询，完整简报保留运动含义与不确定性。

| `section` | 返回内容 |
| --- | --- |
| `default` 或省略 | 完整 `briefing`、原文 `urdf`、补充参数 `reference`、原生几何文件引用与当前 `calibration` |
| `calibration` | 再读当前最新已发布观察的相机内外参、手眼与基座变换 |
| `kinematics` | 完整关节与运动学数值 |
| `geometry` | 完整几何记录及原生模型资源说明 |
| `dynamics` | 质量、质心、惯量及适用边界 |
| `control` | 完整控制、仿真配置、动作时钟和命令语义 |
| `hardware` | 可选硬件比较、冲突规格与未知项 |
| `all` | 完整简报和全部详细静态物理资料；本局标定通过默认或 `calibration` 读取 |
| `resources` | 注册文件资源清单，包含 `resource_id`、格式与说明 |
| `resource` | 指定 `resource_id` 的文件引用 |
| `geometry_image` | 按需读取的旧静态示意图；默认不发送 |

省略 `robot_id` 使用当前运行的 `robot_model_id`；显式指定只查询对应机器人，不切换实际机器人。不能为另一 `robot_id` 返回本局机器人的标定。资源不存在时明确返回不可用；`resource_id` 仅用于 `section="resource"`。

## 原生几何文件

X5 的 `geometry_model` 资源是 `geometry_model.zip`，包含 `X5A.urdf`、9 个 STL 和 2 个 GLB。底座、六段臂部、两指、相机支架和相机的原生几何均保留；相对目录、URDF 原点与比例、关节变换和 GLB 节点变换保持有效。清理只移除无用的文档元信息，不改变顶点、三角面或装配变换。两只机械臂共用这套本体几何，各自基座位姿通过本局标定获得。

在已有 `program_run` 中可直接读取默认回包的 ZIP：

```python
import io, json, struct, zipfile
import xml.etree.ElementTree as ET
from auriga_client import AurigaClient

client = AurigaClient()
reply = client.call("robot_information")
info = json.loads(next(i["text"] for i in reply["contentItems"] if i["type"] == "inputText"))
model = info["reference"]["geometry"]
with zipfile.ZipFile(io.BytesIO(client.artifact(model["resource_ref"]))) as archive:
    assembly = ET.fromstring(info["urdf"])
    stl = next(n.get("filename") for n in assembly.findall(".//mesh")
               if n.get("filename", "").lower().endswith(".stl"))
    triangle_count = struct.unpack_from("<I", archive.read(stl), 80)[0]
    print({"links": len(assembly.findall("link")), "first_stl_triangles": triangle_count})
```

此例仅演示读取 URDF 与二进制 STL；完整装配需应用 URDF 和 GLB 中的变换，保留各格式单位与坐标约定。原生外观 mesh 不等于 PhysX 最终碰撞体，也不证明连续运动净空或真实接触。

可选 STEP 部件仍通过 `base_step`、`end_step`、`gripper_1_step`、`gripper_2_step` 读取。它们不是整臂 STEP 装配体；标准手指也不能替代上述原生几何中的修改手指。

## 本局相机与基座标定

默认 `calibration` 从最新已发布观察的许可 artifact 读取原始数值，包含 `available`、`observation_id`、`sim_step`、坐标和单位，以及每相机的 `K`、畸变、手眼 `T_link_camera`、本帧 `T_world_camera` / `T_world_link` 和每臂 `T_world_base`。读取前校验运行、episode、环境及 generation，检查步数、时钟域和相机帧一致；对模型使用可恢复的观察编号，省略内部运行标识。不同相机只返回其实际提供的字段，缺失基座变换会明确列为不可用；不拿静态默认挂载补齐，默认简报与精简 JSON 也不携带示例安装坐标。

这些是本局模拟器的实际配置和采样/派生位姿，不是真机实验标定。腕相机的本帧位姿由本帧连杆实测变换与本局初始化手眼变换得到，接口直接传递原发布数值。默认的 calibrated 观察条件允许读取；旧 `rgb_proprio_v1` 条件、资料缺失或不一致时明确返回 `available=false`。`section="calibration"` 只读取最新已发布帧，不刷新传感器、不重新观察，因此必须结合返回的观察 ID 和步数判断时效。

这一接口不提供隐藏任务物体状态、接触真值或评分信息。`robot_capabilities`、当前测量及当前工具约定继续优先；目标、规划、下发、实测和物体结果仍须分别判断。

## 输出边界与新增机器人

完整文件清单、环境/机械臂解耦、组合校验、控制适配与验收步骤见[机械臂与环境接入指南](robot-environment-integration.md)。这里的注册只提供资料，不代表后端已经支持该型号。

模型输出清理 SHA/hash/digest 字段、文档出处和仓库溯源，保留物理精度、测量/命令/推导标记、坐标、观测与动作标识。长 artifact 地址转成可恢复的 `artifact:ref_N`；工具参数和程序源码中的别名在读取前还原，未知别名拒绝。Store 原件与内部完整性校验保留，用户自由文本不做泛化正则改写。

新增机器人时，在 [`robot_information.py`](../../auriga/robot/model/robot_information.py) 的 `ROBOT_INFORMATION_REGISTRY` 注册 ID 和 `robot_information_data/<目录名>/`，提供完整 `briefing.txt`、`physical.json` 及原生几何 ZIP。在 `physical.json` 的 `geometry.native_model` 声明资源 ID、装配入口、格式和坐标约定，并在 `resources.json` 注册 ZIP；保留装配文件的 mesh 相对路径。默认 URDF 直接读取 `assembly_member`，不解析后重写；JSON 只选取不与 URDF 重复的补充参数，不另维护形状文字文件。`hardware.json`、STEP 部件和示意图均可选。

将资料纳入包数据并验证安装后可独立读取；新的机器人沿用同一工具接口，不添加型号专用工具。机器人 ID 必须与运行能力一致；本局标定始终由当前观察提供，不写进静态资料包。资料修改后使用新进程加载，并验证未知 ID 不回退、文件引用可读、数值与装配变换保留、缺失标定明确报告。

这些资料接口的验证不启动仿真，也不测量载荷、摩擦、顺应性、停止响应或整局任务收益。
