# RGB-D 数值工具与有界操作技能

深度默认关闭。默认配置仍为 `rgb_proprio_calibrated_v1`；原生采集在任务配置合并后主动关闭深度，actor 看不到深度工具，程序、历史观察和几何别名也不能绕过 profile。以下配置必须由操作者显式选择，actor 不能修改本局 profile。

## 分阶段启用

| 配置 | 数值工具与候选 | 已登记目标的动作后核验 | 抓放技能 | 事件式精简回包 |
|---|---|---|---|---|
| 默认 `configs/auriga.json` | 关闭 | 关闭 | 关闭 | 关闭 |
| `configs/rgbd-numeric-v1.json` | 开启 | 关闭 | 关闭 | 关闭 |
| `configs/rgbd-feedback-v2.json` | 开启 | 开启 | 关闭 | 关闭 |
| `configs/rgbd-skills-v3.json` | 开启 | 开启 | 开启 | 开启 |

这三个文件是显式选择的配置样例，没有替换默认配置。启动时使用 `--config`；也可在自己的统一配置中设置：

```json
{
  "features": {
    "observation_profile": "rgbd_proprio_calibrated_v1",
    "depth_auto_feedback": true,
    "depth_skills": true,
    "depth_event_payload": true
  }
}
```

对应 CLI 参数是 `--observation-profile rgbd_proprio_calibrated_v1`、`--depth-auto-feedback`、`--depth-skills`、`--depth-event-payload`，三个布尔参数均支持 `--no-...`。布尔开关不能自行放行深度：非 RGB-D profile 启用任一开关会报错。精简回包还依赖自动核验。实验队列支持相同字段；effective config 随运行冻结，恢复采用原归档配置，缺少新字段的旧归档保持关闭。

## 传感器与数值契约

- 原生 `distance_to_image_plane` 是光轴 z-depth，单位米；不是沿归一化射线的距离。使用现有相机 K、ROS optical 轴（右/下/前）和 `T_world_camera`，直接转换到 `env_origin`。
- float32 `depth[H,W]` 与 bool `valid[H,W]` 保存在 NPZ artifact。有效条件为有限、正数且位于相机近远裁剪范围。RGB 和 depth 使用同一 camera frame ID、原始尺寸和观察时间步。
- Published camera 仅保存 `depth_ref`、`depth_kind:"z"`、`depth_unit:"m"`、`depth_clip_m`、`depth_shape`。禁止把原始深度矩阵放入观测 JSON。
- 工具校验 profile、episode/generation、原图尺寸、标定、相机位姿时间步、artifact 内容以及 NPZ 形状和类型。隐藏物体姿态、接触、评分和 evaluator 仍不允许进入 actor。
- 数值返回是可见表面证据或明确的候选假设。物体背面、完整尺寸、质量分布、接触、抓稳和长期稳定仍可能未知。有效像素比例、拟合残差是质量指标，不是抓取成功概率。

## 工具

`depth_query` 是主要数值入口，参数为 `{"request":{...}}`。同名 kind 也可通过现有 `geometry_query` 使用，使用相同深度权限检查。禁用 `depth_query` 会同时禁用这个深度别名。返回不包含点云：最多 16 个指定像素测量、最多 3 个候选，其他查询返回固定大小的数值摘要。

| kind | 输入 | 数值结果 |
|---|---|---|
| `depth_points` | observation_id、camera_id、pixels（1–16 个），可选 radius_px | 各像素的可见表面 XYZ、z-depth、邻域有效比例和歧义 |
| `region_geometry` | observation_id、camera_id、roi，可选 seed_pixel | 可见中心/范围/尺寸、局部宽度、主轴、平面法向与拟合误差 |
| `grasp_candidates` | region 字段，可选 count、grasp_depth_m、clearance_m | 1–3 个 top-down 抓取假设、夹持中心、姿态、可见宽度、开口余量、接近位置 |
| `place_candidates` | region 字段、held_dimensions_m、grasp_offset_m，可选 yaw_deg/count/margin_m | 支撑范围内的物体放置中心和夹持中心假设、姿态、支撑间隙 |
| `compare_regions` | before_observation_id、after_observation_id、camera_id、前图 roi，可选 arm、seed_pixel | 世界位移/旋转/抬升、相对夹爪位移/旋转、实测开口、分离变化和更新 ROI/seed |

