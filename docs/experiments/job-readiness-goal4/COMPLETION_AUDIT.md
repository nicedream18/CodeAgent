# Goal 4 完成审计

范围保持原目标：自动化展示包与维护演练材料。不以简历草稿或测试通过推断本人掌握、求职效果或新模型能力。

| 原要求 | 当前证据与判定 |
|---|---|
| 先核对工作区并保留已有工作 | 初始 `5be75d9` 干净；[baseline](baseline.json)固定历史文件与源码摘要；没有覆盖其他修改 |
| 读取 README、状态、D01、三轮结果和关键源码 | [事实索引](../../internship/showcase/FACTS.md)、[源码审阅](source-review.json)记录版本、来源、边界与实现顺序 |
| 每条事实有范围、版本、来源、限制 | 12 条 [facts.json](../../internship/showcase/facts.json)绑定 JSON pointer、预期值与原件哈希；只读检查通过 |
| 区分运行、验收、有效对照、单/跨文件及本地/远程 | [实验单页](../../internship/showcase/experiments.md)与[案例页](../../internship/showcase/cases.md)逐项说明；无效 56 次不进入正式分母，D01 不并入矩阵 |
| README 首屏定位、架构、启动、结果和限制 | [README](../../../README.md)提供四条结果与原始链接；离线 fixture 与自己的模型分开，教学细节另放文档 |
| 三份单页材料 | [架构](../../internship/showcase/architecture.md)、[实验](../../internship/showcase/experiments.md)、[取舍](../../internship/showcase/tradeoffs.md)；分别为 37/28/12 行的 Markdown 概览，不宣称已排成单页 PDF |
| 三链有时序、源码、不变量、测试、正常/失败及限制 | [chains](../../internship/showcase/chains.md)覆盖；尤其明确独立评分在运行之后，不改变 daemon 终态；6 图语法通过并审阅源码对应性 |
| 保留 D01、diff、独立评分、trace，不夸大 | 原 D01 cast 与归档摘要不变；[成功案例](../../internship/showcase/cases.md)链接需求、实际补丁、XML、收据与重建评分，仅单文件单例 |
| 真实失败及定位，不混淆推测与确认 | B1/M08-v2-full-1 的 seq 282/290/292/294、stream/call/run ID 和原始报告均可核对；verifier 结论未知，不宣称收敛已修复 |
| 离线只读回放，不执行历史工具 | 两段播放器实际运行；[查看器](../../../scripts/inspect_showcase_case.py)读取归档不解压；测试禁止创建进程或连接网络 |
| 干净安装、CLI、离线示例、归档、预设错误 | [clean-base](clean-base/report.json)与精确交付提交的 [clean-delivery](clean-delivery/report.json)实测；固定异常 fixture 3 passed；示例额外阻止网络连接 |
| Docker 路径、依赖阶段、错误和成功标准 | [复现说明](../../internship/showcase/reproduce.md)给出显式 --image、独立标签、资源和剩余边界；真实 29 项非空通过与清理收据保存；未覆盖历史镜像 |
| 简历介绍、3～4 条要点、数字关联与 AI 范围 | [简历草稿](../../internship/showcase/resume.md)有四条可选要点和证据；独立贡献保留待填写，无效率提升宣传 |
| 小功能练习不直接实现 | trace --limit 需求与可检验标准已给出；产品源码保持不变，没有代做功能补丁 |
| 失败题、安装/Docker 清单、讲解追问、记录模板 | [练习](../../internship/showcase/practice.md)、[单独解析](../../internship/showcase/answers.md)、[空白记录](../../internship/showcase/record-template.md)齐全；未代填本人通过 |
| 链接、数字、CLI、图、Ruff/mypy/测试/diff | [专项报告](delivery-checks/report.json)、[交付提交报告](clean-delivery/report.json)、[指标](metric-review.json)、[图](mermaid-parse.json)；本地链接及行号检查覆盖 README 和 internship 下 Markdown |
| 不记录密钥、不修改历史或冻结版本 | [凭证扫描](credential-review.json)、[完整性](integrity.json)；导出日志有界并缩减原回放输出。扫描不是通用 DLP 保证 |
| 本地中文提交、不推送、不发布、不新增真实实验 | `edeacfd` 是材料与工具交付；后续仅归档本轮报告；真实模型调用、远程写入和发布均为 0 |

只读文档检查不会探测 HTTP 链接在线性，Mermaid 没有截图排版验收；这些限制写入报告，没有冒充已验证事项。真实 CI/wheel 保留 `748ca5d` 的已有证据，不重复触发远程作业。最终状态以 [acceptance.json](acceptance.json) 的范围为准。
