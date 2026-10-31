# Token 与费用记录

计量来自 Codex 原生 `thread/tokenUsage/updated` 的累计 `total`。每次通知替换快照，不将重复累计值相加。原生 `last`、`total`、原文 artifact 和 turn 关联继续保留；新增 `token_accounting` 写入 runtime、每 turn 状态、通知事件、实时 snapshot 及最终 summary。

## 统计口径

- `input_tokens` 包含缓存读取；`cached_input_tokens` 为命中输入；`uncached_input_tokens = input_tokens - cached_input_tokens`。
- `cache_hit_rate = cached_input_tokens / input_tokens`，0–1 十进制字符串；输入为 0 或缺失时为 null。
- `output_tokens` 已包含 `reasoning_output_tokens`，推理输出单独展示但不重复计费。
- `cache_write_input_tokens` 原样保留；缺失不是 0。当前独立缓存写入计费语义不明或非零时，不输出完整费用。
- 缺失/非法计数、身份未知、价卡不匹配均保留 unknown；汇总同时输出已知小计、完整总额和已知/未知 run 数，失败 run 不被丢弃。

`rate_card_estimate` 是指定价卡计算出的费用；`provider_reported` 是供应商账单金额。当前原生通知只有 token，没有账单字段，因此后者保持 null，不把估算命名为实际扣费。

## 官方参考价卡

公开配置为 `configs/pricing/openai-gpt-6-astra-standard-2026-10-01.json`：OpenAI Standard，输入 $10、缓存读取 $1、输出 $50 / 百万 token，适用于每次请求输入不超过 272000 tokens。采用原生 `model_context_window <= 272000` 作为平价适用范围的上界证据；不拿一个 thread 累计几百万输入当作单请求长上下文。未知/超过上界时费用为 null，需按单请求档位另算。

官方还列出缓存写入 $12.50 / 百万；本价卡保留其参考值，但当前仅在原生明确写入数为 0 时给出完整总额。Fast、Ultrafast、Batch/Flex、区域附加费及长上下文都有独立规则，此卡只作为 Standard 参考，不声称自定义供应商实际采用这些价格或档位。

对于当前支持条件：

`费用 = (未命中输入 × 10 + 命中输入 × 1 + 输出 × 50) / 1,000,000`

金额使用 `Decimal`，不提前四舍五入为美分。价卡按 provider/model 精确匹配，保留版本、来源、币种、所用 rate、计价说明及原文件 SHA256。恢复同一累计 thread 需保持 provider/model 和价卡；更换价卡回算请用离线报告。

```sh
python scripts/benchmark_report.py /absolute/run1 /absolute/run2 \
  --price-card configs/pricing/openai-gpt-6-astra-standard-2026-10-01.json \
  --output /absolute/new-cost-report.json
```

旧归档无需改写：从该 run 冻结的 manifest/config 恢复身份并按保存的 token 回算。新版 gateway 支持 `--price-card` 或 `AURIGA_PRICE_CARD`，将完整价卡冻结到 manifest。

## 实验专用配置与生效范围

自动队列每次发射 case 前读取 `~/.config/auriga/experiments.json`，也可用 `AURIGA_EXPERIMENT_CONFIG` 指定其他文件。仅允许 `codex_home` 与 `price_card` 两个绝对路径；实验专用 Codex home 包含 `config.toml` 与 `auth.json`，敏感文件为 0600、目录为 0700，密钥不进入仓库或实验报告。

队列仅对子 gateway 设置 `CODEX_HOME` 与 `AURIGA_PRICE_CARD`，不修改全局 Codex auth。这兼容已冻结的旧 actor 源码：原有 credential preparation 会从所选 home 拷贝本次凭据到 run 私有目录；旧源码不产生新 accounting 字段，但可离线回算已有原生计数。

新逻辑在新启动的 queue 进程生效。更新前已经运行的整个 queue 仍使用旧代码/环境，包含其中未启动的 case；不通过修改冻结 checkout 或重启实验来热切换。持续开发 controller 的 Codex 凭据保持原配置，与 benchmark actor 分开。
