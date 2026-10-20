# 求职展示包

材料基于 `5be75d9` 的代码与已有归档整理。Goal 4 只新增文档、只读查看和检查工具；没有新真实模型试验。远程 CI 的被测版本始终是 `748ca5d`。

当前运行编排已迁移到 CrewAI Flows，见[迁移说明](../../crewai-migration.md)。下方冻结实验、演示及远程 CI 仍对应各自历史版本；没有据此宣称 CrewAI 效果改善。

建议依次阅读：

| 顺序 | 材料 | 能回答的问题 |
|---|---|---|
| 1 | [架构单页](architecture.md) | 哪一层负责执行、控制、记录和独立评分？ |
| 2 | [实验单页](experiments.md) | 实际成功多少，哪些结果不能合并？ |
| 3 | [取舍单页](tradeoffs.md) | 为什么这样做，代价和剩余问题是什么？ |
| 4 | [三条执行链](chains.md) | 如何从一个事件走到源码、测试和终态？ |
| 5 | [成功与失败案例](cases.md) | D01 完成了什么，预算失败如何定位？ |
| 6 | [干净环境复现](reproduce.md) | 如何不调用模型体验、检查和排错？ |
| 7 | [简历草稿](resume.md) | 哪些表述有证据，哪些贡献需本人确认？ |
| 8 | [维护练习](practice.md) | 不看参考解析，能否独立修改和诊断？ |

[事实索引](FACTS.md)与 [facts.json](facts.json)固定了数字、适用版本、JSON pointer 和原始文件哈希。运行 `uv run --no-sync python scripts/check_showcase.py` 检查事实和本地链接；检查不会请求模型或改文件。

[本轮验收与检查](../../experiments/job-readiness-goal4/README.md)记录自动化交付范围。材料已经准备好不等于本人已独立掌握；[记录模板](record-template.md)保持待填写，参考解析在[单独文件](answers.md)。

原有 [D01 演示](../demo.md)、[Goal 2 真实案例回放](../../experiments/job-readiness-goal2/DEMO.md)及所有历史失败继续保留。没有新增或宣称跨文件成功案例，也没有新录制的实时生成。
