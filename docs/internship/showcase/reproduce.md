# 干净环境复现：先离线体验，再检查 Docker

从源码 checkout 根目录执行。需要 Python 3.13、uv、Git；仅最后的容器检查需要已启动的 Docker 引擎。准备阶段可以联网，离线执行阶段不调用模型。无需复制维护者 HOME、配置或 `.env`。

## 1. 创建干净环境与冻结安装

```sh
git clone https://github.com/nicedream18/CodeAgent.git
cd CodeAgent
git checkout <本地交付的提交号>
uv sync --frozen
uv run --no-sync mokioclaw --help
uv run --no-sync python -m mokioclaw --help
```

Goal 4 提交目前仅在本地，公开 main 不含本展示包；请使用交付者提供的包含该提交的源码或本地仓库，不把“尚未推送的文件”写成公开克隆已可获取。使用本地仓库可执行 `git clone --no-hardlinks /path/to/MokioAgent CodeAgent`。上面占位符必须换成实际提交，参见本轮最终汇报。

这一步会新建 `.venv`，不是复用旧虚拟环境。第一次需要下载锁定依赖；有完整 uv 缓存时可改用 `uv sync --frozen --offline`。缓存缺失时应报错并转回联网准备，不宣称冷缓存也可离线安装。需要个人模型配置的交互不属于这条离线路径。

如需隔离个人规则，用仓库外临时 HOME 运行示例，并设置 `PYTHON_DOTENV_DISABLED=1`。不要把这个 HOME 复制回项目：

```sh
trial_home=$(mktemp -d)
HOME="$trial_home" PYTHON_DOTENV_DISABLED=1 .venv/bin/python examples/internship_offline.py
```

## 2. 离线体验工具循环

```sh
uv run --no-sync python examples/internship_offline.py
```

应输出 `simulation=true`、`real_model=false`、`independent_check="add(2,3) == 5"`，实际修改的是临时目录中的 app.py，退出即清理。固定模型会读文件并修改减法为加法；这证明工具循环可运行，不是新真实模型试验。

## 3. 查看 D01 与失败归档

```sh
uv run --no-sync python scripts/replay_internship_demo.py docs/internship/success-demo.cast --no-wait
uv run --no-sync python scripts/inspect_showcase_case.py d01
uv run --no-sync python scripts/inspect_showcase_case.py verifier-budget
uv run --no-sync python scripts/replay_internship_demo.py docs/experiments/job-readiness-goal2/failure-demo.cast --no-wait
```

不加 `--no-wait` 会按既有讲解时间轴播放。成功输出为 finished/5 项测试；失败输出为 failed/token_budget_exhausted/13 项测试通过。查看器直接读取归档中的 canonical JSONL，不解压、不执行其中的命令，输出只包含元数据。原始 diff、XML 和判断位置见[案例页](cases.md)。

## 4. 用固定 fixture 触发并定位一个错误

```sh
uv run --no-sync pytest -q tests/test_generation_reliability.py::test_second_truncation_stops_without_third_request tests/test_generation_reliability.py::test_partial_json_never_uses_sdk_tolerant_parse
```

应为 **3 passed**：一项连续截断测试、两项不同结束原因的非法 JSON 测试。通过代表受控错误被正确捕获，不代表模型完成任务。定位顺序：测试中的 `cut()` → provider `invoke_streaming` → `validate_generation` → `ModelGenerationStopped`。两次截断最多两个请求，不能再调用工具或发第三次请求。[源码入口](chains.md)。

这是隔离 fixture 中的异常，pytest 本身应成功退出，不需要改坏工作区代码。真实预算失败的定位练习另见[维护题](practice.md)。

## 5. 按需检查真实 Docker

首次依赖镜像准备允许联网；使用自己的标签，避免覆盖已有实验镜像：

```sh
docker info
docker build -t mokioclaw-showcase:goal4 docker/execution
uv run --no-sync mokioclaw doctor --execution-backend docker --image mokioclaw-showcase:goal4 --json
MOKIO_DOCKER_IMAGE=mokioclaw-showcase:goal4 make check-docker
```

成功条件：doctor `ok=true`、后端为 docker 且有固定镜像 ID；检查 **29 passed、0 skipped**，真实测试收据包含隔离/超时/中文路径和可信评分负控制，cleanup.confirmed=true。引擎或镜像缺失会非零退出，不能把全部跳过写成成功。实际命令容器无外网，默认 2 CPU / 2 GiB / 128 pids；MCP/hooks 和文件工具不在此隔离范围。

Goal 4 本机复验复用了已有固定镜像，没有覆盖历史标签；记录实际镜像、架构与命令在[本轮报告](../../experiments/job-readiness-goal4/README.md)。不同平台不能要求镜像 ID 相同。

## 常见失败

| 现象 | 下一步 |
|---|---|
| Python 版本不足 | 用 `uv python install 3.13.9` 准备；不要改项目最低版本绕过 |
| 离线安装缺依赖 | 联网执行 `uv sync --frozen` 后重新做离线验证；保留原失败日志 |
| 找不到展示查看器 | 检查是否拿到了 Goal 4 本地提交；公开 main 的历史版本可能尚无此文件 |
| fixture 得到 model_output_truncated | 先看是否由 pytest 正确捕获；它是预期保护信号，不是网络故障 |
| Docker 不可用或镜像不存在 | 启动引擎、按指定标签准备镜像；禁止回落到 local 冒充容器通过 |
| 非 root 容器不能写工作区 | 检查工作区权限及宿主 UID；不要通过 root 或挂载整个 HOME 放宽边界 |

[远程 wheel 安装证据](../../experiments/job-readiness-goal3/remote/39028453552/wheel/report.json)对应 `748ca5d`；本轮验证的是源码 checkout 与新增只读工具，不把它冒充新 wheel 安装证明。
