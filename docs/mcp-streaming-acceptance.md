# MCP stdio 与模型流式输出验收

本章在已有工具注册、权限、LangGraph 事件、checkpoint 和上下文治理上，补齐取消与故障收尾。实现保留同步图节点接口，生产模型与 MCP 的异步等待由运行级 `RunControl` 管理。

## 从调用链学习

```text
Ctrl+C / mokioclaw cancel RUN_ID
  → daemon cancel_run → RunControl.cancel_event
  → 关闭模型异步流 / MCP Client
  → llm_stream_end(interrupted) / mcp_diagnostic
  → checkpoint + session.last_run_status + trace
  → run_finished(interrupted)
```

`RunInterrupted` 是控制信号。路由、聊天、压缩、工具重试和同步 Subagent 会继续传播它。每个前台运行拥有一个延迟启动的异步事件循环；有界队列最多保留 64 个 chunk，事件由同步调用线程发出。兼容同步模型在调用和 chunk 边界检查取消。

显式后台 Subagent 拥有独立的 `RunControl`，前台取消后仍可通过 `subagent status/result/events` 查询。已经开始的普通同步工具在原超时内收尾，之后禁止启动新工具；已有文件或外部副作用保留。

LangGraph 可能在节点抛出异常时丢弃已排队的 custom 事件。因此图 writer 在事件发出时先写 trace，并添加 `trace_id/trace_seq`；外层消费时去重。流事件使用独立 `stream_id`，相同 node 的两条流不会合并。trace 在路由前创建，聊天和复杂工作流共用一个生命周期。部分工具调用参数不会作为可执行调用写回历史。

## 使用与实验

更新代码后重启已有 daemon，使服务端加载新的取消协议：

```bash
uv run mokioclaw daemon stop
uv run mokioclaw --workspace /tmp/mokio-live "请用中文分20节解释Python生成器，每节100字。"
# 生成期间按 Ctrl+C，观察 cancelling、interrupted 和恢复信息。

uv run mokioclaw tui --workspace /tmp/mokio-tui
# Ctrl+C 取消前台任务；Ctrl+Q 退出界面，服务端继续运行。

uv run mokioclaw daemon status
uv run mokioclaw cancel run-xxxxxxxxxxxx
```

CLI 全局选项放在任务文本前。TUI 可在路由阶段、首个 token 前及审批弹窗中取消。取消请求不会立即宣告任务完成，资源收尾后才显示 `interrupted`；CLI 的中断退出码为 130。

不需要模型凭据的慢流实验：

```bash
uv run python examples/mcp_streaming_demo.py --workspace /tmp/mokio-stream-demo
```

真实模型实验读取现有 `.env`，先正常生成并比较文本聚合，再收到两个文本增量后取消：

```bash
uv run python examples/mcp_streaming_demo.py --real --workspace /tmp/mokio-stream-real --report /tmp/mokio-stream-report.json
```

本地 MCP 示例：

```bash
MOKIO_MCP_SERVERS='[{"name":"example","command":".venv/bin/python","args":["examples/mcp/echo_server.py"]}]' uv run mokioclaw "调用 MCP echo 工具回显 hello"
```

MCP 每次发现和调用都创建、关闭一个进程；调用仍受默认 `ask` 策略约束。拒绝审批后不会启动调用进程。`MOKIO_MCP_TIMEOUT_SECONDS` 默认 30 秒，覆盖启动、握手和操作等待。

## 事件与故障诊断

- `run_started` 在路由前提供 run ID；`cancel_run` 重复调用保持幂等，已结束的任务返回原有终态，未知 ID 返回 `unknown_run`。
- `llm_stream_start/token/end` 共享 `stream_id`、`node`，结束状态为 `finished/failed/interrupted`。空流、无法聚合的 chunk 返回错误，不会偷偷追加请求。
- `mcp_diagnostic` 包含服务、`discover/call` 阶段、操作 ID、工具调用 ID、耗时、状态、错误原因、stderr 摘要和日志引用。超时按 `transient` 分类，MCP 调用不会自动重试。
- stderr 持续排空，内存仅保留末 4 KiB，单份诊断文件最多 1 MiB。MCP stderr 和模型文本文件共用 `.mokioclaw/diagnostics` 下最多 32 份、16 MiB 的保留配额；截断通过元数据明确标注。旧日志会清理。
- Trace 的 `events.jsonl` 记录流起止、工具和权限事件；文本保存在日志引用中，长内容在事件内保留摘要。中断时也保存已生成部分。

排查时先看 `mcp_diagnostic.reason`：`launch_failed` 检查 command/cwd/env；`timeout` 检查服务是否响应及超时设置；`connection_closed` 检查服务退出和 stderr；`protocol_error` 检查 stdout 是否混入非协议内容及响应格式；`server_error` 查看工具失败结果。SDK 对某些损坏协议会等待到超时，这类情况表现为 `timeout`，具体内容仍可结合 SDK 错误与服务日志排查。

## 验证记录

新增自动化覆盖集中在 `tests/test_mcp_streaming.py`、`tests/test_run_cancellation.py`；现有 runtime、daemon、TUI 和上下文治理测试同步更新，保留原有实质断言。

覆盖正常聚合、中文内容块、工具参数分块、usage、空流、错误 chunk、静默等待取消、审批取消、同步 Subagent、后台独立运行、daemon RPC、真实 TUI 按键、Rich 原文显示、单次 trace 收尾、日志配额、MCP 启动失败/退出/超时/协议损坏/工具错误/大量 stderr 和进程回收。

2026-10-16 使用配置中的 `deepseek-chat` 完成实验：

- 正常生成 19 个文本增量，界面输出文本与同一次请求的聚合响应一致，首个文本增量约 936 ms。
- 两个文本增量后取消，保留“好的，”；从取消到停止及记录收尾约 4 ms。
- Textual 无窗口测试使用真实模型，正常回复的 25 个增量与聚合响应、可展开全文一致；实际发送 Ctrl+C 后约 154 ms 回到可输入状态。此实验将取消回调直接连到本地 RunControl，daemon RPC 由 CLI 实验单独验证。
- 实际 Rich CLI 通过隔离 daemon 运行，对客户端发送 SIGINT；退出码 130，daemon、trace 和聊天流均为 `interrupted`，保存了 51 个字符的部分回答。

机器可读结果见 [模型实验](experiments/mcp-streaming-2026-10-16.json)、[CLI 实验](experiments/mcp-streaming-cli-2026-10-16.json)、[TUI 实验](experiments/mcp-streaming-tui-2026-10-16.json)。记录中的临时路径用于本机检查，可能随系统临时目录清理而失效；实验结论和文本保留在 JSON 中。

最终全量回归：**282 项通过**，本次新增 **39 个用例**；包含轻量／严格 checkpoint 在启动快照写入前恢复旧任务及验收条件、多轮 TUI 的迟到取消响应隔离。耗时约 23.58 秒。新增运行控制、诊断文件和演示脚本通过 Ruff；本次涉及 Python 文件的导入与名称检查及 `git diff --check` 通过。详见 [自动化结果](experiments/mcp-streaming-tests-2026-10-16.json)。
