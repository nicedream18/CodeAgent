# 求职收敛 Goal 1 状态

- Goal 1 工程交付：已完成，逐项证据见 ../experiments/job-readiness-v1/acceptance.json。
- B0：`cad8a153584aa5f860982dee33719dc2664730db`，任务环境及角色诊断基线。
- B1：`ee5f073a7640c15c1d693c7e93bc067e9eb8da51`，完成预算预留机制及回归。
- 固定任务：M02-v2、M07-v2、M08-v2；可见 smoke、缺陷/参考修复及负控制均已真实容器验证。
- 检查：make check 437 passed / 2 skipped；mypy 24 个严格模块；真实 make check-docker 29 passed。
- 模型调用：本 Goal 为 0；固定模型比较仅验证控制流，不计真实能力。
- 历史实验、缓存 manifest、标签、旧 wheel：摘要复核保持原样。
- 下一阶段：goal2-input.json 固定 18 条待授权计划，目前没有启动。
- 远程推送/发布：本 Goal 未执行。

## 关键结果和限制

三种离线容量上限下，实际 B0 源码都在修改后停止；同一固定驱动的 B1 都进入 verifier 并正常结束。
验收失败负例保持 failed 和已有文件修改；取消、纠正、工具重试、旧批次关闭、压缩、后台隔离、light/strict checkpoint 与 trace 均有回归。
预留采用当前输入/输出预算预测，不能保证真实模型一定完成，不能作为费用硬上限或成功率提升结论。
基线实验、真实跨文件任务、远程 Linux CI 和本人掌握程度分别属于后续 Goal，本轮不代填通过。

设计与操作说明：[Goal 1](../experiments/job-readiness-v1/README.md)、[预算设计](../experiments/job-readiness-v1/DESIGN.md)。
