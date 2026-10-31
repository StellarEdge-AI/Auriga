# 图像传输与存储

新运行默认：相机 RGB → JPEG 95 保存 → WebP 85 发送。保留原像素尺寸、相机顺序、动作边界与采样时机；不通过缩图或删相机节省数据。历史实验不回写。

## 配置

默认读取仓库的 `configs/images.json`：

```json
{
  "transport_format": "webp",
  "webp_quality": 85,
  "webp_method": 4,
  "jpeg_quality": 95,
  "storage_jpeg_quality": 95
}
```

- `transport_format` 可取 `webp` / `jpeg`。对于只接受 JPEG 的接入端，改为 `jpeg`，其质量由 `jpeg_quality` 控制。
- `webp_quality`、`jpeg_quality`、`storage_jpeg_quality` 是 1–100 的整数；`webp_method` 是 0–6 的整数。未知字段、布尔值或越界值拒绝启动，防止拼错后静默使用默认值。
- JPEG 使用 Pillow 的 `subsampling=2`（4:2:0）、`optimize=True`。已有 Pillow/OpenCV/FFmpeg 足够，无新增依赖。
- 可以用 `AURIGA_IMAGE_CONFIG=/absolute/path/images.json` 指定另一份配置；部分字段省略时使用上述默认值。相对路径按调用进程当前目录解析，实验配置建议用绝对路径。
- Harness 将配置保存在运行库的 `runtime/image_settings`，并写入 gateway manifest；恢复同一运行沿用已保存的配置。worker 使用父进程传入的冻结设置，不在每帧读取会变化的文件。内部传递变量为 `AURIGA_IMAGE_SETTINGS`，普通启动无需设置它。

WebP 编码库缺失或编码失败时，当前图片自动改用 JPEG 95，并在发送记录中注明 `fallback_reason`。**这不是远程 HTTP 拒绝 WebP 后的自动重试机制。** 远程只支持 JPEG 时应在新运行配置中选择 `jpeg`；不会为了换图片格式重新执行机器人动作。

## Codex 原生接口

当前固定 Codex SDK/CLI 0.160.0 的动态工具返回图片为：

```ts
{ type: "inputImage", imageUrl: string }
```

`imageUrl` 目前填 `data:image/webp;base64,...`，兼容分支填 `data:image/jpeg;base64,...`。具身 actor 直接调用 native dynamic tools；图片项通过 Codex 的 direct adapter 进入 Responses 的独立 `input_image`，无需模型提取 URL 或调用 `image(...)`。Base64 是图片项的传输容器，不能把包含同一 URL 的 `input_text` 当作视觉输入。原生 shell 可用于数值计算与文件工作；涉及机器人工具的程序反馈循环使用 `program_run`。

运行私有配置关闭 `code_mode` 和 `code_mode_host`，并将同一个请求模型的私有 catalog metadata 指定为 `tool_mode="direct"`。仅关闭 feature 开关不够：0.160.0 内置目标模型的 `tool_mode="code_mode_only"` 优先于这些开关。模型名称和 reasoning effort 保持请求值，其他模型能力沿用固定版本的 catalog。不得通过更换模型或仅追加提示词替代这个边界修复。原生 shell/unified_exec 和文件工具已独立开启，direct transport 只决定工具及图像的传输形式。

program 工具将 broker 保存的预览、历史及其它补充图片作为原生 `inputImage` 项返回，保留来源标签和原始字节；stdout 中自行打印的 data URL 不构成图片来源。`program_status(detail="full")` 控制日志和投影的完整程度，不是图像 fidelity 参数。

模型展示统一去除哈希、校验摘要和冗余来源元数据，嵌套结构和 JSON 文本也应用同一规则。当前 `source_url` 用于来源追溯，所需内容已直接提供，因此省略；实际读取内容或理解结果必需的链接应按工具用途保留。图像／音频内容项的传输 URL、动作／观察编号和可读取的短 artifact 引用仍保留。本地数据库保留完整调用关系和原始结果引用，原始返回在展示过滤前归档；程序执行证据和原始日志也保留。`codex-raw.jsonl` 记录实际发给模型的精简回包，完整原始返回则由本地 `tool_result` 记录及其 artifact 保存。

