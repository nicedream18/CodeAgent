# MokioClaw Agent 能力扩展：学习与实验路线

这份路线把新增能力拆成独立章节。每一章先运行实验，再实现功能，最后用验收用例确认边界行为。现有 CLI、TUI 和 Python event generator 保持可用；CLI/TUI 默认通过本机 daemon 运行，Python API 默认仍在当前进程执行。

## 章节顺序

| 章节 | 先学什么 | 动手实验 | 完成标准 |
| --- | --- | --- | --- |
| 1. Tool Registry 与权限策略 | 参数 schema、最小权限、allow/ask/deny | 配置文件写工具为 ask，分别批准和拒绝；再配置 Bash 为 deny | 未批准操作不执行；授权只覆盖单次操作；旧 Bash 审批仍通过 |
| 2. 失败分类与重试 | 瞬时错误、幂等操作、有界退避 | 用假工具依次抛 timeout、参数错误和权限拒绝 | 仅幂等的瞬时失败重试；重试次数有限；事件和结果包含错误类别与次数 |
| 3. Skills | 能力描述、发现、按需加载、权限范围 | 将 `examples/skills/html-page/SKILL.md` 复制到 workspace 的 `.mokioclaw/skills/html-page/`；再制作格式错误文件 | Agent 可发现并加载 Skill；超出 Skill 工具/权限范围的调用被拒绝；trace 记录加载行为 |
| 4. 可配置 Subagent | 任务委派、上下文隔离、结果契约、预算 | 让 planner 将研究交给 searchAgent，再把证据交给 codeAgent | Subagent 串行运行；工具限制和循环上限生效；handoff、证据、未完成项可追踪 |
| 5. 上下文治理 | token 水位、输出裁剪、保留关键记忆、compact | 让工具返回超长文本并让对话超过水位；执行 `/compact` 或 `mokioclaw compact` | 模型输出预留预算；长工具字段有截断标记；摘要保留任务和未完成 TODO |
| 6. MCP stdio | MCP initialize/list/call、子进程生命周期和 schema | 用 `examples/mcp/echo_server.py` 作为本地 MCP server 调用 echo | 工具进入统一权限、重试、输出裁剪、事件和 trace 路径；服务异常返回结构化错误 |
| 7. Token streaming | 增量响应、工具调用 chunk 聚合、UI 渲染 | 用慢响应 mock 模型观察 Rich/TUI 的增量文字 | 最终消息与聚合文本一致；工具调用 chunk 保留；流与工具事件可关联；中断时保留部分文本及日志引用 |
| 8. Daemon 与 IPC | JSON-RPC 2.0、NDJSON、事件订阅、断线恢复 | CLI/TUI 同时订阅任务；关闭一个客户端后重连；在另一个客户端批准操作 | daemon 只监听 loopback；客户端自动托管 daemon；run 事件持久化并按序号续订；客户端断开不取消任务 |

## 配置实验

### 每工具权限

通过环境变量配置 JSON 对象。未列出的工具沿用当前默认行为；Bash 的命令风险检查仍然有效。

```bash
MOKIO_TOOL_POLICIES='{"FileWriteTool":"ask","FileEditTool":"ask","BashTool":"deny"}' uv run mokioclaw "创建一个网页"
```

## 第 1 章：统一工具注册与权限策略

### 概念

- **注册表**负责维护工具名称、参数 schema、能力和适用 Agent 类型，并生成模型可见的工具集合。
- **授权层**在工具真正运行前检查参数、Skill/Subagent 范围和全局策略。Prompt 的说明不能替代这层检查。
- **审批**是对某个工具调用 ID 的一次决定；获批不创建持久白名单。
- **执行入口**是唯一普通工具调用入口，统一做校验、授权、执行、失败分类、重试和输出裁剪。

### 数据流

```text
ToolSpec → ToolRegistry → 当前 Agent 的工具 schema
                         ↓ 模型 tool call
             参数校验 → scope 检查 → 策略评估
                                      ├─ deny → 返回权限错误
                                      ├─ ask → permission.requested → 单次决定
                                      └─ allow → 继续
                         ↓
                 Tool.invoke → ToolMessage / trace
```

