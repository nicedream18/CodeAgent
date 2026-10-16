# 前台 Bash 执行后端

默认 `local`；使用 `--execution-backend docker` 或 `[runtime] execution_backend="docker"` 显式选择。
镜像配置为 `docker_image` / `MOKIO_DOCKER_IMAGE`，默认 `mokioclaw-execution:0.2.0`。
模型请求在宿主执行；Docker 不覆盖 FileWrite、MCP、Python hooks 或整个 Agent。
工作区本身可能包含秘密，挂载工作区等于允许容器读取其中内容。

准备（允许联网）：

```sh
uv sync --frozen
uv export --frozen --no-emit-project --no-hashes --format requirements-txt -o docker/execution/requirements.txt
docker build -t mokioclaw-execution:0.2.0 docker/execution
uv run mokioclaw doctor --execution-backend docker --json
make check-docker
```

Docker Desktop 必须运行。镜像缺失、引擎关闭、后台 Bash 请求返回受控错误，不回落到宿主。
每个命令 fresh shell，工作区以同一绝对路径挂载，执行时固定镜像 ID。
不读宿主 `.mokioclaw.env`、不注入宿主环境/凭证、不挂载用户目录或 socket。
只读 root、非 root UID/GID、network none、cap-drop ALL、no-new-privileges，
2 CPU / 2 GiB / pids 128；`/tmp` 为 64 MiB tmpfs。依赖只能在准备阶段安装。

参数校验 → 静态权限 → hooks → 审批 → 版本核对 → 后端 → 有界结果 → 后置 hook。
审批资源包含 local/Docker 镜像 ID，旧 local 授权仍匹配旧资源；换后端或镜像不能复用。
恢复时后端变化清除旧待执行消息及验证结论，重新规划，不恢复 PID 或容器。

输出通过两个线程持续排空，内存只保存有界首尾；诊断文件复用 1 MiB/份、32份/16 MiB 配额。
超限后继续读管道，记录截断；保存故障明确返回 artifact_error，不改变命令本身退出状态。
超时只删除该调用创建的容器，不扫描杀死其他容器。评测 watchdog 按专属 owner 标签清理。
普通前台取消沿用同步工具在超时内收尾的语义。

2026-10-16 macOS ARM64 实验：Docker 引擎启动后真实执行 `MOKIO_TEST_DOCKER=1 uv run pytest -q tests/test_execution_backend.py`，7 passed / 8.28s。
覆盖中文/空格路径、只读文件、无宿主密钥/socket/未挂载文件、外网禁用、资源限制、3 MB 输出和超时无延迟写入。
