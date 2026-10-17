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

本文件随本批真实观测追加报告链接，不预填成功率。
