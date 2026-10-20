# 历史代码任务评分镜像

Dockerfile 与 requirements.txt 原样取自 `8df67d7` 的 `docker/execution/`。历史任务源码使用 MCP 2 与 LangGraph，不能在当前 CrewAI / MCP 1 的执行镜像内评分。这里固定旧依赖，仅用于评分既有任务，不属于产品运行依赖，也不覆盖原评分镜像标签或任务清单。

CI 分别构建当前执行镜像和本评分镜像，按不可变 ID 记录；本地通过 `MOKIO_DOCKER_IMAGE` 与 `MOKIO_EVAL_DOCKER_IMAGE` 分别选择。未指定评分镜像时继续使用既有 `mokioclaw-execution:0.2.0`，不可用即明确失败。