用户消息输入另有 `localImage.path` 和 `image.fileId`，动态工具输出没有这两个变体。换成本地路径不能直接用于此工具返回类型。所用提供商的 Files 查询返回 HTTP 404，也没有其他图片存储接口，因此不采用“上传一次、后续只传引用”，继续压缩直传；没有引入图片服务或变更权限边界。

重复传同一张图时，进程内复用已经编码好的 data URI，缓存键包含源字节和全部编码配置。最多缓存 16 张、每张不超过 1 MiB 且不超过 640×480 像素；较大的图片正常编码、不进入缓存。返回的编码元数据独立复制，WebP 回退结果不会阻止编码器恢复后的重试。缓存省去重复编码的 CPU 时间，**请求仍发送完整图片，网络传输字节和视觉 token 不因此减少**。

历史协议核对来自 `codex app-server generate-ts --experimental`。当前协议以固定 SDK/CLI 0.160.0 的生成 schema、对应源码和下面的本地实际请求测试为准。

## 保存与历史读取

worker 直接把相机数组编码为 `.jpg`，通过 `rgb_path`、`image_encoding` 返回。读取端仍接受旧的 `png_path`。Harness 对已校验哈希且质量匹配的 worker JPEG 直接入库，不再压一遍 JPEG。旧格式或其他来源图片按当前存储质量转换；旧运行中已有 artifact 不改写。

新 `RobotBackend` 总会给 worker 传递冻结的设置。已启动的旧父进程如果随后拉起新 worker，因为没有这一字段，worker 保持其旧 PNG IPC 格式，避免运行到一半突然更换图片契约；新启动的完整进程使用上述 JPEG/WebP 默认值。直接手工启动 worker 的诊断场景也需要传递 `AURIGA_IMAGE_SETTINGS` 才启用新格式。

新生成且位于同一运行目录内的不可变 capture 文件，与 Store blob 通过硬链接共享数据；保留两个访问路径但只占一份图片空间。跨文件系统或不允许硬链接时保留两份有效 JPEG，不让空间优化导致观察丢失。已经发布的图片仍支持 `history_view_observation`、局部视觉比较、Sol 复核、π0.5 的 RGB 读取，以及网页观察检查。

裁剪图片保存为 JPEG；原始观察保留，ROI、缩放倍数与像素中心变换照常记录。开发诊断的几何 overlay 也保存为 JPEG。发送时统一使用传输配置。这里的“原始观察”指裁剪前的完整相机画面，**新运行不再保存无损相机 PNG**。

录像仍复用现有 FFmpeg/H.264 导出，保留三路相机与帧号、PTS、仿真步映射。仅为录像生成、未发布的帧是临时 JPEG：三个视频全部经过 ffprobe 检查 codec、像素格式及完整解码帧数，且索引写入成功后，才删除这些临时文件。导出失败保留输入；重复导出返回已验证的 manifest。清理失败保留文件并在 manifest 中记录。

默认仍保留已发布的观察图片。需要释放空间时，可以显式对一个已结束、进程已退出的运行执行离线归档。

## 可选的结束后视频归档

```sh
mamba run -n Auriga python scripts/compact_observation_images.py /absolute/path/to/ended-run --apply
```

`--apply` 会删除校验成功的 RGB 原件，是明确的有损归档选择。要求 `runtime.json` 有完成时间、状态为 `completed/stopped/failed`、没有活动动作且记录的进程已退出；还要取得该运行的 `gateway.lock`。没有后台自动清理，也不在机器人动作期间执行。

执行前，查看服务需要使用支持归档索引的读取接口，网页需要完成常规构建；旧服务不识别归档索引。

现有任务录像按仿真步去重，不能覆盖每一次观察。因此归档从已发布的 RGB 独立制作视频：同一相机和尺寸一组，每段最多 16 张不同内容的图片，H.264 CRF 18、5 fps；不缩图。正常尺寸用 yuv420p，奇数边长用 yuv444p。这里的帧率只用于归档容器，**不表示真实任务时间**；任务时序仍以原观察的 `sim_step`、时钟和 observation ID 为准。同字节图片只保存一帧，多个原引用可指向同一个已校验帧。

