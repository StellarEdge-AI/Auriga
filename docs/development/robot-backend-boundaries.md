# 机器人后端与动作编译边界

后端实现保留 RoboDojo 的执行语义，拆开纯数据校验、ARX 编译、进程通信与原生操作。所有物理动作仍由 `Harness.execute` 准入；插件不能直接取得 worker 写入通道。

环境初始化由 `robot/backends/robodojo/environment.py` 管理，机械臂控制标识与环境资产绑定见 `embodiments.py`。`robot/backends/factory.py` 选择环境构造器，`action_compilation.py` 将公共动作交给实际后端的编译/校验入口；RoboDojo 的原生执行仍限定在已登记的 X5 组合。完整接入文件和适配要求见[机械臂与环境接入指南](robot-environment-integration.md)。

| 模块 | 输入与输出 | 责任 |
| --- | --- | --- |
| `action_validation.py` | 动作数据、可选能力 → 校验结果或错误；双臂动作 → 单臂投影 | 纯基本校验，不导入运行实例、后端或渲染器。`contracts.py`、`motion_speed.py` 保留旧 import 名称。 |
| `arx_contract.py` | ARX EE/关节前缀请求 → 被选机械臂与目标或错误 | ARX frame/TCP、关节前缀维度及原生数值约束；不启动 worker。 |
| `arx_actions.py` | `kind, args, identity, capabilities` → 动作请求 | 编译移动、抓取参考点、夹爪、等待及双臂请求；不读取 Harness 状态，不执行动作。 |
| `proxy_contract.py` | 调用方提供的障碍/持物包围盒 → 规范化估计 | 校验几何输入；不导入 FK、图像渲染或仿真场景。 |
| `robot_process.py` | worker 命令/环境、JSONL 请求 → 进度/回包/清理报告 | 独占进程、通信、取消通道和退出确认；超时保留 unknown，不重放动作。 |
| `robodojo.py` | 运行配置、有类型操作 → worker 请求 | 组合 RoboDojo 启动命令、校验后端请求、串行操作、reset。继承具体进程 owner，保留原 `RobotBackend` import 和诊断属性。 |
| `worker_protocol.py` | JSONL 输入、adapter factory → JSONL 输出 | 输入线程只接收请求/取消；全部 adapter 调用留在原生主线程。owner EOF 终止进程。 |
| `execution_evidence.py` | receipt、segments、尝试/完成信号、实测样本与计数 → 执行证据 | 每动作独立归集 attempted/completed/uncertain、双臂段记录及三种步数；不读取或写入 simulator。 |
| `robot_worker.py` | 已验证请求 → 规划/原生动作/测量与回执 | 持有 Isaac、RoboDojo、evaluator；公开观察与 observer 证据保持分离。 |

新后端提供 `start/observe/execute/reset/close`，按实际能力提供 `preview_batch` 等接口。`validate_action` 是后端自己的数值约束入口；Harness 先执行身份、generation、当前观察、权限与预算等通用检查，再调用后端校验。不能在未知后端缺少某个能力时偷偷使用 ARX 的转换或执行方式。

`compile_action` 的 `kind` 为 `move`、`grasp`、`gripper`、`wait`；`compile_dual_action` 显式接收两臂参数。旧工具名只由 Harness 的兼容转换入口映射，现代 `robot_action` 批次编译直接调用这些数据函数。调用者提供的单位、TCP、夹爪阶段、速度和依赖关系保持原语义。

reset 只有在前一 worker 的 `process_exit_confirmed=true` 且读取线程的 `reader_exit_confirmed=true` 时才建立新 worker。读取线程绑定启动时的 process、log 与 generation；阻塞回调尚未退出时保留其资源，旧 generation 不能给新 worker 投递回包或写入断连状态。动作末帧缺失会使当前观察失效，但不改写已确认的动作完成/部分执行事实；新动作需要先取得 fresh observation。同动作身份重放仍返回原回执。动作 unknown 不能通过一次观察解除准入阻断。

`ProgramRunner(harness.program_services())` 通过 frozen `ProgramServices` 接收存储、程序角色的校验/调用/effect 回调、观察 ID/profile 快照和按 program ID 查询/取消动作的回调。Runner 不持有 Harness、工具注册表、机器人、Harness 锁或取消事件；服务端 Harness 保留这些状态的所有权。动态插件仍由同一注册表判断 schema、角色、可用性和物理动作审核要求。取消仅能设置匹配 program ID 的当前动作事件，不能影响其他程序的动作。

CPU 验证入口为 `tests/test_robot_boundaries.py`、`test_robot.py`、`test_runtime_shutdown.py`、`test_runtime_lifecycle.py`，以及原有 batch/双臂/夹爪/速度/接触点测试。边界测试在新解释器中禁止导入 Harness、进程 owner、渲染器及 Torch，再加载纯契约和编译器；进程测试使用微型 JSONL fake，覆盖取消、丢连接、OOM 分类和退出确认。真实 Isaac 轨迹与任务结果仍须由独立仿真验收确认。

已取得空闲 GPU 租约后，使用新目录执行无模型的当前接口矩阵：

```sh
python scripts/robot_validation.py --matrix current --gpu <leased-gpu> --run-dir <fresh-directory> --task stack_bowls
```

入口保留 `--matrix legacy` 默认行为。`current` 的所有动作通过 Harness，使用实测 EE 位姿作默认 5 mm 上抬与返回，检查单臂预览不推进时钟、观察检查点暂停与同缓存续跑、不同长度双臂序列及实测同时运动、运动期间夹爪命令变化、三步等待，并在实际执行进度到达后请求停止。`--displacement-m` 仅接受 `(0, .02]`，`--jaw-goal` 仅接受 `[.5, 1]`；夹爪目标必须与当前命令不同，执行后还原。停止测试结束后关闭动作准入。

`validation-report.json` 增量保留请求、回执、观察/轨迹引用、源文件哈希及关闭确认，视频保存为 observer artifact。进程、读取线程或原生关闭缺少确认时不能标记通过。该矩阵的 `execution_role=fixed_interface_validation_without_model`、`task_success=null`：其结论限于接口与执行证据，不能替代任务成功率评估。`tests/test_robot_matrix.py` 用实际编译、Harness 与批次调度代码配合合成规划/跟随器验证矩阵本身；这些 CPU 测试不属于真实仿真验收。

矩阵还运行真实隔离程序：固定 operator 批准原缓存计划、在审批暂停时取消并验证原生时钟不推进。末帧恢复检查在真实两步等待完成后人为丢弃返回的 terminal observation，保留 completed 回执，确认旧观察不能支持新动作，再通过 fresh observe 恢复；该段明确标记 fault injection，不是自然发生的相机故障。
