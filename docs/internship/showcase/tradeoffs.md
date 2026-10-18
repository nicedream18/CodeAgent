# 核心取舍单页

| 问题 | 当前选择与代码 | 证据 | 代价与限制 |
|---|---|---|---|
| 多 Agent 是否值得 | 默认 full，用显式图表达协调、进展和验收；[工作流](../../../src/mokioclaw/graph/workflow.py)保留 light/auto 和评测 baseline | [正式四架构结果](../../experiments/internship-v1/RESULTS.md)均无完整交付，B0/B1 也未证明预留改善 | 增加请求和交接成本；保留教学结构不是性能领先证据 |
| 截断批次有一个完整调用，要不要先执行 | [完整性检查](../../../src/mokioclaw/providers/generation.py)先检查结束原因和原始 JSON；输出截断整批作废，最多重新决策一次 | [混合完整/半截 JSON 测试](../../../tests/test_generation_reliability.py) | 牺牲可能正确的前半批，换取不执行未知片段；额外请求仍受取消、纠正和预算约束 |
| 用户改要求后如何处理旧计划 | [RunInbox](../../../src/mokioclaw/core/run_inbox.py)持久去重，边界处应用；[执行器](../../../src/mokioclaw/tools/execution.py)复核版本；旧审批撤销 | [真实 daemon 纠正/迟到批准测试](../../../tests/test_optimization_integration.py) | 当前模型请求或已启动同步工具先收尾，不撤销已发生的副作用；复杂度来自并发边界 |
| verifier 能否证明完成 | [模型 verifier](../../../src/mokioclaw/graph/steps/review.py)是运行内判断；[grader](../../../src/mokioclaw/evaluation/grader.py)在新容器重建可信测试，单独给 trial 判定 | [B1 产物通过但运行失败](cases.md)；[空/伪造报告测试](../../../tests/test_internship_eval.py) | 多一次评分成本，可信测试只覆盖固定要求；不能声称任意任务绝对正确 |
| 是否把整个 Agent 放进 Docker | [执行后端](../../../src/mokioclaw/core/execution_backend.py)只隔离前台 Bash/评分；命令容器无外网、非 root、只读根、资源受限 | [真实容器测试与报告](../../experiments/job-readiness-goal3/remote/39028453552/docker/report.json) | 每次启停有开销；工作区秘密可见，宿主文件工具/MCP/hooks 未隔离；后台 Bash 不回落到 local |
| checkpoint 能否原样继续 | [恢复](../../../src/mokioclaw/core/checkpoint.py)重新检查规则、读取快照、未应用消息和执行上下文；后端变化作废旧验证 | [恢复后端变化测试](../../../tests/test_execution_backend.py)、[规则回归](../../../tests/test_project_optimization.py) | 恢复会增加读取与重新决策；不能恢复旧 PID、容器或未完成批准 |

讲解时先说触发场景，再指出不可破坏的不变量和对应测试。把“实现了保护”“测试覆盖了某场景”“真实任务效果改善”分开，不能用第一项替代第三项。[逐链时序与失败路径](chains.md)。
