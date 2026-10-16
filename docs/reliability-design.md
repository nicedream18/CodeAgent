# 可靠生成、协议与交互：实现说明

本轮在已有七阶段实现上继续完善。默认 `workflow_mode=full`；OpenAI 兼容接口继续作为生产入口。MCP 仍按调用启停，后台任务使用独立 RunControl。开始前的工作区清单和内容哈希见 [baseline](experiments/reliability-baseline-2026-10-16.json)，实际起始基线为 **323 passed / 31.49s**。该清单用于区分上一轮未提交改动，未重置已有工作区。

## 1. 生成边界

`providers/generation.py` 定义 `GenerationOutcome`、适配协议和 `ModelGenerationStopped`。`invoke_streaming` 保持原返回消息接口，统一检查 `finish_reason/stop_reason` 和可取得的原始参数；对原始 JSON 使用严格解析，包括拒绝 NaN/Infinity。工具 schema 仍在执行入口验证。缺少结束原因的兼容模型继续可用。

遇到 `length/max_tokens` 时，整个响应的所有调用均产生 `not_executed` 结果；修复后的 SDK 参数不进入执行器。历史中加入只用于关闭工具消息组的占位调用与结果，然后要求重新生成一个更小的完整步骤。最多重生成一次，角色、工具和输出预算不变。重生成前重新经过取消、补充指令、上下文和 token 预算检查。每次请求分别记录 stream ID、结束原因、generation_attempt 和用量。

再次截断或内容过滤、异常结束、空流、无法聚合的流会透传受控生成错误。路由、聊天和压缩不把它转换成普通回复。生命周期合并前台和同步子 Agent 已有进展，以 failed 收尾，checkpoint 保存 stop_reason；部分文字保存在 trace。成功重生成只返回新响应。真实模型仍可能不能在小预算内完成任务，安全停止不等于任务完成。

## 2. 协议与检查

`core/protocol` 是 RPC、运行选项及基础事件的共享 Pydantic 契约。NDJSON、RPC 名称和业务 result 结构保留。传输入口先验证 envelope，再验证参数，随后才创建运行或修改授权。未知运行选项报错，避免创建一个无法启动的任务。旧客户端可省略协议版本；status 返回版本和 capabilities。新客户端使用显式 Skill/审批管理前检查相关能力。旧订阅客户端收到原有审批字段。

错误码沿用 JSON-RPC：解析 -32700，请求 -32600，方法 -32601，参数 -32602。动态工具参数不塞进静态协议，由工具 schema 负责。

`scripts/gen_protocol_doc.py` 从模型生成 [WIRE_PROTOCOL.md](WIRE_PROTOCOL.md)；`--check` 只比较，不写文件。`make check` 与 CI 都执行冻结安装、Ruff、mypy、文档一致性、pytest 和 diff 检查。严格 mypy 当前覆盖协议、generation、usage、payload_trace、approval、approval_rules、agent_profiles、tool_loop、transport_trace 共 **9 个模块**。其余原有模块尚未整体开启 strict；随重构逐步迁入，不将它们描述为已完成全仓库类型检查。Ruff 覆盖 src/tests/scripts/examples；本轮统一了这些目录的格式和可安全修复项。

## 3. 运行职责与公共循环

`graph/nodes.py` 保留公开导入和可替换依赖入口，实现移到 `graph/steps/{routing,planning,review,compression,delegation,final}.py`。`core/agent.py` 保留公开函数，实现分到 `core/runner/{lifecycle,events,session}.py`。内部通过 facade 访问依赖，兼容旧调用方和已有模型替换测试；这是渐进拆分，不引入第二套运行状态。

`ToolConversation` 统一请求、消息追加、模型轮次及工具批次边界；角色继续提供提示词、动态 schema 绑定和结果转换。codeAgent 在请求前重绑定 MCP 工具，保留轻量升级和预算收尾。工具批次的 context manager 必须处于调用者的执行作用域，保证同步子 Agent 抛出的 RunRedirected 能补齐外层未执行工具结果，不能把它留在生成器 yield 两侧。

`RuntimeConfig` 承载配置默认值，`RuntimeState` 继承它并保存运行可变字段，保留原有扁平访问和构造参数。配置、取消对象和工具状态并未在一次改动中彻底替换。

## 4. 分层 trace

运行 trace 增加 ipc/event/llm 层、方向以及 run/stream/tool/event ID。daemon 传输日志记录接收、校验、响应和订阅推送；运行前的请求用 request ID 与启动响应关联，推送附事件序号和相关 ID。原 trace 的 `custom:*` 包装结构保留，查询层能识别新旧记录。

默认模型记录只保存计量和摘要，部分文本使用诊断文件。只有配置 `[trace] include_model_payload=true` 才额外保存脱敏模型消息/响应。密钥、授权字段及已知环境秘密被替换；文件从创建开始限制为 0600。诊断文件沿用单份 1 MiB、32 份/16 MiB 配额；传输日志同样有轮转配额。日志 I/O 故障产生诊断，不能改变模型或工具执行结果。

