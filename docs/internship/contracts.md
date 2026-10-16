# 实习评测契约

基线为 `6e88c21c294fe041a821a21e0abad9dc095b9364`，工作区最初干净。
实际 `make check`：366 passed / 53.79s，Ruff、严格类型、协议文档和 diff 检查通过。
文件逐项 SHA256 见 `docs/experiments/internship-baseline-2026-10-16.json`，不包含 `.env`。

`EvalTask` 固定源码、依赖、镜像、修改范围及独立测试；`EvalTrialConfig` 固定模型与资源条件。
`EvalTrialResult.execution_status` 表示运行终态，`grade.valid` 表示评分是否有效，
`grade.solved` 表示独立产物测试通过，`safety_assertions` 单独描述控制行为。
正常运行后测试失败仍是未解决；二次截断的安全停止可以符合安全断言，但不能计为任务成功。
旧可靠性演示中的 `passed` 不转换为新任务解决率。空测试集不能判为成功。

12 个任务和 core=48 / coverage=8 的顺序由固定种子 20261016 产生。
M01–M06 是固化版本上的最小注入缺陷，不伪称真实历史提交；M07/M08 是增量需求。
外部案例为 SWE-bench Lite 的自定义离线子集，保留上游来源和许可，不宣称官方得分。
真实模型运行前必须冻结全部验收清单。失败/中断只记一次，不自动重试。
