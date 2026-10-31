# 实验库与常驻网页

实验库入口是 `http://127.0.0.1:8780`。它独立于各次实验进程，使用 Auriga mamba 环境里的 Python、aiohttp 和现有网页，不加载模型或 Isaac，也不分配 GPU。实现位于 `auriga/apps/library/experiments.py`、`auriga/apps/library/service.py` 和 `auriga/apps/evaluation/recording.py`。

## 保存规则

默认根目录为 `~/exp/auriga`。新实验无需手工指定输出目录，各入口按下面的规则分配唯一目录：

```text
~/exp/auriga/
  runs/YYYY/MM/DD/category/task/run-id/
    experiment.json       # 分类、任务、标题、标签、创建时间与身份
    runtime.json          # 原子更新的心跳、阶段、终态、live endpoint
    summary.json          # 结束结果；任务得分、接口验收分别记录
    manifest.json         # 可用时保存实际配置与版本
    events.sqlite         # 支持协议回放的运行才有此文件
    blobs/                # SHA256 内容寻址的 RGB、轨迹、媒体、评测
    terminal.bin          # 确实存在原生 Codex 终端时才保存
    ...                   # 原生报告、媒体和各 producer 的结果
  service/
    installation.json     # install_library_service.py --install 的安装回执
    experiment-groups.json  # 可选实验登记，格式见下文
```

目录日期采用 UTC+8（Asia/Shanghai）的分配日期；JSON 时间戳保存 UTC，网页日期筛选与显示采用 UTC+8。归档导入时目录日期是导入日期，兼容字段 `created_at` 的原来源由 `date_source` 记录，导入时间为 `imported_at`；旧档有以报告 mtime 代替 created_at 的情况，不能将该字段当作实际运行时间。实际开始/结束使用下文的独立时间投影。原记录的身份是 `evidence_run_id`；网页路径使用按目录生成的稳定 `exp-...` ID，避免不同来源同名运行互相覆盖。

实验库也会发现根目录内自定义布局中已注册的 `experiment.json`，例如 `benchmark/<group>/<batch>/runs/<case>/`。扫描跳过符号链接和私有凭据目录，遇到已注册运行后不再扫描其内部，避免把归档证据重复列为实验。没有 `experiment.json` 的普通报告目录不会自动入库。

建议分类为 `main`（主实验）、`reference`（API/黄金参考）、`acceptance`（联合验收）、`validation`（机器人矩阵）、`geometry`（真实记录几何）、`synthetic`（合成夹具）。任务如 `stack_bowls`、`local-scene`。标题与标签可用中文；路径分类、任务和 run ID 使用字母、数字、点、下划线、连字符。显式 `--run-dir` / `--output` 仍可用；根目录以外的旧记录要通过导入进入实验库。

## 使用与更新

主页默认进入**实验对比**，左侧按实验方向折叠，各方向内区分方案版本与对照。选择方案或“对比整个方向”会一次选中该方向全部版本及它们各自的 baseline，数量不限。名字直接说明改了什么，例如“夹爪动作后重新观察 · 第1版”；同方案的准备、故障恢复和补跑收进执行详情。概览显示整组均分、成功数／预定局数、已评分覆盖、同场景配对分差和迭代记录。左侧可搜索方案、分支或实验组，按协议筛选，并勾选跨方向自选对比；比较基准可切换，手动选择不会改写已记录的 baseline 关系。

视频表按 task、variant、eval seed、layout 的完整身份配对，每行一个场景、每列一个方案，全部场景同时列出；格内显示得分、开始/结束时间和录像。表头和场景名固定，可横向、纵向滚动；画面外的媒体按需加载。每行有“播放这一行”，同步控制只作用于当前行，支持头部／左右腕统一视角、暂停、倍速与按录像秒数拖动。时间从各自录像起点对齐，不保证动作阶段或物理时刻同步。未评分、缺失视角、缺失录像和多次尝试未明确选定分别显示，恢复历史逐次显示时间并保留运行入口。选择保存在 URL 中：`scope=direction` 保持方向全选，`scope=custom` 保持自选列；旧的显式 compare 链接保留原自选语义。刷新不重建同源视频。

### 运行时间及来源

