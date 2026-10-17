# 150 秒真实产物诊断回放

**当前没有正常结束且独立验收通过的真实 Agent 案例。此回放展示产物通过、运行失败，不能当作端到端成功演示。**
离线脚本化模型的成功演示仍可运行，但明确不计作真实模型能力证据。

```sh
uv run python scripts/replay_internship_demo.py docs/internship/demo.cast
# 快速查看全部内容，不等待时间轴
uv run python scripts/replay_internship_demo.py docs/internship/demo.cast --no-wait
uv run python examples/internship_offline.py
```

回放取自正式批次 E02-baseline-1，使用该次真实候选补丁、可信 JUnit、run_id 和原始失败终态。
需求为 Flask 配置文件支持二进制读取；独立测试验证产物，但原运行达到请求预算，没有正常完成。
播放不调用模型、不执行候选工具；`demo.cast` 是 asciicast v2，也可用兼容播放器打开。

| 时间 | 讲解内容 |
|---|---|
| 0～12 秒 | 说明真实归档回放、原运行失败及证据版本 |
| 12～32 秒 | 需求、固定基础源码与上游来源 |
| 32～52 秒 | 修改文件和 trace 中的仓库证据 |
| 52～90 秒 | 查看实际 diff，解释 `text=False` 与二进制打开方式 |
| 90～112 秒 | 独立容器重建可信测试，读取目标及回归结果 |
| 112～132 秒 | 查看请求预算停止、run_id 和原始 task_solved=false |
| 132～150 秒 | 解释产物、终态和完整交付的区别，不隐藏失败 |

重新构建相同内容：

```sh
uv run python scripts/build_internship_demo.py E02-baseline-1 \
  --archive docs/experiments/internship-v1/corrected --output /tmp/mokio-demo.cast
```

重新验收已有产物，不发起新模型请求：

```sh
uv run python scripts/reproduce_internship_trial.py E02-baseline-1 \
  --archive docs/experiments/internship-v1/corrected
```

完整输入、依赖、评分口径和失败记录见 [结果报告](../experiments/internship-v1/RESULTS.md)。
求职前仍需修复实际交付可靠性并验证成功案例，不能用参考补丁或脚本模型替代这项证据。
