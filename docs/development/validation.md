# 开发验证与版本发布

验证应证明改动在实际调用链中的行为，并写清证据范围。小型文案变更检查链接和内容即可；权限、物理执行、状态迁移、归档或评分变更需要覆盖异常路径的回归。CI 定义见 [ci.yml](../../.github/workflows/ci.yml)。

## 环境与公共检查

从仓库根目录执行，项目开发默认使用 Miniforge 的 `Auriga` 环境，不创建项目 `.venv`，不就地升级正在运行实验所用的环境。Python >= 3.11，CI 在独立干净环境使用 Python 3.12 / Node 22；依赖版本以 `environment.yml`、`requirements.lock.txt` 和 `pyproject.toml` 为准，当前 Codex SDK/CLI 为 0.160.0。运行前从该解释器核对实际 SDK/CLI，不以 PATH 中的 Codex 版本替代；命令见[环境准备](running.md#环境准备)。`ffmpeg` 用于录像相关检查。

```sh
# 已有 Auriga 环境可跳过创建；先按运行指南核对依赖。
mamba env create -f environment.yml
npm ci --prefix web
mamba run -n Auriga python -m unittest discover -s tests
npm run check --prefix web
npm run build --prefix web
mamba run -n Auriga python scripts/build_package.py --output dist
mamba run -n Auriga python scripts/check_package.py dist/auriga_harness-0.2.0-py3-none-any.whl
git diff --check
```

发布后续版本时使用本次新生成的精确 wheel 路径，避免 `dist/*.whl` 同时选中旧包。`check_package.py` 用当前解释器将 wheel 以 `pip --target` 安装到临时目录，再在 checkout 外以 `-I` 隔离模式检查；不另建项目虚拟环境。CPU 套件不需要真实模型或启动 GPU 仿真；部分资源映射测试会在缺少外部 RoboDojo checkout 时跳过。需要验证该映射时，按本机真实路径设置 `AURIGA_ROBODOJO_ROOT` 与 `AURIGA_ROBODOJO_PYTHON` 后重跑，分别记录 pass/skip。原生 app-server 测试使用 loopback 模型 stub；SDK/CLI 不匹配应换正确环境，不能跳过失败冒充验证。

## 按修改选择回归

开启隔离（`features.sandbox_enabled=true`，默认关闭）的原生 Codex 沙箱回归需要 Linux、可发现的 `bwrap` 和系统 `python3`；优先使用非特权 user namespace，宿主禁用时需 `sudo -n` 与 `setpriv`。测试使用真实文件、进程和 loopback Responses stub 验证边界，并覆盖工具安装位置变化和 IPC 目录配置；PyPI 的实际下载安装与本地认证模型调用另行记录，不由 CPU 套件隐式触发。关闭 `features.sandbox_enabled` 的回归应在禁止调用沙箱探测/加载的条件下验证主 Codex、终端、evidence agent 与 `program_run` 可启动，确认私有 home、占位认证、broker 审核及资源限制仍有效，并检查两种模式互相恢复被拒绝。宿主文件/loopback 网络可访问是关闭模式的预期行为，应使用临时文件与本地 stub 验证；不能将此结果报告为隔离测试通过。

下面是定向入口，文件均在 `tests/`。例如 `PYTHONPATH=tests mamba run -n Auriga python -m unittest test_tool_registry test_typed_actor_output`。跨多个公共边界或发布版本时，定向测试通过后跑完整 CPU 套件。

| 修改范围 | 优先测试 | 要证明的行为 |
| --- | --- | --- |
| ToolSpec / handler / 输出 | `test_tool_registry`、`test_typed_actor_output`、`test_program_review` | 两个真实入口、非法输入零副作用、权限、typed 事实、后执行展示故障不重放。 |
| 动作/worker/停止 | `test_unified_actions`、`test_native_wait`、`test_runtime_lifecycle` | 预算/幂等/检查点、partial/stopped/unknown、终态采集失败保留回执并阻断旧观察。 |
| 场景证据 | `test_scene`、`test_scene_contracts`、`test_scene_service` | 每实体来源与时钟、dirty/freshness、服务与 job 生命周期。 |
| Store/评分/归档/报告 | `test_contract_store`、`test_read_models`、`test_scoring_policy`、`test_experiments`、`test_evaluation_reports` | sealed WAL 原档文件/hash/mtime不变、坏档隔离、固定分母、完整引用和 scope、导入保留评分事实。 |
| 队列与完成钩子 | `test_benchmark_queue`、`test_experiment_hook` | 新旧入口的精确 argv 识别、PID/start ticks/output 归属、重试边界。 |
| Web/只读 API | `npm run check --prefix web`、相关 Python API 测试、浏览器 fixture | 分页、终局刷新、replay 只读、输入注入和手机布局。 |
| 配置/资源/入口/发行包 | `test_config`、完整 wheel build/check | checkout 外 imports/CLI、默认配置、静态资源和来源信息可用。 |
| Codex 原生工具与权限 | `test_codex_sandbox`、`test_actor_source`、`test_codex_native_preferences`、`test_actor_isolation`、`test_program_isolation`、`test_codex_image_transport`、`test_codex_evidence_agent` | 真实 native shell/记忆操作、开启隔离时宿主文件/网络拒绝、关闭隔离（默认）时无需沙箱依赖且保留 broker/审核/预算、恢复模式一致性、源码发布与占位认证、动态图像传输。 |

运行前用 `rg --files tests` 核对模块名；新增功能在相应 owner 的现有测试中补回归。故障测试应断言持久化事实与副作用次数，不能仅断言返回字符串；历史最小复现若断言的是旧错误行为，应保留为基线材料，不计入修复后的绿色套件。

## 浏览器与原生证据

`web/check-task-review.mjs` 的 synthetic-only 模式在浏览器中注入合成数据，无需启动真实模型/机器人。先 build，再使用已安装的 Playwright（必要时设置 `PLAYWRIGHT_MODULE` 为本机模块路径）：

```sh
AURIGA_BROWSER=firefox \
AURIGA_URL=http://127.0.0.1:18787 \
AURIGA_PREVIEW_ASSETS="$PWD/web/dist" \
AURIGA_SYNTHETIC_ONLY=1 \
AURIGA_SCREENSHOT_DIR=/tmp/auriga-browser-check \
node web/check-task-review.mjs
```

该检查证明 Web 合成状态、交互和只读边界，不证明真实任务回放同步或 GPU 执行。真实运动语义改动另需在明确授权的资源上冻结源码/config/provider/model/task/seed/layout，记录完整 action request、执行回执、实测轨迹、原生独立评分、原图/回放和审计结果。软件 pass、队列启动、模型自述和有视频均不能替代任务成功。已有旧实验不自动成为新源码的效果证据。

`audit_run.py --profile native` 可由已完成的 Codex turn 满足会话条件；若 Harness 在已记录的原生终态后中断活动 turn，也只在归档同时证明终态来自 private evaluator、该 turn 已启动并属于同一 thread、且 owner 对该 turn 发起的 interrupt 晚于终态时接受。该情况计入 `native_terminal_interrupted_turns`，不会增加 `native_completed_turns`，也不代表任务成功；单独的 `interrupted` 状态不满足原生审计。`--require-continuity` 仍要求两个 completed turn。

评分报告保留 missing/invalid/unknown 的 null 及固定 case 分母；说明有几局可评分和失败类别。完整性审计、原始 RGB 字节完整性、重建图可用性、任务得分分别报告。导入后复核 native summary 的关键字段及原档不变性。

`benchmark_report.py` 的逐 run `recovery_guidance` 区分 manifest 的 `configured_version`、主线程 `benchmark.actor_turn_continuation` 的尝试次数，以及 `codex-raw.jsonl` 中实际 `sent turn/start` 的匹配次数。使用已保存的短引用映射恢复文本后，将事件完整 prompt 与原生请求的 text 输入逐条精确匹配；每个请求只匹配一次，其他线程、received 记录和仅包含提示片段的文本均不计入。`versions` 按事件实际记录的恢复版本分别保存尝试／发送次数，不用配置版本补造触发事实；普通 continuation 与带 recovery context 的 continuation 分开计数。报告不复制提示正文或哈希。

`native_sent_recovery_count` 只证明日志记录了向原生运行时提交该提示的请求，不证明请求被接受、模型已遵守或任务改善。`unmatched_continuation_attempt_count` 表示已记录尝试但未找到相应发送。旧档缺少 raw 日志或主线程身份时，发送次数保留 `null`；主线程及完整可读日志存在且没有匹配事件时为 `0`。malformed JSON／RPC 记录按既有坏档隔离保留在报告分母中，不转换成零触发。回归见 `test_evaluation_reports`。

## 发布检查

先保存现有未提交工作和分支基线，确认目标 main 与待合并分支各自独立改动。使用普通 merge 保留祖先，不用 reset/force push 丢弃历史；冲突根据职责和测试解决。旧版本使用 annotated tag 固定，后续不得挪动。

在最终合并树更新 `pyproject.toml` 的 package version，更新变更说明，完成全套软件/包检查。验证 wheel 元数据版本、两个 tag 对应提交、旧 main 与待合并分支 HEAD 都是新 main 祖先、main 独有功能已保留、工作区无意外变更。只有验证通过的源码打新 tag；正常推送并读取远端 refs 确认。Git tag 与包版本同步不代表已经发布到 PyPI；外部部署和正式 benchmark 是独立交付。
