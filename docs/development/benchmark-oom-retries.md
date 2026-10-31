# Benchmark OOM 重试

`scripts/benchmark_queue.py` 对**没有形成有效原生终态、且有原生 GPU OOM 证据**的失败安排新 episode。普通策略失败、`native_step_limit`、模型请求预算终止及已完成 episode 后的视频/清理 OOM 不重跑。首次未评分尝试仍保留；重试不会把它改写成首次成功。

## 配置与调度

manifest 可配置：

```json
{
  "oom_retry": {
    "max_retries": 2,
    "initial_delay_s": 60,
    "backoff_multiplier": 2,
    "max_delay_s": 300
  },
  "cases": []
}
```

`cases` 仍填写现有完整 case，不能留空。上例是默认策略：最多重试 2 次，共 3 次尝试，先等待 60 秒、再等待 120 秒。`max_retries: 0` 关闭自动重试，上限为 20；其余数值必须有限且至少为 1。等待退避不消耗 GPU 等待超时；到期后重新检查 GPU 利用率、剩余显存、共享预留、启动速率及 manifest 并发限制。当前默认 11 GiB/case、零额外余量，动态补位规则保持有效。其他待启动任务可先运行。

每次重试重新启动 gateway，保留原 case 的源码目录、任务、seed、layout、guidance 等参数，使用原始 `run_dir` 的兄弟目录 `<run_dir>.oom-retry-01`、`02` 等。目录已存在即拒绝，绝不复用轨迹，也不传 `--resume`。模型请求与动作预算按每个新 episode 的原规则重新计算，不改变预算值。

只有 gateway 已退出且 worker 退出得到确认时才可重试。现有 worker PID 身份核对及租约隔离仍生效；退出不确定会保留显存预留、标记 `stop_unconfirmed` 并停止队列。SIGINT/SIGTERM 会取消尚未启动的重试，不触发新的尝试。

## 证据与结果

OOM 检测优先采用 `summary.json` 的结构化 `infrastructure_error` 中原生错误标识，例如 `robot_gpu_out_of_memory:`。

运动规划器（cuRobo/torch）在 worker 内抛出的 `OutOfMemoryError` 会被 worker 捕获，成为 `backend_error: OutOfMemoryError: ...` 失败回执或 `OutOfMemoryError: ...` 错误回包，旧版本因此把它当作普通规划失败，整局可能以有效 0 分结束。现在 `RobotProcess` 按回包中的异常类型（不按引用的消息文字）以及 worker 日志中的 `CUDA out of memory`、`torch.OutOfMemoryError:` 等签名，把它与 Vulkan/PhysX 分配失败同样标记为 `robot_gpu_out_of_memory:`：该请求按 unknown 处理、不重放动作，`wait_turn` 以 `robot_gpu_out_of_memory` 停止本局，summary 的 `infrastructure_error` 写入同一标识，结果为 `unscored_infrastructure` 并可进入本页的 OOM 重试。原生 episode 已先结束时仍以原生终态为准。签名列表由 `auriga.robot.backends.robodojo.robot_process.native_gpu_oom_signature` 与队列证据共用。旧版本的 `bootstrap_failed` / worker disconnect 可由 `private/robot/robot-worker.log` 中 CUDA、Vulkan 或 PhysX 的明确分配失败补证。只在失败退出时读取一次最多 1 MiB 的日志尾部；保存源路径、签名、字节位置、摘要及 SHA-256，不反复全量扫描。不扫描 actor 对话或 queue stdout，不把任意 `oom` 文本、SIGKILL 或 exit 137 当作 OOM。超出日志尾部的旧错误可能无法识别；这种情况不会自动重试。

`state.json` / `events.jsonl` 持久保存每次尝试及重试决策：

