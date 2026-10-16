# 上下文预算与治理：实现与验收

日期：2026-10-15。对应 [完善计划](context-governance-plan.md)。

## 已实现内容

| 工作项 | 实现 | 证据 |
| --- | --- | --- |
| 统一预算 | `core/context_budget.py` 计算窗口、输出预留、安全余量和高/目标水位；请求统计消息及工具 schema | 阈值前一 token、等于高水位、等于硬上限和超过硬上限的测试；未知模型回退到 UTF-8 字节估算 |
| 模型输出约束 | `providers/openai_provider.py` 将预留落实到生成参数，并保留选定的 `max_tokens` / `max_completion_tokens` 字段 | 两种真实请求 payload 的测试，避免 SDK 对兼容端点静默改名 |
| 工具结果总量 | `core/tool_output.py` 对成功/失败/异常结果统一限制字段、总量、集合、深度和节点数 | 大集合、深层对象、循环对象、长异常、真实 stdio MCP 大结果测试；Bash 原生裁剪也计入事件 |
| 渐进读取 | 文件工具增加字符范围读取和 `model_visible_complete`；完整结果按内容寻址保存；Bash 保留头尾 | 20 万字符单行尾部补读、尾部错误及完整日志测试；结果产物最多 32 份/每份 16 MiB，Bash 日志另保留最近 32 份 |
| 每轮治理 | `invoke_streaming` 在每次调用前准备上下文并做最终预算检查，覆盖 planner、verifier、两个子 Agent、聊天、路由和摘要模型 | 实际 codeAgent 多轮产物测试；所有捕获请求符合估算预算；流式路径沿用相同入口 |
| 受控压缩 | `core/context_compaction.py` 对完整消息组压缩；当前要求由运行时复制，模型只摘要历史发现 | 无悬空工具调用、晚到用户修正不被旧工具组覆盖、无效摘要/模型失败降级、必要内容超限拒绝测试 |
| 事实与恢复 | 任务、用户修正、TODO、验收、命令、来源、后台任务与已应用结果保持可追溯；保存完整事实和活动版本 | 两次压缩后的 strict/light checkpoint 恢复测试；100 条来源和结果去重记录恢复；会话 compact 完整来源归档 |
| 手动 compact | 空闲会话压缩、运行中请求/状态查询/待执行请求取消、本地 Python 控制对象 | CLI 测试和真实 loopback daemon RPC 测试；pending→applied 的事件落盘；运行中的操作在执行边界应用 |
| 并发与写入协调 | 后台检索预算/缓存/控制状态独立；父级合并来源；工作区会话写入租约防止离线 compact 覆盖活动会话 | 两个后台任务并发压缩、父级手动请求独立、来源合并且结果只应用一次、session_busy 测试 |
| 副作用保护 | 压缩器不重放工具；相同文件内容再次写入返回 `unchanged`，保留 mtime | 真实 codeAgent 压缩后只写一次；重复相同写入无第二次实际写入的测试；完整真实模型流程最终只有一次实际写入 |
| 可观察性 | TUI 事件及侧栏展示预算和压缩前后估算量；CLI 与 trace 记录范围、方法、状态和版本 | Textual 实际组件/运行中快捷键测试；trace 事件去重和汇总测试；完整流程 trace 记录 33 次压缩、73 次裁剪、0 次压缩失败 |

当前事实是权威输入，摘要不能自行修改验收标准、完成 TODO 或添加来源。大量来源使用有界目录与完整事实引用；不可丢弃的要求本身超出窗口时明确拒绝发送请求。

## 自动化验证

```bash
uv run pytest -q
uv run ruff check src/mokioclaw/core/context_budget.py src/mokioclaw/core/context_compaction.py src/mokioclaw/core/context_control.py src/mokioclaw/core/tool_output.py src/mokioclaw/core/session_lock.py tests/test_context_governance.py examples/context_governance_demo.py
git diff --check
```

全量结果：**243 passed**，其中本次新增的上下文治理文件包含 **44 个用例**（含参数化用例）。原有水位与事件测试已按新的预算和事件契约更新。新增模块、实验脚本和新测试通过 Ruff；既有文件的历史 lint 问题未作为本次任务扩大处理。

## 真实模型实验

使用已有 `.env` 中配置的 `deepseek-chat` 和兼容端点，未使用模拟模型代替以下实验。没有进行网页检索，来源 URL 是测试明确提供的字符串。

### 1. 构造长历史后的真实续作

复现命令（使用全新的目录）：

```bash
uv run python examples/context_governance_demo.py --workspace /tmp/mokio-context-demo-new
```

