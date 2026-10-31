# 官方任务说明与得分表

新 benchmark 运行直接注入 RoboDojo 官网任务页的 **Instruction、Description、Comment（若有）和完整 Scoring 表**。原文不压缩、改写、删减或补充；保留固定物体数量、每档分数、数值阈值、标点及官网自身的矛盾。HTML 转为纯文本与 Markdown 表格时只调整排版空白、解码 HTML 实体并转义表格分隔符，不改正文。

当前 [目录](../../auriga/cortex/context/robodojo_task_rules.json) 为版本 2，覆盖 42 个基础任务和 12 个 random 变体；random 变体沿用对应基础任务的原文。共有 42 条 Instruction、42 条 Description、2 条 Comment、42 张评分表和 147 条评分条件。官网导航、视频控件及 Platform / Category / Data Source / Usage 等页面元信息不属于任务说明，不注入模型。

## 来源与重建

目录来自 2026-10-09 对全部 42 个官方页面的读取，均返回 HTTP 200。原始 HTML、抓取时间、URL 和校验信息保存于 [来源快照](../../scripts/data/robodojo-public-rules-20261009/README.md)。来源直接为 `robodojo-benchmark.com`，没有使用 RoboProbe recipe、评分器源码或布局真值补写内容。

从冻结的 HTML 离线核对当前目录：

```sh
python scripts/extract_robodojo_task_rules.py
```

需要从同一快照重新生成目录时加 `--write`。脚本只访问本地冻结文件；任务字段、表格结构或覆盖数量异常时拒绝抽取，不能悄悄省略不认识的段落。原网页与 2026-10-04 旧快照相同，但旧的摘要目录和新原文目录是不同模型输入条件。

## 注入与恢复

[`public_task_rules()`](../../auriga/cortex/context/public_task_rules.py) 读取冻结目录，按原顺序拼接完整章节，不追加框架对数量、顺序或评分的解释。公开来源与校验信息保存在运行 manifest，模型只收到正文。

[`Workbench.start`](../../auriga/apps/gateway/workbench.py) 保留当局环境给出的原生 instruction，同时附加官网原文。任务专用章节独立于通用 `Task notes`，关闭通用操作经验不会关闭官网原文。框架工具说明、预算、通用操作经验及执行逻辑仍按原配置提供；不引入 RoboProbe 的非官网 Notes 或任务专用执行流程。

resume 使用原 manifest 中归档的 `public_task_rules`，不重新读取新目录，也不重发首次任务提示。旧 run 不会被升级为版本 2。新旧输入条件应在结果中区分；新增 42-task benchmark 的 task、seed、layout、15-case 采样及四组划分没有改变。

官网 Description 与 Scoring 若有冲突，两段均原样保留。例如 `press_by_number` 不再由框架删去有争议的确认顺序，`arrange_largest_number` 不再将固定数量改为当前布局数量。框架不替官方原文作纠正，也不把这些静态评分说明当成当前回合的实时分数。

## 验证

`test_public_task_rules` 对全部原文做冻结来源重建核对，验证 HTML inline 文本、完整评分行、异常页面拒绝、启动归档以及旧版本恢复。`test_model_proxy` 检查全部 54 条文本经过首次提示过滤及实际 loopback HTTP `/responses`、`/responses/compact` 出口后逐字相等。`scripts/check_package.py` 还核对 checkout 外 wheel 中的版本 2 数据与完整评分表。

这些验证证明内容和交付路径，不代表模型已经执行任务，也不验证官网说明与 native evaluator 实现完全一致。
