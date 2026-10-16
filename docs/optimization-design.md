# 七阶段优化：设计与学习指南

本轮在已有 MCP、流式输出、取消、审批、后台任务和上下文治理之上，加入项目规则、仓库探索、补充指令、按需工具目录、hooks、文件回退以及流程与模型预算。默认仍是完整多 Agent 流程。实现参考公开的 Agent 产品机制，没有依赖未公开的 Claude Code 内部源码。

## 1. 项目规则与通用提示词

入口在 `core/instructions.py`。`ProjectInstructions` 按用户级 `~/.mokioclaw/AGENTS.md`、工作区根目录、已访问子目录的顺序加载规则。每条记录保存绝对来源、相对作用目录和 SHA256。不同目录的规则并存，但提示词明确要求仅对各自目录生效；最新用户要求优先，Markdown 不能授予工具权限。

每次请求前，`inject_instructions` 刷新规则，用一条带标记的系统消息替换旧版本，避免相同规则不断追加。FileRead 会发现对应目录规则；FileWrite/FileEdit 写入前也会检查。首次发现或版本变化会设置 `context_dirty`，拒绝尚未开始的修改，下一次请求读取新规则后重新决策。规则文本作为必要指令参与预算，预算不足会明确抛出 `ContextBudgetError`。

压缩保留必要系统消息及规则来源清单；checkpoint 保存来源与作用目录，恢复时重新读取磁盘版本。`instructions_loaded` 事件记录版本和来源摘要，不复制规则全文。Amiya 的文件名、角色介绍及演示验收标准已移出通用 prompt，示例数据位于 `examples/amiya_plan.py`，可显式加载 `examples/skills/amiya-profile/SKILL.md`。

学习重点：规则的版本和作用范围属于运行时事实；遵循自然语言约定依靠模型，实际工具授权仍由权限代码执行。

## 2. 仓库探索与搜索

`agents/repo_agent.py` 注册只读 `repoAgent`，工具仅有 FileReadTool、GrepTool、FileGlobTool，默认预算 8 次。完整流程的 planner 在工作区已有实现文件时先探索，再规划；空工作区可直接规划。返回入口、调用链、约定、建议修改点、测试及未确定项。证据由实际读取和搜索结果生成文件行号，模型凭空返回的 evidence 不会直接作为已核实证据。

`tools/grep_tool.py` 的两种后端共享候选文件遍历器：用 pathspec 解释根和嵌套 `.gitignore`，跳过 `.git`、`.mokioclaw`、虚拟环境及常见依赖目录，不遍历目录符号链接，也不读取工作区外链接文件。默认先用 rg；未安装 rg 或正则不被支持时降级 Python。结果继续提供 `path/line/text`，增加 `backend/elapsed_ms/truncated`。rg 进程通过运行级异步桥执行，达到结果上限或取消时清理进程。

学习重点：先统一搜索范围，再比较后端。小文件实验可能由进程启动开销主导，不能据此断言 rg 总是更快。

## 3. 运行中补充指令

`core/run_inbox.py` 提供持久队列，`submit_run_message(run_id, message_id, text)` 是 daemon RPC，客户端使用同名函数。消息 ID 最长 128 字符，正文最多 8192 字符，单次运行最多 100 条。相同 ID 和正文返回原序号与状态；相同 ID 不同正文拒绝。消息落盘失败不会确认接收，也不会留下一条可被错误确认的内存记录。

数据流：

```text
CLI send / TUI 输入 → daemon inbox 持久化 → queued
    → 当前请求或工具收尾 → RunRedirected
    → 关闭旧 graph iterator / 补齐旧工具结果
    → latest_user_corrections + 已完成工作 → planner → applied
```

`RunRedirected` 是控制流异常，普通工具错误捕获不会吞掉它。模型请求结束、工具开始、重试开始以及节点结束会检查队列。`tool_call_batch` 补齐被作废调用的 ToolMessage 和事件；正在执行的同步 Subagent 可携带已经完成的状态返回。审批期间收到新消息会唤醒等待并使旧审批失效。后台任务有独立控制对象，结果合并时仍以当前要求为准。

