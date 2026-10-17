# 求职交付收敛状态

## Goal 3：本地修复完成、远程普通检查待复验授权

- 最新被测代码：`35162c0342f8600b3b7ce37d84ac55e5b0e3cee4`，工作区干净；源码与检查输入摘要 `e2f8298b151a2c93cc2468a35486d0a1e163bf0468517f0ade1679f10d894d54`。
- macOS ARM64 全量：455 passed、2 skipped；Ruff、31 个 mypy 模块、协议/评测文档及 diff 检查通过。
- 本地真实 Docker：29 passed、0 skipped；独立镜像及 owner 清理确认。wheel 仓库外干净安装、两种 CLI 和离线资源检查通过。
- 补充 Linux ARM64 全量：455 passed、2 skipped；依赖准备后禁网执行，UV_OFFLINE=1。它不能代替远程 x86_64 CI。
- [首轮远程 8ca634d](https://github.com/nicedream18/CodeAgent/actions/runs/39026034469) 因使用 Python 3.12.3 失败；[第二轮 ea542c0](https://github.com/nicedream18/CodeAgent/actions/runs/39027177601) Docker/wheel 通过，普通测试 454 passed、1 failed、2 skipped，失败为 TUI 固定等待时间。
- 两轮全部报告均已下载并校验 GitHub SHA256。新增测试修复等待实际完成/token 事件，并修正审批单测过时 mock，禁止真实安装副作用；本地诊断失败均保留。
- 产品源码、锁、Goal 2 的 384 份产物、7 份缓存、旧 wheel/标签保持原样；未请求真实模型或发布产物。
- 新增提交尚未推送；需按具体清单授权后复验。Goal 3 尚未完整完成。

证据：[验收索引](../experiments/job-readiness-goal3/acceptance.json)、[故障复盘](../experiments/job-readiness-goal3/CI_FAILURES.md)、[设计和操作](../experiments/job-readiness-goal3/README.md)。

## Goal 2：18 次有限真实任务验证已完成

- 固定三任务 × B0/B1 × 三次：18/18 有终态，原顺序执行，无重跑或追加。
- 完整交付：B0 1/9、B1 0/9；独立产物通过均 8/9。B1 未显示完成率改善。
- 16 次 token 停止、1 次正常完成、1 次模型连接基础设施失败，全部保留在分母内。
- 427 次请求、486 次实际工具；token 合计 3,350,157，其中一个请求为估算；缓存创建量未知，不报告费用。
- 两版源码、提示词、锁、任务、实际导入路径、请求配置、事件和恢复前 15 份收据均已审计。
- 成功的 M08-v2（B0）与产物通过但运行失败的 M08-v2（B1）均从归档重建，可信 Docker 13/13 再次通过；原终态未改。
- 唯一成功实际只改一个源文件，多源文件修改后完整交付目标仍为 0，未达成；没有追加实验凑成功。
- `make check`：444 passed、2 skipped；Ruff、26 个严格 mypy 模块及文档/diff 检查通过；真实 `make check-docker` 29 passed（linux/aarch64）。
- 未推送、发布、修改历史批次或冻结 B0/B1。远程 Linux CI 仍未运行。

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
基线实验、真实跨文件任务、远程 Linux CI 和本人掌握程度分别属于后续 Goal，本轮不代填通过。

设计与操作说明：[Goal 1](../experiments/job-readiness-v1/README.md)、[预算设计](../experiments/job-readiness-v1/DESIGN.md)。
