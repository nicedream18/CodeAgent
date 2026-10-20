# CrewAI Flow 迁移验收（本地）

基于 `8df67d7`，将运行编排替换为真实 CrewAI Flows，保留原有受控执行循环。本目录不是新真实任务评测，也不是远程 Linux CI 结果。

| 验证 | 实际结果 | 证据 |
|---|---|---|
| macOS ARM64 全量检查 | 496 passed / 2 skipped；Ruff、38 个严格 mypy 模块、协议/评测文档及 diff 通过 | [report](check/report.json)、[日志](check/make-check.log) |
| 真实容器 | 33 passed / 0 skipped；执行用新镜像、历史任务评分用冻结旧镜像，所属容器清理确认 | [结果](docker/result.json)、[构建日志](docker/build.log) |
| 同一新 Linux 镜像的真实 Flow | 11 个编排测试通过，容器禁网、非 root、只读根文件系统 | [结果](linux-flow/linux-flow-result.json)、[复现脚本](linux-flow/check_linux_flow.py) |
| wheel 干净安装 | 仓库外全新虚拟环境、site-packages、CLI、依赖一致性、离线真实 Flow 执行通过 | [结果](wheel/result.json)、[探针](wheel/probe.py) |
| 固定模型示例 | 3 次请求、2 次文件工具，独立断言通过；仅模拟 | [结果](offline/result.json)、[输出](offline/demo.log) |

完整被测代码指纹、锁哈希和镜像 ID 见 [acceptance.json](acceptance.json)。检查报告中的 HEAD 仍为迁移前基线且 dirty=true，须同时使用源码指纹识别该被测工作树，不能只凭 HEAD 声称基线也有这些新检查。

新镜像固定为 `sha256:98528618fbe210796610fabfd3b80acf5645271109acb71d726d7b525b5c1609`，使用独立本地标签 `mokioclaw-crewai-migration:20261020`。没有覆盖历史 `mokioclaw-execution:0.2.0`；旧实验及评分继续使用其原镜像。准备阶段因下载速度使用公开 PyPI 镜像，项目锁仍保留官方源，未改依赖版本与哈希。wheel 依赖从已校验锁哈希的缓存离线安装；移除临时 editable 后才安装并验证实际 wheel。

全量中的两个跳过项是需要显式 Docker 开关的集成测试；独立容器检查启用后均执行。新增 11 个编排测试覆盖聊天分支、完整流程循环、增量事件、上下文传递、节点边界关闭、异常透传、节点上限、无自动 memory、消息重置、禁止 LangGraph 导入及轻量升级压缩历史。

首次新镜像容器检查为 32 passed / 1 failed，保留 [失败报告](failed-docker/result.json) 与 [导入失败详情](failed-grading/reference/execution.json)。历史 M07 源码使用 MCP 2 的 `Client`，误用新 MCP 1 镜像导致收集失败。修复将执行与评分镜像分开，增加缺失评分镜像失败及两种镜像绑定的 2 项测试；没有修改历史任务、可信测试、参考补丁或放宽评分。CI 已配置分别构建两个镜像，但本轮没有触发远程 CI。

复现命令与设计见 [迁移说明](../../crewai-migration.md)。测试使用临时 HOME 和无模型密钥子环境；wheel 探针禁止 socket 连接。没有新增真实模型请求、推送、远程 CI 或发布。原真实成功率、延迟和历史 Linux CI 不能转记为 CrewAI 的结果。

日志每份不超过 1 MiB，总量不超过 16 MiB，归档前检查已配置凭证值及常见密钥模式。归档只含白名单报告，不含用户配置、凭证目录、完整虚拟环境或 wheel 二进制。[SHA256 清单](SHA256SUMS)不包含清单自身。
