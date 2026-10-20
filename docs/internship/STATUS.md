# 求职交付收敛状态

## 独立 testAgent：本地功能验收完成

- 新增第四个内置子 Agent，full / light 实现后自动测试，再由 Verifier 验收；测试失败、未完成或必要测试缺失不能被成功回复覆盖。
- 默认 8 次工具预算，复用审批、取消、纠正、上下文和 checkpoint；补测试与修复继续由 codeAgent 执行。预算收尾明确标为未运行，不冒充通过。
- 实际工作区 511 passed / 2 skipped，Ruff、39 个严格 mypy 模块和文档/diff 检查通过；本地及真实 Docker 演示均识别错误/修正实现的退出 1/0。
- 仅离线模型与真实本地/容器工具；未新增真实模型实验、推送或远程 CI。下方各归档保留原有版本与数字。

证据：[测试角色说明](../testing-agent.md)、[验收记录](../experiments/testing-agent-v1/README.md)。

## CrewAI Flow 迁移：本地验收完成

- 使用真实 CrewAI Entry / Full / Light Flows 替换 LangGraph 编排，保留原有模型—工具循环、审批、取消、纠正、checkpoint、trace 与后台隔离。
- 固定 CrewAI 1.15.28、MCP SDK 1.28.1；移除 LangGraph 运行依赖，消息 reducer 继续兼容旧压缩标记。
- 本地 macOS ARM64 全量为 496 passed / 2 skipped；Ruff、38 个严格 mypy 模块与文档检查通过。新执行镜像与旧冻结评分镜像分别验收，真实容器 33 passed / 0 skipped，清理确认；wheel 仓库外安装和禁网 Flow 通过。
- 离线端到端文件修复通过；没有新增真实模型请求、推送、发布或远程 CI。下列历史成功率、延迟、演示与 Linux CI 保留其原版本，不能视为 CrewAI 成绩。

证据：[设计和复现](../crewai-migration.md)、[本地验收](../experiments/crewai-migration-v1/README.md)。

## Context Compression A/B：框架与 18 次真实开关对照已完成，实际压缩效果未确定

- 冻结运行时 `d3d9521`，D01 / M07-v2 / M08-v2 × 开关 × 三次，共 18/18 次；419 次请求、2,804,909 个 API 返回 Token，计量核对通过，无估算或缺失 usage。
- 开启 / 关闭完整交付为 3/9 / 4/9，独立产物通过均为 9/9；11 次预算停止保留为 failed。
- 两组总 Token 为 1,395,523 / 1,409,386，生成中位数为 68.344 / 64.569 秒。**开启组实际压缩 0 次，因此不能作为压缩节省或降低完成率的证据。**
- 全部请求输入估算最大 18,781，低于 23,500 压缩水位。当前任务的累计预算先成为限制，真实长上下文压力覆盖仍不足；没有追加真实试验凑效果。
- `make check` 为 483 passed、2 skipped；Ruff、34 个严格 mypy 模块与文档检查通过。此为本地新版本证据，未推送或重跑远程 CI，历史 Goal 1～4 不改写。

证据：[结果与限制](../experiments/context-compression-v1/RESULTS.md)、[运行方式](../experiments/context-compression-v1/README.md)。

## Goal 4：自动化展示包已完成，本人验收待实操

- 新入口：[求职展示包](showcase/README.md)。包含架构、实验、取舍、三条执行链、成功/失败案例、简历草稿与独立维护练习。
- 12 条事实带原始报告、版本、JSON pointer 和摘要；只读案例查看不执行历史工具。
- 本轮 13 项专项测试通过，6 张 Mermaid 图语法通过；干净交付提交 edeacfd 的新环境安装、CLI、两段回放、只读工具及 8 项新测试通过。真实 Docker 29 项通过（未改变的产品代码），详见[验收记录](../experiments/job-readiness-goal4/README.md)。
- 没有新增真实模型试验、跨文件成功宣传、推送或发布；本人练习结果保持待填写。

## Goal 3：已完成

