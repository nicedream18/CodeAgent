# Goal 4：展示包与维护演练材料验收

状态：**自动化展示材料完成，本人独立维护验收待实操**。从干净 `5be75d9` 开始，整理既有证据，不新增真实模型试验或改变历史成绩。展示入口是[求职展示包](../../internship/showcase/README.md)。

## 实际结果与版本

| 检查 | 结果与证据 |
|---|---|
| 精确交付提交 | `edeacfd`，仓库外重新 clone、独立 HOME、新 `.venv`；[原始命令与退出码](clean-delivery/report.json) |
| 干净安装与体验 | 冻结依赖缓存安装、两种 CLI、离线示例、两段回放、两种案例元数据查看均通过；不依赖 `.env` |
| 新工具 | 8 项正/负测试通过；Ruff、2 文件严格 mypy 通过；[测试收据](clean-delivery/pytest-summary.json) |
| 控制边界专项 | 13 项通过，含新工具、整批截断、取消审批、纠正/迟到批准、同步子任务及伪造评分；[收据](delivery-checks/pytest-summary.json) |
| 固定错误 fixture | 连续截断与两项非法原始 JSON，3 项通过；[日志](clean-base/fixed-fault-controls.log) |
| 真实 Docker | 29 passed、0 skipped，linux/aarch64；[收据](docker/result.json)、[node ID](docker/pytest-summary.json)；owner 清理确认 |
| 图与数字 | 6 图 Mermaid 11.12.0 语法检查；[解析](mermaid-parse.json)、[源码对应审阅](source-review.json)、[指标复核](metric-review.json) |
| 历史完整性 | 2,245 历史文件、7 缓存清单、产品源码、锁、旧 wheel/标签/镜像保持原样；[审计](integrity.json) |

干净交付安装使用已有 uv 缓存，不把约半秒的暖缓存安装写成冷机器速度。实际步骤耗时见报告。Docker 在 `5be75d9` 的新 checkout 执行；交付提交没有改变产品源码、依赖、Docker 或 workflow，新增查看/检查工具另在 `edeacfd` 验证。

最终归档只更新状态和证据文档，不能把它描述为新的远程 CI 或 wheel 验收版本。本轮没有全量模型矩阵或新完整 pytest；13 项专项、8 项交付工具及29 项真实 Docker 的范围单独记录。

## 验证范围

- 复用 D01 回放，新增只读案例元数据查看器，不解压或执行历史命令。
- 事实索引固定来源、版本、JSON pointer、预期值与 SHA256；文档检查只读。
- 三份概览和三条时序链结合当前源码人工审阅；单独核对 Mermaid 语法。
- 在仓库外干净 checkout 冻结安装，使用临时 HOME、禁用 dotenv，没有模型凭证。
- 离线示例额外阻止 socket.connect；真实 Docker 检查沿用原有隔离和 owner 清理。
- 新脚本进行 Ruff、严格 mypy 与只读/伪造证据等负例测试；不重新运行真实任务矩阵。

检查原始命令和终态记录保存在本目录；逐项结果见 [acceptance.json](acceptance.json) 和 [完成审计](COMPLETION_AUDIT.md)。报告导出时替换了本机根路径；完整回放输出不重复写入 Git，仅保留实际退出码、字节数和原日志 SHA256。日志有界排空，单份上限 1 MiB，未发生截断；导出总量低于 16 MiB。

图语法检查工具在仓库外安装 `mermaid@11.12.0`、`jsdom@26.1.0`，没有改项目依赖。可将 [parse-diagrams.mjs](parse-diagrams.mjs) 复制到新的外部目录，在该目录安装上述依赖，再用 `node parse-diagrams.mjs /absolute/path/to/checkout` 复核；源码对应性仍需阅读代码，语法通过不等于图的逻辑正确或已经导出图片。

本轮是自动化材料交付；[本人练习记录](../../internship/showcase/record-template.md)不填通过。小功能练习只给要求和验收标准，不由 Agent 实现。独立维护能力、跨文件真实成功及求职效果仍未验证。

版本边界：远程 Linux/wheel 验收是 `748ca5d`，本轮最终文档提交未推送、不冒充远程被测代码。历史试验、旧 wheel、标签与缓存保持原样。[初始清单](baseline.json)记录本轮前 2,245 个历史文件摘要。
