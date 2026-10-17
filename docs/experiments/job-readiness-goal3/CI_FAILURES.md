# CI 故障定位与修复边界

本轮没有修改产品源码、依赖锁、任务评分或冻结的 B0/B1；真实模型调用为 0。
两次已授权远程运行及三次本地 Linux 全量诊断均保留原结果。

| 记录 | 观察 | 处理 |
|---|---|---|
| 远程 39026034469 / 8ca634d | 三作业冻结安装失败，Python 实际为 3.12.3 | workflow 显式通过 uv 选用 3.13.9；第二次远程已确认解释器正确 |
| 远程 39027177601 / ea542c0 | Docker/wheel 通过；普通检查 454/1/2，TUI 在 0.2 秒后仍运行 | 按轮次、运行完成与 finished 状态有界等待；保留原断言并检查聊天正文 |
| 本地 Linux attempt1 / 8192108 | 439/16/2，线程耗尽；取消测试还暴露生产者/消费者同步假设 | 测试宿主 PID 上限从 128 调为 1024；取消前等待实际 llm_token 事件，保留部分 trace、终态与禁止后续节点的原断言 |
| 本地 Linux attempt2 / fe53124 | 454/1/2，仅审批单测失败；过时 subprocess.run mock 未阻止真实 pip 安装 | mock LocalBackend.run，核对先审批后执行、只执行一次，并禁止该单测启动真实进程 |
| 本地 Linux final / 35162c0 | 455/0/2，Ruff、31 模块 mypy、文档及 diff 通过 | 相同入口在禁网环境执行，UV_OFFLINE=1；等待新 commit 的远程复验 |

数字顺序为 passed / failed / skipped。没有重跑旧远程运行来隐藏失败，也没有跳过失败测试。
本地 Linux 三次尝试见 [诊断历史](local-linux-full/diagnostic-history.json)，完整退出码及日志均保留。

## Linux 补充验证环境

使用临时 Git clone，不复制 .env 或个人配置；基于本轮独立执行镜像增加 make、uv 0.12.19 和 Python 3.13.9。
[Dockerfile](local-linux-full/image/Dockerfile)、构建结果、依赖准备及实际命令 JSON 位于同一目录。
依赖准备阶段允许联网；全量检查阶段使用 Docker `--network none` 和 `UV_OFFLINE=1`。
每次独立容器、2 CPU、2 GiB 内存、仅测试 clone/cache/evidence 挂载，结束时按独立 owner 清理。

1024 PID 是完整 pytest 宿主的测试资源设置。Agent 不可信命令的 Docker 后端继续使用原 128 PID 限制，源码未变，真实后端集成测试仍为 29 项通过。
补充容器是 Linux ARM64、glibc 2.41，远程为 Ubuntu Linux x86_64、glibc 2.39；本地结果不能代替远程普通检查通过。

## 当前候选

被测代码为 35162c0342f8600b3b7ce37d84ac55e5b0e3cee4，源码与检查输入摘要为
e2f8298b151a2c93cc2468a35486d0a1e163bf0468517f0ade1679f10d894d54。
macOS ARM64 全量 455/0/2、真实 Docker 29/0/0、干净 wheel 安装通过；Linux ARM64 禁网全量 455/0/2。
随后获准推送 748ca5d，[第三次远程运行](https://github.com/nicedream18/CodeAgent/actions/runs/39028453552) 三作业均通过，普通检查 455/0/2、真实 Docker 29/0/0，wheel 安装通过；三份报告已下载并验证摘要。
最终归档更新状态与顶层 README 文案；验收始终引用实际远程被测提交 748ca5d。
