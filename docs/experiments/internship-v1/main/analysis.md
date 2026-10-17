# 首批真实实验诊断（无效架构对照）

实验 `internship-v1-20261016`，冻结源码 `486a0f9a14a3110287de8dd2dd87693f6e1c68df`。
56/56 条预定 trial 已执行并保留，模型为 deepseek-chat，同一官方兼容端点；凭证不归档。

**整批 comparison_valid=false，不能用于 baseline/full/light/auto 的公平架构结论。**
同步 Subagent 把运行级 model_profile 清空，未继承 SDK 零重试配置。
环境回退仍为同名模型，但重试条件不一致，适配入口请求计数不能代表所有实际 HTTP 尝试数。
原 manifest、result 收据、worker 记录不修改；标记写在独立 validity.json。
原版本输出保留为 report.original.json/md，report.json/md 是带有效性标记的派生报告。

| 观察口径 | 次数 / 预定 56 |
|---|---:|
| 正常完成且独立验收通过（预先固定的主口径） | 0 / 56 |
| 仅产物通过独立验收（运行失败仍不计完成交付） | 8 / 56 |
| 请求预算停止 | 33 / 56 |
| 上下文预算错误 | 13 / 56 |
| 供应商连接失败（基础设施单列，不排除分母） | 10 / 56 |
| 越界修改 / 固定回归退化 | 0 / 56 |

没有删除失败、替换任务或把参考补丁通过率混入模型成功率。
8 份产物通过都来自 E02，不能外推到其他任务。
这些真实请求只能用于诊断本版本表现，不能将 0/56 解释为架构本身的能力结论。

## 可核对的证据

- `index.json`：全部 trial 的终态、独立验收、错误和文件摘要。
- `trials/<ID>/result.json`、`worker.json`：用量、实际路径、相关 run/stream/tool ID。
- `candidate.patch`：便于阅读的 diff；完整增删文件以 changed-files.tar.gz 和 file-changes.json 为准。
- `trace.tar.gz`：流、模型请求、工具与上下文记录。归档已扫描当前凭证，未发现密钥。
- `grade/target.xml` 和 `regression.xml`：可信评分环境的原始 JUnit。

## 两个不调用模型的复现

```sh
uv run python scripts/reproduce_internship_trial.py E02-light-3
uv run python scripts/reproduce_internship_trial.py M02-baseline-3
```

E02-light-3 在全新容器中再次通过 1 个目标测试和 18 个固定回归测试，但原运行因 ContextBudgetError 失败，原 task_solved 仍为 false。
M02-baseline-3 重现 2 个目标失败、固定回归未退化。两次重验均使用原镜像，same_test_outcome=true。
这是已有产物重验，real_model=false，不新增模型 trial。原始重验收据见 reproductions/。

## 定位与修复边界

M02-baseline-3 的 trace 记录 40 次适配入口请求，其中 12 次用于压缩、28 次用于 codeAgent。
未知模型的计数器会退回 UTF-8 字节估算，可能较保守地触发压缩；这是依据日志和源码的解释，还不是经过消融验证的因果结论。
32K 是配置的估算窗口，不能描述为已实际利用 32K 供应商 token。
模型还曾请求未注册的 Bash 名称；工具执行器返回受控拒绝，没有把它映射为任意 Shell 执行。

本次只修复模型 profile 继承，补充实际工厂/绑定/流式路径的本地回归；旧实现能触发四种架构回归失败。
另补齐每 trial 冻结检查、进程归属恢复、有界报告读取和小预览内存边界，不更改任务、提示词或预算。
用户已单独授权新一批 56 次真实请求，使用独立 ID internship-v1-20261017，不覆盖本批。
新批仍可能失败；只有审核实际配置和记录后，才决定是否具备对照有效性。
