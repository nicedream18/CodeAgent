# testAgent 本地验收

在此前 CrewAI 迁移工作树上增加独立测试角色，没有提交或推送，也没有调用真实模型。

| 检查 | 实际结果 | 证据 |
|---|---|---|
| 实际项目统一检查 | 511 passed / 2 skipped；Ruff、39 个严格 mypy 模块、协议/评测文档、diff 通过 | [报告](check/report.json)、[日志](check/make-check.log) |
| 本地测试演示 | 错误实现 failed / 退出 1；修正实现 passed / 退出 0 | [输出](demos/local.log) |
| 真实 Docker 演示 | 同样识别两个实现，使用新 Linux ARM64 镜像 | [输出](demos/docker.log)、[调用记录](demos/result.json) |

两种演示的固定模型均声称“All tests passed”，状态仍由实际 pytest 结果决定。simulation=true、real_model=false；验证的是控制与工具执行，不代表真实模型测试质量。

新增 15 个测试实例覆盖角色范围、真实本地成功/失败、没有执行却声称成功、写工具及后台调用拒绝、生成后取消、实现使旧报告失效、Verifier 门禁、full/light 顺序、压缩事实及 light/strict 恢复重测、预算收尾不消耗预留。

开发期间的首次全量为 502 passed / 8 failed / 2 skipped，保留 [记录](development-failure/report.json)。原固定模型夹具未适配新角色，且新增阶段干扰原预算收尾；修复后明确记录预算跳过为 not_run，并保留 Verifier 所需预留，未将跳过计为测试通过。

最终报告 HEAD 仍是原基线且 dirty=true，应结合 [acceptance.json](acceptance.json) 中的源码与检查指纹识别被测工作树。历史迁移、真实矩阵和 CI 归档不改写。每份日志最多 1 MiB，白名单归档经凭证检查；[SHA256 清单](SHA256SUMS)不包含自身。

[设计、使用与限制](../../testing-agent.md)。