特别要注意 LangGraph 的 custom event 由消费线程接收时，节点生产线程可能还在运行。消费者不会在 token 事件上直接应用并清空队列，而是等待生产线程交回控制。否则生产线程可能看不到新要求，继续执行旧调用。本轮真实模型实验发现并修复了这个竞态。

完成前的 inbox `seal()` 与接收消息共用同一把锁：已接收消息不会被最终完成吞掉。checkpoint 保存队列引用、消息 ID、应用位置；恢复合并未见消息。会话要求也按 ID 合并，避免后续保存旧会话对象时丢失补充指令。

用法：

```bash
uv run mokioclaw send run-xxxxxxxxxxxx "不要修改数据库" --message-id correction-001
```

TUI 运行期间在输入框直接提交，分别显示已排队、已应用。Ctrl+C 仍取消前台，Ctrl+Q/断线仍保留 daemon 重连语义。

## 4. MCP 按需目录

原生工具注册与 MCP 工具发现分离。默认 `mcp_loading="deferred"`，初始只绑定带服务名称/用途摘要的 ToolSearchTool，不启动服务。查询先按服务名或用途选择服务，没有匹配则列候选，不遍历启动所有服务。发现后默认最多加载 5 个工具（可指定上限，最大 20），下一请求重新绑定 schema，实际绑定 schema 继续计入上下文预算。

发现结果仅在运行内缓存；显式 refresh 或配置指纹变化使缓存失效。Skill 列表接受配置中服务前缀的延迟引用，实际加载 Skill 时才发现并校验。加载工具不能扩大 Agent/Skill/全局权限，MCP 调用默认仍要求审批。`mcp_loading="eager"` 和原有直接调用 `build_mcp_tools` 的接口保留。发现及调用继续使用各自短生命周期 stdio 进程，复用已有诊断、超时、取消和日志配额。

学习重点：schema 的可见性、服务的生命周期和操作权限是三个不同问题；加载 schema 不等于批准执行。

## 5. Hooks

`core/hooks.py` 定义 `HookContext`、`HookDecision` 和显式 `register_hook`。声明式规则通过工具名 glob、参数正则匹配。

| 事件 | 允许决定 | 故障处理 |
| --- | --- | --- |
| PreToolUse | warn、block | 异常、超时或无效返回会阻止调用 |
| PostToolUse | warn、add_context | 保留原工具结果，记录诊断 |
| RunFinished | warn、report | 保留运行终态，记录诊断 |

工具顺序固定为参数校验 → 静态权限 → 前 hook → 审批 → 取消/指令检查 → 执行及重试 → 结果裁剪 → 后 hook。同一逻辑调用的前后 hook 各执行一次，重试不重复触发。hook 不修改参数，不提升权限，也不能把工具失败改成成功。

Python 回调必须由可信入口显式注册，且是可导入的模块级函数；配置文件不动态导入项目代码。回调在独立 spawn 进程中运行，默认 2 秒超时，支持取消，结束后回收进程。输出尾部最多 4096 字符，提示最多 4000 字符。进程隔离用于生命周期管理，不是操作系统安全沙箱，回调仍须可信。

```python
# my_hooks.py，可导入的独立模块
from mokioclaw.core.hooks import HookDecision

def verify_edit(context):
    return HookDecision("add_context", "读取刚修改的文件并验证验收标准。")

# 可信的 Python 启动入口，在运行任务的进程中显式注册
from my_hooks import verify_edit
from mokioclaw.core.hooks import register_hook
register_hook("PostToolUse", verify_edit)
```

本地 Python API 可直接使用；独立 daemon 需要在 daemon 的可信启动入口注册，CLI 客户端内存中的注册不会自动跨进程传递。

## 6. 文件变更与回退

`core/changes.py` 使用写前日志：先持久保存完整原始字节及 manifest，再检查当前文件哈希，最后通过同目录临时文件原子替换。保留已有权限。FileWrite/FileEdit 的结果关联运行 ID、调用 ID、变更 ID、前后哈希。备份配额默认 64 MiB，只回收已结束运行中最旧的备份；备份不截断，空间不足则拒绝写入。