`held_dimensions_m` 是调用者提供的保守物体完整外形估计；`grasp_offset_m` 是所请求物体姿态下、世界轴表示的“物体中心到夹持中心”偏移。放置工具验证可见支撑区域内的 footprint；它不执行运输、不证明途中避障或物理稳定。

查询示例：

```json
{"request":{"kind":"depth_points","observation_id":"obs-...","camera_id":"cam_head","pixels":[[320,240],[330,240]]}}
```

数值结果采用统一 envelope：`status`、`frame_id`、`unit`、`value`、`quality`、`unknowns`、`measurement_id` 和来源观察。`supported` 只说明本次测量有足够对应/几何支持；抓取与放置候选保持 `partial` 并列出假设。遮挡、深度边界、缺少纹理、非刚性或退化时返回 `uncertain`，未知数值保持空值。

给模型的投影去除重复运行身份字段，浮点数保留最多 6 位小数；完整精度仍保留在归档及程序接口。目标、测量、候选使用局内短 ID，例如 `d1t1`、`d1m2`、`d1m2c1`。相同请求复用测量 ID，并重新校验来源完整性；命中内存缓存时不重复几何拟合。

### 目标与候选 ID

`register_target` 接受当前 observation_id、camera_id、roi，可选 seed_pixel、arm、label，返回 `target_id`。每局最多同时跟踪 4 个目标；`forget_target` 移除不再需要的目标。随后 `region_geometry`、`grasp_candidates`、`place_candidates` 可直接使用 target_id。

`refresh_target` 使用当前观察和重新选择的 roi 刷新目标；`target_status` 返回当前登记状态。ID 不跨 episode/generation。发生动作后，没有可靠更新的目标会过期，不能默默复用旧坐标。

候选获得 `candidate_id`，可通过 `depth_move`（已在 2026-10 重构中删除；当前须把候选位姿写入 `robot_action` 移动阶段提交）执行一次明确选择：

```json
{"candidate_id":"d1m2c1","arm":"left","stage":"approach","gripper_goal_normalized":1}
```

`stage` 为 `approach` 或 `target`。执行仍走原 `robot_move_grasp` 和唯一 action writer，不自动闭爪或重试。候选绑定生成它的观测；任何新观察后需重新测量生成候选。完整精度和来源保存在 store，Astra 无需复制坐标。

### 自动操作后核验

`depth_auto_feedback` 仅对已登记目标使用现有动作前后观察，不新增 capture 或动作。比较先使用 RGB 对应点，再读取对应深度拟合刚体变化，并用测量相机/夹爪位姿补偿相机运动。有效结果更新目标 ROI；对应不足则标记 uncertain。动作回包附上 `depth_feedback` 数值，并保留独立 evidence 记录。

不要把纹理对应直接当作完整对象身份。ROI 应放在目标上并避开夹爪与背景；短间隔、可见、刚性区域最适合该方法。透明、反光、薄边、严重遮挡或变形目标可拒判，需重新选择区域或视角。

## 有界技能

`depth_query {kind:"skill_help"}` 给出完整参数与本局启用状态。两个技能原由 `skill_run` 执行（该入口已在 2026-10 重构中删除，目前技能只能通过 `skill_help` 查看，不能执行）：

- `depth-grasp-lift-v1`：显式 approach_pose、grasp_pose、lift_pose；接近并开爪、到抓点、闭合、最多 5 cm 短抬。最后测量物体抬升及相对夹爪变化。
- `depth-place-release-retreat-v1`：显式 place_pose、retreat_pose，可选最多两个 via_poses；放置、释放、最多 15 cm 撤手。测量物体留置与夹爪分离变化。

