# 协作可靠性：循环保护、后台队列和上下文压力

默认开启 LoopGuard，保留 CrewAI Flow 和已有 testAgent、权限、审批及执行器。目标是**限制协作循环、治理上下文增长**；检测依赖可观察证据，不能证明消除了所有循环。

## 1. 检测与数据流

`ToolConversation.request → LoopGuard → 正常模型请求 → batch → execute_tool → 重试/有界结果 → LoopGuard.observe`。逻辑调用结束后观察一次；内部瞬态重试不增加历史条数。结果已写入消息后，在下一调用或请求前处理序列决策。

| 规则 | 处理 |
|---|---|
| 同一成功结果 3 次 | 下一次正常请求提示复用证据，允许正常重复读取 |
| 相同输入连续同类非瞬态失败 3 次 | 提示调整策略；第 4 次执行前拦截 |
| 2～4 个调用构成的序列完整重复 3 次 | 首轮可建立证据，后两轮无新证据/工作区变化时触发 |
| 6 轮请求无进展且已有重复调用或重复验收失败 | 触发恢复 |

调用摘要由工具名、规范化参数及后端指纹计算 SHA-256。对象键排序，数组顺序保持，Bash 只去掉命令首尾空白。结果排除调用/容器 ID、耗时、日志路径（含 stdout/stderr 前缀引用）等字段，保留 ok、错误类型、退出码与内容哈希；普通耗时/ISO 时间文本做稳定化。状态最多保存 128 条调用和 128 个证据哈希，没有原始参数或输出。完整工具记录仍遵守原有 trace/结果文件策略。

实际文件内容变化、新的非空文件/行号证据、新命令检查结果、失败修复计为进展。TODO/NOTEPAD/HISTORY_SUMMARY 更新、模型宣言及压缩不计。工作区内容摘要沿用搜索忽略范围，按流读取文件；当前每个边界需扫描可见文件，极大仓库的 I/O 开销是已知限制。只在忽略范围外发生的变化不能作为进展证据；应通过受控工具读取其必要结果。

用户补充要求应用、全局权限规则或执行后端变化使旧窗口失效，但**不返还恢复额度**。切换角色及上下文版本变化本身均不重置检测。不同读取范围、修改后的重读/复测可以产生新证据。

## 2. 控制流与恢复

`LoopRecovery` 继承 BaseException，专用于控制流，普通模型/工具错误处理不能吞掉它。未启动的旧批次调用收到 `not_executed`；已开始同步子 Agent 的外层调用标为 interrupted，已完成文件修改保留。

第一次触发由运行协调层关闭 Flow、保留父任务和子任务允许回传的进展、交回 Planner 重规划。light 升级 full。第二次触发以 `failed / collaboration_loop_detected` 收尾，保存 checkpoint、部分流和 trace；正常验收次数上限不改变。取消或已接收的补充要求优先，旧审批版本失效并关闭等待弹窗。

后台任务有独立 guard、恢复额度与 RunControl；后台恢复由原受限 runner 重新决策一次，不能使用父 Planner 的写入能力。再次触发只结束该后台任务，其他任务与父任务继续。后台恢复不增加权限，也不重放已执行工具。

light/strict checkpoint 保存摘要与额度；旧 checkpoint 缺字段时使用默认值。压缩的摘要模型不能清除运行时检测状态。事件 `loop_warning/replan/stop` 含运行、最近模型流、工具调用和 handoff 关联，CLI/TUI 共享摘要。

配置（用户/项目 TOML 的现有优先级不变）：

```toml
[runtime]
loop_guard_enabled = true
```

或 `MOKIO_LOOP_GUARD_ENABLED=false` 暂时关闭。首版阈值固定，不增加独立 CLI 参数。

## 3. 后台队列收敛

调度锁统一保护接收、FIFO 出队、取消和终态发布。默认 4 个 worker、16 个等待项；排队取消立即删除待执行 ID 和 runner 引用。worker 空闲前也释放上一任务闭包。

内存只保留最近 128 个终态任务及每任务最近 256 条事件，事件序号独立递增。旧查询从磁盘 JSON/JSONL 读取，返回结构保持；旧事件回放需要扫描对应日志，但不长期驻留内存。关闭后拒绝提交；超时关闭标记 interrupted，迟到返回不能覆盖终态。线程无法被 Python 安全强杀，正在执行的任务仍须遵守已有取消和工具超时边界。

接收持久化失败返回 `persistence_failed`，不入队；执行/终态持久化故障保留失败诊断并释放调度资源。daemon 重启仅标记旧未完成任务 interrupted，不重建 runner、不自动执行工具。

## 4. 离线实验与复现

```sh
uv run --no-sync pytest -q tests/test_loop_guard.py tests/test_subagent_tasks.py tests/test_collaboration_pressure.py
uv run --no-sync python examples/collaboration_pressure.py --output /tmp/mokio-pressure
# 需已准备固定镜像和运行中的 Docker；失败不会回退 local
uv run --no-sync python examples/collaboration_pressure.py --execution-backend docker \
  --docker-image sha256:YOUR_PREPARED_IMAGE --output /tmp/mokio-pressure-docker
```

三个场景 × 压缩开关 × 自动/手动，共 12 个有限用例。窗口 16384、输出预留 1024、安全余量 512，高水位 12620，输入硬限制 14848。fixture 采用 UTF-8 字节/4 的明确估算器，并计入固定工具 schema；摘要请求也纳入计量。

- 长文件：初次工具结果实际裁剪；多个读取范围越过水位后，补读尾部要求，独立检查 answer.txt。
- 大输出：真实 Bash 产生大量输出及尾部错误；上下文只接收有界摘要，完整日志可查，产物必须包含实际尾部错误。
- 修复和纠正：实际写文件、失败检查，压缩后应用补充要求，再修复并检查；压缩事实文件保留任务、最近失败和未完成 TODO。

开启时必须至少一次 applied 压缩且产物断言通过；关闭时必须越过硬限制、受控停止并保留 checkpoint。每例预先消耗一个恢复额度，检验压缩不会返还；这是测试设置，不计为真实循环事件。

结果带 `simulation=true / real_model=false`，只验证控制契约，不展示真实 token/延迟收益、不混入解题成功率。既有 [18 次真实 A/B](experiments/context-compression-v1/RESULTS.md)归档不变。新验证记录见 [本轮证据](experiments/collaboration-reliability-v1/RESULTS.md)。

## 5. 分阶段提交说明

1. `feat(runtime): 新增有界协作循环检测与进展识别`
2. `feat(flow): 贯通循环重规划与安全停止并保留恢复额度`
3. `fix(queue): 收敛后台待执行集合及终态事件缓存`
4. `test(context): 增加实际越过水位的离线压缩压力实验`
5. `docs(reliability): 归档循环与队列验证及已知限制`

源码、测试和文档可按上述边界评审；本轮不自动推送，不追加真实模型请求。
