# Goal 2：固定 18 次真实任务验证

用户已在本 Goal 授权 18 次固定试验，授权收据见 authorization.json。
Goal 1 的原始输入与当时未授权声明保持原样。历史 56 次成绩不并入本批。

## 运行边界

任务、预算、顺序逐条来自 ../job-readiness-v1/goal2-input.json。
B0/B1 各 9 次，独立 experiment_id；单并发，已启动 trial 不重跑。
源码在独立 detached checkout；外层脚本只选择版本、校验、调度和写收据。
每次调用冻结版本自己的 run_trial，它再启动该版本原有 worker。
worker-import-contract.json 使用同一 Python、环境构造器、临时 HOME/cwd 验证实际模块解析路径；前后核对源码哈希。
不会将当前版本的实现或新产品逻辑注入 B0。

```sh
uv run python scripts/job_readiness_trials.py                  # 只预览
uv run python scripts/job_readiness_trials.py --preflight      # 离线模型、版本、任务、Docker
uv run python scripts/job_readiness_trials.py --real-model     # 本 Goal 已授权的固定矩阵
```

缓存位于 ~/.cache/mokioclaw/eval/job-readiness-v1；其中 checkouts/B0、B1 为冻结 Git checkout。
从已包含两版 Git 历史的新本地 checkout 恢复调度环境时，可先准备任务，再建立两个独立 checkout：

```sh
uv run python scripts/prepare_job_readiness.py
mkdir -p "$HOME/.cache/mokioclaw/eval/job-readiness-v1/checkouts"
git clone --shared --no-checkout "$PWD" "$HOME/.cache/mokioclaw/eval/job-readiness-v1/checkouts/B0"
git -C "$HOME/.cache/mokioclaw/eval/job-readiness-v1/checkouts/B0" checkout --detach cad8a153584aa5f860982dee33719dc2664730db
git clone --shared --no-checkout "$PWD" "$HOME/.cache/mokioclaw/eval/job-readiness-v1/checkouts/B1"
git -C "$HOME/.cache/mokioclaw/eval/job-readiness-v1/checkouts/B1" checkout --detach ee5f073a7640c15c1d693c7e93bc067e9eb8da51
```

这些命令不请求模型；已有目录时不要覆盖，直接校验现有内容。新的真实复测仍须另行授权。
调度器在独立批次写入 dispatch.json 后，以子进程执行原 run_trial；它在首个请求前写 result.json。
若已有 dispatch 或 running 收据，先展示真实进程命令并停止调度，不推断进程死亡，不复用名额。
基础设施故障会停止启动后续 trial，需核实并记录修复，之后只继续未启动项。
原 worker 保持专属进程、900 秒墙钟、容器 owner 清理；外层观察等待不等于终止。

## 离线证据

offline-preflight.json 记录两个真实 checkout 的源码摘要、导入路径，以及相同固定模型驱动分别运行三种预算场景的结果。
固定模型用量为合成值，不能混入真实能力统计。
tests/test_goal2_driver.py 覆盖固定 18 条顺序、两版选择、预算、失败收据跳过、恢复不重跑、变更版本拒绝及预览无副作用。
冻结任务的缺陷/参考/可见测试证据逐字节校验；原始任务缓存同样核对源码、可信测试和补丁摘要。

## 结果口径

完整交付 = execution_status 为 finished 且独立 grade.solved；其他产物通过单列。
模型自评、内部 verifier、独立验收分别展示，缺失保持未知。
所有已开始失败和基础设施问题保留在固定 18 次分母内。
缓存读取已包含在输入 token 中，不额外累加；未知缓存创建量保持未知。
200K 预算依赖估算预留与 usage 回填，不是精确费用上限。
本地真实 ARM64 容器检查不能证明远程 Linux CI 已运行。

## 实际交付

18/18 终态：B0 完整交付 1/9、B1 0/9，独立产物通过均为 8/9。
427 次请求配置与事件关联审计通过；包含一次保留的连接失败，没有重跑。
本批未显示完整交付改善。唯一成功修改一个源文件，多源文件修改后完整交付仍未达成。

- [逐次结果与三次重复分布](RESULTS.md)，完整结构化数据见 comparison.json。
- [角色开销、停止位置、失败原因与后续建议](ANALYSIS.md)。
- [成功和失败的实际归档回放、重建复验方式](DEMO.md)。
- provenance-audit.json 记录实际导入和顺序；b0/audit.json、b1/audit.json 记录请求与事件审计。
- regrades 保存两个既有产物的独立容器复验，未调用模型、未覆盖首次评分。
- checks 保存最终统一检查及真实 Docker 检查日志。

复现统计不请求模型：

```sh
uv run python scripts/report_job_readiness.py --root docs/experiments/job-readiness-goal2
```

重新启动真实 trial 或扩大矩阵不在本批完成后的授权范围内；额外复测需另行授权。
