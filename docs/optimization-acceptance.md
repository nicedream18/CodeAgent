# 七阶段优化验收记录

实验日期：2026-10-16。本轮从 `a41b053` 附近已有 MCP/流式与运行取消实现继续开发。JSON 中的 `source_ref` 是实验时 HEAD，不代表未提交改动为空；本轮实现仍在工作区，未代替用户创建 Git 提交。实验仅使用临时工作区，真实模型读取已有 `.env`，报告不包含密钥。

## 回归基线与最终检查

实施前重新运行：`uv run pytest -q`，**282 passed in 26.91s**。这是功能测试基线，没有保存改动前真实模型成本，不能据此推算历史费用节约。

当前完整回归：**323 passed in 30.26s**。本轮新增 41 个测试实例，覆盖规则、搜索、补充指令、MCP 目录、hooks、回退及工作流；旧测试同步去除了通用 Amiya 假设，并在 Skill 加载后显式经过新上下文边界。变更 Python 文件执行 Ruff，另执行 `git diff --check`；最终命令结果保存在 `experiments/optimization-checks-2026-10-16.json`。

| 阶段 | 主要自动化证据 |
| --- | --- |
| 规则 | 根/子目录新规则阻止旧写入；压缩保留、超预算明确失败；strict/light 恢复重读来源 |
| 探索/搜索 | 只读工具集；丢弃模型伪造 evidence；中文路径、嵌套忽略、工作区外链接、正则降级和截断 |
| 补充指令 | 模型请求后作废旧工具；实际同步 codeAgent 批次保留已完成写入；审批撤销/迟到批准；生产消费线程竞态；消息持久化失败、去重、会话重载 |
| MCP | 实际 stdio 发现/调用、下一请求动态绑定、Skill 延迟校验、无匹配不启动、权限不扩大、显式刷新及配置失效 |
| hooks | 前置阻止无副作用、静态拒绝优先、重试前后各一次、追加上下文、Python 超时/异常/无效返回、工作进程回收 |
| 文件回退 | CRLF 原字节、权限、连续修改、新建删除、用户后续修改冲突、活动工作区锁、配额回收、日志收尾失败及恢复后重新验证 |
| 流程/模型 | full/light/auto 实际图执行、升级一次保留已完成编辑、TUI 输入/CLI 选项、角色模型输出预留、token 预算停止并保存原因 |

原有取消、审批、MCP 故障、大量 stderr、工具输出裁剪、自动/手动压缩、断线订阅、后台任务隔离等测试也包含在全量回归中。自动化使用本地服务、临时目录和可控模型，不依赖网络。

测试入口：`tests/test_project_optimization.py`、`tests/test_optimization_integration.py`、`tests/test_optimization_workflows.py`；原有故障回归仍在 `tests/test_mcp_streaming.py`、`tests/test_run_cancellation.py` 等文件中。

## 每阶段可复现实验

```bash
uv run python examples/optimization_demo.py --scenario rules
uv run python examples/optimization_demo.py --scenario search
uv run python examples/optimization_demo.py --scenario inbox
uv run python examples/optimization_demo.py --scenario steering
uv run python examples/optimization_demo.py --scenario mcp
uv run python examples/optimization_demo.py --scenario hooks
uv run python examples/optimization_demo.py --scenario rewind
uv run python examples/optimization_demo.py --scenario workflows
uv run python examples/optimization_demo.py --scenario cross_module
# 不指定场景会运行全部，输出 JSON 可留作后续固定基准
uv run python examples/optimization_demo.py --output /tmp/mokio-optimization.json
```

