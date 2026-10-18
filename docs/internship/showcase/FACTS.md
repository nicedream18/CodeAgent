# 展示事实与证据索引

由 facts.json 生成；检查命令只读，不修改文件。数值对应各自冻结版本，不合并分母。

## F01：远程 Linux 普通检查 455 通过、2 跳过

版本：`748ca5d`。范围：Goal 3 单次远程运行。

证据：
- [pytest-summary.json](../../../docs/experiments/job-readiness-goal3/remote/39028453552/check/pytest-summary.json) · JSON pointer `/counts/passed` = `455`
- [pytest-summary.json](../../../docs/experiments/job-readiness-goal3/remote/39028453552/check/pytest-summary.json) · JSON pointer `/counts/skipped` = `2`
- [report.json](../../../docs/experiments/job-readiness-goal3/remote/39028453552/check/report.json) · JSON pointer `/metadata/commit` = `"748ca5d198df5f42c4a1fc8364022dc7abddda25"`

限制：不是 Agent 任务成功率；文档归档提交未被该运行测试。

## F02：远程真实 Docker 29 通过、0 跳过

版本：`748ca5d`。范围：Linux x86_64 实际容器检查。

证据：
- [pytest-summary.json](../../../docs/experiments/job-readiness-goal3/remote/39028453552/docker/docker-detail/pytest-summary.json) · JSON pointer `/counts/passed` = `29`
- [pytest-summary.json](../../../docs/experiments/job-readiness-goal3/remote/39028453552/docker/docker-detail/pytest-summary.json) · JSON pointer `/counts/skipped` = `0`
- [report.json](../../../docs/experiments/job-readiness-goal3/remote/39028453552/docker/report.json) · JSON pointer `/docker/cleanup/confirmed` = `true`

限制：Docker 仅覆盖前台 Bash 和可信评分；不证明整个 Agent 沙箱化。

## F03：wheel 在仓库外全新环境导入、CLI 和离线资源检查通过

版本：`748ca5d`。范围：Goal 3 wheel 作业。

证据：
- [report.json](../../../docs/experiments/job-readiness-goal3/remote/39028453552/wheel/report.json) · JSON pointer `/installed_probe/ok` = `true`
- [report.json](../../../docs/experiments/job-readiness-goal3/remote/39028453552/wheel/report.json) · JSON pointer `/installed_probe/site_packages_verified` = `true`
- [report.json](../../../docs/experiments/job-readiness-goal3/remote/39028453552/wheel/report.json) · JSON pointer `/wheel/sha256` = `"605001843b1c5e1ff3a105bf76abdbee5a17f43e1e19338d28522e1974f38359"`

限制：已验收 wheel 对应远程版本；后续 README 变化会改变新 wheel 元数据。

## F04：D01 正常完成并通过 5 项可信测试

版本：`fd4f29f`。范围：一个六行 FIFO 函数的受控单文件任务。

证据：
- [result.json](../../../docs/experiments/completion/d01/trials/D01-full-1/result.json) · JSON pointer `/execution_status` = `"finished"`
- [result.json](../../../docs/experiments/completion/d01/trials/D01-full-1/result.json) · JSON pointer `/task_solved` = `true`
- [result.json](../../../docs/experiments/completion/d01/trials/D01-full-1/result.json) · JSON pointer `/grade/tests_run` = `5`

限制：自主生成的一次成功案例；不属于 12 题矩阵，不代表跨文件成功率。

## F05：D01 使用 19 次模型请求、18 次工具、36.845 秒生成

版本：`fd4f29f`。范围：同一个 D01-full-1 trial。

证据：
- [result.json](../../../docs/experiments/completion/d01/trials/D01-full-1/result.json) · JSON pointer `/model_metrics/requests` = `19`
- [result.json](../../../docs/experiments/completion/d01/trials/D01-full-1/result.json) · JSON pointer `/tool_count` = `18`
- [result.json](../../../docs/experiments/completion/d01/trials/D01-full-1/result.json) · JSON pointer `/generation_seconds` = `36.845`
- [result.json](../../../docs/experiments/completion/d01/trials/D01-full-1/result.json) · JSON pointer `/model_metrics/total_tokens` = `89762`
- [result.json](../../../docs/experiments/completion/d01/trials/D01-full-1/result.json) · JSON pointer `/model_metrics/cache_read_tokens` = `16256`
- [result.json](../../../docs/experiments/completion/d01/trials/D01-full-1/result.json) · JSON pointer `/model_metrics/cache_creation_tokens` = `null`