- `cases` 每行是一段独立轨迹，含 `logical_task_id`、`attempt_number`、前后 run 关联、原始 summary、OOM 证据、`retry_status`、退避秒数和 UTC epoch `retry_not_before`。
- `logical_tasks` 按逻辑任务聚合，分别给出 `first_attempt_outcome` 与 `final_attempt_outcome`，保留全部 attempt、`had_oom` 与已启动重试数。
- `retry_status` 的 `pending`、`scheduled`、`exhausted`、`blocked_cleanup`、`cancelled` 区分等待、已安排、用尽、清理阻塞和停止取消。重试用尽时队列返回非零；重试成功可返回零，但所有历史 OOM 与首次结果仍在报告中。

旧分析脚本若逐行统计 `cases`，统计的是尝试数；逻辑任务成功率必须按 `logical_tasks` 聚合，并同时报告首次和最终有效尝试。恢复队列还把原始记录保存为 `prior_attempts`，其中包括源 queue 路径和 state/summary SHA-256。

## 已启动旧队列的接入与恢复边界

运行中的冻结队列继续使用旧调度器，不热改源码、不重启。等待其自然结束后，可用新调度器补排：

```bash
python -m scripts.benchmark_queue \
  --recover-oom-from /absolute/path/to/old-queue \
  --output /absolute/path/to/new-oom-retry-queue \
  --wake-thread ''
```

上例以空字符串明确关闭完成通知，避免读取 `~/.config/auriga/completion-thread` 中保存的默认接收会话后通知旧会话。

恢复只选择每个逻辑任务最后一个已启动尝试：必须有 OOM 证据、gateway/worker 退出确认及剩余重试额度。已成功、普通失败、预算终止和清理未知均不进入新队列。恢复时重读磁盘 summary 并与队列 terminal state 核对，不一致即拒绝；旧缓存的 OOM 标签不能覆盖原生终态。源 manifest、state、summary、轨迹和协议只读；新队列保留 attempt 链和证据哈希。原 case 的 `cwd` 继续指向冻结实验源码，原 gateway 命令（包括解释器、观测协议和所有开关）只替换 GPU 与新 run 目录，并传递给后续重试，避免新调度器默认值改变旧协议。新队列的输出目录必须不存在；确定性的重试目录防止已运行尝试被再次覆盖。已有待重试记录保留其退避截止时间，旧队列没有重试时钟时从恢复调用开始计算。

恢复与重试绑定原 attempt 记录的 `settings_file`，核对 `codex_home` / `price_card` 路径与原记录一致；文件缺失或路径变化即拒绝。凭据内容不读入重试报告、不归档，路径内的配置/认证文件内容仍可能由用户更新。原 attempt 明确未使用 settings 时，不引入当前默认 settings 文件，报告以 `inherited_environment_unrecorded` 标明旧继承环境没有留档、无法重建。缺少原 gateway command 或 experiment settings 记录的旧队列不能自动恢复。

现有队列不支持原地断点续跑。`--recover-oom-from` 只接受有 terminal state 的队列，拒绝仍为 `running` 的活跃队列或崩溃残留状态，也拒绝记录中仍有 `starting` / `running` 的队列；不凭陈旧 PID 接管进程。`stop_unconfirmed` 的尝试不能靠该接口解除隔离。没有合格 OOM 时明确报 `No cleanup-confirmed OOM attempts with retries remaining`，不会创建新队列。恢复是显式新调度调用；不会在用户停止队列后自行恢复。

为既有实验补排时，应在实验记录中关联原队列、新队列与所用重试策略，并保留原协议中关于是否允许替换尝试的原文。运行中的实验不应因升级调度器而重新启动；没有 OOM 就不补排。

## CPU 验证

`python -m unittest tests.test_benchmark_queue -q` 覆盖原调度行为，以及原生 OOM 样本、普通失败/未知 kill 排除、原生终态优先、清理未知隔离、独立 run 和关联、退避与新 GPU 准入、等待超时边界、上限用尽、停止取消、terminal queue 恢复、历史哈希与目录防覆盖。使用合成 GPU 采样和 CPU 假进程，不启动仿真、模型或实际 GPU 分配。
