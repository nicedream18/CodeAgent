"""Frozen task selection; failures never cause substitution."""

from __future__ import annotations

import random
from typing import Any

from mokioclaw.evaluation.models import Architecture

BASE_COMMIT = "6e88c21c294fe041a821a21e0abad9dc095b9364"
DATASET_REVISION = "6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2"
CORE = ("M02", "M03", "M07", "E02")
TASKS: dict[str, dict[str, Any]] = {
    "M01": {
        "requirement": "修复 daemon 结束状态与订阅推送之间的竞争：订阅者看到终态时必须已经收到唯一的 daemon_run_finished 事件。保持正常完成、失败、中断及断线重连语义。",
        "file": "src/mokioclaw/core/daemon.py",
        "old": '            with record.condition:\n                record.status = terminal_status\n                record.append({"type": "daemon_run_finished", "status": terminal_status})\n                record.condition.notify_all()',
        "new": '            record.status = terminal_status\n            record.append({"type": "daemon_run_finished", "status": terminal_status})',
        "tests": ["tests/test_eval_terminal.py"],
        "regression": [
            "tests/test_daemon.py",
            "tests/test_run_cancellation.py::test_cancel_rpc_is_idempotent_wakes_approval_and_preserves_terminal_state",
        ],
    },
    "M02": {
        "requirement": "模型返回 length/max_tokens 时，该响应的所有工具调用（包括看似完整的调用）都必须作废并补齐未执行结果；只允许重新决策一次，再次截断安全停止。原始半截 JSON 不得用 SDK 补全值执行。保留取消、纠正和预算检查。",
        "file": "src/mokioclaw/providers/generation.py",
        "old": '    if result.reason == "output_limit":',
        "new": "    if False:",
        "tests": [
            "tests/test_generation_reliability.py::test_truncated_batch_is_closed_and_replanned_once",
            "tests/test_generation_reliability.py::test_second_truncation_stops_without_third_request",
        ],
        "regression": [
            "tests/test_generation_reliability.py::test_partial_json_never_uses_sdk_tolerant_parse",
            "tests/test_generation_reliability.py::test_abnormal_finish_does_not_retry",
        ],
    },
    "M03": {
        "requirement": "首次接触子目录 AGENTS.md 或规则变化时，刷新上下文并使尚未执行的旧修改决策失效。直接 FileWrite/FileEdit 和一批工具中的后续修改均不得沿用旧决策。新模型请求读取规则后允许继续；读操作不应被永久阻止。",
        "file": "src/mokioclaw/core/instructions.py",
        "old": "    if writing and runtime.context_dirty:",
        "new": "    if False:",
        "tests": [
            "tests/test_project_optimization.py::test_rules_refresh_before_write_and_stop_remaining_batch"
        ],
        "regression": [
            "tests/test_project_optimization.py::test_rules_persist_through_compaction_and_cannot_be_silently_dropped",
            "tests/test_optimization_integration.py::test_checkpoint_restores_nested_rules_and_requires_reverification_after_rewind",
        ],
    },
    "M04": {
        "requirement": "审批响应必须保持单次生效：重复响应不能反转已完成决定；收到补充指令后旧审批失效，迟到批准不得启动旧调用。保持运行/调用 ID、参数及上下文版本校验。",
        "file": "src/mokioclaw/core/daemon.py",
        "old": "            if waiter is None or waiter.event.is_set():",
        "new": "            if waiter is None:",
        "tests": [
            "tests/test_approval_memory.py::test_late_second_approval_cannot_reverse_first_decision"
        ],
        "regression": [
            "tests/test_optimization_integration.py::test_actual_daemon_approval_correction_replans_and_rejects_late_approval",
            "tests/test_daemon.py::test_daemon_rejects_approval_response_for_different_tool_call",
        ],
    },
    "M05": {
        "requirement": "修复后台 Subagent 与前台取消信号耦合的问题。显式后台任务必须有独立 RunControl，父运行 Ctrl+C 不取消后台任务；同步 Subagent 仍遵循前台控制。私有资源必须正确关闭。",
        "file": "src/mokioclaw/graph/steps/delegation.py",
        "old": "        child_runtime.run_control = RunControl()",
        "new": "        child_runtime.run_control = runtime.run_control",
        "tests": [
            "tests/test_run_cancellation.py::test_background_child_control_is_independent_and_loop_is_closed"
        ],
        "regression": [
            "tests/test_subagent_tasks.py::test_subagent_manager_persists_events_and_result",
            "tests/test_subagent_tasks.py::test_unfinished_persisted_task_is_marked_interrupted",
            "tests/test_subagent_tasks.py::test_subagent_queue_limit_and_queued_cancel",
            "tests/test_run_cancellation.py::test_live_daemon_cancel_foreground_preserves_background_work",
        ],
    },
    "M06": {
        "requirement": "文件回退不得覆盖用户在 Agent 修改之后的编辑。恢复前必须比较当前文件与记录的 after_hash，冲突保持文件原样；同文件连续修改逆序恢复，新建文件只在内容匹配时删除。保持备份完整性、锁及逐项结果。",
        "file": "src/mokioclaw/core/changes.py",
        "old": '            if item["path"] in blocked or digest(current) != item["after_hash"]:',
        "new": '            if item["path"] in blocked:',
        "tests": [
            "tests/test_project_optimization.py::test_rewind_conflict_new_file_quota_and_lock"
        ],
        "regression": [
            "tests/test_project_optimization.py::test_rewind_restores_bytes_modes_and_consecutive_edits",
            "tests/test_project_optimization.py::test_backup_gc_only_removes_closed_runs",
        ],
    },
    "M07": {
        "requirement": "为 grep(state, pattern, path='.', glob=None, head_limit=50, ignore_case=False, fixed_strings=False) 增加可选字面量搜索，GrepTool 暴露 fixed_strings: bool=False。开启时点、括号、方括号等按字面量匹配，兼容 ignore_case、中文路径、忽略规则、结果上限。rg 与 Python 后端的 matches 内容和顺序保持一致，旧正则默认行为不变。",
        "files": ["src/mokioclaw/tools/grep_tool.py", "src/mokioclaw/tools/registry.py"],
        "tests": ["tests/test_eval_literal.py"],
        "regression": [
            "tests/test_project_optimization.py::test_search_backends_share_ignore_scope_unicode_and_symlink_boundary",
            "tests/test_project_optimization.py::test_search_python_regex_fallback_and_limit",
        ],
    },
    "M08": {
        "requirement": "为 mokioclaw trace 增加 --since ISO8601 时间过滤，要求带 Z 或 UTC 偏移、包含边界时刻并按 UTC 比较。不合法或没有时区的参数返回 CLI 用法错误，不能启动任何运行。旧记录无时间戳或时间戳无效时，在有 --since 条件下跳过；无该参数时仍保持原输出。--follow 与其他筛选可组合。",
        "files": ["src/mokioclaw/cli/app.py", "src/mokioclaw/core/transport_trace.py"],
        "tests": ["tests/test_eval_since.py"],
        "regression": [
            "tests/test_reliability_extensions.py::test_transport_trace_incremental_read_old_records_and_cli",
            "tests/test_cli_smoke.py",
        ],
    },
    "E01": {"instance": "pallets__flask-4045", "regression": ["tests/test_blueprints.py"]},
    "E02": {"instance": "pallets__flask-4992", "regression": ["tests/test_config.py"]},
    "E03": {"instance": "pallets__flask-5063", "regression": ["tests/test_cli.py::TestRoutes"]},
    "E04": {
        "instance": "psf__requests-3362",
        "regression": [
            "tests/test_requests.py::TestCaseInsensitiveDict",
            "tests/test_requests.py::TestMorselToCookieExpires",
            "tests/test_requests.py::TestMorselToCookieMaxAge",
        ],
    },
}


def schedule(profile: str, seed: int = 20261016) -> list[tuple[str, Architecture, int]]:
    modes: tuple[Architecture, ...] = ("baseline", "full", "light", "auto")
    if profile == "core":
        rows = [(task, mode, repeat) for task in CORE for mode in modes for repeat in range(1, 4)]
    elif profile == "coverage":
        rows = [(task, "full", 1) for task in TASKS if task not in CORE]
    else:
        raise ValueError("profile must be core or coverage")
    random.Random(seed).shuffle(rows)
    return rows