每帧索引保存源图片 SHA256、视频引用、段内帧号、宽高、解码器版本与重建 PNG 的 SHA256。所有候选视频编码结束并逐帧解码、核对帧数后，才删除对应 RGB blob 和本运行内内容相同的 raw capture 文件。若视频不比源图小，该段保留原图。非 RGB 或 observer artifact 共用的 blob 不删除；原始模型消息、动作记录、其他录像和场景库不清理。索引先持久化，重复执行可复用已验证的视频并补做清理；报告汇总既有和新增归档，不会把第二次执行误报为归档数量清零。

读取接口区分原件与重建：

- `Store.resolve_artifact` 和默认 `GET /v1/artifacts/{id}` 仍要求原字节。归档后对应原件不可用，HTTP 返回 404，不把重建图伪装成原件。
- `Store.resolve_image` 和 `GET /v1/artifacts/{id}?view=image` 明确允许读取重建帧；原图还在时优先读原图。重建图片返回真实 PNG MIME 和实际 SHA256，另带 `X-Image-Reconstructed: video`、`X-Source-SHA256`。
- 历史观察、ROI 裁剪、视觉比较、Sol 复核、π0.5 图片读取使用上述图片接口。返回元数据注明 `reconstruction`；观察 HTTP 视图增加 `image_reconstruction`，不修改原始不可变 observation。网页相机选项和帧说明显示“视频重建（有损）”。
- 视频仍使用原文件响应，保留 Range 请求；归档帧按段内帧号顺序解码，不用可能跳到邻近关键帧的时间定位。

重建图片经过又一轮有损编码，不能恢复 JPEG 原字节。原始模型回包仍保留实际发送图片，但 `audit_run.py` 对已删除的原始源图会明确报告“exact source-byte audit incomplete”，不会宣称完整源字节审计通过。需要保留原图用于精确复现或独立场景引用的实验，不执行此清理。重建哈希用归档时的解码结果核验；后续若升级解码器产生不同像素，会明确拒绝哈希不匹配的结果。

每个运行的 `image-archive.json` 和 `runtime/image_archive` 记录归档段、删除的不同内容原图数量、源图字节、视频字节及清理错误。空间统计仅覆盖被替换的 RGB blob，不代表整局目录（还含原始模型消息、其他媒体）的压缩比例。

## 模型请求的上下文与图像压缩

以下是 actor 请求从 Harness 到上游各层可能缩减内容的全部手段，按作用层次排列。“改写历史”指发往上游的请求不再是上一请求的严格延伸，上游 prompt cache 会从改动处失效。

| 层次 | 手段 | 开关（默认） | 改写已发送历史 | 代码 |
|---|---|---|---|---|
| 单张图编码 | WebP 85 发送、JPEG 95 存储，不缩图 | `images.transport_format` 等（`webp`） | 否 | `auriga/core/storage/images.py` |
| 当前回包 | 观察元信息精简 | `features.actor_payload_profile`（`lean_v1`） | 否 | `harness.py`、`actor_payload.py` |
| 当前回包 | 相对上次已见画面省略无变化相机 | `images.feedback_policy`（`all`，即关闭） | 否 | `view_selection.py` |
| 单次请求 | 同一请求内完全相同图片换为引用 | `images.reuse_exact_images`（`true`） | 否，只替换同请求内后出现的副本 | `image_reuse.py` |
| 代理 | 请求体 > 4.5 MB 时「保首尾、压中段」一次性裁剪，触发后窗口锁定到下一次原生压缩 | `images.trim_oversized_requests`（`true`） | 每个原生压缩周期一次 | `model_proxy._prepare_responses_body` |
| 代理 | 转发请求内联图片数 > 60 时同一窗口裁剪到约 40 张，窗口同样锁定 | `images.trim_max_inline_images`（`60`，`0` 关闭；需开启上一行） | 每次超限一次 | `model_proxy._prepare_responses_body` |
| 代理 | 上游 `image_compression_failed` 后继续裁剪重试 | 同上 | 仅失败的那次请求 | `model_proxy._complete` |
| 代理 | 历史投影：闭合块非锚点步换摘要和图像占位（分块 B=8 或末尾 k 步） | `features.lean_projection`（`false`）、`lean_projection_block`、`lean_projection_k` | 分块模式每 B 步一次 | `lean_projection.py`，见 [lean_context](../lean_context.md) |
| 代理 | 决策提醒：关闭投影时只在 `/responses/compact` 注入；分块模式放在固定槽位 | 随 `lean_projection` | 否 | `codex._task_decision_context`、`lean_projection.place_block_reminders` |
| 原生 | Codex 自动上下文压缩（`/responses/compact`） | Codex 自带 | 是，压缩后重新建立前缀 | 原生 |