Catalog、运行详情和每个 attempt 均返回 `started_at`、`finished_at`、`registered_at`、`imported_at` 及对应的 `*_source`。时间规范化为 UTC ISO 字符串，页面统一以北京时间（UTC+8）显示年月日时分秒，与浏览器本地时区无关。开始优先采用原档身份匹配的首条 `run.started`，缺失时读取原始 summary/runtime/report 的明确开始字段；结束只接受这些原始文件的明确结束字段。停止事件、最后日志、heartbeat、登记、准备和导入时间不能替代结束或启动。导入记录只从 evidence 读取原始执行时间，根目录 runtime 仅描述导入生命周期。

缺失、无时区、单位不明的数字时间及结束早于开始的无效结束保持 null，并保留 `time_warnings`。组和执行批次的范围由全部唯一 attempts 的最早已知开始、最晚已知结束计算，`time_coverage={run_count,started_count,finished_count}` 明确覆盖范围；部分覆盖不代表完整持续时间。manifest 的 `prepared_at` 仍单独保留，`created_at` 不再作为网页的实际开始依据。运行列表的“运行开始”排序和日期筛选使用 `started_at`，未知值排序置后，带日期条件时不混入未知时间。

时间修正只作用于只读投影，原始归档不重写。归档数据库使用现有 `open_read_store`；时间缓存绑定文件身份和 WAL，稳定的重复扫描不重新打开数据库。原生运行首次归档的启动预热与已缓存扫描耗时分别记录，不向每个页面请求临时扫描归档。

**全部运行**保留分类、状态、任务、标签、名称/运行 ID、日期范围、排序与分页。没有分组证据的记录仍在此处查看。选中记录后显示任务得分、报告、媒体及原始信息。支持协议日志的记录可以打开原工作台，查看观察、动作、轨迹、事件、场景版本和原生终端；仅有报告或视频的记录直接查看实际已有的证据，不生成终端。

### 实验组与比较口径

只读 `GET /v1/experiment-groups` 来自 `experiment_comparisons.py`：扫描实验根目录的 benchmark manifest，结合明确的 case tags、`experiment_panels`／`case_roles`、协议身份和归档运行身份生成分组。随 library 快照后台刷新，页面请求不另行扫描或修改原始归档。API 只投影评分、代码来源和允许的实验条件，不交付完整配置、凭据或内部错误内容。

- 整组均分使用正式评分的归一化分乘以 100，并同时显示评分覆盖；未评分不会补零。成功数以协议预定局数为分母，缺少协议分母时显示 `?`。
- 组间、前后迭代的分差仅使用相同场景中双方均有正式评分的交集。场景集合不一致时明确提示；数字差异不单独证明某项源码变更的因果收益。
- 登记文件用 `execution_groups` 将同条件的准备与恢复并回原方案，保持原方案的固定协议和分母。按原方案在先、登记的执行组随后，取每个场景首个正式分数，包括 0 分；后续高分不会替换。只有未评分或缺失场景可由恢复结果补齐，全部尝试在录像下方可查看。无正式分数时显示最后一次未评分尝试的状态和录像，分数保持 null。未经显式合并的多次尝试继续显示待确认。
- 可选登记文件位于 `<实验根目录>/service/experiment-groups.json`，不存在时只使用自动扫描结果。包内 `auriga/apps/library/experiment_group_relations.json` 使用相同格式（发布版不含条目），其条目只在对应 manifest 存在时生效；根目录登记随后按精确的根目录相对 `manifest` 路径和 `condition` 覆盖。组 ID 只由这两个字段决定，改中文名不会改变链接。登记只描述实验定义、来源和关系；分数仍从原始归档读取，不在登记文件里维护第二份分数。

### 登记文件格式

登记文件是一个 JSON 对象，实验库只读取其中的 `groups` 数组，其他顶层键被忽略：