| 场景 | 预期与实际结果 |
| --- | --- |
| rules | 根规则 ROOT_OK、子目录 DIR_OK 同时保留；流式文本等于聚合结果；通过 |
| search | 300 个无关文件和被忽略目录中，只返回中文文件的函数位置；两后端结果一致；通过 |
| inbox | 重复 message_id 不重复应用，完成 TODO 保留；通过 |
| steering | 首次流开始时提交“不得创建文件”，原写调用作废，第二次只回复“已收到”；通过 |
| mcp | 初始零个 MCP schema；只发现 echo，调用返回 echo: hello；通过 |
| hooks | 生成目录写入被阻止，目标文件不存在；通过 |
| rewind | 预览后应用，`before\r\n` 逐字节恢复；通过 |
| workflows | 三模式把单文件 before 改为 after 并读取验证；通过 |
| cross_module | full/auto 使用完整流程，显式 light 升级一次完成两个文件；结果见独立 JSON |

本地综合报告：[optimization-local](experiments/optimization-local-2026-10-16.json)。双文件报告：[local-cross-module](experiments/optimization-local-cross-module-2026-10-16.json)。

### 搜索与工具目录数据

小文件搜索一次观测：rg **34.749 ms**、Python **16.373 ms**。该样本显示进程启动成本，结果一致不代表 rg 在小样本更快，也未证明大型仓库的普遍提速。

配置 echo、notes、metrics 三个本地 MCP 服务：按需模式只启动 echo，eager 比较分支启动全部三个；初始全工具 schema 序列化估算从 **4755 字节降到 3928 字节**（约 17.4%），按需模式初始 MCP 工具数为 0。这是 schema 字节量，不是供应商账单 token；收益取决于实际服务工具数和 schema 大小。

离线单文件固定模型：full 10 次请求、light/auto 各 6 次；完整图与短路径实际写入并独立读取相同产物。用量为保守 UTF-8 估算，分别 42773、26313、26313，不应当作真实 token。保留完整流程默认值，避免以单一简单任务替代质量评估。

## 真实模型观察

本机配置模型为 **deepseek-chat**。为控制实验输出预算，工作流命令明确设置 `MOKIO_MODEL_CONTEXT_RESERVE_TOKENS=1024`，没有修改项目默认值。以下为实际执行，不是自动化 mock：

```bash
uv run python examples/optimization_demo.py --scenario rules --real-model --output /tmp/real-rules.json
uv run python examples/optimization_demo.py --scenario steering --real-model --output /tmp/real-steering.json
MOKIO_MODEL_CONTEXT_RESERVE_TOKENS=1024 uv run python examples/optimization_demo.py --scenario workflows --real-model --output /tmp/real-workflows.json
MOKIO_MODEL_CONTEXT_RESERVE_TOKENS=1024 uv run python examples/optimization_demo.py --scenario cross_module --real-model --output /tmp/real-cross-module.json
```

- 规则实验：实际回复为中文，包含 ROOT_OK、DIR_OK；流式文本逐字等于同一次请求聚合结果。1 次请求，200 输入 + 26 输出 = **226 token**，场景约 1.04 秒。[原始记录](experiments/optimization-real-rules-2026-10-16.json)
- 补充指令实验：生成开始时提交“只回复已收到，不得创建文件或调用工具”。最终回复“已收到”，原写文件调用标记 not_executed，磁盘没有目标文件。2 次请求，585 输入 + 59 输出 = **644 token**，约 1.47 秒。[原始记录](experiments/optimization-real-steering-2026-10-16.json)

单文件工作流观测：

| 模式 | 实际选择 | 请求数 | 供应商报告总 token | 端到端耗时 | 产物检查 |
| --- | --- | ---: | ---: | ---: | --- |
| full | full | 25 | 88220 | 42.11 s | after |
| light | light | 9 | 17230 | 10.75 s | after |
| auto | light | 10 | 24190 | 11.56 s | after |

full 数据来自 [首次三模式实验](experiments/optimization-real-workflows-2026-10-16.json)，light/auto 数据来自收窄轻量工具集后的 [复测](experiments/optimization-real-light-2026-10-16.json)。这是开发过程中的单次样本，不是严格配对性能统计；网络、模型决策轮数、验收轮数均会影响结果，不据此改默认流程。三个结果都独立检查文件内容。

