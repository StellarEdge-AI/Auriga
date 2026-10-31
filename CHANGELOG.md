# 版本记录

版本号对应 `pyproject.toml` 与同名 annotated Git tag。项目在 0.2.0 由 Daedalus 更名为 Auriga，更早版本的记录保留原名。以下是软件发行记录，任务效果仍以对应源码/配置/模型的独立实验为准。

## 0.2.0 — 2026-10-31

- 项目更名为 Auriga：Python 包 `auriga`、发行包 `auriga-harness`、命令 `auriga` / `auriga-queue` / `auriga-report` / `auriga-audit`、环境变量前缀 `AURIGA_`、conda 环境 `Auriga`、默认路径 `~/exp/auriga`、`~/.config/auriga`、`~/.local/share/auriga`、`~/.cache/auriga`，程序内客户端为 `AurigaClient`（`auriga_client.py`），模型工具命名空间为 `auriga`。
- 更名前写入的数据继续可读：manifest 的 `daedalus_config`、`daedalus.robodojo_trajectory.v1` 轨迹包、旧日志中的 worker 标记与连续性标记、旧队列状态中的恢复命令；队列仍可启动更名前与包结构重构前的冻结 checkout，并以其 `DAEDALUS_` 前缀传入设置。旧名称集中在 `auriga/core/legacy_names.py`。更名前开始的运行请用其原 checkout 恢复会话。
- 删除面向内部迁移的模块路径对照文档。

## 0.1.0 — 2026-10-31

- 包结构重构：平铺的 `daedalus/` 模块按层迁入 `core/`、`robot/`、`perception/`、`cortex/`、`runtime/`、`apps/`；网关拆为 `cli`/`server`/`routes`/`workbench`，`robot_action` schema 与 worker 侧原生状态恢复各自成模块。函数体除 import 与路径定位外不变，模型可见的工具目录、说明与回包逐字节一致。命令入口改为 `python -m daedalus.apps.gateway` 等，队列仍可启动重构前的冻结 checkout。
- 删除只有 `trusted_legacy` 角色可调用的 21 个旧工具及只服务它们的模块、测试和 probe 脚本；模型与程序在任何配置下都调不到它们，相关名字仍可写入 `disabled_tools`。
- 开源整理：删除内部实验记录、验收与研究笔记、`analysis/` 数据、内部部署配置与团队结果填报；官方任务页面快照移至 `scripts/data/`，RoboDojo/IsaacLab 补丁移至 `docs/development/patches/` 并新增 [RoboDojo 运行环境](docs/development/robodojo-setup.md)；新增英文 README、中文 README 与 CONTRIBUTING；打包的实验组关系登记置空，测试不再依赖本机路径或旧 checkout；以 MIT 许可证发布。

## 0.0.4 — 2026-10-30

- 新增可选的 self-briefing：首次物理动作前生成与任务无关的操作要点，追加在初始任务文本之后；本版本起默认开启（`features.self_briefing=true`）。
- 正式运行的模型请求预算由 170 次提高到 180 次。
- 模型代理：完整响应交付后客户端断开连接，不再阻塞本局。

## 0.0.3 — 2026-10-30

该版本不含 self-briefing。

- 可选 OS 沙箱（`features.sandbox_enabled`，默认关闭）：开启后主 Codex、子代理、原生工具和终端运行在 Linux namespace 沙箱；只读发布的 Harness 源码，关闭搜索及宿主扩展，包安装仅经受控 PyPI 下载通道，真实认证保留在宿主 relay。
- 收窄独立程序的 Python 运行依赖，分离程序工作区与宿主日志，拒绝通过输出文件符号链接读取宿主内容；旧 full-access 会话需使用新运行目录。
- 模型上下文统一删除非必要校验元信息与文本哈希；主 Codex、辅助视觉核验、程序回包/历史缓存、文本 artifact 和 π0.5 提示均覆盖。
- 必要对象引用统一为可逆短 ID，保留内部执行事实和归档校验；实际 Responses/compact 出口清理恢复历史的可读文本，保持媒体与协议载荷。
- 开发约定明确“非必要内容，尤其元信息，不占模型上下文”，新增实际 HTTP、隔离程序和策略输入回归。
- 机械臂与环境适配器解耦，按经过验证的环境/型号组合绑定部署。
- 新增 `robodojo-42-15` 任务组（42 个官方任务 × 15 个场景，统一 eval seed），逐字注入官方任务说明与得分表；部分任务可按公开来源追加说明（`features.task_instruction_overrides`，默认开启）。
- 接入外部感知并默认开启：VGGT 物体高度、对象表、scene3d 点云、观察附带的三维信息与持物感知；服务不可用时对应结果标为不可用，运行继续。
- `robot_action` 的几何、历史与夹爪检查点按需提供，`robot_observe` 支持按需 ROI 放大；阶段级竖直顺应 `max_upward_dz_m` 与实际生效的容差 `effective_tolerances` 对模型公开。
- 新增可选的 lean_projection（默认关闭），保持提示缓存的历史投影；模型代理在请求体超过 4.5 MB 时一次性压缩中段图像。
- 基础设施：模型传输有界恢复（最多 10 轮冷却重发）、连接阶段失败重试、仿真 CUDA 故障时重启一次、规划器 OOM 计为基础设施失败；队列默认并发上限 60。
- 原生 Codex 记忆模型固定为本次运行的模型。

## 0.0.2 — 2026-10-09

合入 `refactor/systematic-20261007`，保留 main 已有的 HD 归档动作回放、轨迹下载、GPU 准入/共享设备选项和独立项目域名配置。

- 工具注册收敛到 ToolSpec/Registry，统一输入输出 schema、role/effect、availability 与 manifest；插件通过窄 ToolContext 接入。
- typed 工具事实与 actor 展示、程序 broker、wire 适配分离；worker/进程协议、生命周期、只读 API、评测队列与包资源有明确模块边界。
- 修复已完成动作的图像/展示异常丢失回执、完整状态查询截断诊断、旧检查点重放倒退 revision、末状态采样失败丢失执行事实，以及程序动作 ID 遗漏。
- 修复封存 WAL 评分读取产生 shm、归档导入丢评分信息、坏 summary 阻断扫描、完整性审计遗漏悬空引用、新队列入口无法被完成钩子识别，以及实体证据时效借用旁证的问题。
- 合并时将 HD 归档加载和对照图读取迁移到既有只读 Store helper，防止轨迹下载或回放读取在封存 WAL 原目录生成 SHM。
- 补齐 agent 开发约定、架构、扩展和验证指南，纠正 actor provenance 递归过滤的文档。见[文档索引](docs/README.md)。

已知边界：可信宿主插件不是恶意 Python 沙箱；standalone replay 使用归档副本；integrity 可验证显式重建 RGB，但原图字节完整性单独报告。本次软件发布不宣称新的 GPU/模型任务成功率。

## 0.0.1 — 2026-10-08

保存重构合并前的 main 主线，包含当时已有的 HD 回放/轨迹下载、GPU 调度与域名改动，以及工作区的模型默认配置、配置覆盖逻辑和重构计划。package version 从占位 `0.1.0` 校正为本次指定的 `0.0.1`；tag 固定提交 `0a59e0e6b0a6f475fa5b88e7638bec92d8801ef3`。

这是旧主线的保存版本。既有环境下配置定向测试 15 项通过；没有为该快照重新宣称完整软件或真实仿真验收。
