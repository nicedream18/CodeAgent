# Goal 1：任务环境、角色诊断与完成预算预留

本轮仅本地代码、离线模型、真实 Docker 和本地提交。真实模型调用为 0，不推送、不发布。
原 internship-v1 及 completion 实验保留；本轮不是新的能力成绩。

## v2 任务的数据流与边界

固定 M02-v2、M07-v2、M08-v2，独立缓存 `~/.cache/mokioclaw/eval/job-readiness-v1`。
从同一份 `baseline.tar.gz` 构造原缺陷/增量需求，保留原参考补丁、可信目标和回归选择。
原准备流程删除整个 examples，部分测试仍引用 echo_server.py。v2 只恢复该非答案 fixture；工作区只公开三个已验证的基础 smoke tests。
可见范围明确为文件读取/搜索、CLI 帮助和本地 stdio：不宣称原完整套件都可在任务容器运行。原宿主 Python shim 及其他无关平台集成测试不作为可见基线；隐藏评分范围在 task.json 冻结。
参考补丁、隐藏测试和报告仅留在评测器侧，不复制到 Agent 工作区。文件范围仍是 `src/mokioclaw/**`，修改可见测试或 fixture 会被拒绝。
执行与评分无外网、无模型凭证，使用已解析的不可变镜像 ID。依赖以基线 uv.lock 和固定执行镜像关联，不在评分时装包。

```sh
uv run python scripts/prepare_job_readiness.py
uv run python scripts/validate_completion.py --task M07-v2
```

第一条会实际启动本地容器进行准备验收，不调用模型；第二条只输出真实试验预览。
`tasks/` 保存公开基础源码压缩包、可信测试压缩包、任务契约、参考补丁及真实验收结果；这些属于评测器材料，不放进候选工作区。
`task-artifacts.json` 保存摘要；`rebuild.json` 证明从新缓存重建三个任务获得相同源码、依赖和完整任务契约。
伪造成功文本及只改测试两个负控制均实际评分，结果在 negative-controls。

## 角色开销诊断

```sh
uv run python scripts/analyze_role_overhead.py PATH_TO_CANONICAL_EVENTS_OR_TRACE_TAR --output /tmp/overhead.json
```

分析器只读取 canonical trace，不重放模型或工具；输入最多 16 MiB，支持已归档 trace.tar.gz。
按 stream ID 去重请求和 usage，保留未知缓存量、估算标记、缺少 usage 的请求；缓存读取不额外计入输入。
角色工具次数定义为 tool_result 观察，含拒绝、失败及未执行，不能等同于精确执行计数。
相同参数、可见结果和上下文版本的重复读取只是诊断线索；缺少参数时记为未测量，不把它当零开销。
最终阶段来自 run_end，停止原因来自预算事件；重复结束事件单独报告。

旧 M02-v4 的 23 次请求中：repoAgent 6 次，planner 4 次，codeAgent 12 次，router 1 次；最后模型请求仍在 codeAgent，没有 verifier 请求。
D01 的 19 次请求含 verifier 3 次。它们是不同任务和版本，不能据此推断机制效果；只用于确定要验证的预算交接边界。

## B0 / B1 与下一阶段

B0 在环境、诊断及统一检查完成后冻结，保持现有 Agent 决策机制。
B1 只引入一项主要机制：有限运行预算下的完成预留与协作阶段交接。离线验证通过后记录两个 Git commit 及源码/锁/提示词摘要。
Goal 2 经另行授权后，再在相同三个任务上执行 B0/B1 × 3 次；本轮不发起这 18 次真实请求。

## 准备过程中的失败

首次新增全量检查与负控制并行执行时，pytest 收集到了报告目录内暂存的可信测试，出现模块收集冲突。
已将 grader 的可执行 staging 移至系统临时目录，报告目录只保存输出，并补充边界测试。
原失败日志保留在 checks；后续统一检查重新运行。该问题属于评测器目录隔离，不计为 Agent 任务失败。

## 本轮交付结果

- B0：`cad8a153584aa5f860982dee33719dc2664730db`；B1：`ee5f073a7640c15c1d693c7e93bc067e9eb8da51`。两者任务、提示词目录和依赖锁摘要相同，运行时主要差异为完成预算预留。
- 额外修正了诊断器对声明式角色名的归属，属于离线报告兼容，不改变模型请求；B0/B1 比较统一使用最新诊断器。
- `make check`：437 passed、2 skipped；Ruff、24 个严格 mypy 模块、协议/评测文档和 diff 检查通过。
- `make check-docker`：真实 linux/aarch64 29 passed。
- 设计、故障处理和限制见 [DESIGN.md](DESIGN.md)。离线前后结果见 offline-controls/comparison.json，所有用量都是固定模型的合成值。
- 下一阶段输入见 goal2-input.json：18 条任务/版本/重复组合，需单独授权真实模型。现有 validate_completion 脚本用于单次预览/验证；重复编号与双版本编排由 Goal 2 的外层驱动调用冻结 checkout 的 run_trial，不修改 B0/B1。

## 复现 B0 / B1 离线边界

从包含本轮实验驱动的项目根目录执行。驱动会输出实际载入的源码路径，便于确认版本。

```sh
# 在独立目录导出冻结源码，不改当前工作区
mkdir -p /tmp/mokio-b0-src
git archive cad8a153584aa5f860982dee33719dc2664730db src | tar -x -C /tmp/mokio-b0-src
# 使用项目 venv 的 Python 和同一驱动，对三个 kind 分别运行
PYTHONPATH=/tmp/mokio-b0-src/src .venv/bin/python examples/completion_budget_demo.py --kind requests
uv run python examples/completion_budget_demo.py --kind requests
```

同样替换为 tokens/tools。若从其他工作目录执行，请将 Python、DRIVER 和 PYTHONPATH 改成绝对路径。
`b0.json`、`b1.json` 保存源码/提示词/锁摘要；task-artifacts.json 保存任务材料摘要；acceptance.json 保存最终逐项审计。
