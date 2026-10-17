# 真实模型完整交付演示

已完成一个小型受控代码任务：**真实模型探索 → 规划 → 修改 → verifier 检查 → 正常汇报 → 独立 Docker 验收**。
使用实际归档生成的 150 秒讲解回放，不是新一次实时请求；原生成耗时 36.845 秒。

```sh
# 不调用模型、不执行工具，按时间轴播放
uv run python scripts/replay_internship_demo.py docs/internship/success-demo.cast
# 立即查看全部帧
uv run python scripts/replay_internship_demo.py docs/internship/success-demo.cast --no-wait
```

## 任务和真实结果

D01 修复一个六行 FIFO 队列函数：最多取 limit 项、零不消费、负数抛错且不改列表，保留 FIFO 与正常排空。
它是独立微型演示，**不属于原来的 12 任务，也不替换正式 0/56 的完整交付成绩**。

| 项目 | 实际记录 |
|---|---|
| 实验 / trial | `completion-20261017-d01-v1` / `D01-full-1` |
| 冻结版本 | `fd4f29f7b012d0b5bf13714b813f7fdb05ec9b12` |
| 模型 / 流程 | deepseek-chat / full，所有角色相同模型，temperature=0 |
| 路径 | router → repoAgent → planner → codeAgent → verifier → final |
| 请求 / 工具执行 | 19 / 18；19 条流的起止与配置核对通过 |
| 用量 | 输入 83,967，输出 5,795，总计 89,762 token（API reported） |
| 缓存 | 读取 16,256，创建量未知；读取量已包含在输入中，不重复相加 |
| 生成 / 独立评分耗时 | 36.845 / 1.367 秒 |
| 终态 / 主判定 | `finished` / `task_solved=true`，模型自评与独立结果一致 |
| 可信评分 | 3 项隐藏目标 + 2 项基础回归，5/5 通过；无越界和测试篡改 |
| 第二次重建评分 | 同一镜像、同样 5/5 通过，不再请求模型 |

代码改动由模型生成，采用切片和删除队首，并在修改列表前校验负数。
没有向工作区提供参考补丁或隐藏测试，也没有由维护者补写候选源码。
过程仍出现一次 `Bash` 未知工具名和一次 verifier 命令非零退出，模型读取错误后继续完成；原记录完整保留，不宣称全过程无错误。

## 两分半讲解顺序

| 时间 | 展示内容 |
|---|---|
| 0～32 秒 | 说明受控任务范围、真实模型和冻结版本，阅读需求 |
| 32～52 秒 | 查看探索证据和修改范围，说明隐藏测试不在 Agent 工作区 |
| 52～90 秒 | 解释实际 diff：负数前置校验，切片取出至多 limit 项 |
| 90～112 秒 | 独立容器重建可信测试，展示 3+2 项结果 |
| 112～132 秒 | 查看用量、run_id、finished 和 checkpoint 中的最终汇报摘要 |
| 132～150 秒 | 说明单例边界，保留复杂任务失败，不推断多 Agent 优势 |

完整证据：[收据](../experiments/completion/d01/trials/D01-full-1/result.json)、[实际 diff](../experiments/completion/d01/trials/D01-full-1/candidate.patch)、[请求审计](../experiments/completion/d01/audit.json)、[任务有效性](../experiments/completion/d01/task-validation.json)、[第二次评分](../experiments/completion/checks/d01-reproduction.json)。
`trace.tar.gz` 保存探索、工具、模型部分输出及最终 checkpoint；可以通过 run_id、stream_id、tool_call_id 关联。

## 复现

先启动 Docker 并构建 `docker/execution` 镜像。已有归档可不调用模型重新验收：

```sh
uv run python scripts/reproduce_internship_trial.py D01-full-1 --archive docs/experiments/completion/d01
```

重建相同演示回放：

```sh
uv run python scripts/build_internship_demo.py D01-full-1 --archive docs/experiments/completion/d01 --output /tmp/mokio-success-demo.cast
```

自己尝试真实生成时，配置 `.env.example` 中的兼容模型信息，使用新的实验 ID：

```sh
# 不加 --real-model 只预览，既不请求模型也不启动容器
uv run python scripts/validate_completion.py --task D01
uv run python scripts/validate_completion.py --task D01 --architecture full --experiment-id my-completion-demo --real-model
```

新实验仍可能失败或预算停止；已有收据不会被自动重跑。默认 200K 运行 token、40 请求、80 工具、15 分钟；首次准备先验证缺陷版失败、参考补丁通过。
这些脚本属于源码 checkout 的实验工具，wheel 的安装和 CLI 独立检查另见[检查证据](../experiments/completion/checks/)。

## 保留的失败案例

原 `docs/internship/demo.cast` 继续作为正式批次 E02-baseline-1 的“产物通过、运行失败”诊断回放，可用同一播放器查看。
本轮成功之前的四次定向失败也全部归档，见[修复记录](../experiments/completion/README.md)。
离线脚本化演示 `examples/internship_offline.py` 仍可运行，但不计作真实模型能力证据。