决策优先级为：危险 Bash 命令硬拒绝、Bash 显式拒绝模式、显式工具拒绝策略、工作区外操作强制审批、显式审批策略、Bash allow 模式、Bash 原有风险分类、其他工具默认策略。内置工具默认值保持兼容，MCP 默认要求审批，未知工具拒绝。

### 配置和动手实验

模式变量使用 JSON 正则数组，正则在 runtime 创建时校验：

```bash
MOKIO_BASH_DENY_PATTERNS='["\\bdeploy\\b"]'
MOKIO_BASH_ALLOW_PATTERNS='["^pytest(?:\\s|$)"]'
```

1. 把 `FileWriteTool` 配成 `ask`，验证批准后只执行当前写入；拒绝时文件不存在或内容不变。
2. 使用缺少必填参数的工具调用，确认先返回 `invalid_input`，不会出现 `permission_requested`。
3. 让 Bash 尝试部署命令、工作区外路径和 allow 模式命令，观察拒绝/审批/允许的优先级。
4. 从只读 Agent 和执行 Agent 分别查看工具清单，确认它们共享注册元数据但能力不同。

### 验收

- 每个内置工具从统一 ToolSpec 注册；重复工具名或定义不匹配会在注册时失败。
- 所有 Agent 工具调用，包括 SearchAgent 与 TodoUpdateTool，都经过统一参数校验和授权入口。
- 参数错误不会触发审批；危险命令不会触发审批；外部路径审批请求与结果携带同一 `tool_call_id`。
- 批准仅对本次调用有效；CLI/TUI 和 Python 事件流仍保留原有调用方式。

## Skill 格式、权限与运行过程

Skill 文件使用标准 YAML frontmatter 和 Markdown 正文。工作区目录 `.mokioclaw/skills/<目录名>/SKILL.md` 优先于 `~/.mokioclaw/skills/<目录名>/SKILL.md`；同名工作区文件即使格式错误也会遮蔽用户级版本，并在发现列表中显示诊断。

```yaml
---
name: html-page
description: Create an accessible, self-contained HTML page.
tools:
  - FileWriteTool
permissions:
  - filesystem_write
---

Write a semantic HTML page, then report its path.
```

`tools` 和 `permissions` 都必须是字符串列表。省略或写成 `[]` 都表示不额外授予工具或能力。所列工具必须已注册，且它的全部 capability 都必须出现在 `permissions` 中。`LoadSkillTool` 与 `TodoUpdateTool` 是 CodeAgent 必需的框架控制工具，不需要在 Skill 中重复声明；Skill 权限不会给 Subagent 或其他 Agent 使用。

```text
CodeAgent 展示 Skill 摘要
  → 模型按需调用 LoadSkillTool
  → YAML / 工具注册 / capability 校验
  → 成功后替换当前 Skill scope，并将正文返回给模型
  → 每次后续工具调用由统一授权入口执行 scope 检查
  → CodeAgent 调用结束时恢复进入前的 scope
```

失败加载保留此前权限范围，不会清空或扩大权限。一次 CodeAgent 调用只保留一个活动 Skill；成功加载另一个 Skill 会替换它。Trace 的 `events.jsonl` 中搜索 `custom:skill_loaded` 和 `custom:skill_load_failed`，可以按 `tool_call_id` 查看结果；专属事件只记录名称、来源、路径和诊断，不记录正文。`summary.json` 的 `skill_load_successes`、`skill_load_failures` 和 `skills_used` 是汇总信息。

### CLI 手动演示

```bash
mkdir -p .mokioclaw/skills
cp -R examples/skills/html-page .mokioclaw/skills/
cp -R examples/skills/invalid-skill .mokioclaw/skills/
uv run mokioclaw "创建一个无障碍 HTML 页面，按 html-page Skill 工作流执行"
```

完成后从运行事件中的 `trace_summary` 找到 trace 目录，检查 `events.jsonl` 与 `summary.json`。观察有效 Skill 的 `skill_loaded` 事件和格式错误 Skill 的 `skill_load_failed` 诊断。可尝试把 HTML Skill 的 `tools` 改为空列表，再要求它写文件，确认 `FileWriteTool` 在实际执行前被 Skill scope 拒绝。

### Subagent 预算与工具范围

