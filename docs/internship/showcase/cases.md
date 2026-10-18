# 一个成功与一个失败

两者均来自已归档真实请求。以下查看过程离线、只读，不重新执行模型或历史工具命令。

## D01：单文件 FIFO 修复成功

任务：最多取 limit 项；零不消费；负数在改列表前抛错；保持 FIFO 和正常排空。模型在 `fd4f29f` 的 full 流程探索、修改、验收并正常汇报。

证据顺序：

1. [任务与来源](../../experiments/completion/tasks/D01.json)：受控微型任务，不属于正式 12 题。
2. [实际 diff](../../experiments/completion/d01/trials/D01-full-1/candidate.patch)：先负数校验，再切片取出并删除队首，修改一个源文件。
3. [独立收据](../../experiments/completion/d01/trials/D01-full-1/result.json)：`finished`、`task_solved=true`，3 项目标 + 2 项回归通过，19 请求、18 工具、36.845 秒生成。
4. [目标 XML](../../experiments/completion/d01/trials/D01-full-1/grade/target.xml)、[回归 XML](../../experiments/completion/d01/trials/D01-full-1/grade/regression.xml)、[归档重建评分](../../experiments/completion/checks/d01-reproduction.json)。
5. [请求审计](../../experiments/completion/d01/audit.json)、[trace 归档](../../experiments/completion/d01/trials/D01-full-1/trace.tar.gz)与既有 [D01 回放说明](../demo.md)。

```sh
uv run --no-sync python scripts/inspect_showcase_case.py d01
uv run --no-sync python scripts/replay_internship_demo.py docs/internship/success-demo.cast --no-wait
```

查看器应报告 `execution_status=finished`、`artifact_passed=true`、`tests_run=5`。它关联 run ID、最近 stream ID 与工具调用 ID，不打印完整模型 payload，也不解压归档。

这是一次受控单例，不是多文件修改或稳定成功率的证据。过程发生过未知工具名和检查命令错误，模型恢复后完成；没有剪掉这些错误来表示全程无故障。

## B1/M08-v2-full-1：产物正确，验收没有收敛

任务是 trace 的 `--since` 时间过滤与参数校验。它针对冻结任务副本，不表示当前产品 CLI 已包含这个评测任务的实现。

```sh
uv run --no-sync python scripts/inspect_showcase_case.py verifier-budget
uv run --no-sync python scripts/replay_internship_demo.py docs/experiments/job-readiness-goal2/failure-demo.cast --no-wait
```

按以下顺序定位，不从最后的“failed”直接推断代码错误：

| 证据 | 已确认观察 |
|---|---|
| [结果收据](../../experiments/job-readiness-goal2/b1/trials/M08-v2-full-1/result.json) | `failed`、`token_budget_exhausted`；25 请求、31 工具、187,464 token；`grade.valid=true`、`grade.solved=true`，13 项测试 |
| [候选 diff](../../experiments/job-readiness-goal2/b1/trials/M08-v2-full-1/candidate.patch) | 候选实现确实存在，不靠修改测试取得评分 |
| [verifier 最后响应](../../experiments/job-readiness-goal2/cases/b1/verifier-last-response.md) | stream `545885638baa4202aeed3c81f378ec47` 仍在安排检查，没有形成 passed/failed 判断 |
| [trace 归档](../../experiments/job-readiness-goal2/b1/trials/M08-v2-full-1/trace.tar.gz) | canonical 事件 seq 290 是 verifier 的工具结果，292 是预算停止，294 是 run_end failed/latest_node=verifier |
| [最终快照](../../experiments/job-readiness-goal2/cases/b1/final-report-snapshot.md) | 仅保存“预算耗尽、进展已保存”的有界快照，不伪造完整汇报 |
| [重新评分](../../experiments/job-readiness-goal2/regrades/regrade-b1-m08-1/reproduction.json) | 同一归档产物可重新验证；原运行终态不会变为成功 |

从 seq 290 的工具调用 ID `call_01_omTcZXh7rtswVrZJogn79969` 可追到检查命令结果；从最后 stream 可追到对应 verifier 模型输出。run ID 是 `job-readiness-goal2-20261017-b1-M08-v2-full-1`。查看器输出这些关联信息，但不再次执行命令。

直接停止原因已确认：下一请求预留无法满足运行 token 预算。内部 verifier 结论为未知，独立评分通过，因运行没有正常交付而 `task_solved=false`。

“重复检查和规范工具名错误增加开销”是 trace 支持的解释线索；不能据此单独断言它们造成版本差异。当前未实施新的 verifier 收敛修复，也未获新真实复测证据。后续方向是固定必要检查、复用证据和限制验收循环，不能通过放宽测试或增加预算改写本批结果。[原始分析](../../experiments/job-readiness-goal2/ANALYSIS.md)。