如果替换已发生而日志收尾失败，返回 `outcome_unknown`，不假装没有副作用；prepared 记录仍能通过结果哈希判断是否可回退。回退使用工作区写锁，逐个文件检查当前哈希，按逆序恢复连续编辑，匹配的新建文件可删除；后续用户编辑产生冲突时保留现状，不提供强制覆盖。

```bash
# 默认预览最近一次有文件工具变更的运行
uv run mokioclaw rewind --workspace ./workspace
uv run mokioclaw rewind --workspace ./workspace --run-id local-xxxx --file src/app.py
# 显式应用；多个文件可重复 --file
uv run mokioclaw rewind --workspace ./workspace --run-id local-xxxx --apply
```

TUI 空闲时 `/changes` 查看记录，`/rewind [run-id]` 先预览再确认。运行中不允许回退。覆盖范围明确为 FileWriteTool/FileEditTool，Bash/MCP 修改显示未纳入覆盖。回退不删除对话；标记需要重新验证，strict/light checkpoint 均清除旧 passed，成功验证并保存新 checkpoint 后再清除此标记。

## 7. 流程、角色模型与运行预算

`graph/light.py` 提供 `light_executor → verifier`，成功进入 final，否则带进展升级完整流程一次。轻量实现阶段只暴露文件类工具；若请求第二个修改文件、Shell 或 MCP 等更广操作，在副作用前要求升级。完整流程不会重新自动切回轻量。

`workflow_mode=auto` 复用路由请求，只有高置信度、目标明确、单文件、无跨模块依赖才进入 light；缺失/异常提示保守使用 full。默认 full 未改变。

```bash
uv run mokioclaw --workspace ./workspace --workflow-mode light "修改 app.txt 并读取验证"
uv run mokioclaw tui --workspace ./workspace --workflow-mode auto
```

`providers/model_policy.py` 用角色配置选择模型，Subagent 配置可指定 profile；未指定仍使用 MODEL/BASE_URL/API_KEY。profile 必须同时有 context_tokens 和 output_tokens，预算检查与实际输出上限都使用所选模型的预算。路由、探索、规划、实现、验证和压缩统一计数。缺少 usage 时使用带估算标记的保守 UTF-8 字节估计，不能当作账单 token。

可选 `run_token_limit` 默认 0（禁用），请求前按累计用量加预计输入与最大输出预留，超过上限就停止新增请求，保存进展，终态 failed，原因 token_budget_exhausted。实际供应商计量与请求前估算可能有差异，这个上限是客户端预算控制，不是供应商账单硬上限。

## 配置、接口与交付拆分

用户配置 `~/.mokioclaw/config.toml`，项目配置 `<workspace>/.mokioclaw/config.toml`。优先级：显式 CLI/API 参数 → 环境变量 → 项目配置 → 用户配置 → 默认值。CLI 未显式指定的默认选项不再遮蔽配置文件。旧环境变量保持含义；运行控制对象不能通过 TOML 替换。完整示例见 `examples/optimization-config.toml`，凭据只引用环境变量名。

新增事件通过运行生命周期补充 run_id/event_id/context_version，工具相关事件保留调用 ID。规则正文保留原文件引用，长模型文本和 diff 使用已有受配额的诊断产物。旧 checkpoint 的新增字段都有缺省值，可继续读取。

建议学习时按以下中文 Conventional Commits 拆分评审；本次工作区尚未代为创建这些 Git 提交：

1. `feat: 加载分层项目规则并整理通用提示词`
2. `feat: 增加只读仓库探索与双后端搜索`
3. `feat: 支持运行中补充指令与安全重新规划`
4. `feat: 按需发现并动态绑定 MCP 工具`
5. `feat: 增加受限 hooks 扩展机制`
6. `feat: 记录文件工具变更并支持冲突安全回退`
7. `feat: 增加轻量流程与角色模型预算统计`

验收数据、每阶段实验命令与已知取舍见 [验收记录](optimization-acceptance.md)。