```json
{
  "version": 1,
  "groups": [
    {
      "manifest": "benchmark/my-direction/v2/queue-manifest.json",
      "condition": "candidate",
      "name_zh": "夹爪动作后重新观察 · 第2版",
      "purpose": "检验夹爪动作后重新观察能否减少空抓",
      "series": "gripper-observe",
      "series_name_zh": "夹爪动作后的观察",
      "iteration": 2,
      "role": "candidate",
      "experiment_nature": ["消融"],
      "parent": {"manifest": "benchmark/my-direction/v1/queue-manifest.json", "condition": "candidate"},
      "baseline": {"manifest": "benchmark/my-direction/v2/queue-manifest.json", "condition": "baseline"},
      "recovery_of": null,
      "relation_status": {"parent": "confirmed", "baseline": "confirmed"},
      "baseline_reason": "同批对照，task/seed/layout 与预算一致",
      "source": {"branch": "feature/gripper-observe", "revision": "<commit>"},
      "evidence": ["benchmark/my-direction/v2/README.md"]
    }
  ]
}
```

每个条目必须有 `manifest`（实验根目录内的 `.json` 清单，按根目录相对路径记录；不能位于 `private` 或被排除的目录）和 `condition`（清单中 case 的实验条件，通常是 case 的 `experiment_condition`）。条目格式无效或路径越界时，该条目被跳过并在分组 API 的 `errors` 中报告，其余条目照常生效。可选的说明字段原样投影到分组：`name_zh`（中文名）、`label`、`purpose`（检验什么）、`series` / `series_name_zh`（方法系列）、`iteration`（方法轮次，未知为 null）、`role`（建议使用 baseline/candidate/recovery/benchmark/validation/preparation）、`experiment_nature`（字符串数组）、`note`、`baseline_reason`、`source`（读取其中的 `branch` 与 `revision`）和 `evidence`（字符串数组，显示为关系证据）。关系字段如下：

| 字段 | 含义 | 约束 |
|---|---|---|
| `parent` | 本方案从哪个实验方案迭代而来 | `{manifest, condition}` 或 null；显式 null 清除扫描自动推断的关系；目标组不存在时显示 `parent_group_unavailable` 警告。不能用执行尝试编号推断。 |
| `baseline` | 本组应比较的对照 | 同样使用精确引用；可以不同于 parent。历史参考须在 `baseline_reason` 写明来源、模型或环境混杂。 |
| `recovery_of` | 本次是哪个批次的补跑/恢复 | 保留原批次和补跑各自结果；不据此递增方法轮次。 |
| `execution_groups` | 并入本方案的准备、恢复及重复执行 | 按明确顺序列出 `{manifest, condition}`，不含本方案自身；原条目保留，旧链接映射到本方案。 |
| `relation_status` | parent / baseline 各自是否查明 | 对象，`parent`、`baseline` 各取 confirmed、unresolved 或 not_applicable；其他取值被忽略。 |
| `baseline_reason` | 为什么选这个对照 | `confirmed` 只表示引用有证据，不自动证明是严格单变量对照。 |

登记可以显式引用自动扫描未发现的 root 内 JSON 清单。以下字段只用于有证据的历史修复：`condition_aliases`（字符串数组）把这些条件名合并到本条目的 `condition`，用于同一方案的并行执行副本；`protocol_cases` 明确绑定原本缺失或条件名不一致的协议，case 身份不能重复或与清单冲突；`manifest_copies` 声明备份清单，仅内容完全相同且路径合法时不重复列组；`execution_run_dirs` 限定本批实际启动的归档，防止后续复用未启动路径的成绩回填到旧批次，它保留原协议和 missing 行，不缩小分母。登记不移动录像或重命名运行目录，也不能从当前 worktree HEAD 补造历史 revision。

分组 API 返回 `protocol_denominator_known`、`scope`（`protocol`／`recovery_subset`／`recorded_cases`）、`protocol_case_count`，以及逐场景 `selection`、`selection_reason`、`run` 和所有 `attempts`。合并组的 `selection` 为 `first_scored`、`latest_unscored` 或 `missing`；每个 attempt 带 `execution_group_id`，组级 `execution_groups` 含原方案和后续执行的来源摘要，`id_aliases` 保留被折叠的旧 ID。旧 ID 的详情请求返回规范组，网页同时修正 URL 中的选择。缺失引用、重复归属、引用环、协议或已知条件冲突时，整个关联合并保持原组并报错。归档身份冲突、缺失／重复身份不参与自动配对；无法归组的记录保留在全部运行中。