代理裁剪与 `lean_projection` 相互独立：后者为了降低每次请求的上下文主动丢弃历史细节，前者只在上游会拒绝时才动图片，文本、数值和轨迹不动，占位文本保留 `observation_id` 与相机，可用 `history_view_observation` 回读。关闭 `trim_oversized_requests` 后请求原样转发，超限时由上游返回 `image_compression_failed`，代理不重试同一请求体，该局按模型请求失败处理。运行记录 `runtime/model_image_policy.oversized_trim` 保存实际取值，每次改写记一条 `model_images_trimmed` 事件（原因、窗口起止、上一窗口、前后字节数）。

### 已知问题

上游实际限制的是单请求图片数（64 张），不是体积。在实际请求中观察到，图片数未超限时 9 MB 以上的请求也被接受，而只有 3–4 MB、但含 65 张以上图片的请求返回 `image_compression_failed`。因此 4.5 MB 字节阈值会删去上游本可接受的图片，又挡不住超过 64 张图的请求，只能靠失败后重试。

`images.trim_max_inline_images`（默认 60）补充按图片数触发：图片数超限时复用上面的「保首尾、压中段」窗口和占位文本，目标约 40 张，`model_images_trimmed.reason` 记为 `image_count`（或与字节同时超限时 `body_bytes_and_image_count`），并记录 `images_forwarded`。字节阈值保持不变；该规则对 `image_compression_failed` 次数、缓存命中和任务分的影响仍需实验确认。

### 缓存命中观察

以下观察来自上游返回的 `usage.cached_tokens / input_tokens`，不是回放推算：

- 决策提醒放在每次请求末尾时，请求不再是前一请求的严格延伸，token 加权命中率约 35%；改为仅在 `/responses/compact` 注入后，命中率回升到约 77%。
- 代理裁剪真正触发的那次请求命中率明显下降，随后因窗口锁定而恢复；锁定期间请求的整体命中率与未裁剪请求接近。
- 不同批次的场景集合和其他 Harness 改动不同，这些命中率差异不是严格受控比较，也与任务成功率无关。

## 模型实际输入的证据

### 可选 adaptive v2 反馈

`images.feedback_policy="all"` 仍是默认值。选择 `adaptive` 后，正常完成的单动作反馈和普通模板末端反馈可以逐相机省略相对模型已见基准未发生显著像素变化的画面；head 和参与运动的腕部相机也适用。每次观察仍保存全部原图，省略项带当前 observation ID、基准 observation ID 和 `history_view_observation` 检索参数。首次发送、显式观察或历史查询、失败/执行不确定、交接或支撑变化核验仍返回完整视角。模板成功状态只表示到达人工视觉复核边界，不能证明物体被夹住、释放成功或任务完成。

基准只从返回给主模型的完整未裁剪画面建立，程序内部观察、numeric-only、裁剪和已省略画面不推进基准。run/episode/environment 身份、尺寸、相机校准或上下文 epoch 不匹配时重新发送；上下文压缩及会话替换会清空基准。像素阈值见 `auriga/perception/view_selection.py`，其结果只表示低于图像差异阈值，不能证明任务语义未变。

目前基准在工具返回内容构建完成时登记，**并非提供商接收 ACK**。最终 actor 投影失败或回复 `success=false` 时，Harness 写入唯一的 `tool_output_failed` epoch；如果工具回复写入 Codex RPC 失败，`RecordedClient._reply_request` 写入唯一的 `tool_reply_delivery_uncertain` epoch。下一次反馈重新发送全视角，并恢复 program 补充图片和日志，即使继续使用同一个会话。上下文压缩、native 窗口压缩和 app-server 替换也失效旧基准。发送记录与 provider 请求仍需分别核对；不能单凭基准记录断言提供商已接收图片。

