# 真实代码任务评测

`internship-v1` 固定 12 个任务：6 个源码缺陷注入、2 个增量需求，以及 SWE-bench Lite 中的 3 个 Flask / 1 个 Requests 案例。
源头、基础提交、完整目标和固定离线回归清单见 `src/mokioclaw/evaluation/catalog.py`、`docs/experiments/internship-v1/tasks/`。
数据集版本为 `6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2`；外部源码归档、参考补丁、测试补丁均校验 SHA256。
外部任务保留仓库 LICENSE，分发的补丁许可副本位于 `evaluation/assets/licenses/`，不变更本项目许可证。

## 准备与离线评分

```sh
uv sync --frozen
docker build -t mokioclaw-execution:0.2.0 docker/execution
make eval-images
uv run mokioclaw eval list
uv run mokioclaw eval prepare --suite internship-v1
```

准备允许下载源码、镜像和依赖，不调用模型。旧仓库分别使用 Python 3.11 / 3.9 兼容镜像，主程序保持 Python 3.13。
镜像基底使用固定摘要，依赖精确版本写入 requirements；执行和评分时 network none。
默认缓存为 `~/.cache/mokioclaw/eval/internship-v1`，用 `--cache` 可指定其他目录。
缓存包括不可变 base、可信测试、参考补丁及验证收据；缓存完整性失败必须重新准备，不能继续使用。
打包的基础源码来自固定 `6e88c21`，wheel 用户不需要完整 Git 历史。

Agent 工作区是全新源码副本，不含 `.git`、评分器、参考补丁、隐藏测试、历史实验报告或用户配置。
可见基础测试保留；评分时从缓存重建可信基础/隐藏测试，仅接收允许范围内的源代码改动。
测试、依赖或 pytest 配置修改会报告越界，不能用作评分依据；过程文件 TODO/NOTEPAD/HISTORY_SUMMARY 允许生成。
目标与回归分两次独立 pytest 运行，空 XML、跳过测试和测试过程中篡改可信测试都不能判为成功。

12 个任务已在真实容器中验证：缺陷版目标失败、固定回归均通过；参考修复目标与回归均通过。
原始 JUnit XML 和执行结果见 `docs/experiments/internship-v1/validation/`。
只改测试、假成功文本和空报告的防伪回归分别由 `make check-docker` 与普通单元测试覆盖。
评分器不是针对恶意 Python 代码的完整防篡改证明；容器不挂载凭证和宿主评分缓存。

## 56 次固定主实验

```sh
# 只验证离线框架契约，real_model=false，不产生能力分数
uv run mokioclaw eval run --profile core
# 仅下面的显式命令才会请求真实模型
uv run mokioclaw eval run --profile core --real-model
uv run mokioclaw eval run --profile coverage --real-model
uv run mokioclaw eval report internship-v1-20261016
```

核心 M02/M03/M07/E02 × baseline/full/light/auto × 3 = 48；其余 8 个任务只运行 full，共 56。
baseline 直接使用 codeAgent 的现有工具循环，没有 router/planner/其他 Subagent/模型 verifier；外部验收对四种架构相同。
产品默认 full、默认 local 不变。评测中各架构的执行 Agent 使用相同文件/搜索/Bash/Skill 能力；light 仍在跨文件修改或验收失败时升级一次。
为保证资源可比，评测 light 允许同一 Docker Bash，不沿用日常 light 禁止 Shell 的产品限制。
网页、MCP、自定义 hooks、持久授权、后台执行关闭；临时 HOME 隔离个人规则及配置。

所有角色使用同一现有兼容模型、temperature 0、32K 上下文、4096 输出。
每次总 token 预算 200K、请求 40、工具 80、生成 900 秒、单 Bash 60 秒、验收 120 秒。
token 是请求前估算预留加 usage 回填，不是精确费用硬上限。usage 缺失标估算，缓存未知值保留 null。
SDK 自动请求重试在评测中关闭；截断后的唯一重生成仍计量。
种子 20261016 固定顺序，跨进程租约保证单并发。每次使用独立源码/会话/进程。
主实验开始前冻结源码、HEAD、uv.lock、提示词、模型配置摘要及任务清单；每次新 trial 前再核对源码和任务，变化时拒绝启动下一项。

每个 trial 在请求前写入收据。恢复只运行没有收据的项目；中途遗留 running 先通过 PID、独有 nonce、spec 路径和进程组核对归属，清理专属 worker/容器后改记 interrupted，保留已有用量，不重跑。worker 监视父进程，父进程退出会发出取消并清理自己的容器。
失败、超时和基础设施问题保留原记录。额外复测应使用独立 experiment-id，不能替换主实验结果。
终态、独立验收、模型自评和安全断言各自记录；失败/安全停止不会作为完成任务计入解决数，即使部分产物已通过测试。
`task_solved` 需要正常完成和独立验收同时通过。

报告按相同核心任务比较四种架构，coverage 的八次 full 另列。三次重复提供 min/median/max，不用单次最快值替代。
报告保留未开始、基础设施失败、预算停止、越界、回归失败及模型自评差异，分母不删失败。
公开 Issue 可能在训练数据中，不能据此声称未见任务泛化；也不能声称官方 SWE-bench 成绩。

## 故障定位

- doctor 失败：先确认引擎和已准备镜像；不会回落到 local。
- reference validation 失败：属于任务/依赖基础设施问题，不能替换任务；读取对应 XML 和 execution.json。
- 任务失败但 run finished：查看独立测试，不接受自然语言“成功”。
- request/tool/token budget：不增加请求，保留 checkpoint、补丁和部分 trace。
- wall timeout：先向专属 worker 发送取消，再清理 owner 标签的容器，必要时强制结束该进程组。
- 中断恢复：重跑同一命令仅继续未开始项；已失败记录不会被覆盖。

## 对照有效性与首批诊断

每批可附 `validity.json`，与冻结 manifest 和 trial 收据分开；默认 unreviewed，只有确认条件一致后才标 valid。
invalid 批次仍可查询所有失败、用量和产物，但生成报告明确禁止用它比较架构优劣。

`internship-v1-20261016` 已执行 56/56。同步 Subagent 曾将运行级 model_profile 清空，导致 SDK 重试设置未继承，整批对照标 invalid。
真实观察是 0 次正常结束并独立验收通过、8 次仅产物通过、33 次请求预算停止、13 次上下文预算错误、10 次连接故障。
原始收据和报告保留，不能把 8 份部分产物当成完成交付，也不能据此判断多 Agent 优劣。
详见 [首批诊断](../experiments/internship-v1/main/analysis.md)。

修复采用默认继承运行配置、显式角色配置覆盖、退出后恢复；回归经过真实模型工厂/工具绑定/流式适配，使用本地固定响应，无联网请求。
恢复旧实现时四种架构回归均失败，证明测试能捕获此次缺陷。新增 llm_request 记录不含凭证的实际模型预算和重试配置。
经用户另行授权，新一批使用独立 ID `internship-v1-20261017`，保持任务/模型/预算/顺序，不覆盖原记录。

新批次现已完成 56/56，完整交付 0/56，部分产物通过 8/56。
canonical trace 的 1,903 次请求配置和计数一致；14 次升级路径的外层事件缺口及重复结束单列为事件验收失败。
配置对照有效性不代表所有事件要求通过，核对方法和原始失败保留在 [结果报告](../experiments/internship-v1/RESULTS.md)。
事件转发修复在实验结束后进行，不改写旧数据，不自动追加真实实验。