- 远程已验收提交：`748ca5d198df5f42c4a1fc8364022dc7abddda25`；[Linux CI 运行](https://github.com/nicedream18/CodeAgent/actions/runs/39028453552) 三作业均 success。
- 远程普通检查：455 passed、2 skipped；Ruff、31 个 mypy 模块、协议/评测文档及 diff 通过。
- 远程真实 Docker：29 passed、0 skipped，linux/x86_64；独立镜像及 owner 清理确认。wheel 仓库外全新环境安装、site-packages、两种 CLI 和离线资源检查通过。
- 三份报告均下载并验证 GitHub SHA256；Python 3.13.9、uv 0.12.19，检查输入与本地被测代码一致。
- 保留本地 macOS ARM64 的 455/2 全量、29/0 Docker 和 wheel 证据；补充禁网 Linux ARM64 全量为 455/2。
- 两次远程失败及本地诊断失败保留。完成固定 Python 入口、TUI/取消测试同步与审批单测执行后端 mock 修复，没有改产品源码或任务评分。
- 产品源码、锁、Goal 2 的 384 份产物、7 份缓存、旧 wheel/标签/镜像完整性复核通过；未请求真实模型或发布产物。
- 最后状态与报告归档为本地文档提交，未增加推送范围；CI 与 wheel 证据准确对应 748ca5d。

证据：[验收索引](../experiments/job-readiness-goal3/acceptance.json)、[远程报告与下载收据](../experiments/job-readiness-goal3/remote/39028453552/status.json)、[故障复盘](../experiments/job-readiness-goal3/CI_FAILURES.md)。

## Goal 2：18 次有限真实任务验证已完成（历史记录）

- 固定三任务 × B0/B1 × 三次：18/18 有终态，原顺序执行，无重跑或追加。
- 完整交付：B0 1/9、B1 0/9；独立产物通过均 8/9。B1 未显示完成率改善。
- 16 次 token 停止、1 次正常完成、1 次模型连接基础设施失败，全部保留在分母内。
- 427 次请求、486 次实际工具；token 合计 3,350,157，其中一个请求为估算；缓存创建量未知，不报告费用。
- 两版源码、提示词、锁、任务、实际导入路径、请求配置、事件和恢复前 15 份收据均已审计。
- 成功的 M08-v2（B0）与产物通过但运行失败的 M08-v2（B1）均从归档重建，可信 Docker 13/13 再次通过；原终态未改。
- 唯一成功实际只改一个源文件，多源文件修改后完整交付目标仍为 0，未达成；没有追加实验凑成功。
- `make check`：444 passed、2 skipped；Ruff、26 个严格 mypy 模块及文档/diff 检查通过；真实 `make check-docker` 29 passed（linux/aarch64）。
- Goal 2 收尾时未推送、发布或修改历史批次及冻结 B0/B1；当时远程 Linux CI 尚未运行。后续 CI 验收见上方 Goal 3。

报告：[结果](../experiments/job-readiness-goal2/RESULTS.md)、[失败分析](../experiments/job-readiness-goal2/ANALYSIS.md)、[回放与复验](../experiments/job-readiness-goal2/DEMO.md)。
后续优先收敛 verifier 与规范工具名反馈；新产品修复后的真实复测、额外 trial 或远程操作需另行授权。

## Goal 1：已完成（历史记录）

- Goal 1 工程交付：已完成，逐项证据见 ../experiments/job-readiness-v1/acceptance.json。
- B0：`cad8a153584aa5f860982dee33719dc2664730db`，任务环境及角色诊断基线。
- B1：`ee5f073a7640c15c1d693c7e93bc067e9eb8da51`，完成预算预留机制及回归。
- 固定任务：M02-v2、M07-v2、M08-v2；可见 smoke、缺陷/参考修复及负控制均已真实容器验证。
- 检查：make check 437 passed / 2 skipped；mypy 24 个严格模块；真实 make check-docker 29 passed。
- 模型调用：本 Goal 为 0；固定模型比较仅验证控制流，不计真实能力。
- 历史实验、缓存 manifest、标签、旧 wheel：摘要复核保持原样。
- 当时的下一阶段输入：goal2-input.json 固定 18 条待授权计划，保留原始未授权文件；本次授权和完成记录另存 Goal 2。
- 远程推送/发布：本 Goal 未执行。

## 关键结果和限制

三种离线容量上限下，实际 B0 源码都在修改后停止；同一固定驱动的 B1 都进入 verifier 并正常结束。
验收失败负例保持 failed 和已有文件修改；取消、纠正、工具重试、旧批次关闭、压缩、后台隔离、light/strict checkpoint 与 trace 均有回归。
预留采用当前输入/输出预算预测，不能保证真实模型一定完成，不能作为费用硬上限或成功率提升结论。
以上是 Goal 1 当时的边界；后续真实对照与远程 CI 结果见上方 Goal 2/3。真实跨文件完整交付和本人掌握程度仍未确认。

设计与操作说明：[Goal 1](../experiments/job-readiness-v1/README.md)、[预算设计](../experiments/job-readiness-v1/DESIGN.md)。
