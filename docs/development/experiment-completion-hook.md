# 实验完成后通知主会话

实验由主会话安排；一批实验结束后，`scripts/experiment_hook.py`（实现位于 `auriga/apps/evaluation/completion.py`）向同一个 Codex 主会话发送一句话：

```text
实验已结束，结果目录：/absolute/path/to/queue
```

主会话根据已有上下文自行读取结果、决定改进和下一批实验。hook 不附加研究策略、输出格式、迭代次数或开发限时。主会话发布队列后结束当前回合，等待阶段只有普通后台进程，没有模型轮询。

若队列进程已退出但记录不完整，消息改为说明部分实验停止状态尚未确认；若调度已结束但正常关闭未确认或发生强制回收，消息提示检查清理记录。两种消息同样附结果目录。

## 由队列发送通知

接收通知的主会话 UUID 按以下顺序确定：命令行 `--wake-thread UUID`；统一配置的 `queue.wake_thread`；最后是 `~/.config/auriga/completion-thread` 文件中保存的 UUID。`--wake-thread ''` 显式关闭通知且不读取该文件；`--no-wake-enabled` 或 `queue.wake_enabled=false` 同样关闭通知。没有接收方时队列独立运行。实验 actor 继续读取 `experiments.json` 中的 `codex_home` 和 `price_card`，凭据只保存在本地 profile 中；接收方单独保存，两者互不影响。

```sh
mamba run -n Auriga python scripts/benchmark_queue.py \
  /absolute/path/cases.json --output /absolute/path/new-queue \
  --wait-timeout-s 86400 --wake-thread MAIN_THREAD_UUID
```

队列在启动前解析并固定接收方，运行和清理结束后自动通知一次；通知未被接受（回执不是 `sent`）时，队列以非零状态退出。GPU 等待和单个 benchmark 的预算属于实验配置，与主会话的研究轮次无关。

`--wake-codex /absolute/path/to/codex`（配置字段 `queue.wake_codex`，默认 PATH 中的 `codex`）指定发送通知所用的 Codex 可执行文件；`--wake-remote unix:///path/to/app-server.sock`（配置字段 `queue.wake_remote`）指定接收会话所在的 app-server 端点。二者仅用于完成通知，不改变实验 actor 的模型或环境。若 PATH 中的包装脚本注入 `--no-daemon`，它会与通知的 `--remote` 冲突，应显式使用原生 binary。当前 TUI 使用独立 daemon 时，通知可以由经验证的独立端点在同一 thread UUID 续跑，但这不表示当前 TUI 被直接唤醒。更换机器或端点后，应先实际验证通知能够送达。

GPU 调度由队列后台进程执行：默认每 90 秒（manifest 的 `sample_interval_s` 可调）检查全部可见 GPU，综合利用率和剩余显存补排，允许同一卡运行多个 case；每次启动前再检查一次。默认每个 case 按 11264 MiB（11 GiB）预算，每卡额外余量为 0，可用 manifest 的 `memory_per_case_mib`、`gpu_headroom_mib` 调整。新队列默认 `max_parallel=60`，实际并发受待办 case 数和资源准入限制；显式配置或 manifest 可覆盖上界。已完成 case 的回收和停止信号仍及时处理，不等待采样周期。

消息使用原生 `codex queue --thread UUID --message TEXT --remote unix://...` 提交到当前用户的共享 app-server。默认 socket 是 `~/.codex/app-server-control/app-server-control.sock`，hook CLI 可用 `--remote` 指定其他已确认端点。显式端点避免实验 actor 的独立 `CODEX_HOME` 把消息送往错误的 daemon。空闲会话自动开始新回合；正在工作的会话在当前回合结束后处理排队消息。

## 接管已经运行的队列

启动时未配置通知的队列可挂一个一次性 watcher：

```sh
mamba run -n Auriga python scripts/experiment_hook.py \
  --queue-output /absolute/path/existing-queue --thread MAIN_THREAD_UUID \
  --pid QUEUE_PID --start-ticks PROC_START_TICKS
```

`--pid` 与 `--start-ticks` 必须同时提供；省略二者时 hook 不等待进程，只在队列已留下终态时立即通知。`--codex` 和 `--remote` 的含义与队列的 `--wake-codex`、`--wake-remote` 相同。

watcher 核对 PID、`/proc` 启动时间、队列命令（`benchmark_queue.py` 或 `-m auriga.apps.evaluation.queue`）和输出目录，再通过 Linux pidfd 阻塞等待退出。若进程已经消失，只有队列留下终态且没有运行中 case 时才直接通知。普通终态包括失败、中断和 GPU 等待超时；缺失 case summary 不阻止主会话获知失败。若已核实进程退出但记录不完整，则发送简短的异常通知，说明部分实验停止状态尚未确认。

每个队列的 `.main-session-wake.json` 独占认领防止重复发送，`completion-hook/` 保存 prompt、队列快照、命令、CLI 输出和回执。`sent` 仅表示原生队列接受了消息，主会话是否执行以原生 turn 记录为准。发送中断或结果不明时记录 `delivery_unknown`，不自动重发。没有独立的后台研发 thread，也不使用 `codex exec resume` 与主会话争用。

## 验证

```sh
mamba run -n Auriga python -m unittest tests.test_experiment_hook -v
```

检查覆盖精确主会话目标、成功和失败后的通知、重复/不明交付、真实 pidfd 等待及运行中记录拒绝。