```bash
MOKIO_SUBAGENTS='{"searchAgent":{"max_tool_calls":3,"allowed_tools":["WebSearchTool"]},"codeAgent":{"max_tool_calls":8,"allowed_tools":["FileReadTool","FileWriteTool","FileEditTool","GrepTool","BashTool","NotepadReadTool","NotepadAppendTool","LoadSkillTool","TodoUpdateTool"]}}' uv run mokioclaw "查资料并创建网页"
```

Python 扩展可以在创建 runtime 前注册自定义串行 Subagent：

```python
from mokioclaw.agents.registry import SubagentDefinition, register_subagent


def run_auditor(state, instruction, *, writer=None, max_loops=4):
    return {
        "ok": True,
        "summary": f"Audited: {instruction}",
        "evidence": [],
        "unresolved": [],
    }


register_subagent(
    SubagentDefinition(
        name="auditor",
        description="Review a deliverable without modifying it",
        runner=run_auditor,
        allowed_tools=frozenset({"FileReadTool", "GrepTool"}),
        max_tool_calls=4,
    )
)
```

Planner 可通过 `CallSubagentTool` 按名称派发注册角色。内置 `CallSearchAgentTool` 和 `CallCodeAgentTool` 保留为兼容别名。

### MCP stdio server

`MOKIO_MCP_SERVERS` 是一个 JSON 数组。`command` 和 `args` 应指向用户信任的本地程序。

```bash
MOKIO_MCP_SERVERS='[{"name":"example","command":".venv/bin/python","args":["examples/mcp/echo_server.py"]}]' uv run mokioclaw "使用 MCP echo 工具回显 hello"
```

### Context 与 daemon

```bash
MOKIO_CONTEXT_TOKEN_LIMIT=400000
MOKIO_MODEL_CONTEXT_RESERVE_TOKENS=8192
MOKIO_CONTEXT_OUTPUT_LIMIT_CHARS=12000
MOKIO_CONTEXT_RESULT_LIMIT_CHARS=24000
MOKIO_CONTEXT_SAFETY_TOKENS=1024
MOKIO_CONTEXT_HIGH_RATIO=0.85
MOKIO_CONTEXT_TARGET_RATIO=0.60
MOKIO_TOOL_RETRY_LIMIT=2
MOKIO_MCP_TIMEOUT_SECONDS=30
MOKIO_DAEMON_DIR=~/.mokioclaw/daemon
MOKIO_DAEMON_PORT=8765
```

CLI/TUI 首次运行会自动启动本机 daemon。可用 `uv run mokioclaw daemon status` 查看状态，用 `uv run mokioclaw daemon stop` 关闭。

上下文治理的详细实现、自动/手动压缩实验和验收映射见 [完善计划](context-governance-plan.md) 与 [验收记录](context-governance-acceptance.md)。运行中的 TUI 可按 `Ctrl+K` 请求压缩；CLI 可使用 `compact --run-id ... --wait 30`。所有 token 数均注明估算方式，完整工具结果与来源通过磁盘引用补读。

## 系统数据流

```text
CLI / TUI
   │ JSON-RPC 2.0 over NDJSON (loopback TCP)
   ▼
Local daemon ── task worker ── existing synchronous LangGraph API
   │                               │
   │ event log + subscription      ├─ Tool Registry / Permission Policy
   │ approval request/response     ├─ Skills / serial Subagents / MCP stdio
   ▼                               ├─ context monitor / compaction
Reconnectable client               └─ trace / checkpoint
```

## 端到端演示

1. 将 HTML Skill 放入 workspace 的 Skill 目录。
2. 配置 example MCP server，并让任务需要调用它。
3. 运行需要研究和文件变更的任务，确认 planner handoff 给 Subagent。
4. 将 `FileWriteTool` 配置为 `ask`，在 CLI/TUI 批准一次操作。
5. 运行期间断开一个客户端，再用另一个客户端订阅同一 run。
6. 检查 workspace trace、daemon run event log、TODO 和最终验收结果。

测试使用 fake model 和临时 MCP stdio server，不依赖真实模型凭据或外部网络；端到端演示才需要模型和对应 MCP 服务配置。

MCP stdio 与 token streaming 的运行取消、故障边界、实验和验收记录见 [MCP 与流式输出验收](mcp-streaming-acceptance.md)。
