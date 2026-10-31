# RoboDojo 10 任务复现协议：GPT-as-Policy 对齐集

核对日期：2026-10-02。本文件记录 GPT-as-Policy 在 RoboDojo 上的原始 10 任务评估协议，供其他框架使用相同 case 复现与比较。**每种方法运行 50 回合**。

## 原始实验的范围与结果

[GPT-as-Policy 仓库](https://github.com/anonymous-report-421/GPT-as-Policy/blob/main/README.md)报告两种方法：`gpt`（GPT-6 Astra Direct）和 `mix`（GPT-6 Astra 审核、必要时纠正 π0.5 候选动作）。两种方法使用**相同的 50 个 case**：10 个任务，每任务 5 个 case。因此作者共记录 **2 × 50 = 100 条评估结果**；这不等于单个方法跑 100 次，也不等于 100 个不同初始场景。公开的[逐回合记录](https://github.com/anonymous-report-421/GPT-as-Policy/blob/main/public_results/evaluation_cases.json)给出 `case_id`、方法、任务、场景变体、seed、成功与原生 Score。

| 方法 | 成功数 / 回合数 | SR | 作者报告的平均原生 Score |
| --- | ---: | ---: | ---: |
| Direct (`gpt`) | 13 / 50 | 26% | 37.81 / 100；仅 48 个 case 有原生 Score |
| Hybrid (`mix`) | 24 / 50 | 48% | 62.60 / 100；50 个 case 均有原生 Score |

两种 SR 的分母都是 50；缺少 Score 的 2 个 Direct case **仍计入 SR**。作者另报告 Hybrid 在已执行控制步中有 14.4% 使用 GPT 修正。仓库中的其他官方模型对照是重加权公开参考，不能视为同 seed 重跑。上述数值均为作者结果，尚非本项目本地复现。

## 评估任务与回合：每方法 50 回合

`standard` 与 `random` 是 RoboDojo 场景变体，布局编号相同也代表**不同 case**。下表的任务顺序和布局依据作者[公开 case manifest](https://github.com/anonymous-report-421/GPT-as-Policy/blob/main/public_results/evaluation_cases.json)；[cases.csv](cases.csv)列出所有 50 个完整 `case_id` 和 seed。

| 任务 ID | 能力维度 | `standard` 布局 | `random` 布局 | 每方法回合 | 作者步数上限 | Direct 成功 | Hybrid 成功 |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: |
| `organize_table` | Long-Horizon | 0–4 | — | 5 | 1000 | 0/5 | 0/5 |
| `classify_objects_by_language` | Open | 0–4 | — | 5 | 1100 | 2/5 | 1/5 |
| `imitate_sorting_sequence` | Memory | 0–4 | — | 5 | 1600 | 0/5 | 2/5 |
| `arrange_largest_number` | Generalization | 0–1 | 0–2 | 5 | 1050 | 2/5 | 2/5 |
| `pack_objects_into_box` | Generalization | 0–1 | 0–2 | 5 | 1300 | 1/5 | 1/5 |
| `classify_objects` | Long-Horizon | 0–4 | — | 5 | 1100 | 5/5 | 3/5 |
| `build_tower` | Precision | 0–4 | — | 5 | 1050 | 0/5 | 3/5 |
| `make_kong` | Long-Horizon | 0–4 | — | 5 | 600 | 0/5 | 2/5 |
| `fold_clothes` | Generalization | 0–1 | 0–2 | 5 | 500 | 2/5 | 5/5 |
| `put_bottles_into_dustbin` | Long-Horizon | 0–4 | — | 5 | 700 | 1/5 | 5/5 |
| **合计** | | | | **50** | | **13/50** | **24/50** |

计算方式：7 个非 Generalization 任务 × 5 个 `standard` 布局 = 35；3 个 Generalization 任务 ×（2 个 `standard` + 3 个 `random`）= 15；**35 + 15 = 50**。公开记录中所有 case 的 `eval_seed=0`、`simulator_initial_seed=0`、`policy_rng_seed=0`，且 `reset_seed=layout_id`；控制步长 `dt=0.04 s`。`case_id` 如 `fold_clothes__random__g0__l2`，必须保留任务、变体和布局三个部分。两方法在 `case_id` 和 seed 上一一配对；复现其他框架时也使用这 50 个 case。步数上限是作者记录的原生值，移植其他框架时应记录实际上限；若要严格对照，须统一这些上限及步长。

## 执行与报告约定

1. 固定同一 RoboDojo 代码与资产版本、任务成功判据、机器人、相机和动作步长；先用 [cases.csv](cases.csv)按 `case_id` 建立任务队列。GPT-as-Policy 作者的[来源记录](https://github.com/anonymous-report-421/GPT-as-Policy/blob/main/hybrid_rollout/robodojo/SOURCE.json)记录了 RoboDojo/OpenPI commit 和 π0.5 checkpoint 配置；仓库本身不附仿真资产或权重，需从上游准备。不同版本只能称同任务重新评估，不能称逐 case 精确复现。
2. 对每个框架固定并记录精确模型版本、prompt/示范、策略权重、观测和动作接口、最大控制步数、超时及恢复预算。GPT-as-Policy 仓库使用 Isaac Sim 5.1、GPT-6 Astra `xhigh`，Hybrid 还需 OpenPI/JAX 和 π0.5 权重；Direct 无 π0.5 推理。其运行变量 `ROLLOUT_EVALUATION_METHOD` 对应 `gpt_only`（Direct）或 `pi05_plus_gpt`（Hybrid）。[运行说明](https://github.com/anonymous-report-421/GPT-as-Policy/blob/main/README.md#run-a-new-evaluation)。
3. 每个 `case_id` 保存原始 `success`、原生 `Score`、是否有效、终止原因、步数、模型调用数、耗时以及轨迹文件路径。对仿真/资产故障单独标记；不要静默删除失败回合或把缺失 Score 补零。
4. 结果按**任务 5 回合成功数**和**50 回合总成功数 / 50**报告；10 个任务均为 5 回合，因此任务 SR 宏平均等于总 SR。Score 单独报告均值及有效样本数 `n`，不要将它与 SR 混用。每任务只有 5 回合，一次成功即改变 20 个百分点。

资料入口：[GPT-as-Policy 代码与说明](https://github.com/anonymous-report-421/GPT-as-Policy)、[100 条作者结果](https://github.com/anonymous-report-421/GPT-as-Policy/blob/main/public_results/evaluation_cases.json)。

## Auriga benchmark 组入口

固定组名：**`gpt-as-policy-10`**。后续可以直接说：**“用 `gpt-as-policy-10` 组的 setting 跑 Auriga，每个 case 一次，共 50 回合。”** 这表示采用本文件和 [group.json](group.json) 中的目标协议，以及 [cases.csv](cases.csv) 中的完整 case 集；方法实现与任何协议偏差另行记录。比较两种方法时分别生成两份 50 回合队列，使用不同运行根目录，按 `case_id` 配对。

**模型和推理档位使用我们自己的配置。** 实验组不覆盖 Auriga 的模型或 reasoning effort，也不要求切换为作者的 `xhigh`。作者档位仅保留为来源信息；本地报告记录本次实际模型与推理档位。

从仓库根目录离线生成可供现有队列使用的 manifest（只需 Python 标准库，不启动 GPU、模型或通知）：

```sh
mamba run -n Auriga python scripts/benchmark_group.py --group gpt-as-policy-10 \
  --run-root ~/exp/auriga/benchmark/gpt-as-policy-10/auriga-001 \
  --output /tmp/gpt-as-policy-10-auriga-001.json
```

输出文件必须尚不存在。每条运行目录和标签都保留完整 `case_id`；`standard` 直接使用任务 ID，`random` 映射为 `<task>_random`，例如 `fold_clothes_random`。manifest 同时保存完整目标条件、来源版本和文件校验值，运行时不需要再访问 GitHub。

后续明确开始实验时，用现有队列执行生成文件；`--output` 为新的队列状态目录，不能与 case 运行目录重叠：

```sh
mamba run -n Auriga python -m auriga.apps.evaluation.queue \
  /tmp/gpt-as-policy-10-auriga-001.json \
  --output ~/exp/auriga/queues/gpt-as-policy-10-auriga-001
```

生成时可用 `--max-parallel N` 指定并发上限，默认取配置中的 `queue.max_parallel`（仓库默认 60）；实际并发由剩余 case 数、GPU 显存和共享租约准入共同决定，完成通知沿用现有队列。`--pi05` 生成启用 Auriga π0.5 工具的另一方法队列，须另选输出文件和运行根目录；它不自动成为作者的 `mix`。作者的 `gpt`/`mix` 成绩仅作参考，不写入本地运行结果。

### 操作经验的共享与持久化

后续重建 benchmark 使用 [`configs/benchmark-shared-memory.json`](../../configs/benchmark-shared-memory.json)，通过 `--config configs/benchmark-shared-memory.json` 传给组生成器。该配置将 `features.grasp_memory_dir` 固定为 `~/.local/share/auriga/grasp-memory`，其余设置继承仓库配置。普通组生成器沿用这个共享目录，不创建逐 case 副本。模型调用 `operation_memory` 时，`scope='shared'` 的记录进入该目录的 `events.sqlite`，后续任务可以检索；`scope='task'` 的记录只进入当前 run。共享配置不会自动提炼经验，模型仍须成功调用 `kind='record'`，并提供实际动作、观察 ID 和适用条件。

使用共享库的面板应在 manifest 中记录实际路径、起始库快照和跨 case 学习条件。共享写入会使后启动 case 读取到更早 case 的经验，运行顺序及并发因此成为评估条件；比较不同模型时应分别声明初始库和共享策略。

### 当前适配范围与严格对照前检查

生成器直接应用任务/变体映射、`eval_seed`、`layout_id` 和方法开关；`reset_seed` 由现有 worker 按 `layout_id` 使用。CSV 中的其余作者条件保存为 **`reference_protocol` / `protocol_cases` 目标元数据**，不宣称由队列强制执行。当前 Auriga 使用安装的原生任务上限和控制配置；建立组入口时静态核对过所用 RoboDojo 中 13 个任务类的上限与表中一致，控制频率为 25 Hz。生成 manifest 不等于运行时已验证这些条件。

严格逐 case 复现前仍需处理这些差异，并保存实际运行配置：

- 作者来源记录固定 RoboDojo `ee67a1468510da7624a089164402359f2afc72c8`、OpenPI `432f82b1758c5b1202e42a3dfe014546dbc50871`。资产、机器人、相机和成功判据也必须核对；组生成器不切换外部源码或下载资产。
- 当前 [worker](../../auriga/robot/backends/robodojo/robot_worker.py) 用 `[layout_id]` 初始化 simulator；作者记录 `simulator_initial_seed=0`，因此非零布局的初始化设置尚未对齐。不能把 `--seed 0`（eval seed）误当作已经统一所有 RNG。
- 作者 `policy_rng_seed=0` 指新进程、无 warmup 推理时的 OpenPI JAX 初始 key，不是 GPT 的采样 seed；当前 Auriga π0.5 工具没有统一落实这项 RNG 约定。组名不覆盖方法自身的 prompt、权重或 RNG 实现。
- 当前 Auriga 原生动作、EE 工具、可选 π0.5 工具及预算仍按项目[固定运行规则](../../docs/development/configuration.md#统一记录但不开放的协议与边界)执行：180 次有效模型返回（`model_request_limit`）、单次请求无硬期限、明确可重试的 API 错误默认最多 12 次退避重试、启动 900 秒/运行中连续 1800 秒无实际活动保护，没有整局墙钟上限。这些须作为被测方法的实际条件披露，不能仅凭原生步数一致宣称与作者执行协议完全一致。

因此当前生成队列标记为 **`same_case_set_re_evaluation`（同一 case 集重新评估）**。本次只建立协议文档、case 清单和组入口，没有开展本地复现实验。

### 来源冻结与统计核对

本次核对固定 GPT-as-Policy commit [`8f3d362b077d8efb77e2a7274d5b2c20e2243846`](https://github.com/anonymous-report-421/GPT-as-Policy/tree/8f3d362b077d8efb77e2a7274d5b2c20e2243846)；[group.json](group.json) 保存 README、公开结果及 SOURCE.json 的固定 URL 和 SHA256。CSV 从该公开结果按方法配对、去重生成，共 50 行，按上述任务顺序再按变体和布局排序。

该 commit 的 `evaluation.py` 默认生成另一套 60-case panel，不能用它替代本文件指定的公开 50-case 清单。CSV 还保留每 case 的原生任务指令用于核对；其中 `classify_objects_by_language` 的类别/篮子指令随布局变化，执行时应由原生环境提供，不能写成一个固定任务 prompt。

独立重算公开结果：Direct 13/50，48 个 Score 的均值为 37.8125/100；Hybrid 24/50，50 个 Score 的均值为 62.60/100。Direct 缺 Score 的 case 是 `classify_objects_by_language__standard__g0__l1` 和 `pack_objects_into_box__random__g0__l2`，保留在 CSV 和 50 回合分母中。公开原始 Score 为 0–1，本文件展示时乘 100；保存本地结果时保留原始量纲。
