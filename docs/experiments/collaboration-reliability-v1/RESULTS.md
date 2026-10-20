# 协作可靠性验证 v1

本轮起点 `3b4899cb93ea97a4e95600f5af089a8362bb5c13`，初始工作区干净。实现保留为待评审工作区改动，未推送、未调用真实模型。源码与检查指纹 `7645e55cedd525844bd1da0c2621c2bef781d6f2fa17438cb9f01345f24697c9`；锁文件 `68fc0a65d66931e0f465dae77db41e6155da4412909e4464381721a72002b05e`。逐文件实现与基线哈希见 implementation-manifest.json / baseline.json。

## 统一检查

- 初始基线：511 通过、2 跳过。
- 交付 `make check`：**553 通过、3 跳过**；冻结安装、Ruff、mypy（41 个严格检查文件）、协议/评测文档一致性与 diff 检查均通过。跳过项为显式真实 Docker 用例，在独立作业执行。
- 真实 Docker 作业：**34 通过、0 跳过**；Linux/aarch64（本地 Docker Desktop），清理确认 `True`。这是本地真实容器证据，本轮没有远程 CI。
- 当前镜像 `sha256:98528618fbe210796610fabfd3b80acf5645271109acb71d726d7b525b5c1609`；冻结评分镜像 `sha256:122a00775cb27f1e8fc02b10ca32b4466d21f5e14df7964c6de0436dce9d48ba`，未覆盖历史镜像。

原始检查见 [checks/report.json](checks/report.json)、[Docker 结果](docker/result.json)、[基线](baseline-check/report.json)。开发失败见 [failures/NOTES.md](failures/NOTES.md)，包含真实容器发现的易变日志引用误判。

## 循环与队列

固定本地模型触发同步子 Agent 相同失败，当前批次后续调用未执行；一次重规划后再次触发，终态 failed，checkpoint 中恢复额度为 1，已有修改和父任务状态保留。取消/新要求优先，审批失效、恢复、后台独立及关闭竞争通过专项断言。可读停止样本见 loop-stop/；完整用例由 checks/pytest-summary.json 对应源码测试定位。

队列固定 4 运行/16 等待，满载拒绝后取消可再次提交。连续 **1,000 次排队取消**验证待执行集合和 runner 表立即清空、闭包弱引用释放，终态缓存不超过 128；600 条事件验证仅保留尾部 256 条且磁盘回放序号完整。测试本身是离线控制实验，不是模型性能实验。

## 16K 上下文压力

窗口 16384、输出预留 1024、安全余量 512，高水位 12620、硬输入限制 14848。固定本地模型使用明确的 UTF-8 字节/4 估算器，工具 schema 同样计入治理预算。表中输入列为治理前估算，不是供应商实际 token。每例预设消耗 1 次恢复额度并植入旧历史摘要，压缩后历史仍在、额度不返还。

| 场景 | 压缩 | 请求方式 | 最大输入估算 | 实际压缩 | 摘要请求 | 工具调用 | 独立产物/停止 |
|---|---|---|---:|---:|---:|---:|---|
| long_file | 开 | 自动 | 15634 | 3 | 3 | 13 | 通过 |
| long_file | 开 | 手动 | 15634 | 3 | 3 | 13 | 通过 |
| long_file | 关 | 自动 | 15634 | 0 | 0 | 5 | context_budget_exceeded |
| long_file | 关 | 手动 | 15634 | 0 | 0 | 5 | context_budget_exceeded |
| command_tail | 开 | 自动 | 13682 | 2 | 2 | 13 | 通过 |
| command_tail | 开 | 手动 | 13682 | 2 | 2 | 13 | 通过 |
| command_tail | 关 | 自动 | 15615 | 0 | 0 | 8 | context_budget_exceeded |
| command_tail | 关 | 手动 | 15615 | 0 | 0 | 8 | context_budget_exceeded |
| repair_correction | 开 | 自动 | 13118 | 2 | 2 | 23 | 通过 |
| repair_correction | 开 | 手动 | 13118 | 2 | 2 | 23 | 通过 |
| repair_correction | 关 | 自动 | 15281 | 0 | 0 | 14 | context_budget_exceeded |
| repair_correction | 关 | 手动 | 15281 | 0 | 0 | 14 | context_budget_exceeded |

12/12 控制契约通过：开启的 6 例实际压缩并通过产物断言；关闭的 6 例越过硬限制，受控停止、保存已有文件及 checkpoint。多轮失败场景在压缩后接受补充要求，并最终检查文件内容。真实 Docker 另对命令尾部和修复纠正场景覆盖开关，以及易变容器 ID/日志引用不绕过失败检测。

[机器结果](pressure-results.json)计入摘要开销、请求数和工具次数；[完整压力工作区归档](pressure-workspaces.tar.gz)保留实际输出、完整日志、事实文件、checkpoint 和 trace。包内均为固定本地数据，条目使用相对路径并剔除符号链接；本轮所有归档总量受 16 MiB 上限检查。运行中的诊断仍沿用单文件 1 MiB / 32 份 / 16 MiB 配额。

这些数据标记 simulation=true / real_model=false，不代表真实任务完成率、真实 token 或延迟收益。原有 18 次真实 A/B 归档逐文件未改动。复现命令与设计边界见 [协作可靠性说明](../../collaboration-reliability.md)。