页面按需加载分组和录像信息。`GET /v1/experiment-groups?view=summary` 返回全部组元数据，每个 case 只保留 `case_key` 和 `run.summary` 中正式评分、成功状态及未评分判断所需的字段；未选定运行仍为 `null`。摘要保留精确的同场景配对统计与固定分母，省去逐场景身份、尝试和来源明细，并标记 `details_loaded=false`。选择组后，`GET /v1/experiment-groups/{id}` 返回完整组并标记 `details_loaded=true`；两者的组级 `revision` 均由完整组内容生成。原 `GET /v1/experiment-groups`（或 `?view=full`）继续返回完整列表。未知 view 返回 400，未知组返回 404。

`GET /v1/experiments/{id}/media` 只返回 `id`、`view_url`、`replay_available`、视频媒体清单，以及 `reports[].content.videos` 中供镜头选择使用的 `camera_id`／已清理文件名／`url` 描述；它不构建 task review，也不返回完整报告或服务器绝对路径。完整实验详情仍使用原 `GET /v1/experiments/{id}`。录像保存在本机磁盘，播放器选择源后通过原 files URL 按需读取，继续支持 HEAD 和 Range。

运行列表、分组摘要／详情、媒体清单及完整实验详情使用 `Cache-Control: private, no-cache` 和弱 ETag；客户端发送 `If-None-Match` 时，内容未变返回无响应体的 304。`observed_at` 的时钟更新不使缓存失效，路径、查询参数及实际返回数据参与校验；权限与路径检查仍在条件响应之前执行。JSON 根据 `Accept-Encoding` 协商 gzip／deflate，并返回 `Vary: Accept-Encoding`；视频文件与 SSE 不使用此 JSON 压缩路径。实际 HTTP、正式评分等价、镜头别名、权限及 Range 回归见 `tests/test_library_loading.py`。

后端每秒在后台扫描元数据与事件数据库/WAL/终端等关键文件，并验证活动 endpoint，完成后一次发布目录快照，通过 SSE 发出变化通知。列表、详情和运行请求读取最新已发布快照，不触发全目录重扫；新注册或终态更新最多等待一个扫描周期及实际扫描耗时。SSE 正常时网页不重复下载分组摘要；连接中断或上次刷新失败时每 5 秒重试，重新进入可见状态时立即刷新。对比页仅每 5 秒核对当前可见或正在播放单元格的轻量媒体清单，以发现晚到的录像；录像正文仍按需读取。未应用的筛选草稿、焦点、选中详情、展开报告和既有媒体播放器保留。

- **可控直播**：心跳有效，并且本机 gateway 的 `/v1/run` 身份匹配。原有单写入租约、请求 ID、原生问答/审批和动作控制继续由该 gateway 负责。代理每次请求附带运行 ID，gateway 拒绝身份不符的请求，避免端口被另一运行复用时误投动作；代理不自动重试写操作。
- **只读实时记录**：API 参考、机器人矩阵、几何等 producer 写 `recorded_stream` 心跳。机器人 producer 被动保存实际收到的 RGB、动作请求、进展、真实回执与评测；同一动作只下发一次，不增加观测、动作或重试。几何的实时阶段与结果在库详情查看。
- **历史回放**：实验完成或停止后同一 URL 自动切换到只读模式。没有心跳或 backend 身份不能验证时显示连接中断，保存的证据仍可读；不能由心跳缺失推断任务已经完成。终端与事件都有各自运行内游标，不复用另一实验的状态。

历史回放读取 SQLite 与原文件，不实例化 Workbench 或启动机器人。正常终端使用原始 PTY 字节，标记 `replay.kind=original_pty`；缺失或启动失败且有完整归档时，独立适配器将归档复制到私有缓存，探测数据库表/字段兼容性后，用隔离且禁止联网的原生 Codex TUI 重新显示保存的历史。该结果标记 `recovered_native_tui`、`original_recording=false`，保存 CLI 版本、schema 探测、来源 hash 与历史未变校验；它是派生显示，不是原始 PTY 或活动任务恢复。未知 schema 或不完整显示不会发布为可用缓存。

写接口拒绝，终端只读；闭合无 WAL 数据库使用 immutable 读模式，活动 WAL 使用常规只读模式以跟进新增事件。媒体支持 HEAD、Range 和 SHA256 响应头；文件通过流式 SHA256 校验后由文件响应发送，不将整个视频读入内存。校验缓存最多 512 项，文件设备、inode、大小、mtime 或 ctime 改变即重新校验，访问范围与路径仍逐请求核验。实际可播放性取决于保存编码；旧 mp4v 不兼容时仍可下载原文件。

