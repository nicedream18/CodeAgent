# 已归档真实案例回放与复验

两段都是根据真实 trial 制作的 150 秒讲解回放，不是新一次实时生成。未调用模型补拍。

```sh
uv run python scripts/replay_internship_demo.py docs/experiments/job-readiness-goal2/success-demo.cast
uv run python scripts/replay_internship_demo.py docs/experiments/job-readiness-goal2/failure-demo.cast
```

成功案例为 B0 / M08-v2-full-1：实现 trace --since 的时区校验、UTC 包含边界过滤及 CLI 参数错误。
24 次请求后正常完成，可信容器验收 13 项通过。实际只修改 src/mokioclaw/cli/app.py，不把它宣传成多个源文件修改的完成案例。
原始 diff、变更字节、工具检查、模型流及有界最终汇报在 b0/trials/M08-v2-full-1。

失败案例为 B1 / M08-v2-full-1：产物同样通过 13 项，但运行因 token 预算停止，不能计完整交付。
失败回放明确标注“产物通过、运行失败”。原始记录在 b1/trials/M08-v2-full-1。
两者重新从归档变更字节重建，在全新可信容器再次得到相同评分，且未改变原运行终态；证据在 regrades。

## 不请求模型地复验

从项目根目录执行；输出使用仓库外的新临时目录，避免 pytest 收集到复制的任务测试。

```sh
uv run python scripts/reproduce_internship_trial.py M08-v2-full-1 \
  --archive docs/experiments/job-readiness-goal2/b0 \
  --output /tmp/mokio-goal2-success-regrade
```

换为 b1 可复验失败案例的产物。依赖固定任务缓存与固定 Docker 镜像；准备方式见 Goal 1 文档。
参考补丁和隐藏测试只由评测器读取，不放进候选工作区；不修改候选测试作为评分依据。

## 记录限制

冻结 worker 的最终汇报在 checkpoint 中是 1,800 字符的有界快照，trace 的图更新同样是有界摘要。
回放注明该限制；没有伪造未保存的完整最终汇报。
cases 下另存这些实际快照，以及保留的完整 codeAgent 报告和 verifier 模型输出，带 stream ID 与日志引用。
唯一完整交付的 verifier 判断与外部评分一致；大量失败运行的 verifier 没有形成判断，保持未知。
历史 D01 成功演示和原 56 次记录保持原样。
