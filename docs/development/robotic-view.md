# 腕部相机视角解算

`robotic_view` 将调用者选定的相机位置、观看目标和图像朝上方向换算为机械臂 link6 目标姿态，省去手工组合相机安装变换的步骤。它是只读计算工具：不自动检测遮挡、选择空闲机械臂或视点，不调用运动预览、执行或观察。

## 开启方式

设置 `features.robotic_view=true` 或使用 Gateway 参数 `--robotic-view`；`--no-robotic-view` 可显式关闭。默认 `false`，与 `stereo_guide` 相互独立。队列 manifest 的 case 接受布尔字段 `robotic_view`，省略时继承统一配置。恢复和回放沿用归档开关，旧档缺失字段时按关闭解释，完整优先级见[配置说明](configuration.md#可选视角工具)。

关闭时不导入或装配插件，不注册对应 actor/program 工具，也不交付该工具的使用说明。开启后仍可用 `features.disabled_tools` 或 `--disabled-tools robotic_view` 禁用。说明随工具自身的 ToolSpec 交付，不添加特定任务 prompt，也不修改 Task Notes。

## 输入与输出

五个参数均必填：

| 字段 | 含义 |
| --- | --- |
| `observation_id` | 当前空闲状态下的正式观察 ID；历史或动作进行中的观察不适用。 |
| `camera_id` | 该观察中的已标定腕部相机，须通过公开固定关节链连接到 link6；固定头部相机不适用。 |
| `eye_position_m` | 期望相机光心的 `[x,y,z]` 米制坐标，使用观察标定参考系，原生为 `env_origin`；不是 link6 位置。 |
| `target_position_m` | 光轴要指向的 `[x,y,z]` 米制坐标，与光心使用同一参考系。它是调用者从许可证据得到的估计或假设，不是工具测得的物体位置。 |
| `up_vector` | 希望图像上方对应的三维方向，使用同一参考系；只取方向，长度不参与计算，无默认值。 |

三个向量都要求有限数。光心与目标相距至少 `1e-6 m`；`up_vector` 非零，且它与观看方向夹角的正弦须大于 `1e-6`。光学轴采用 +x 向右、+y 向下、+z 向前的约定。

成功返回 typed `kind="robotic_view"`、`data.status="calculated"`。`data.robot_action_preview` 包含 `tool="robot_action"` 和 `arguments`：其中 `mode="preview"`，`stages` 只有一个 `move`，给出对应机械臂、link6 的 `position_m` 与 `quaternion_xyzw`。`frame_id` 和 `tcp_config_id` 保留坐标与 TCP 约定。

`checks.ik`、`checks.collision` 为 `not_checked`，`checks.visibility`、`checks.retention` 为 `unknown`。`calculated` 只表示坐标变换完成，不证明机械臂可达、路径无碰撞、目标可见或持物稳定。

## 调用与动作衔接

下面的函数可在 `program_run` 中使用；从当前正式观察填入相机 ID、参考系内的视点/目标和朝上方向。它只计算并请求预览，不执行运动：

```python
from auriga_client import AurigaClient

client = AurigaClient()

def preview_view(observation_id, camera_id, eye, target, up):
    result = client.call("robotic_view", {
        "observation_id": observation_id,
        "camera_id": camera_id,
        "eye_position_m": eye,
        "target_position_m": target,
        "up_vector": up,
    })
    assert result["kind"] == "robotic_view"
    assert result["data"]["status"] == "calculated"
    return client.call("robot_action", result["data"]["robot_action_preview"]["arguments"])
```

主模型也可直接调用同名工具，使用相同字段。查看预览的轨迹、成本和未知项后，若决定移动，再单独调用 `robot_action(mode="execute", plan_id=...)`，使用预览返回的计划 ID。程序入口同样通过 `AurigaClient.call("robot_action", {"mode": "execute", "plan_id": ...})` 请求，遵循原有审批与预算；细节见[统一动作与程序审批](unified-actions.md)。执行结果按 typed `kind/data.receipt` 判断，不从 stdout 推断。

尽量选择能提供互补观察的空闲腕部，并检查其移动路径。正在持物的机械臂移动时会同时改变物体姿态；工具不把相机当作能独立移动的自由视点。动作消耗正常运动预算。发生其他动作后，重新使用最新观察计算；视角变化后还要从实际新图像确认是否获得所需信息。

## 实现与验证边界

实现位于 [`auriga/runtime/tools/plugins/robotic_view.py`](../../auriga/runtime/tools/plugins/robotic_view.py)。相机标定的 `link_name` 不必等于 link6，工具沿公开机器人模型中的固定关节链组合安装变换，再求相机目标到 link6 目标的变换。Gateway 通过 `make_robotic_view_tool(read)` 注入窄读取回调，不向插件开放任意 Store 路径或 worker 控制。

计算前后检查正式观察的身份、时效及当前空闲状态；仅使用许可标定、机器人模型与本体状态。工具不会解除 unknown/stop 门禁。后续运动仍通过原有 `robot_action` 和唯一的 `Harness.execute` 入口。

坐标变换、退化输入、信息边界和两端契约的回归见 [`tests/test_robotic_view.py`](../../tests/test_robotic_view.py)。软件几何正确不等于视角一定有用，也不证明任务成功率提高；任务效果须以冻结条件下的独立原生评分为依据。