工作台优先使用 `/v1/records` 增量投影：不可变 observation 按记录游标读取，mutable item/request 按持久修订游标读取；相同内容不会产生新修订。旧数据库仍可只读查看，不为回放升级原始证据。增量协议当前视图最多保留 2000 条 item/observation/request 与 5000 条阶段事件；达到窗口边界时显示“较早记录”，从服务器按原生 item 页读取历史，窗口之外的上下文明确标识。旧服务器不支持分页时保留旧接口完整返回的 item/observation/request。历史页最多显示 5000 条关联事件；达到此上限或当前阶段事件上限时，页面明确说明并提供完整 `/v1/events?after=0` 事件流入口，可将 `after` 设为已读 `server_seq` 继续读取。较早 item 页不能补回同一 item 超限的关联事件。原始 SQLite 与媒体保持完整；进入历史窗口不接收当前阶段更新，返回最新记录只重载页面，不启动运行。

`task_success=false`、`score=0` 显示为“否、0”；缺评测显示“未知”。接口验收通过、运动完成和任务成功是三个不同结果。几何实验结束也不代表机器人任务成功。

任务结果和分数可用性分别记录。已确认的启动无动作超时、动作进展超时、模型调用额度耗尽、实验时间预算耗尽及连续无法完成停止均记为任务失败，保留有效原生过程分。例如动作进展超时且过程分为 40 的运行显示“任务得分：0.4 · 任务成功：否 · 失败原因：动作进展超时”。归一化失败分取原生过程分除以 100；无独立过程分的二元任务失败记 0。已锁定的有效原生终局结果优先于同时发生的任务上限。基础设施中断、动作/停止未确认或无效 episode 仍单列未知，不借用局部快照伪造成功或分数；有效 episode 的已确认任务失败若缺少可核验分数，则显示失败及原因，得分不可用。

`failure_reason` 保存稳定代码、中文原因及判定来源；`termination_reason`、`termination_authority`、`native_episode`、`native_evaluation_snapshot` 保留实际停止及原生证据。历史上被旧策略排除的任务上限运行，只在清理已完成、运行身份、动作计数、评测 artifact 内容与 SHA256、最后评测事件均匹配，且之后无动作/重置时提供当前策略的有效结果；`scoring_reconciliation` 保存原分类和来源引用，原始归档文件不改写。实验库、明细、离线分数/费用报告及配对分析共享 `auriga.apps.library.scoring`，队列另保存有效摘要而保留恢复校验依赖的原摘要。成功率的分母仍是协议预定样本数，过程分 0.4 不代表成功率 40%。

新 benchmark 的 `actor_stall_policy` 在模型连续 5 个结束回答明确声明无法继续完成、期间没有原生动作进展时提前停止该局。harness 只读取本线程、本轮次已完成的 `final_answer`，用中英文明确无法继续的表述检测，不额外调用模型；有动作进展、换 episode/thread 或回答不再声明无法继续时清零，同一 turn 只计一次。第 5 次保存 `benchmark.actor_inability` 证据，停止原因是 `actor_repeated_inability`，随后沿用停止、原生 `final_check` 评测、录像和清理流程。结果记任务失败和当前有效过程分，仍记录 `termination_authority=harness` 及实际 `native_episode.ended`；未确认的停止或无效证据不强行计分。

## 启动与维护

```sh
# 先构建网页
npm --prefix web ci
npm --prefix web run build

# 临时启动，CPU-only
mamba run -n Auriga python -m auriga.apps.library

# 输出 unit 供检查；--install 安装并启用当前解释器对应的 user service
mamba run -n Auriga python scripts/install_library_service.py
mamba run -n Auriga python scripts/install_library_service.py --install

# shell 若未设置 user bus 环境，使用这两个变量
export XDG_RUNTIME_DIR=/run/user/$(id -u)
export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus
systemctl --user status auriga-library.service
journalctl --user -u auriga-library.service -n 50
systemctl --user restart auriga-library.service
loginctl show-user "$USER" -p Linger
```