限制：150 秒是讲解回放时长，不是原运行耗时。

## F06：正式矩阵 56 次完整交付 0，独立产物通过 8

版本：`1e6d1cc`。范围：12 题：48 次核心架构对照与 8 次 full 覆盖。

证据：
- [report.json](../../../docs/experiments/internship-v1/corrected/report.json) · JSON pointer `/scheduled` = `56`
- [report.json](../../../docs/experiments/internship-v1/corrected/report.json) · JSON pointer `/recorded` = `56`
- [report.json](../../../docs/experiments/internship-v1/corrected/report.json) · JSON pointer `/solved` = `0`
- [report.json](../../../docs/experiments/internship-v1/corrected/report.json) · JSON pointer `/independent_artifact_passed` = `8`
- [report.json](../../../docs/experiments/internship-v1/corrected/report.json) · JSON pointer `/budget_stops` = `32`
- [report.json](../../../docs/experiments/internship-v1/corrected/report.json) · JSON pointer `/infrastructure_failures` = `11`

限制：8 份通过产物均未正常收尾；不称为完成 8 题，不与后续批次合并。

## F07：此前 56 次配置不一致的真实试验保留为无效对照

版本：`初始 internship-v1 批次`。范围：同步 Subagent 未继承 SDK 零重试配置。

证据：
- [validity.json](../../../docs/experiments/internship-v1/main/validity.json) · JSON pointer `/status` = `"invalid"`
- [report.json](../../../docs/experiments/internship-v1/main/report.json) · JSON pointer `/recorded` = `56`

限制：不能作为公平架构比较；既不是正式成绩也不能删除。

## F08：B0/B1 各 9 次：完整交付 1/9 与 0/9，产物通过均 8/9

版本：`B0 cad8a15 / B1 ee5f073`。范围：M02-v2、M07-v2、M08-v2，各版各题 3 次。

证据：
- [comparison.json](../../../docs/experiments/job-readiness-goal2/comparison.json) · JSON pointer `/terminal` = `18`
- [comparison.json](../../../docs/experiments/job-readiness-goal2/comparison.json) · JSON pointer `/versions/B0/complete_deliveries` = `1`
- [comparison.json](../../../docs/experiments/job-readiness-goal2/comparison.json) · JSON pointer `/versions/B1/complete_deliveries` = `0`
- [comparison.json](../../../docs/experiments/job-readiness-goal2/comparison.json) · JSON pointer `/versions/B0/independent_artifact_passes` = `8`
- [comparison.json](../../../docs/experiments/job-readiness-goal2/comparison.json) · JSON pointer `/versions/B1/independent_artifact_passes` = `8`
- [comparison.json](../../../docs/experiments/job-readiness-goal2/comparison.json) · JSON pointer `/versions/B0/multiple_source_file_deliveries` = `0`
- [comparison.json](../../../docs/experiments/job-readiness-goal2/comparison.json) · JSON pointer `/versions/B1/multiple_source_file_deliveries` = `0`

限制：没有证明完成预留提高交付率；唯一完整成功仅修改一个源文件。

## F09：Goal 2 共 427 请求、486 工具、3,350,157 token（含估算）

版本：`B0/B1 冻结版本`。范围：18 个 trial，B0 有 1 个估算请求。

