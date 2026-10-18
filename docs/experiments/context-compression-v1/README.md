# Context Compression A/B Benchmark

目标：在同一冻结版本、模型和预算下，比较**开启与关闭上下文压缩**的实际模型 token、完整交付率与生成延迟。用户已选择并授权 18 次真实试验，见 [authorization.json](authorization.json)。

**18/18 次真实试验已归档**，详见 [结果与限制](RESULTS.md)。开启组 / 关闭组实际总 Token 为 1,395,523 / 1,409,386，完整交付为 3/9 / 4/9，生成延迟中位数为 68.344 / 64.569 秒。开启组实际压缩 0 次；本批未形成实际压缩处理差异，不能将这些差异归因于压缩。实现回归通过与真实效果结论分开记录。

## 运行方式

从源码 checkout 根目录执行。普通预览不读模型凭证，不启动容器或请求模型：

```sh
uv run mokioclaw eval compression run my-compression-ab
```

首次准备允许使用已经构建的依赖镜像；任务验证在无外网的真实 Docker 中进行，不使用模型。需要 Docker 引擎与 `mokioclaw-execution:0.2.0` 镜像：

```sh
uv run mokioclaw eval compression prepare
```

先提交所有代码，再使用自己的 `MODEL`、`BASE_URL`、`API_KEY` 配置执行固定矩阵：

```sh
uv run mokioclaw eval compression run my-compression-ab --real-model
uv run mokioclaw eval compression report my-compression-ab
```

`--repeats 1` 是 6 次真实冒烟，默认 `--repeats 3` 为 18 次；真实执行始终要求 `--real-model`。额外试验使用独立实验 ID；同 ID 恢复只继续未开始项，不重跑已结束或已开始但缺终态的项。

默认缓存是 `~/.cache/mokioclaw/eval/context-compression-v1`，独立于旧实验。prepare/run/report 均可用 `--cache` 指定。机器报告在 `benchmarks/EXPERIMENT_ID/report.json`，可读表格在同目录的 `report.md`；每组的原始 trial 在 `experiments/EXPERIMENT_ID-on` 或 `-off`。

## 冻结的比较条件

| 项目 | 两组共同条件 |
|---|---|
| 任务 | D01：FIFO 单文件短任务；M07-v2：字面量搜索；M08-v2：trace 时间过滤。后两项在冻结任务副本上修改，不声称当前主程序已有这些功能 |
| 重复与顺序 | 每题每组 3 次，固定种子 20261018；随机任务/重复顺序，每对交错开关。每题先执行 on/off 的次数为 2:1 或 1:2，避免所有长任务都先跑同一条件 |
| 流程与模型 | full；所有角色同一模型、temperature=0、SDK max_retries=0；逐流核对实际模型、窗口、输出预算及重试配置 |
| 上下文 | 32,768 窗口、4,096 输出预留、1,024 安全量；输入硬限制 27,648，水位比 0.85、目标比 0.60 |
| 总预算 | 每 trial 200K token 估算预留上限、40 请求、80 工具、900 秒生成；Bash 60 秒、外部评分 120 秒；摘要与重生成请求占用同一预算 |
| 工具与隔离 | 相同文件/搜索/Bash/Skill 范围；Bash 和评分使用固定镜像的无外网容器；不启用网页、MCP、自定义 hooks、持久审批和后台任务 |
| 状态 | 每次新进程、工作区、HOME、会话；固定源码/提示词/锁/任务/评分指纹；模型凭证仅通过宿主环境传入 |

唯一处理差异为 `context_compression_enabled=true/false`：同时控制模型请求前压缩和图水位压缩。关闭时仍保留局部工具结果裁剪、权限、取消、输出预留和输入硬限制；超过窗口会安全停止，不允许给关闭组无限窗口或静默丢历史。显式 compact 在关闭状态返回受控拒绝。

产品默认仍为开启；可通过 `[runtime] context_compression_enabled = false` 或 `MOKIO_CONTEXT_COMPRESSION_ENABLED=false` 配置，环境变量接受 true/false/1/0。旧配置与旧评测文件省略此字段时按开启解释。

## 指标口径

- **实际用量**：canonical trace 按 stream_id 聚合输入、输出和总 token，与运行收据交叉核对。API 返回量与估算请求分开显示；usage 缺失不伪装为零消耗。摘要的所有请求、失败和重生成均计入总量。
- **摘要开销**：由 `llm_request.node=context_compressor` 关联 usage，不能只按角色名统计，因为同步子任务可能仍持有父角色名。压缩事件按 event_id 去重，单列开启但未实际压缩的 trial。
- **缓存**：读取包含在输入量中，不重复累加；创建量未知保持未知。远端前缀缓存无法被本地完全控制，交错顺序和原始缓存字段帮助解释影响，不等同于消除了缓存干扰。
- **完整交付**：运行 `finished` 且可信独立评分通过；仅产物通过另列。失败、预算停止、越界修改及基础设施失败保留固定分母。未完成矩阵的完成率标为未知，不拿部分 trial 当完整结果。
- **延迟**：生成秒包括路由、探索、实现、摘要、验收和正常收尾；外部评分耗时另列。提供每题/每组 min、median、max，以及配对差值；成功任务耗时独立统计。失败更早停止不代表完成任务更快。
- **核对**：请求/usage/结束事件、模型配置或实际开关不一致时标计量无效，停止新 trial，保留原件。预算停止后的候选仍进入独立评分。

不输出未经价格配置支持的费用结论。三个固定任务的小样本不提供统计显著性、架构优越性或官方 SWE-bench 分数。D01 是短任务控制；若未触发压缩，它只能说明该任务下两组没有实际压缩处理差异。

## 代码与离线回归

[开关](../../../src/mokioclaw/core/context_compaction.py)、[图节点](../../../src/mokioclaw/graph/steps/compression.py)、[协调器](../../../src/mokioclaw/evaluation/compression_benchmark.py)、[计量](../../../src/mokioclaw/evaluation/compression_metrics.py)、[测试](../../../tests/test_compression_benchmark.py)。

```sh
uv run pytest -q tests/test_compression_benchmark.py tests/test_context_governance.py
make check
```

测试中的 usage 是可控 fixture，明确不计入真实效果；真实试验的原始记录、版本、结果表和限制见 [RESULTS.md](RESULTS.md)。[归档复核](verify_archive.py)与[压力分析](analyze_pressure.py)只读已有 trace，不调用模型或工具。旧 Goal 1～4 和正式/无效批次不被覆盖。
