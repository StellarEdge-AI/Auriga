# RoboDojo 42 任务 × 15 case：可分四组并行的评测

组名：`robodojo-42-15`。每种被测方法共 **630 局**，可按完整 task 划为四组并行执行。四份 `partition-*.json` 是可跨机器使用的任务分组配置，交给 `benchmark_group.py --partition` 生成各机器自己的队列 manifest；不是 `auriga.apps.gateway --config` 的运行配置。

## 官方范围与缩减规则

核对日期：2026-10-09。来源为 [RoboDojo 官方仓库](https://github.com/RoboDojo-Benchmark/RoboDojo/tree/726e9aabfaa642203722eb126f5eaf0f37f3e1ad)，其 README、`scripts/internal/task_inventory.py` 与 `scripts/internal/summarize_result.py` 一致列出 42 个基础任务：Generalization 12、Memory 6、Precision 8、Long-Horizon 8、Open 8。12 个 `_random` 是泛化任务的变体，不额外计为基础 task。具体来源版本与文件校验记录在 [group.json](group.json)。

官方完整协议每个 task 使用 eval seed **0、1、2**，每个 seed 50 局，共 150 局；泛化任务每个 seed 为 standard 25 + random 25。本组现在统一只用 **eval_seed=0**，每个 task 选择 **15 个不同场景，每个场景运行一次**：

- 30 个非泛化任务：取 standard 布局 **0–14**。
- 12 个泛化任务：按 `group.json` 中泛化任务顺序编号 `i=0..11`；偶数任务取 standard 布局 **0–7**、random 布局 **0–6**，奇数任务取 standard **0–6**、random **0–7**。
- 15 为奇数，单个泛化任务分为 8+7 或 7+8，两种分配各 6 个任务；泛化部分合计 **standard 90 + random 90**，总体保持 1:1。相同 `layout_id` 在 standard 与 random 下是不同场景。

[cases.csv](cases.csv) 固定全部 630 个 case；所有行的 `eval_seed=0`，每个 task 的 `(variant, layout_id)` 都有 15 个唯一组合，没有按结果筛选。`eval_seed` 选择官方布局目录，`layout_id` 选择其中的场景；原生 reset 和当前 Auriga simulator 初始化仍沿用 `layout_id`，不把这两个内部字段强制改成 0。

本清单替代先前“每个 seed 5 局”的三 seed 版本。已有队列 manifest 是冻结副本，不会随 CSV 自动变化；采用新规则必须重新生成 manifest，并使用新的输出文件和运行目录。
任务专用信息使用[官方原文目录 v2](../../docs/development/public-task-rules.md)：新运行在当局原生指令之外，完整提供官网 Instruction、Description、可选 Comment 与全部 Scoring。取消原先的摘要改写和额外解释；每局 manifest 记录实际文本。此次输入更新不改变本组的 case 清单或运行参数。

这是按官方任务和配比构建的 **15-case 缩减集**，不能冒充完整官方 leaderboard 协议。原生任务指令由环境提供；CSV 中的步数上限与 0.04 秒控制步长是核对目标，不由 manifest 强制覆盖。`policy_rng_seed=0` 仅兼容既有 CSV 的参考字段，不控制 GPT 或策略 RNG。

## 四份分组配置

每组均含 3 个泛化、2 个精确操作、2 个长程、2 个开放任务，另含 1 或 2 个记忆任务。按原生控制步数预算尽量均衡分配；预算仅用于粗略均衡工作量，不代表实际模型耗时。执行顺序沿用主 case 清单，分组配置只选择 task。

| 分组配置 | task 数 | case 数 | 原生控制步数预算合计 |
| --- | ---: | ---: | ---: |
| [partition-1.json](partition-1.json) | 11 | 165 | 123750 |
| [partition-2.json](partition-2.json) | 11 | 165 | 126000 |
| [partition-3.json](partition-3.json) | 10 | 150 | 120000 |
| [partition-4.json](partition-4.json) | 10 | 150 | 122250 |
| **合计** | **42** | **630** | **492000** |

**第 1 组：** `make_toast`、`store_laptop_and_headphones`、`pour_liquid_into_cup`、`build_tower`、`deposit_coin`、`fill_pen_holder`、`put_bottles_into_dustbin`、`match_and_pick_from_conveyor`、`swap_blocks`、`pour_by_language`、`solve_equation`。

**第 2 组：** `pack_objects_into_box`、`stack_bowls`、`fold_clothes`、`pour_balls_into_vase`、`plug_in_charger`、`play_stacking_toy`、`fill_egg_holder`、`imitate_sorting_sequence`、`swap_T`、`pick_from_conveyor_by_image`、`general_pickup`。

**第 3 组：** `arrange_largest_number`、`sweep_blocks`、`stack_blocks`、`insert_tubes`、`play_Xylophone`、`classify_objects`、`organize_table`、`cover_blocks`、`classify_objects_by_language`、`stack_blocks_by_language`。

**第 4 组：** `sort_nesting_dolls_by_size`、`hang_mugs`、`push_T`、`fasten_screws`、`insert_key`、`play_tic_tac_toe`、`make_kong`、`press_by_number`、`store_tools_in_toolbox`、`align_blocks`。

## 各分组的执行命令

在各自 Auriga checkout 根目录执行。第一组用 `PART=1`，其余分别改为 2、3、4；每轮使用新的 `BATCH` 名称。第一步只生成配置，不启动实验：

```sh
PART=1
BATCH=trial-001
PANEL=robodojo-42-15
RUN_ROOT="$HOME/exp/auriga/benchmark/$PANEL/$BATCH/partition-$PART"
MANIFEST="/tmp/$PANEL-$BATCH-partition-$PART.json"

mamba run -n Auriga python scripts/benchmark_group.py \
  --group "$PANEL" \
  --partition "benchmarks/$PANEL/partition-$PART.json" \
  --config configs/benchmark-shared-memory.json \
  --run-root "$RUN_ROOT" \
  --output "$MANIFEST"
```

生成器会检查主清单校验值、case 覆盖和任务分组，拒绝覆盖已有 manifest 或运行目录，并把本机生效的模型/工具/队列配置保存进 manifest。上述共享配置显式指定主模型 **`gpt-6.1-sol` / `high`**，四组统一使用；其余运行参数沿用现有 Auriga 默认设置。四组采用相同 revision、模型、推理档位、观察和工具设置；如需指定其他方法，四组在生成时统一改用同一 `--config`。分组本身不改变模型与 effort，也不自行启动 π0.5。

准备好执行环境后，每组启动自己的队列：

```sh
mamba run -n Auriga python -m auriga.apps.evaluation.queue \
  "$MANIFEST" \
  --output "$HOME/exp/auriga/queues/$PANEL-$BATCH-partition-$PART"
```

每个队列的并发上限默认取配置中的 `queue.max_parallel`（仓库默认 60），可在生成时用 `--max-parallel N` 显式覆盖；实际并发仍受剩余 case 数、GPU 显存和共享租约准入限制。生成器不硬编码某台机器的 GPU 编号。各组需协调实际 GPU 资源，运行目录和队列状态目录按分组隔离。需要整组 630 局时省略 `--partition`。

操作经验沿用 [共享经验配置](../../configs/benchmark-shared-memory.json)：`~/.local/share/auriga/grasp-memory`。同机同用户四队列会读写同一经验库，跨机器则默认各有本地库；这会影响后续 case 可读到的经验，须记录初始库状态、共享范围和运行顺序。组生成器只准备协议与配置，不创建、清空或同步任何经验库。

## 验证与结果汇总

离线回归检查 630 个唯一 case、每任务 15 个不同场景、全组仅 seed 0、泛化变体配比、四组无重复无遗漏，以及含大小写官方任务名的命令映射：

```sh
PYTHONPATH=tests mamba run -n Auriga python -m unittest test_benchmark_group test_queue_config
```

建立时已检查所选 630 个布局 JSON、任务 YAML 及任务源码在 RoboDojo 中存在。运行机器仍须具有匹配的 RoboDojo 代码、资产、Isaac Sim 和模型配置，准备方法见 [RoboDojo 环境配置](../../docs/development/robodojo-setup.md)；打过补丁的 RoboDojo checkout 不能仅凭来源 commit 代表整个运行树，实际运行由队列绑定源码。

结果按每任务 **15**、各组 **165/165/150/150**、总体 **630** 为固定成功率分母。合并按完整 `case_id` 去重并检查覆盖；四组规模不同，不能简单平均四组成功率，应汇总成功数除以 630。缺失/无效/未完成保留 unknown/null，Score 单独报告有效样本数。manifest 中 `reference_protocol` 保留完整 630-case 定义，当前分组的实际范围以 `benchmark_partition`、`protocol_cases` 和 `cases` 为准。

本目录只提供离线协议与配置，不包含模型、GPU 或任务成功率结果。
