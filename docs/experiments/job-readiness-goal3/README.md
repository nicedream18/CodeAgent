# Goal 3：Linux CI 与干净安装证据

本地可评审准备与远程验收分开。远程推送、触发、发布及真实模型调用当前均未授权。
最终状态和本轮证据见 ../../internship/STATUS.md；配置文件和本地 ARM64 结果不能代替远程 Linux 运行链接。

## 三个作业、同一入口

.github/workflows/check.yml 保留 push/pull_request，增加 workflow_dispatch。
固定 ubuntu-24.04、Python 3.13.9、uv 0.12.19，第三方 actions 固定到已核验的提交 SHA。
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
workflow_dispatch 的 GitHub 手动入口需默认分支包含配置；本轮拟推送目标 main，实际操作仍待授权。

远程通过后保存对应 commit 的 run/job/artifact 链接，并下载报告核对，不把 queued/running 当通过。
本地 ARM64 与远程 Linux 分别归档，保留失败，不修改 Goal 1/2 原始记录。

参考：[GitHub 触发事件](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)、[运行日志](https://docs.github.com/en/actions/how-tos/monitor-workflows/use-workflow-run-logs)、[artifacts](https://docs.github.com/en/actions/tutorials/store-and-share-data)、[uv 的 GitHub Actions 集成](https://docs.astral.sh/uv/guides/integration/github/)。
