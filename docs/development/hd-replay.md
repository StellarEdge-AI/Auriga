# 归档动作的高清离线重放

`auriga.apps.replay.hd_replay` 从已完成回合的归档读取实际下发的关节与夹爪命令，使用原任务、seed、layout 和原生仿真时钟重新执行并录制高清画面，不调用模型。它是 **action resimulation（动作重新仿真）**；归档没有完整场景物理状态，因此不能保证物体运动逐帧一致。

## 从网页下载后重放

在实验库中选中已结束的实验，点击 **下载原始轨迹**，得到一个 ZIP。在安装了 Auriga 的机器上运行以下命令，直接读取 ZIP，无需解压，也无需原实验目录：

```sh
python -m auriga.apps.replay.hd_replay \
  --source-run ~/Downloads/exp-example-trajectory.zip \
  --output ~/hd-replays/example-1920 \
  --width 1920 --height 1440 --fps 25
```

渲染需要匹配的 RoboDojo / Isaac Sim 环境、任务资产、GPU、`ffmpeg` 和 `ffprobe`。归档绑定了 adapter 源码与原生依赖版本；0.0.2 更新了 worker，因此旧归档可能被 `runtime_versions_differ_from_archive` 拒绝实际重放，应使用归档匹配的源码/环境。ZIP 下载和 `--inspect` 不受这一执行准入限制；不要为跨版本回放放松版本核验。ZIP 使用本机 Auriga 配置中的运行环境；如果本机安装路径不同，可以显式指定：

```sh
python -m auriga.apps.replay.hd_replay \
  --source-run ~/Downloads/exp-example-trajectory.zip \
  --output ~/hd-replays/example-1280 \
  --width 1280 --height 960 \
  --robodojo-root /path/to/RoboDojo \
  --robodojo-python /path/to/robodojo-env/bin/python
```

ZIP 格式为 `auriga.robodojo_trajectory.v1`，只包含 `manifest.json`、`commands.jsonl`、`measured_states.jsonl`：任务与 seed/layout、版本和时钟、原评分、逐物理 tick 的执行器命令、原生动作边界的机器人实测状态。它不包含模型对话、凭据、服务器安装路径、可执行代码、视频或仿真资产。导入时校验成员、大小、hash、命令连续性及数据格式；归档不能指定要执行的 Python 或程序。

网页仅对已结束且具有原始轨迹文件的运行显示下载入口。点击后还会严格验证完整性；不完整的动作轨迹会拒绝导出。历史导入副本暂不支持此入口，可使用保存完整证据的原始运行目录。下载不会启动 GPU 或调用模型，也不会修改原 benchmark。

## 本地原运行目录与参数

同一个工具也接受原运行目录。在仓库根目录、Auriga Python 环境中运行；目录输入默认使用源回合 `effective_config.json` 记录的 RoboDojo 路径及 Python，也支持上述本机路径覆盖参数。

先做只读检查，无需 GPU，也不创建输出目录：

```sh
python -m auriga.apps.replay.hd_replay \
  --source-run /path/to/original-case \
  --inspect
```

检查会验证归档引用、命令覆盖和原生动作边界测量，输出 tick 数、仿真时长、原评分及来源 hash。含 WAL 的封存目录通过 `open_read_store` 私有副本读取，归档加载、轨迹 ZIP 导出与回放对照图读取均不会在原目录生成 SHM。它不启动仿真，因此不验证当前 GPU 环境能否重建原场景。

导出默认 **1920×1440、25 fps** 的三相机视频：

```sh
python -m auriga.apps.replay.hd_replay \
  --source-run /path/to/original-case \
  --output /path/to/hd-replays/new-case
```

`--output` 必须是尚不存在的新目录，与源目录互不包含。源回合以只读方式打开；新视频、评分和报告全部写入独立输出，原 benchmark 结果不被替换。

不指定 `--gpu` 时，通过 Auriga 共享预留机制选择利用率为 0 且显存满足准入条件的 GPU；没有合适设备就退出，不抢占已有任务。可以用 `--gpu 4` 限定设备，该设备仍须通过准入检查。

若要与其他任务并行渲染，可加 `--allow-busy-gpu`。此时也会逐次检查并预留未占用显存（当前每次预留 14336 MiB，另留 2048 MiB 余量；普通空闲 GPU 模式仍为 32768 + 4096 MiB），允许选择利用率非零的 GPU；`--gpu` 仍可用于限定设备。并行门槛依据一次 1920×1440、50 fps 重放时观察到的 12631 MiB 显存占用设定；这只是一次采样，不保证所有任务资产有相同峰值。没有足够未预留显存时会退出，由批量调度脚本稍后重试。

如需较小画面，可保持原相机 4:3 比例：

```sh
python -m auriga.apps.replay.hd_replay \
  --source-run /path/to/original-case \
  --output /path/to/hd-replays/new-case-1280 \
  --width 1280 --height 960 --fps 25
```

宽高须为正偶数且保持 4:3；帧率须能整除原物理频率。视频按均匀**仿真时间**采样，初始化与模型思考耗时不进入视频。旧录像将稀疏观察及额外观察边界按 5 fps 播放，所以新旧视频的播放时长可能不同；请按 `sim_step` 对照。

运行要求源回合记录的版本字段与当前运行时全部一致，包括 RoboDojo、局部修改 hash、相关组件、机器人配置和 adapter 源码；物理 dt 与每个 native action 的 tick 数也必须一致。版本不一致会停止，不会忽略差异继续输出。

命令缺口、重复 tick、跨多个环境 generation、原生动作中途故障或缺失边界测量都会拒绝重放。已完成的动作前缀可以属于失败回合；“任务失败”本身不是拒绝条件。新仿真若先于源回合进入原生终态，会保留已有证据并标为 `incomplete`。

| 输出 | 用途 |
| --- | --- |
| `cam_head.mp4`、`cam_left_wrist.mp4`、`cam_right_wrist.mp4` | H.264 高清视频；完成时用 `ffprobe` 核对实际尺寸、编码及帧数。 |
| `report.json` | 运行状态、版本差异、来源 hash、执行覆盖、新旧评分、机器人轨迹误差与视频校验结果。 |
| `frames.json` | 视频帧号、视频时间戳与原生 `sim_step` / 仿真时间映射；不是逐帧图片目录。 |
| `trajectory.jsonl` | 每个 native action 边界的实测机器人状态，以及相对原轨迹的误差。 |
| `comparisons/`、`comparisons.json` | 目录输入按源观察选取最多 12 个时间点、三相机并排图；右侧高清图缩到原尺寸，索引记录来源及像素差异。ZIP 不含原图，因此此项为空，报告标注 `original_image_comparison_available=false`。 |
| `worker.log`、`*.ffmpeg.log`、`process.json` | 仿真、编码与子进程退出诊断；`admission.json` 记录 GPU 准入。 |

验收先检查 `report.json` 的 `complete`、`source_unchanged`、`robot_trajectory_within_tolerance` 和 `evaluation_matches`，再查看对照图中的物体位置、抓取、释放与最终状态。`status=completed` 或退出码 0 表示导出完整，不能单独证明场景重现；机器人误差、相同评分和像素差异也不能替代物体运动核对。

报告以 `data_origin=archived_action_resimulation`、`exact_scene_replay=false` 标明来源。保留原 benchmark 评分，新评分用于判断本次重放是否偏离；实际效果以每次导出的报告和画面对照为准。
