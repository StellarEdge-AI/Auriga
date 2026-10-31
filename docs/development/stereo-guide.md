# 双目极线引导

`stereo_guide` 把调用者在一张正式 RGB 图中选定的像素标出来，并在另一张同期 RGB 图上画出对应极线，用于缩小寻找同一物理特征的范围。它不识别物体、不自动匹配像素，也不进行三角化、移动相机或获取新观察。

## 开启方式

设置 `features.stereo_guide=true` 或使用 Gateway 参数 `--stereo-guide`；`--no-stereo-guide` 可显式关闭。默认 `false`，与 `robotic_view` 相互独立。队列 manifest 的 case 接受布尔字段 `stereo_guide`，省略时继承统一配置。恢复和回放沿用归档开关，旧档缺失字段时按关闭解释，完整优先级见[配置说明](configuration.md#可选视角工具)。

关闭时不导入或装配插件，不注册对应 actor/program 工具，也不交付使用说明。开启后仍可用 `features.disabled_tools` 或 `--disabled-tools stereo_guide` 禁用。通用说明只随该 ToolSpec 交付，不增加任务专用 prompt。

## 输入与输出

四个参数均必填：

| 字段 | 含义 |
| --- | --- |
| `observation_id` | 当前空闲状态下的正式观察 ID；两个相机必须来自这同一个观察。 |
| `source_camera_id` | 提供选定像素的已标定相机。 |
| `target_camera_id` | 接收极线叠图的另一个已标定相机，须与源相机共用参考系。 |
| `source_pixel` | 源原图中的 `[u,v]`，u 向右、v 向下；有限数且 `0 <= u < width`、`0 <= v < height`。不能直接使用裁剪或缩放后显示坐标。 |

仅支持零畸变针孔标定：原生 `pinhole_no_distortion`，或既有 GeometryTools 支持的 OpenCV 针孔系数格式，畸变系数必须严格全零。其他畸变模型或非零系数会被拒绝；工具不自动去畸变。

成功返回 typed `kind="stereo_guide"`、`data.status="guided"`，以及源/目标相机 ID、源像素、`target_line=[a,b,c]`、`target_endpoints`、两张 overlay 引用。目标原图中的直线满足 `a*u+b*v+c=0`，且 `sqrt(a*a+b*b)=1`；端点位于像素中心矩形 `[0,width-1] × [0,height-1]` 的边界。

额外图像按“源图、目标图”顺序交付，保持原图尺寸。青色十字标记源点，青色细线标记目标约束。叠图另存为 actor 可读 `geometry_overlay`，不改原始 RGB。

## 从极线到三角化

下面示例展示主模型直接调用工具的参数。`current_observation`、两相机 ID 和 `source_pixel` 均来自当前正式观察；查看叠图后，调用者还需辨认并核对目标图中的同一个物理特征，填入 `matched_target_pixel`：

```python
guide = stereo_guide(
    observation_id=current_observation,
    source_camera_id=source_camera,
    target_camera_id=target_camera,
    source_pixel=source_pixel,
)

# 确认两张图中的同一物理特征后，才提交这对像素。
measurement = geometry_query(request={
    "kind": "triangulate",
    "observation_ids": [current_observation, current_observation],
    "camera_id": source_camera,
    "camera_id_b": target_camera,
    "pixels_a": [source_pixel],
    "pixels_b": [matched_target_pixel],
})
```

这里有意使用同一个观察 ID 两次，表示两个同期相机。程序使用 `AurigaClient.call("stereo_guide", {...})` 与 `AurigaClient.call("geometry_query", {"request": {...}})`，按返回的 `kind/data` 读取事实；图像通过既有程序结果交付，不把 base64 图片打印为文字。

`guided` 仅表示已生成约束。整条极线不是正深度区间，目标点也可能被遮挡。线距离或重投影误差小，只说明几何一致性，不能证明选中的是同一个特征；没有可靠对应时应保留未知。

现有三角化会检查正深度、视差及重投影误差；默认最小视差角为 `0.5°`、最大误差为 `2 px`。接受时仍返回 `status="conditional"`，`value.points_m` 已在返回的参考系中，原生为 `env_origin`；按 `accepted_indices` 对应输入点，不再乘一次相机变换。几何不成立时保留 `unknown`，不能把失败结果变成移动目标。可用字段见[几何工具契约](tool-plugins.md)。

## 实现与验证边界

实现位于 [`auriga/runtime/tools/plugins/stereo_guide.py`](../../auriga/runtime/tools/plugins/stereo_guide.py)，复用 `GeometryTools.camera` 读取许可标定、相机 FK 和观察时钟。若相机光学系到共同参考系的变换为 `T_A`、`T_B`，取 `T_B⁻¹ T_A = [R,t]`，则目标线由 `K_B⁻ᵀ [t]× R K_A⁻¹ [u,v,1]ᵀ` 得到，随后规范化并裁剪。

两个相机相同、基线小于 `1e-6 m`、源射线与基线共线、目标图中无有限非零线段、非法像素/标定或非零畸变均报错。当前状态、观察身份或权限不符也会拒绝。工具在 Harness 锁内检查时效并生成派生图，不调用模型、运动、observe 或仿真，不解除 unknown/stop 门禁。

装配使用 `make_stereo_guide_tool(read)` 的窄回调，actor 和 program 复用同一只读 ToolSpec。几何、观察权限、原图保留与 typed 图像输出的回归见 [`tests/test_stereo_guide.py`](../../tests/test_stereo_guide.py)。这些验证不代表已解决语义匹配、完整物体姿态或任务成功率问题；原生收益需独立评估。
