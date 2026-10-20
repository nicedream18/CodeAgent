# 架构单页：执行与控制分层

定位：本地代码任务的教学型 Agent。当前调度采用 CrewAI Flows，模型适配和受控工具循环保持独立；迁移前实验结果对应原版本。默认 full/local，baseline 只用于评测；复杂度带来的可靠性收益需要实验验证。

```mermaid
flowchart TB
    U[Rich CLI / Textual TUI] --> I[NDJSON RPC / 订阅]
    I --> D[daemon: RunRecord / 审批等待]
    D --> C[RunControl / RunInbox]
    C --> L[运行生命周期 / checkpoint / trace]
    L --> G[CrewAI Flow: router / planner / verifier / final]
    G <--> S[同步 repoAgent / codeAgent / testAgent]
    G --> M[统一模型适配与完整性检查]
    S --> M
    M --> LG[LoopGuard: 提示 / 一次恢复 / 停止]
    LG --> T[共享工具循环与执行策略]
    T --> P[参数 / 静态权限 / 前置 hook / 审批 / 版本复核]
    P --> X[执行 / 有界结果 / 后置 hook]
    X --> H[宿主文件工具 / MCP / hooks]
    X --> B[前台 Bash: local 或显式 Docker]
    G -.只读角色显式后台派发.-> Q[任务队列 / 独立 RunControl]
    Q --> A[独立结果与事件 / 持久化]
    L --> O[候选产物及运行终态]
    O -.仅评测驱动.-> E[可信基础源码 + 候选允许修改 + 隐藏测试]
    E --> F[独立容器验收 / trial 结论]
```

| 边界 | 当前实现与源码 |
|---|---|
| UI / 传输 | [CLI](../../../src/mokioclaw/cli/app.py)、[TUI](../../../src/mokioclaw/cli/tui/app.py)、[daemon](../../../src/mokioclaw/core/daemon.py)负责交互与订阅；客户端断线不等于取消 |
| 运行与 Flow | [生命周期](../../../src/mokioclaw/core/runner/lifecycle.py)、[图](../../../src/mokioclaw/graph/workflow.py)管理路由、上下文、重规划和收尾；trace 在路由前建立 |
| 模型与工具 | [provider](../../../src/mokioclaw/providers/openai_provider.py)、[共享循环](../../../src/mokioclaw/core/tool_loop.py)、[执行器](../../../src/mokioclaw/tools/execution.py)隔开生成与副作用；完整响应还要通过工具 schema |
| 子任务 | [同步 runner](../../../src/mokioclaw/agents/registry.py)沿用前台控制；[后台派发](../../../src/mokioclaw/graph/steps/delegation.py)创建独立控制与运行状态，只接收允许后台的只读角色 |
| 持久化 / 验收 | [checkpoint](../../../src/mokioclaw/core/checkpoint.py)、[trace](../../../src/mokioclaw/core/trace.py)保留进展；[独立 grader](../../../src/mokioclaw/evaluation/grader.py)在可信环境重建测试，不能由模型自评分替代 |

工具链：参数校验 → 静态权限 → PreToolUse → 审批 → 取消/指令/参数/策略/后端版本复核 → 执行与允许的重试 → 有界结果 → PostToolUse。被拒绝或未执行的调用不触发后置 hook；前后 hook 按逻辑调用执行一次。

实现交接后，运行时自动安排 `codeAgent → testAgent → Verifier`。testAgent 是独立注册的第四个子 Agent，默认用 8 次工具预算读取代码和测试、运行检查、报告失败及缺口；修复或补测试再交回 codeAgent。`test_report` 的命令、退出码和状态来自实际工具执行，Verifier 保留最终任务验收，历史独立评分器继续保持可信测试边界。[测试角色说明](../../testing-agent.md)。

同步子任务的已完成副作用保留；前台取消阻止后续步骤。显式后台任务有独立生命周期，父任务取消不等于后台取消。Docker 只是命令后端，不隔离宿主的模型调用、文件工具、MCP 或 hooks。

协作保护跨同步角色共享有界摘要，压缩不清空历史或恢复额度。后台独立恢复，不消耗父任务额度；后台 FIFO 排队取消立即释放闭包，终态缓存 128 项、事件尾部 256 条。见[实现与压力复现](../../collaboration-reliability.md)。
