# v0.2.0 本地发布候选（v0.2.0-rc.1）

新增可选 Docker 前台执行、后端/镜像绑定审批、跨后端恢复重新决策，以及固定代码任务评测。
保留完整流程与 local 默认，不增加模型供应商或改变许可证。

## 可复现检查

```sh
uv sync --frozen
make check
uv run python examples/internship_offline.py
docker build -t mokioclaw-execution:0.2.0 docker/execution
make check-docker
make eval-images
uv run mokioclaw eval prepare --suite internship-v1
uv build --wheel
```

wheel 的入口包括主任务、TUI、doctor、eval 和已有管理命令。安装后 `mokioclaw eval list` 不启动 MCP、不要求模型密钥。
干净安装不要复制个人用户配置；真实模型配置通过自己的 `.env` 或环境变量提供。
执行/评分阶段无需联网，镜像及依赖准备阶段允许联网。
冷缓存首次离线安装曾因缺少依赖 wheel 失败，原日志已保留；联网按锁文件及哈希准备后，新的隔离环境离线安装和演示通过。

## 实验结果及未完成验收

正式批次 56/56 已执行，配置/计量由 1,903 次 canonical 请求记录核对，完整交付 0/56、仅产物通过 8/56。
首批另有 56 次因配置继承缺陷标为无效对照。所有失败均保留，不发布成功率提升或多 Agent 优势结论。
正式批次的升级事件转发缺陷已在实验结束后修复，离线回归通过；没有再次运行真实矩阵。

rc.1 冻结时缺少完整成功的真实 Agent 演示。随后在 `fd4f29f` 上补充了一个小型受控 D01 案例：真实模型 full 正常收尾、独立 5/5 验收通过、归档重建后再次通过。[150 秒回放与复现](demo.md)。它不改变原矩阵的结果，也不代表复杂任务可靠性已解决。
本轮源码修复与该演示在 rc.1 之后；原标签和已验证 wheel 保持原样，不能把新增能力归到旧发布候选。
rc.1 当时未执行远程 CI；后续 Goal 3 已在 748ca5d 通过三作业验收，见[报告](../experiments/job-readiness-goal3/README.md)，不是 rc.1 标签被重新测试。维护者是否已能独立解释三条执行链，仍需要本人演练确认。
因此这是可检查的本地候选，**不能将原计划全部验收标为完成**。[详细实验结果](../experiments/internship-v1/RESULTS.md)。

## 已知边界

- Docker 仅隔离前台 Bash/评分；MCP/hooks/文件工具在宿主，工作区中的秘密仍可被挂载容器读取。
- 不支持 Docker 后台 Bash，不自动安装依赖，不自动回落宿主；root 用户需确保非 root 容器 UID 有工作区写权限。
- 实际 ARM64 测试使用 cgroup v2；后续 Linux CI 已按具体授权执行，版本见上述报告。新文档仍只作本地提交，不扩大推送范围。
- 12 个任务规模有限，历史任务为缺陷注入，公开 Issue 可能存在训练污染；三次重复不支持广泛泛化结论。
- 评分防止测试/配置篡改及空报告，不是对任意恶意 Python 代码的形式化防篡改证明。
- 诊断文件采用既有配额，旧引用可能因保留策略过期；运行级摘要、状态和相关 ID 仍保留。
- 大于 1 MiB 的单文件 diff 在报告中明确省略，任务/候选文件摘要仍可用于核对。
- 真实实验必须使用源码 checkout 提供 HEAD/提示词/锁文件版本；wheel 可独立列举、准备任务及离线框架验证。

本轮只创建本地候选和 Git 提交，不推送或公开发布。真实实验数字以归档报告为准。