安装位置为 `~/.config/systemd/user/auriga-library.service`，只管理这个 unit。配置 `Restart=on-failure`、UMask 0077；修改已有同名 unit 时先备份到 `service/`。`--root`、`--port`、`--scan-interval`、`--public-origin`、`--task-source-root` 与统一配置的 `library` 节写入 unit 的启动参数；未指定任务源码目录时使用统一配置的 `backend.root`。要在退出登录后继续运行，systemd 用户需启用 linger。服务绑定 loopback，并验证 Host/Origin。其他电脑访问可使用 SSH 转发，例如 `ssh -L 8780:127.0.0.1:8780 <user>@<host>`，再打开本机 8780。

远程访问需要自行部署带身份认证的反向代理。代理转发时须把 Host 头设为 loopback 地址（例如 `127.0.0.1:8780`），服务拒绝非 loopback Host；启动参数增加 `--public-origin https://library.example.com`（或配置 `library.public_origin`），允许浏览器从这个精确 HTTPS Origin 发起请求。该参数不接受结尾 `/`、路径、通配符或凭据，也不提供身份认证；认证与访问控制须由代理负责。网页使用同域 HTTPS/SSE/WSS，域名应挂载到根路径；通过认证的用户拥有原工作台的直播控制权限。

新运行默认自动入库，并选用空闲端口；GPU 必须按实际空闲状态选择：

```sh
mamba run -n Auriga python -u -m auriga.apps.gateway \
  --gpu 4 --category main --title 'stack_bowls 几何辅助' \
  --tag geometry-assisted --max-actions 10 --wall-budget-s 900

mamba run -n Auriga python scripts/reference_episode.py \
  --gpu 4 --category reference --title '简单 API 参考' --tag baseline

mamba run -n Auriga python scripts/robot_validation.py \
  --gpu 4 --category validation --title '机器人接口矩阵'

mamba run -n Auriga python scripts/probes/geometry_validate.py \
  --raw-recording <robot-validation-run>/calibrated-observations.jsonl \
  --train-indices 6,7 --heldout-index 8 --camera cam_left_wrist \
  --roi 100,150,450,100 --title '真实 RGB 留出几何'
```

这些命令的输出目录、心跳与最终结果进入同一根目录。独立场景服务继续用自己的 `--root` 和接口开发，不依赖实验库；场景服务开发目录本身不自动冒充实验记录。

## 原始轨迹下载

选中已结束的实验后，详情链接提供 **下载原始轨迹**。浏览器通过 `GET /v1/experiments/{catalog_id}/trajectory-bundle` 下载 ZIP，支持 HEAD、Range 与 SHA256 响应头。服务只根据目录中的实验 ID 查找原始证据，在独立缓存生成经过校验的轨迹包；导出不调用模型或 GPU。运行中、缺少证据或历史导入副本会显示不可用原因；文件存在但轨迹不完整的运行在导出时拒绝下载。

ZIP 可直接交给离线工具，在具有相同仿真版本和任务资产的机器上选择分辨率生成视频。文件格式、命令和重放限制见 [高清离线重放](hd-replay.md)。它只携带复现所需的动作、实测机器人状态和环境标识，不打包模型历史、服务器配置或凭据。

## 旧记录导入

```sh
mamba run -n Auriga python -m auriga.apps.library.experiments import \
  runs/<old-run> --category acceptance --task stack_bowls \
  --title '旧运行导入' --tag legacy
mamba run -n Auriga python -m auriga.apps.library.experiments list --category acceptance
```

导入拒绝尚有有效运行心跳或被 gateway 持锁的源目录。以复制方式保存原证据到 `evidence/`，逐文件验证源文件与副本 SHA256，并记录 `import-inventory.json`；凭据、`private/codex-home`、符号链接和非普通文件不归档。源文件不删除、不修改。需要折叠 WAL 时在临时副本上生成 `replay/events.sqlite`，原证据副本保留字节。含失败的记录继续保留，不被改写成通过。

证据和大文件只在实验根目录保存，不提交 Git。

实验详情页从 RoboDojo 任务源码读取评分规则。服务默认使用统一配置的 `backend.root`，任务源码位于其他位置时用 `--task-source-root`（或 `library.task_source_root`）显式指定；目录不存在时详情中的评分规则为空。