`mokioclaw trace --workspace PATH --run-id ID --layer llm --direction 'LLM→CORE' --raw` 只读现有记录；`--follow` 使用每文件 inode/offset 游标，避免按事件保留无限增长的去重集合。模型请求记为 CORE→LLM，响应记为 LLM→CORE。旧记录缺少新字段时仍能读取，无法凭空恢复旧关联信息。

## 5. 输入与显式 Skill

Textual 输入使用 TaskInput/TextArea。Enter 提交，Shift/Alt+Enter 换行；粘贴保留内部换行，不触发提交。`/` 显示命令与合法 Skill 别名，支持筛选、上下移动、Tab 和 Esc。

`/help`、`/skills`、`/skill 名称 参数`、`/agents`、`/permissions` 与 CLI `--skill` 使用同一运行参数。Skill 名与内置命令冲突时记录诊断，仍可通过 `/skill` 调用。目录列举不启动 MCP。显式 Skill 强制进入工作流，进入 codeAgent 后才加载正文和工具限制；planner 的协调工具不受它限制。运行中不能换 Skill，LoadSkillTool 也不能替换已固定的显式 Skill。普通输入继续进入补充指令队列。

## 6. 声明式角色

角色文件位于用户 `~/.mokioclaw/agents/*.toml` 和项目 `.mokioclaw/agents/*.toml`，优先级项目 > 用户 > 内置。配置只引用 repoAgent/codeAgent/searchAgent 的既有受控 runner；字段包括提示词、白名单、预算、model_profile。只读 base 的白名单不能增加 Shell、写入或 MCP。后台资格继承 base，不由 TOML 开启。Python 注册的其他角色继续存在。

加载时保存源文件路径及 SHA256，运行中不重新读取。相同来源重名、无效 TOML、未知模型及越权配置显示来源和原因；无效高优先级配置不会静默回退到同名低优先级角色。范例见 [reviewer](../examples/agents/reviewer.toml) 和 [documenter](../examples/agents/documenter.toml)。工具白名单不等于目录权限，需强制目录约束时配合已有 hooks。

## 7. 审批记忆

默认仍是单次。CLI 提示可选 s/d（会话允许/拒绝）、p/x（项目允许/拒绝）；文件规则可追加 `-dir` 选择父目录。TUI 有对应按钮和目录复选框，显示将匹配的资源。项目规则存在用户目录 `~/.mokioclaw/project-permissions/<canonical-workspace-hash>.json`，不加载仓库中的授权文件；写入使用文件锁、原子替换和 0600 权限。

文件规则匹配工具操作及规范文件/目录范围；Bash 匹配去除首尾空白的完整命令和其他参数摘要；MCP 匹配服务配置指纹及参数摘要。拒绝优先，硬拒绝、Agent/Skill 和 deny 模式仍先于保存的允许。外部路径 Bash 的强制单次审批不由保存规则跳过。

会话规则只在 daemon 内存中保存，以 workspace+session ID 隔离；无会话时绑定运行 ID，结束清理，daemon 重启失效。单次审批绑定运行、调用、参数摘要、上下文版本和策略版本；执行及重试前检查版本。撤销规则会唤醒旧审批并关闭对应弹窗，迟到或重复响应不能改变已经给出的决定。

使用 `mokioclaw permissions list/revoke/clear --workspace PATH`；TUI `/permissions [list|revoke ID|clear]`。无 daemon 时只能操作持久项目规则；会话规则须连接对应 daemon，CLI 可带 `--session-id`。

## 8. 用量与评测

ModelUsage 归一化兼容接口已返回的 usage_metadata/token_usage，包括 OpenAI cached_tokens 和兼容服务的 prompt_cache_hit_tokens。cache_read/cache_creation 缺失为 null，明确表示 unknown；缓存 token 已包含于输入，不再加到 total。混合已知/未知请求保留已知部分及 cache_usage_incomplete。每次请求、失败及重生成均独立计量，CLI/TUI 和 trace 显示运行/角色用量、耗时与估算标记，不计算未配置价格的费用。

`examples/reliability_demo.py` 固定单文件、跨模块、Skill、截断、审批、MCP 故障及补充指令七类输入。每组每种模式重复三次；只有单文件和跨模块比较实际工作流路径，其余是独立边界实验，模式标签用于分组，不把它们解释为工作流性能差异。离线固定模型必须单 worker；真实模型可用独立临时工作区并发运行。结果包含原始观察、失败、请求数、token、时间分布、源码哈希和 lock 哈希。真实网络实验不进入 CI。验收数据及限制见 [reliability-acceptance.md](reliability-acceptance.md)。