证据：
- [comparison.json](../../../docs/experiments/job-readiness-goal2/comparison.json) · JSON pointer `/versions/B0/requests_total` = `208`
- [comparison.json](../../../docs/experiments/job-readiness-goal2/comparison.json) · JSON pointer `/versions/B1/requests_total` = `219`
- [comparison.json](../../../docs/experiments/job-readiness-goal2/comparison.json) · JSON pointer `/versions/B0/tools_total` = `238`
- [comparison.json](../../../docs/experiments/job-readiness-goal2/comparison.json) · JSON pointer `/versions/B1/tools_total` = `248`
- [comparison.json](../../../docs/experiments/job-readiness-goal2/comparison.json) · JSON pointer `/versions/B0/usage/total_tokens/known_sum` = `1656405`
- [comparison.json](../../../docs/experiments/job-readiness-goal2/comparison.json) · JSON pointer `/versions/B1/usage/total_tokens/known_sum` = `1693752`
- [comparison.json](../../../docs/experiments/job-readiness-goal2/comparison.json) · JSON pointer `/versions/B0/estimated_usage_requests` = `1`
- [comparison.json](../../../docs/experiments/job-readiness-goal2/comparison.json) · JSON pointer `/versions/B1/usage/cache_creation_tokens/known_sum` = `null`

限制：缓存读取包含在输入中，创建量未知；连接失败实际计费未知，不报告费用。

## F10：代表失败：产物通过 13 项，但运行因 token 预算停止

版本：`ee5f073`。范围：B1 / M08-v2-full-1 单个 trial。

证据：
- [result.json](../../../docs/experiments/job-readiness-goal2/b1/trials/M08-v2-full-1/result.json) · JSON pointer `/execution_status` = `"failed"`
- [result.json](../../../docs/experiments/job-readiness-goal2/b1/trials/M08-v2-full-1/result.json) · JSON pointer `/stop_reason` = `"token_budget_exhausted"`
- [result.json](../../../docs/experiments/job-readiness-goal2/b1/trials/M08-v2-full-1/result.json) · JSON pointer `/task_solved` = `false`
- [result.json](../../../docs/experiments/job-readiness-goal2/b1/trials/M08-v2-full-1/result.json) · JSON pointer `/grade/solved` = `true`
- [result.json](../../../docs/experiments/job-readiness-goal2/b1/trials/M08-v2-full-1/result.json) · JSON pointer `/grade/tests_run` = `13`
- [result.json](../../../docs/experiments/job-readiness-goal2/b1/trials/M08-v2-full-1/result.json) · JSON pointer `/model_metrics/requests` = `25`
- [result.json](../../../docs/experiments/job-readiness-goal2/b1/trials/M08-v2-full-1/result.json) · JSON pointer `/tool_count` = `31`
- [result.json](../../../docs/experiments/job-readiness-goal2/b1/trials/M08-v2-full-1/result.json) · JSON pointer `/model_metrics/total_tokens` = `187464`

限制：verifier 未形成最终判断；不是测试失败，也不能算完整交付。

## F11：B1 的 9 次均进入 verifier，但完整交付仍为 0

版本：`ee5f073`。范围：Goal 2 角色交接观察。

证据：
- [comparison.json](../../../docs/experiments/job-readiness-goal2/comparison.json) · JSON pointer `/versions/B1/verifier_entered_trials` = `9`
- [comparison.json](../../../docs/experiments/job-readiness-goal2/comparison.json) · JSON pointer `/versions/B1/completion_reserve_triggered_trials` = `8`
- [comparison.json](../../../docs/experiments/job-readiness-goal2/comparison.json) · JSON pointer `/versions/B1/assessments/verifier_passed/unknown` = `9`

限制：交接变化不是有效性提升；因果尚未通过额外消融确认。

## F12：本地 ARM64 与远程 Linux 证据分别保存

版本：`Goal 3 最终验收`。范围：本地 macOS、容器 Linux ARM64、远程 Linux x86_64。

证据：
- [acceptance.json](../../../docs/experiments/job-readiness-goal3/acceptance.json) · JSON pointer `/checks/make_check/passed` = `455`
- [acceptance.json](../../../docs/experiments/job-readiness-goal3/acceptance.json) · JSON pointer `/checks/real_docker/passed` = `29`
- [acceptance.json](../../../docs/experiments/job-readiness-goal3/acceptance.json) · JSON pointer `/remote_verified` = `true`

限制：架构不同的镜像摘要分别记录；不能声称本地运行等于远程 CI。
