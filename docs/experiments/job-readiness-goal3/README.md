# Goal 3：Linux CI 与干净安装证据

本地可评审准备与远程验收分开。用户已授权推送固定的 22 个提交至 `8ca634d`；该次 push 已执行并自动触发 CI。
后续修复提交尚未获远程推送授权；软件包、镜像发布及真实模型调用仍未授权。
最终状态和本轮证据见 ../../internship/STATUS.md；配置文件和本地 ARM64 结果不能代替远程 Linux 运行链接。

## 首次远程运行及入口修复

[39026034469](https://github.com/nicedream18/CodeAgent/actions/runs/39026034469) 对应 `8ca634d`，
触发方式为 push，三个作业均 failed，三个失败报告上传步骤均 success。
[远程状态与下载链接](remote/39026034469/status.json) 保留原失败，不改写为通过。
初始 API 下载返回 401；用户登录内置浏览器后，三份报告均已下载并与 GitHub artifact SHA256 核对一致。
原报告明确记录解释器为 `/usr/bin/python` 3.12.3，冻结安装因不满足 `>=3.13` 退出。

本地 Linux 诊断发现入口未固定解释器：setup-uv v6 的 python-version 设置 UV_PYTHON，
不会替换 shell 中的裸 python；报告器把 UV_PYTHON 设为自身 sys.executable，
因此旧入口可能用 runner 的 Python 3.12 发起要求 >=3.13 的冻结安装。
[实测诊断](local-linux-bootstrap/diagnosis.json) 中，默认 Python 3.12 时旧入口失败，
显式 `uv run --no-project --python 3.13.9 python` 启动后 wheel 检查通过，实际报告解释器为 3.13.9。
三个 workflow 入口均已修正，测试约束版本与 setup-uv 配置一致。
这份 Linux ARM64 容器诊断不是远程 Linux 验收；原远程失败原因已通过下载报告确认。

## 修复后本地复验

固定代码提交 `e5bcf80acff9a094bd5e416e92e3cd34ac8afe6c`，工作区干净。
源码与检查输入 SHA256 为 `6bebff32160cee37f73d0e9c93d1f9d16e5dcfc49b2569a25f8be1de20aa4ad8`；
后续归档提交仅增加文档证据，须核对同一输入摘要。

- [普通检查](local-arm64-repair/check/report.json)：455 passed、2 skipped，Ruff、31 个 mypy 模块、文档和 diff 通过。
- [真实 Docker](local-arm64-repair/docker/report.json)：29 passed、0 skipped，独立镜像及 owner 清理确认。
- [wheel](local-arm64-repair/wheel/report.json)：仓库外干净安装、两种 CLI 和离线资源检查通过；wheel SHA 与首轮相同。

状态：**本地修复完成、远程复验待授权**。原远程失败保留；修复版本尚未获得远程通过证据。

## 首轮本地结果（固定代码 9247f7f）

状态：**本地准备完成、远程待验收**。被测代码提交为 `9247f7f4d91841f817e071ebc1a1e18a24402147`。
后续归档提交只增加本目录证据及 STATUS 文档；源码、检查脚本和打包输入摘要为
`96f8c79beaffd8fd4ee99ca37f5e6b8affe9bcf6e3e57104dbf372ffa64d5a87`。
本地报告保留实际被测提交，不把文档归档提交冒充已执行版本。

| 本地 macOS ARM64 验证 | 实际结果 | 证据 |
|---|---|---|
| 普通检查 | 455 passed / 2 skipped，Ruff、31 模块 mypy、文档与 diff 通过 | [报告](local-arm64/check/report.json) |
| 重新构建及真实 Docker | 29 passed / 0 skipped，linux/aarch64，owner 清理完成 | [报告](local-arm64/docker/report.json) |
| wheel 干净安装 | site-packages 导入、console/module 入口、禁网资源和文件读取通过 | [报告](local-arm64/wheel/report.json) |
| 缺失引擎／镜像 | 两例均明确 exit 1，未启动测试 | [预期失败](local-arm64/negative/checks.json) |
| 历史保留 | Goal 2 的 384 项、7 份历史缓存、旧 wheel/标签/镜像均未变 | [审计](integrity-audit.json) |

新 wheel SHA256：`605001843b1c5e1ff3a105bf76abdbee5a17f43e1e19338d28522e1974f38359`。
wheel 二进制未上传，旧 dist 保留；临时安装目录会随检查结束清理，实际路径及版本收据保留在报告中。
[首轮本地验收](first-local-acceptance-9247f7f.json) 保留当时未授权状态；当前状态见 [验收索引](acceptance.json)，原远程失败链接与已下载报告见上节。

## 三个作业、同一入口

.github/workflows/check.yml 保留 push/pull_request，增加 workflow_dispatch。
固定 ubuntu-24.04、Python 3.13.9、uv 0.12.19，第三方 actions 固定到已核验的提交 SHA。
CI 报告器通过 `uv run --no-project --python 3.13.9 python` 启动，不依赖 runner 默认的 python。
权限仅 contents:read，checkout 不保留认证信息；不提供模型凭证。
依赖缓存关联系统、架构、Python 和 uv.lock，缓存缺失时仍完整安装及检查。

从项目根目录执行；每次输出目录必须是新目录，旧证据不会覆盖：

```sh
.venv/bin/python scripts/ci_report.py --job check --output /tmp/mokio-ci-check
.venv/bin/python scripts/ci_report.py --job docker --output /tmp/mokio-ci-docker
.venv/bin/python scripts/ci_report.py --job wheel --output /tmp/mokio-ci-wheel
```

- check 直接复用 make check：冻结安装、Ruff、mypy、协议/评测文档、pytest、diff；另检查最近提交的 whitespace。
- docker 核对 requirements.txt 与 uv.lock 导出一致，用原 Dockerfile 构建独立随机 tag，记录不可变镜像 ID，再执行 make check-docker。
- wheel 冻结导出运行依赖；用固定 build-constraints 构建 wheel，在仓库外新建虚拟环境，按哈希安装依赖，再安装 wheel。

Docker 的 doctor 失败即失败；两条真实测试必须实际通过，不能通过全跳过或零测试制造成功。
集成测试注入本轮镜像与独立 owner，只改变测试环境选择和容器归属，不改变产品后端、任务配方或评分逻辑。
超时后只清理本轮 owner 的容器，不清理其他任务，不覆盖 mokioclaw-execution:0.2.0。

## 干净安装和普通 CI 的边界

所有子检查使用新 HOME、禁用 dotenv、白名单环境，不传递模型或 GitHub 凭证。
wheel 检查清除 PYTHONPATH、用户 site 和 editable 路径，从仓库外执行 console/module 两种 --help。
独立 Python -I 探针核对 site-packages、包内资源、禁网 tokenizer 及文件读取；记录实际安装路径、包版本与 wheel SHA256。
wheel 和探针目录在系统临时目录，不写入旧 dist，不把复制的测试留在仓库供 pytest 误收集。
依赖准备和镜像构建允许联网；探针禁网、Docker 工具沿用原网络/权限/资源限制；普通检查不调用真实模型。

## 报告、脱敏与配额

每个作业输出 report.json、summary.md、最小 pytest 收据及脱敏日志。
记录 commit、被测源码/脚本/打包输入摘要、锁、Python/uv/平台、运行/作业标识、命令退出码和耗时。
pytest 收据来自 sessionfinish，独立保存 counts 和真实测试 node ID，不能靠被裁剪的控制台末行猜测通过数量。
passed、failed、not_started 与 pytest skipped 分开；失败路径仍保存报告。

单日志最多 1 MiB；超过 8 KiB 的单行整行省略，继续排空；保留有界末尾摘要和截断标记。
每个作业报告总量最多 16 MiB；容量限制不能吞掉命令退出码或测试收据。
过滤已知凭证值、Authorization 头及认证 URL，不复制 HOME、.env、模型 payload、完整工作区或 wheel 二进制。
token 预算是普通数值，不应被识别成认证 token；初版扫描误报已用字段边界修复并回归。

三个 artifacts 均用 always() 上传，名称关联 commit、job、attempt，保留 14 天；不发布软件包或镜像。
若 checkout/setup 早于报告器启动就失败，可能没有自定义报告；GitHub 作业仍须明确失败，保留平台日志。
构建用到的系统包镜像可能随准备环境变化，应比较记录的构建输入和实际镜像 ID，不声称跨架构或跨次构建字节相同。

## 远程授权与历史范围

目标 origin 为公开仓库 nicedream18/CodeAgent；本轮开始时已有 20 个未推送提交。
推送会包含这些历史实验、trace、任务归档及本地绝对路径，不能只列 workflow 改动。
授权清单在最终本地 commit 固定后生成到仓库外，避免“把清单提交后又改变待推送 SHA”的循环：

```sh
uv run python scripts/ci_push_manifest.py \
  --base 6e88c21c294fe041a821a21e0abad9dc095b9364 \
  --branch main --output /tmp/mokio-ci-remote-review
```

脚本列出完整提交范围与文件，扫描所有未发布 Git blob、提交消息及有界嵌套 tar；扫描不到的内容不能标为通过。
它没有 push 或远程触发功能。扫描是项目凭证值和常见高置信模式检查，不等于通用 DLP 保证。
目标 SHA、仓库/分支或远程基线改变后，必须重新核对清单。
取得授权后再推送；push 会自动运行三作业，应复用该运行，不重复 dispatch。
workflow_dispatch 的 GitHub 手动入口需默认分支包含配置；原批准版本已推送 main，后续修复推送仍待具体授权。

远程通过后保存对应 commit 的 run/job/artifact 链接，并下载报告核对，不把 queued/running 当通过。
本地 ARM64 与远程 Linux 分别归档，保留失败，不修改 Goal 1/2 原始记录。

参考：[GitHub 触发事件](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)、[运行日志](https://docs.github.com/en/actions/how-tos/monitor-workflows/use-workflow-run-logs)、[artifacts](https://docs.github.com/en/actions/tutorials/store-and-share-data)、[uv 的 GitHub Actions 集成](https://docs.astral.sh/uv/guides/integration/github/)。