该缓存依赖 actor 的 direct 工具路径。恢复 code-mode 时，native RPC 成功仍可能随后变成 text-only、仅存储结果或宿主失败；没有 typed 图片交付确认就不能沿用视觉去重基准。

`tests/test_codex_image_transport.py` 使用独立临时 `CODEX_HOME`、`--no-daemon app-server`、空白子进程环境和 loopback 假 Responses 服务，检查实际 POST 的工具目录、独立图片类型、解码后的源字节及文字日志。测试只验证 native/model-request 边界，不启动真实模型或机器人，也不证明视觉理解或任务成功。程序环境的初始化同时允许当前 `sys.prefix` 和基础解释器目录，以兼容 symlink venv；私有运行父目录仍不可读。

图片压缩会改变字节，但不改变像素坐标。发送记录分别保留：

- `rgb_ref`、`source_sha256`：保存的源 JPEG 或已有历史图片。
- `sha256`、`encoding`：实际发送字节的哈希、MIME、尺寸、质量、编码器版本等。
- 裁剪时的源图片引用、ROI 与 `pixel_transform`。

传输用 WebP 不再另存为一份图片 artifact；原生工具回包/私有模型请求仍提供实际发送字节的证据。`scripts/audit_run.py` 兼容旧的同字节记录和新的压缩发送记录，并核对实际工具回包。不能把保存的 JPEG 哈希当成 WebP 哈希。

## 验证与实际效果

实际编码器对 24 张 640×480 的 RoboDojo 相机图片执行完整新链路：

| 数据 | 合计大小 | 相对旧 PNG |
|---|---:|---:|
| 原 PNG | 9,180,221 B | 基准 |
| JPEG 95 保存 | 2,100,382 B | 减少 77.12% |
| 从保存 JPEG 生成 WebP 85 | 736,630 B | 减少 91.98% |

传输压缩耗时中位数约 21 ms/张；这是小样本编码耗时，不是整局延迟实验。双重有损编码的 RGB SSIM 均值为 0.9372，不表示“保留信息百分比”或任务成功率。

同一耳机历史序列的 11 对相邻帧，PNG、JPEG 95、JPEG 95→WebP 85 都通过原有跟踪阈值；最大单步位移偏差分别为 0、0.1281、0.3224 像素，最后一档的最终 ROI 纵向差 1 像素。

相同尺寸的压缩图像不会按文件大小同比减少视觉 token；不据此承诺费用、任务成功率或整局延迟改善。

可运行检查：

```sh
PYTHONDONTWRITEBYTECODE=1 mamba run -n Auriga python -m unittest discover -s tests -p test_images.py -v
PYTHONDONTWRITEBYTECODE=1 mamba run -n Auriga python -m unittest discover -s tests -v
```

专门检查使用合成图像及实际 Pillow/OpenCV/FFmpeg，覆盖三路采集、硬链接去重、不可变历史读取、裁剪坐标、JPEG 到 π0.5 输入的读取、WebP 回包、编码回退、配置冻结、录像失败不删图与成功后仅删除临时帧。没有启动机器人、主模型任务或子 agent。

RoboDojo 环境的 Pillow 11.3.0 与 Harness 环境的 Pillow 12.1.1 对上述 24 张图生成了完全一致的 JPEG。

### 视频归档实测

对一个已结束运行的独立副本（77 次观察、三路相机共 231 张不同内容的图像，转换成 JPEG 95 存储格式）执行归档：

| 项目 | 实测 |
|---|---:|
| JPEG 95 源图合计 | 18,975,352 B |
| H.264 归档合计 | 3,874,037 B |
| RGB 存储减少 | 79.58% |
| 完整归档耗时，含多次校验 | 67.80 s |
| 单张重建读取中位数 | 93.06 ms |
| 重建图相对 JPEG 的 RGB SSIM 均值 | 0.9759 |
| 24 张样本首次编码 / 缓存命中中位数 | 17.98 ms / 0.030 ms |

231 张重建图均保持 640×480，原实验源文件 SHA256 复查一致。已有 11 对耳机移动画面，在 JPEG 和视频重建两个分支都通过原视觉跟踪阈值，最终 ROI 相同，最大单步位移差 0.2313 像素。这是单序列离线检查，SSIM 也不代表任务成功率。
