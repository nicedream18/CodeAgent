# 本轮验收与实验记录

日期：2026-10-16。实现工作区：`/Users/nicedream/xm/MokioAgent`。起始 HEAD 为 `a41b0536dc36d9e4b59e457327d49605ed7a3bcb`，工作区包含上一轮尚未提交的七阶段改动；开始前记录 SHA256 清单，未重置或覆盖这些成果。

## 自动检查

起始实际基线 **323 passed / 31.49s**。本轮完整检查 **366 passed / 33.70s**；冻结依赖安装、Ruff、9 个模块的严格 mypy、协议文档一致性和 `git diff --check` 均通过。小屏审批布局及最后的事件字段补充又运行 TUI/扩展相关测试，**31 passed / 3.93s**。检查原始输出见 [checks](experiments/reliability-checks-2026-10-16.json)。

统一复现：

```sh
make check
uv run python scripts/gen_protocol_doc.py --check
uv run python examples/reliability_demo.py --output /tmp/reliability-local.json
uv run python examples/reliability_demo.py --real-model --workers 3 --output /tmp/reliability-real.json
```

最后一项需要已有 `.env` 中的兼容模型配置，普通 CI 不执行它。真实及离线文件操作均在新临时工作区运行。真实实验报告内保留实验开始时源码/依赖指纹；末尾的异常 chunk、事件校验和 UI 防护以最终全量及相关回归核对。

| 阶段 | 固定回归/实验 | 观察结果 |
| --- | --- | --- |
| 生成可靠性 | test_generation_reliability；local/real truncation | 完整调用后半个调用、纯文本截断整批不执行；最多一次重生成；取消/纠正/预算阻止重试；二次截断保存 failed checkpoint；同步子 Agent 错误穿过工具执行器；空流和不可聚合流受控停止 |
| 类型化协议 | test_protocol_contracts；test_daemon | 缺任务、错误类型、未知字段/方法及错误版本均在副作用前拒绝；旧选项/事件仍可读；不支持 Skill 的旧 daemon 明确报错；日志不破坏非法请求错误码 |
| 职责拆分 | test_graph/runtime/optimization_workflows/integration/run_cancellation | 默认完整流程、轻量升级、TODO、请求轮次、后台隔离、严格恢复与同步纠正保持；共享批次能关闭 planner 外层未执行工具组 |
| 分层 trace | test_trace；test_reliability_extensions | 部分中文、stream/tool/run 关联、只读 CLI、增量游标、旧日志、轮转、密钥字段脱敏、payload 配额及日志故障隔离通过 |
| 输入/Skill | Textual pilot；real explicit_skill  | 多行粘贴不提交，Shift+Enter 换行，Tab/Esc 补全；显式 Skill 正文实际进入 codeAgent，不能被路由或后续 Skill 替换；planner 范围恢复 |
| 声明式角色 | test_reliability_extensions；examples/agents | 项目覆盖用户，版本固定；重名/非法 base/越权/无效配置诊断；受限 runner 收到提示和预算，退出后恢复作用域；原 Python 注册兼容 |
| 审批记忆 | test_approval_memory；real approval | 项目重载、会话失效、目录边界、MCP 指纹、拒绝优先、并发保存、上下文变化、撤销/迟到响应均受控；实际两次相同资源调用只审批一次 |
| 缓存与评测 | usage 参数化测试；7 类任务 × 3 模式 × 3 次 | 缓存计量作为 input 子集，缺失显示 unknown，失败与重生成分开计量；不输出未经价格配置的费用 |

## 离线与真实结果

[交付离线报告](experiments/reliability-local-delivery-2026-10-16.json)：**63/63 预期用例通过**。

[交付真实报告](experiments/reliability-real-delivery-2026-10-16.json)：模型 `deepseek-chat`，**63/63 预期用例通过**。其中 MCP 故障使用真实本地 stdio 服务，不要求模型参与；模型/审批/Skill/纠正使用已配置的兼容接口。

“预期用例通过”包含正确拒绝和安全停止，不代表每个任务都已完成。64 token 截断实验共 9 次：**6 次第二段生成正常结束，3 次再次 length 后 failed 停止**，全部只有两次请求且没有旧工具副作用。正常结束的短回答解释了分批完成策略，并未生成最初要求的 2000 项内容；这是响应恢复与停止策略实验，不能将它计为 2000 项任务成功。单文件和跨模块任务则都实际读取产物并核对内容。

