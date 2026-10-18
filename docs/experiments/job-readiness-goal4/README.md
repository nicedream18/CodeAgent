# Goal 4：展示包与维护演练材料验收

本轮从干净 `5be75d9` 开始，整理既有证据，不新增真实模型试验或改变历史成绩。展示入口是[求职展示包](../../internship/showcase/README.md)。

## 验证范围

- 复用 D01 回放，新增只读案例元数据查看器，不解压或执行历史命令。
- 事实索引固定来源、版本、JSON pointer、预期值与 SHA256；文档检查只读。
- 三份概览和三条时序链结合当前源码人工审阅；单独核对 Mermaid 语法。
- 在仓库外干净 checkout 冻结安装，使用临时 HOME、禁用 dotenv，没有模型凭证。
- 离线示例额外阻止 socket.connect；真实 Docker 检查沿用原有隔离和 owner 清理。
- 新脚本进行 Ruff、严格 mypy 与只读/伪造证据等负例测试；不重新运行真实任务矩阵。

检查原始命令和终态记录将保存在本目录；最终结果以 [acceptance.json](acceptance.json) 为准。

本轮是自动化材料交付；[本人练习记录](../../internship/showcase/record-template.md)不填通过。小功能练习只给要求和验收标准，不由 Agent 实现。独立维护能力、跨文件真实成功及求职效果仍未验证。

版本边界：远程 Linux/wheel 验收是 `748ca5d`，本轮最终文档提交未推送、不冒充远程被测代码。历史试验、旧 wheel、标签与缓存保持原样。[初始清单](baseline.json)记录本轮前 2,245 个历史文件摘要。
