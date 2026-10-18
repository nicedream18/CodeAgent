# 实验单页：按版本和分母比较

完整交付要求 `execution_status=finished` 且独立 `grade.solved=true`。后者还要求有效非空测试、固定回归、修改范围和测试完整性通过；只看目标测试绿色会漏掉越界修改。

| 冻结版本 / 范围 | 完整交付 | 独立产物通过 | 解释与证据 |
|---|---:|---:|---|
| 正式 `1e6d1cc`，12 题共 56 次 | 0/56 | 8/56 | [正式结果](../../experiments/internship-v1/RESULTS.md)，此前另 56 次配置不一致，标无效对照 |
| D01 `fd4f29f`，受控 FIFO 单例 | 1/1 | 1/1，5 项测试 | [收据](../../experiments/completion/d01/trials/D01-full-1/result.json)，不并入矩阵，不称跨文件成功 |
| B0 `cad8a15`，3 题各 3 次 | 1/9 | 8/9 | [Goal 2](../../experiments/job-readiness-goal2/RESULTS.md)，唯一成功只改一个源文件 |
| B1 `ee5f073`，相同 3 题各 3 次 | 0/9 | 8/9 | 9 次均进入 verifier，但均预算停止；不能称完成预留提升交付率 |

正式矩阵核心比较为 M02/M03/M07/E02 × baseline/full/light/auto × 3 次，共 48 次；其余 8 题 full 覆盖。四架构均 0/12 完整交付。运行失败包含 32 次请求预算停止、13 次上下文预算错误和 11 次连接失败；不从分母删去基础设施问题。[机器报告](../../experiments/internship-v1/corrected/report.json)。

Goal 2 是 M02-v2/M07-v2/M08-v2 的版本对照，流程均为 full；不是四架构对照。18 次中 16 次 token 停止、1 次正常结束、1 次连接失败；每版各 1 次越界修改，评分保留原文件。[范围与失败分析](../../experiments/job-readiness-goal2/ANALYSIS.md)。

| Goal 2 用量 / 耗时 | B0（9 次） | B1（9 次） |
|---|---:|---:|
| 请求 / 实际工具 | 208 / 238 | 219 / 248 |
| 输入 / 输出 token | 1,572,513 / 83,892 | 1,601,229 / 92,523 |
| 总 token（含估算） | 1,656,405 | 1,693,752 |
| 缓存读取（已包含在输入） | 228,096 | 192,896 |
| 生成秒 min / median / max（含失败） | 46.058 / 60.425 / 128.853 | 54.345 / 71.346 / 77.283 |

原始字段见 [comparison.json](../../experiments/job-readiness-goal2/comparison.json)。B0 有 1 个请求为估算；缓存创建量未知。唯一成功耗时 128.853 秒，B1 没有成功耗时样本，不能把失败停止更快解释为完成更快。没有价格配置，不推算费用。

工程检查另列：`748ca5d` 的[远程 Linux CI](https://github.com/nicedream18/CodeAgent/actions/runs/39028453552)为 455 passed / 2 skipped、真实 Docker 29 passed / 0 skipped、wheel 干净安装通过；它不改变任何模型成功率。完整事实与摘要校验见 [F01～F12](FACTS.md)。

限制：小样本、受控任务与公开 Issue 可能受训练数据影响；无官方 SWE-bench 成绩。保留 full 是教学与协调默认值，当前结果不支持架构优越性；下一步应验证验收收敛，而非用增加角色解释成功。