共同参数为 observation_id、camera_id、roi、arm、max_path_m，可选 seed_pixel 指定 ROI 内的目标像素。技能随匹配结果更新 ROI 和 seed，释放阶段保留释放前的锚点。每个 pose 为 `grasp_position_m:[x,y,z]` 和 `angles_deg:[pitch,roll,yaw]`，采用原 robot_move_grasp（现 `robot_action` 移动阶段 `grasp_position_m` / `angles_deg`）相对 top-down 的约定，不是常规绝对 RPY。技能不选择目标、不自动挑候选；调用者根据当前证据选择姿态。

每个动作必须有 completed 回执、新观察、实测夹持面位姿及足够的表面对应证据才能继续。执行失败、过期、遮挡、越界、阈值不满足或取消会停止程序。阶段判据有区别：接近时物体应保持在场景中，搬运时应与夹爪相对稳定，释放撤手时应留在目的地。程序没有自动重试。

默认单动作 timeout 为 40 秒；完整抓取预留 215 秒，放置预留 167–263 秒。程序上限 300 秒，最多 10 或 8–12 个工具调用。max_path_m 限制按新测量起点计算的请求弦长，不能保证 native 规划路线长度。原生行数和动作 ID 记入结果，任务成功由原生终态判定。

`depth_skills` 与 `vision_compare` 独立；显式启用 RGB-D 技能无需打开旧 RGB 技能。任意 program 内的深度读取仍受 profile 检查，不能通过程序绕过默认关闭。

## 事件式回包

`depth_event_payload` 只改变 actor 交付。完整 RGB、深度、机器人状态、动作与测量始终保存。对于有可靠目标测量的普通中间动作，可返回紧凑数值与 observation_id；Astra 随时可用 history_view_observation 取回图像。

以下情况保留普通 RGB 回包：开/闭爪、等待、策略关节动作、执行异常、目标对应不确定、目标变化超过阈值，以及每第三个符合精简条件的动作。程序内部仍读取完整状态。初始观察和主动观察始终提供 RGB。精简回包不改变动作授权、评分或继续执行的判据。

## 验证与实验边界

CPU 测试覆盖 z-depth 反投影、不同相机位姿、无效深度/混合边界、默认拒绝与历史/程序边界、局部候选、带孔支撑面、相机运动补偿、观测与候选失效、真实隔离程序的停止行为和回包回退。

原生检查中，默认 RGB 探针确认无深度；显式 RGB-D 探针完成三路 640×480 采集、每路 9 像素的前后数值查询、一次请求 1 cm 的左臂上移，以及正常关闭。单次数值查询耗时约 16–33 ms，每次数值 JSON 约 1.7 KB，原始 NPZ 约 1.5 MB 且不进入模型回包。这是局部查询成本，不含采集、仿真启动、模型耗时，也不是实际模型 token 节省比例。

无需模型的原生探针（`scripts/depth_probe.py` 驱动已删除的 `depth_move`，已于 2026-10 删除，以下命令仅作历史记录）：

```bash
python scripts/depth_probe.py --run-dir /absolute/new/probe-rgb --gpu 0
python scripts/depth_probe.py --run-dir /absolute/new/probe-rgbd --gpu 0 \
  --observation-profile rgbd_proprio_calibrated_v1 --move
```

默认探针只验证 RGB；第二条明确开启深度并执行一次 1 cm 向上动作，记录三路 NPZ、数值查询、重投影、动作反馈及 worker 清理。探针使用当前传感器/标定，不能单凭重投影自洽宣称深度已获独立精度标定，也不代表物体任务成功或 token 收益。

正式收益应分开比较：RGB+标定 vs 深度数值；再增加操作后核验、技能与精简交付。固定布局、原生后端及其余提示，报告成功/失败/未评分、工具采用率、模型轮数、cached/uncached/output token、原生行数与墙钟。历史 RGB 归档没有当时真实深度，不得补造 RGB-D 后写成实测对照。
