# 参考解析（与练习分离）

## A. 功能练习的边界提示

入口是 [trace_command](../../../src/mokioclaw/cli/app.py#L438)及 [TraceReader](../../../src/mokioclaw/core/transport_trace.py)。应先阅读 CLI 参数声明、现有过滤和 reader 增量去重行为，再决定计数放在哪里。此处不提供实现补丁。

应防止三类错误：在过滤前计数、按 raw 文本行数计数、follow 每一轮重置上限。参数错误应发生在 reader 调用之前；可用 fake reader 断言非法参数没有读取，并用可控两轮数据验证 follow。普通展示与 raw 的输出格式不同，但记录数语义应一致。

评审不只问“测试是否绿色”，还问是否保持默认行为、是否改变 reader 消费状态、测试有没有依赖真实用户日志，以及新功能是否越过只读边界。

## B. 失败题答案

- run ID：`job-readiness-goal2-20261017-b1-M08-v2-full-1`。
- 最后已结束流：verifier，`545885638baa4202aeed3c81f378ec47`，seq 282。流的 finished 只表示这次模型响应结束，不是任务 finished。
- 工具结果：seq 290，调用 `call_01_omTcZXh7rtswVrZJogn79969`；之后 seq 292 发出 `run_budget_exhausted`、reason=`token_budget_exhausted`，seq 294 运行 failed/latest_node=verifier。
- 收据总量：25 请求、31 工具、187,464 token；下一请求输入和输出预留无法满足预算，所以没有继续发出模型请求。
- 最后 verifier 响应仍在安排检查，未给最终判断，记未知。不能把运行 failed 改写成 verifier 判 false。
- `grade.valid=true`、`grade.solved=true`、13 项测试通过；`task_solved` 还要求运行 finished，所以为 false。外部评分在运行退出之后执行，不改写运行终态。

[失败案例](cases.md)给出逐项原始链接。重复检查与工具命名错误是已观察到的开销；“具体哪项导致版本差异”仍缺消融。当前没有新真实试验证明 verifier 收敛已修复。

## C. 安装检查如何判有效

应记录新目录、commit、Python/uv/平台、冻结安装退出码；缓存命中不等于冷缓存成功。Docker 缺引擎或镜像应失败，不能报告全跳过通过。真实检查包含隔离、超时清理和评分篡改负控制；有专属 owner 清理收据才算清理可核对。

本轮 Agent 的检查证据在[Goal 4 报告](../../experiments/job-readiness-goal4/README.md)，历史远程 wheel 证据在 [Goal 3](../../experiments/job-readiness-goal3/README.md)。它们可帮助排错，但不是本人独立操作记录。

## D. 讲解核对点

1. 原始 JSON 与结束原因先于工具 schema；截断整批作废；重新决策仍经过预算、取消和纠正；失败流有独立 ID 和部分输出。
2. 接收即持久化、message_id 去重，生产者边界应用；撤销审批、补未执行工具结果、增加上下文版本；同步/后台控制不同；普通取消不回滚已发生的写入。
3. verifier 是模型判断，独立 grader 用可信测试与允许的候选修改；运行、评分、trial 三种结果分开；空报告和越界修改不能成功。

源码入口与正常/失败路径见[三条链](chains.md)。只记住这些句子不等于掌握；应能依据事件或新场景解释为何保持不变量。
