# 故障排查

先找到界面中的 run ID，运行：

```sh
uv run mokioclaw trace --workspace /path/to/workspace --run-id RUN_ID --raw
uv run mokioclaw trace --workspace /path/to/workspace --layer ipc --follow
uv run mokioclaw trace --workspace /path/to/workspace --layer llm --raw
```

- RPC 没有 run ID：查看 daemon transport 中相同 request ID 的 received/validated/response；-32602 表示参数未通过校验，不应产生运行或工具副作用。
- 显式 Skill/审批管理提示能力不足：先结束当前任务，再更新并重启 daemon。不会静默忽略新选项。
- 模型生成失败：按 stream ID 查 llm_stream_start、llm_response、llm_stream_end 和 model_usage。`generation_attempt=0/1` 对应原请求和唯一一次重生成。`model_output_truncated` 表示两次均未完整结束，可用原工作区 checkpoint 恢复；先检查输出预算和任务步骤大小。
- 审批等待：查看 permission_requested 及订阅推送关联的 tool_call_id。补充指令、取消或权限撤销会使旧审批失效，重新使用迟到响应不会启动旧调用。
- 权限记忆误匹配：`permissions list` 检查 workspace、操作、resource、scope、match_scope、last_used，使用 revoke ID 或 clear。会话规则不会跨 daemon 重启保存，MCP 配置变化也不会继续命中旧指纹。
- 角色不显示：TUI `/agents` 查看 diagnostics 和来源。同来源重复名、非法字段、越权工具或未知 model_profile 都会拒绝加载；修正后开始新运行。
- MCP 退出/超时：查 mcp_diagnostic 的 server、phase、状态和 stderr 文件引用；在相同运行下确认其他工具继续工作。诊断文件超限时标记截断，不会阻塞排空 stderr。
- 日志写入失败：查看 trace_diagnostic；先修复磁盘/目录权限，运行结果保持原语义。`--raw` 展示已有 JSON，不能恢复被配额淘汰的全文。
- 缓存显示 unknown：供应商未提供该字段，不能据此判断缓存开关。cache_read 已包含于 input，不能再次累加到 total。

若需要完整模型 payload，在用户或项目 config.toml 显式开启：

```toml
[trace]
include_model_payload = true
```

文件位于工作区 `.mokioclaw/diagnostics`，限制权限并沿用有界配额。默认关闭。读取和回放 trace 都不会重新执行工具或请求模型。

复现离线验证：

```sh
make check
uv run python examples/reliability_demo.py --output /tmp/reliability-local.json
uv run python examples/reliability_demo.py --real-model --workers 3 --output /tmp/reliability-real.json
```

最后一个命令使用 `.env` 中已有的 MODEL/BASE_URL/API_KEY；真实请求独立于普通 CI。所有文件任务在新建临时工作区执行。