双文件真实实验中 full 使用完整流程、light 升级一次、auto 直接选择完整流程。三种结果均完成两文件替换，各文件只有一条变更记录。full 用 34 次请求/134910 token，light（含升级）24 次/83012 token。首次 auto 的脚本检查失败，因为写入内容为 `after\n`，任务并未要求末尾换行逐字节一致；保留 [原报告](experiments/optimization-real-cross-module-2026-10-16.json) 和对原始文件的 [独立核查](experiments/optimization-real-cross-module-audit-2026-10-16.json)。修正实验判定以匹配任务要求后，[auto 真实复测](experiments/optimization-real-cross-module-retest-2026-10-16.json) 通过，26 次请求、82951 token、39.68 秒，两个文件内容均为 `after`。新报告同时保存实际内容，避免把宽松比较误当逐字节验证。

## 实验发现的问题与修复依据

1. 首次真实补充指令实验失败：消费线程收到流事件就清空 inbox，生产线程结束请求后看不到消息，继续旧调用。保留 [失败记录](experiments/optimization-steering-race-before-fix-2026-10-16.json)，修改为生产线程安全边界交接，并加入带延迟 astream 的回归测试；复测明确得到 1 个未执行旧调用。
2. 最初轻量流程仍给模型展示 Bash，简单文件检查也请求 Shell 并升级，导致 23/24 次请求。保留首次报告，随后减少轻量实现阶段的可见工具并增加文件读取验证指令；复测为 9/10 次且未升级。需要真正跨文件时仍升级。
3. 旧 Skill 日志测试连发工具而不读取新 Skill 指令。新语义要求刷新上下文，已在测试中加入明确边界，仍验证 Skill 正文不写入事件或 trace 摘要。
4. 真实仓库探索中发现 `**/*.txt` 漏掉根目录文件。FileGlob 与 Grep 统一使用支持零级目录的递归 glob 匹配，并加入根目录/嵌套目录回归用例。
5. 配置优先级补测发现 CLI 未指定的默认值会覆盖项目 TOML。改为传递未指定状态，由运行时解析优先级，显式参数仍优先。

6. 全量回归曾触发一个旧 TUI 测试的调度偶发失败：固定等 50 ms 后假设后台线程已经启动。改为等待实际线程握手，仍验证 Ctrl+Q 不取消 daemon 任务；保留 [失败检查记录](experiments/optimization-checks-before-wait-fix-2026-10-16.json)。

## 排查方式与当前边界

- 新要求一直 queued：检查当前同步调用是否仍在执行；普通同步工具按现有超时收尾。查看 `user_message_received`、`user_message_applied`、`run_redirect` 和 inbox 文件，重复提交使用同一 message_id。
- 旧审批消失：若有 approvals_superseded，说明新指令已撤销旧请求；迟到批准不能启动旧调用。
- 工具消失：检查 Agent、Skill 允许范围及 ToolSearchTool 的候选结果；schema 加载不会自动提升权限，MCP 仍需审批。
- hook 阻止：按 tool_call_id 查 hook_result；前置异常/超时阻止，后置异常保留工具原结果。Python hook 必须在执行任务的进程中注册。
- rewind 冲突：当前内容与该次编辑结果哈希不一致，保持文件不变；自行比较 diff。活动任务必须先结束，Bash/MCP 的外部副作用不属于回退覆盖。
- 预算停止：run_finished 的 stop_reason 为 token_budget_exhausted；查看 checkpoint 和 model_metrics，调整预算后恢复。UTF-8 估算会保守高估，含估算的聚合明确标为 estimated。

真实验证没有覆盖所有供应商或任意大仓库；本轮 UI 自动化使用 Textual headless pilot 和 Rich 捕获，真实模型实验通过 Python 事件流观测，不将它表述为人工操作真实终端。后台进程、外部 MCP/Shell 副作用、可信 Python 回调的操作系统隔离仍沿用现有边界。