由真实文件工具产生长历史，模型随后继续执行读取、写入与回复。窗口 24000、输出预留 1024、安全余量 512。

| 操作 | 压缩前估算 | 压缩后估算 | 结果 |
| --- | ---: | ---: | --- |
| 自动压缩 | 53573 | 3384 | applied，未降级，达到目标 |
| 活动任务手动压缩 | 56918 | 3437 | applied，未降级，达到目标 |
| 保存会话手动压缩 | 7470 | 3181 | 保留 2 条最近记录；真实模型下一轮仍返回指定来源 |

报告的 criterion 必须等于规格末尾的随机标记，source 必须等于指定 URL；由独立 Python 断言检查。报告仅发生一次实际写入。

完整的可提交结果摘要见 [续作实验 JSON](experiments/context-governance-2026-10-15.json)。这项实验明确使用了预先构造的工具历史，并不将它表述成完整 supervisor 流程。

### 2. 完整 router → planner → codeAgent → verifier

```bash
uv run python examples/context_governance_demo.py --full-workflow --workspace /tmp/mokio-context-workflow-new
```

调用公开 `stream_agent_events` API，没有注入预构造的模型消息。真实模型规划任务、读取长规格文件、经历压缩、创建报告并由 verifier 检查。独立 Python 断言另外核验报告字段和规格文件未被改动。

- 完整流程通过，verifier 判定通过。
- 90 次模型请求经过预算检查，包含摘要模型调用；每次发送的估算输入均在预算内。
- 发生 33 次压缩；trace 汇总 73 次裁剪、0 次压缩失败，最大发送输入估算为 34261。
- 报告实际写入一次，相同内容再次写入会返回 `already_applied`，不会再次修改文件。
- 实验配置：窗口 48000、输出预留 2048、安全余量 1024，高/目标水位为 75%/45%；专门将 codeAgent 工具调用预算设置为 20，应用默认值仍为 10。

第一次完整流程实验发现注册器为尚未产生的摘要传入 `None`，已修复文本归一化并加入注册器到实际 runner 的回归测试。进一步补上当前 TODO 同步及相同内容写入去重后，最终流程通过。

可提交的最终结果摘要见 [完整流程 JSON](experiments/context-governance-workflow-2026-10-15.json)。

## 验收矩阵映射

| 原计划用例 | 对应验证 |
| --- | --- |
| A 长文件 | `test_long_single_line_can_be_read_at_tail_and_visibility_is_honest`、实际 codeAgent 产物测试、两种真实实验 |
| B 大命令输出 | `test_bash_preserves_tail_error_and_full_log`，完整日志及尾部错误断言 |
| C 大结构化结果 | 参数化总量/JSON/幂等测试、长异常测试、真实 stdio MCP 大输出测试 |
| D 单节点多轮增长 | `test_code_agent_completes_artifact_after_multiple_local_compactions`、完整真实流程 |
| E 手动压缩 | CLI、TUI、真实 daemon RPC、取消状态测试；真实续作实验同时覆盖运行和会话 compact |
| F 需求保留 | 事实保留、最新修正、两次压缩恢复、会话尾部修正、真实报告和来源校验 |
| G 压缩故障 | 非法预算、无效摘要、模型不可用、必要内容超限、计数器回退、消息协议异常测试 |
| H 后台与并发 | `test_background_results_keep_independent_budgets_and_merge_sources_once`，原有后台队列/取消/并发测试 |
| I 恢复与连续运行 | strict/light 恢复、状态去重、重复请求、同内容写入无副作用、既有循环/工具预算测试 |
| J 展示一致性 | 真实 RPC request_id 与完成事件落盘、TUI 实际组件和侧栏、CLI 格式化及 trace 指标测试 |

## 使用边界

- 兼容端点的 token 计数是保守估算，图状态估算与实际模型请求估算分开标记；不把它们当作厂商报告的 usage。端点若返回 usage，另记录 `model_usage` 事件。
- 真实模型仍可能重复请求读取。现有工具调用与工作流上限保持生效；本轮不宣称解决所有模型规划效率问题。
- 必要规则、用户要求或工具 schema 自身无法装入配置窗口时，返回明确预算错误；不静默删除需求。
- 手动运行压缩作用于主上下文，后台子任务独立管理预算。正在进行的模型请求或工具不会被半途打断；尚未开始的压缩请求可以取消。
- 完整历史、事实、来源和版本在磁盘保留，后续 prompt 使用有界投影和引用。运行中的 daemon 需要加载本次代码后才能提供新增 RPC；本轮实验使用独立测试 daemon，没有停止用户的其他 daemon。