只有以下两类任务比较真实工作流路径，其余边界实验的模式标签用于重复分组：

| 任务 | 模式 | 实际产物成功 | 请求数中位数 | 耗时中位数 | 耗时范围 |
| --- | --- | --- | --- | --- | --- |
| 单文件 | full | 3/3 | 30 | 47.93 s | 44.25–48.02 s |
| 单文件 | light | 3/3 | 11 | 11.56 s | 10.20–12.42 s |
| 单文件 | auto | 3/3 | 10 | 10.56 s | 10.49–10.99 s |
| 跨模块 | full | 3/3 | 25 | 37.44 s | 33.51–37.97 s |
| 跨模块 | light | 3/3 | 32 | 43.56 s | 39.54–47.32 s |
| 跨模块 | auto | 3/3 | 25 | 37.43 s | 34.11–41.20 s |

本组小任务中 light/auto 减少了单文件请求；跨模块的 light 需要升级，额外请求和耗时也如实保留。样本只有每组三次，真实请求并发及供应商缓存/网络会影响时间分布，继续保留 full 默认值。

真实接口返回了缓存读取字段。例如一次 full 单文件运行：input=76874，output=6105，total=82979，cache_read=62720，cache_creation=null。缓存读已包含在 input 中，total 没有再次增加 62720；cache_creation 显示 unknown。每条原始用量来源和估算标记均在报告/trace 中，没有统一价格配置，未计算费用。

## 故障记录与修复

- 第一轮完整检查暴露了 facade 多行导入被 Ruff 当作未使用项移除的问题，导致拆出的模块缺依赖。修复为显式保留 facade 依赖后，既有入口与替换测试恢复；保留兼容 facade 的目的写在文件头。
- daemon 曾先发布状态再追加终态事件，订阅可能少收到最后一条事件。现将状态切换与 daemon_run_finished 追加放在同一个 condition 边界，终态订阅稳定。
- 新回归发现显式 Skill 只收窄工具却未注入正文，以及旧 trace 包装使 layer 过滤不生效，均已修复并固定测试。
- 共享循环初版跨生成器持有批次 context，调用者抛出的纠正异常可能无法到达批次。改为在执行者作用域进入共享 batch，补齐外层调用结果；生成终止异常也显式穿过工具执行器。
- [最初离线](experiments/reliability-local-2026-10-16.json) 与 [最初真实](experiments/reliability-real-2026-10-16.json) 各有 9 个审批用例不满足实验预期：安全 printf 按现有策略不触发审批，无法测试记忆。实验改用明确 ask 的 FileRead，保留旧失败记录；[离线复测](experiments/reliability-local-approval-retest-2026-10-16.json)、[真实复测](experiments/reliability-real-approval-retest-2026-10-16.json) 都为 9/9。未为获得通过而改变安全 Bash 的既有审批规则。
- 早期 64 token 实验 9 次均二次截断；加强“只生成一个小步骤”的重生成提示后，最终观察为 6 次正常结束、3 次受控停止。极小输出预算仍不能保证交付完整任务。

## 中文提交说明

工作区原有未提交修改继续保留，未自动把混合工作区提交到 Git。以下说明可作为八组聚焦提交的标题，评审时结合开始前清单区分上一轮内容：

1. `fix(provider): 阻止截断响应执行工具并统一生成错误收尾`
2. `feat(protocol): 类型化 daemon 协议并统一开发检查`
3. `refactor(runtime): 拆分图节点并复用模型工具循环`
4. `feat(trace): 关联 IPC 与模型事件并提供只读查询`
5. `feat(tui): 支持多行输入与显式 Skill 选择`
6. `feat(agents): 支持受限 TOML 角色配置`
7. `feat(permissions): 增加会话和项目审批记忆`
8. `feat(metrics): 统计缓存用量并加入固定任务评测`

设计边界见 [设计说明](reliability-design.md)，命令和排障流程见 [故障排查](reliability-troubleshooting.md)，协议字段以 [生成文档](WIRE_PROTOCOL.md) 为准。
