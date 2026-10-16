from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from mokioclaw.core.changes import ChangeStore, rewind
from mokioclaw.core.context_budget import ContextBudget, ContextBudgetError
from mokioclaw.core.context_compaction import compact_messages
from mokioclaw.core.instructions import inject_instructions
from mokioclaw.core.run_control import RunControl
from mokioclaw.core.run_inbox import RunInbox, RunRedirected
from mokioclaw.core.settings import configure_runtime
from mokioclaw.core.state import RuntimeState
from mokioclaw.providers.model_policy import RunBudgetExceeded, create_role_model
from mokioclaw.providers.openai_provider import invoke_streaming
from mokioclaw.tools.execution import execute_tool
from mokioclaw.tools.file_tools import edit_file, read_file, write_file
from mokioclaw.tools.grep_tool import file_glob, grep
from mokioclaw.tools.registry import build_tools


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))


class Reply:
    def __init__(self, response=None):
        self.response = response or AIMessage(content="完成")
        self.calls = 0
        self.messages = []

    def invoke(self, messages):
        self.calls += 1
        self.messages = list(messages)
        return self.response


def test_rules_refresh_before_write_and_stop_remaining_batch(tmp_path):
    (tmp_path / "AGENTS.md").write_text("Root coding convention")
    (tmp_path / "src").mkdir()
    (tmp_path / "src/AGENTS.md").write_text("Only this directory uses Chinese comments")
    runtime = RuntimeState(workspace=tmp_path)
    model, messages = Reply(), [HumanMessage(content="implement")]
    invoke_streaming(model, messages, state={"runtime": runtime})
    assert not write_file(runtime, "src/app.py", "# 中文")["ok"]
    assert not (tmp_path / "src/app.py").exists()
    tools = {t.name: t for t in build_tools(runtime)}
    assert (
        execute_tool(
            runtime,
            tools["FileWriteTool"],
            {"file_path": "other.py", "content": "stale"},
        )["error_type"]
        == "context_changed"
    )
    invoke_streaming(model, messages, state={"runtime": runtime})
    assert sum("MOKIO PROJECT INSTRUCTIONS" in str(m.content) for m in messages) == 1
    assert "Chinese comments" in str(model.messages)
    assert write_file(runtime, "src/app.py", "# 中文")["ok"]
    (tmp_path / "src/AGENTS.md").write_text("Changed convention")
    assert not edit_file(runtime, "src/app.py", "中文", "新内容")["ok"]


def test_rules_persist_through_compaction_and_cannot_be_silently_dropped(tmp_path):
    (tmp_path / "AGENTS.md").write_text("Always retain this requirement")
    runtime = RuntimeState(workspace=tmp_path)
    messages = [
        HumanMessage(content="task"),
        AIMessage(content="old observation" * 100),
    ]
    inject_instructions(runtime, messages)
    state = {"runtime": runtime, "task": "task"}
    compacted, _ = compact_messages(
        Reply(),
        messages,
        state,
        budget=ContextBudget(window=10000, output=100),
        summarizer=lambda *args: {"summary": "observed"},
    )
    assert any("Always retain" in str(m.content) for m in compacted)
    runtime.context_token_limit = 500
    runtime.model_context_reserve_tokens = 100
    runtime.context_safety_tokens = 0
    (tmp_path / "AGENTS.md").write_text("essential instruction " * 1000)
    model = Reply()
    with pytest.raises(ContextBudgetError):
        invoke_streaming(model, [HumanMessage(content="task")], state=state, allow_compaction=False)
    assert model.calls == 0


def test_toml_precedence_and_invalid_mode(tmp_path, monkeypatch):
    user = Path.home() / ".mokioclaw"
    user.mkdir()
    (user / "config.toml").write_text('[runtime]\nworkflow_mode="light"\nrun_token_limit=10000\n')
    project = tmp_path / ".mokioclaw"
    project.mkdir()
    (project / "config.toml").write_text('[runtime]\nworkflow_mode="auto"\n')
    runtime = RuntimeState(workspace=tmp_path)
    configure_runtime(runtime)
    assert runtime.workflow_mode == "auto" and runtime.run_token_limit == 10000
    monkeypatch.setenv("MOKIO_WORKFLOW_MODE", "light")
    configure_runtime(runtime, explicit={"workflow_mode": "full"})
    assert runtime.workflow_mode == "full"
    monkeypatch.setenv("MOKIO_WORKFLOW_MODE", "invalid")
    with pytest.raises(ValueError):
        configure_runtime(runtime)


def test_search_backends_share_ignore_scope_unicode_and_symlink_boundary(tmp_path):
    (tmp_path / ".gitignore").write_text("ignored/\n*.cache\n")
    (tmp_path / "ignored").mkdir()
    (tmp_path / "ignored/hidden.py").write_text("needle")
    (tmp_path / "src").mkdir()
    (tmp_path / "src/.gitignore").write_text("skip.py\n")
    (tmp_path / "src/skip.py").write_text("needle")
    (tmp_path / "src/中文.py").write_text("first\nneedle 中文\n")
    (tmp_path / "skip.cache").write_text("needle")
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.write_text("needle")
    (tmp_path / "external.py").symlink_to(outside)
    runtime = RuntimeState(workspace=tmp_path)
    fast = grep(runtime, "needle")
    runtime.settings = {"search": {"backend": "python"}}
    slow = grep(runtime, "needle")
    assert (
        fast["matches"]
        == slow["matches"]
        == [{"path": "src/中文.py", "line": 2, "text": "needle 中文"}]
    )
    assert file_glob(runtime, "*.py")["paths"] == ["src/中文.py"]
    assert grep(runtime, "needle", path="src")["matches"] == slow["matches"]


def test_search_python_regex_fallback_and_limit(tmp_path):
    (tmp_path / "a.txt").write_text("needle\nneedle\n")
    runtime = RuntimeState(workspace=tmp_path)
    result = grep(runtime, r"(?<=need)le", head_limit=1)
    assert len(result["matches"]) == 1 and result["truncated"]
    assert result["backend"] == "python"


def test_repo_agent_uses_only_observed_file_evidence(tmp_path, monkeypatch):
    from mokioclaw.agents import repo_agent
    from mokioclaw.agents.registry import registered_subagents, run_subagent

    (tmp_path / "app.py").write_text("def entry():\n    pass\n")

    class Explorer:
        def __init__(self):
            self.calls = 0

        def bind_tools(self, tools):
            assert {t.name for t in tools} == {
                "FileReadTool",
                "GrepTool",
                "FileGlobTool",
            }
            return self

        def invoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "GrepTool",
                            "id": "repo-1",
                            "args": {"pattern": "def entry"},
                        }
                    ],
                )
            return AIMessage(
                content=json.dumps(
                    {
                        "summary": "entry is in app.py",
                        "evidence": [{"reference": "invented.py:999"}],
                    }
                )
            )

    monkeypatch.setattr(repo_agent, "create_model", lambda: Explorer())
    runtime = RuntimeState(workspace=tmp_path, subagent_registry=registered_subagents())
    result = run_subagent({"runtime": runtime, "task": "explain entry"}, "repoAgent", "trace entry")
    assert result["state_updates"]["repo_explored"]
    assert result["evidence"][0]["reference"] == "app.py:1"
    assert "invented" not in str(result["evidence"])
    assert result["metadata"]["tool_calls_used"] == 1


def test_inbox_durable_idempotent_and_completion_gate(tmp_path):
    inbox = RunInbox(tmp_path / "inbox.json")
    assert inbox.submit("one", "change direction")["seq"] == 1
    assert inbox.submit("one", "change direction")["seq"] == 1
    assert inbox.submit("one", "different")["error_type"] == "message_id_conflict"
    assert not inbox.seal()
    runtime = RuntimeState(workspace=tmp_path)
    state = {"runtime": runtime, "todos": [{"status": "completed"}]}
    restored = RunInbox(inbox.path)
    restored.apply(state)
    restored.apply(state)
    assert len(state["latest_user_corrections"]) == 1
    assert state["todos"][0]["status"] == "completed"
    assert restored.seal()
    assert restored.submit("two", "late")["error_type"] == "run_finished"


def test_model_finishes_current_response_then_discards_pending_tools(tmp_path):
    control = RunControl()
    runtime = RuntimeState(workspace=tmp_path, run_control=control)

    class SlowReply:
        def stream(self, messages):
            from langchain_core.messages import AIMessageChunk

            yield AIMessageChunk(content="先")
            control.inbox.submit("new", "do not edit")
            yield AIMessageChunk(
                content="完成",
                tool_call_chunks=[
                    {
                        "id": "old",
                        "name": "FileWriteTool",
                        "args": '{"file_path":"old.txt","content":"old"}',
                        "index": 0,
                    }
                ],
            )

    events = []
    with pytest.raises(RunRedirected):
        invoke_streaming(SlowReply(), [], state={"runtime": runtime}, writer=events.append)
    assert "".join(e["text"] for e in events if e["type"] == "llm_token") == "先完成"
    assert any(
        e["type"] == "tool_result" and e["result"].get("status") == "not_executed" for e in events
    )
    assert not (tmp_path / "old.txt").exists()
    control.close()


def test_rewind_restores_bytes_modes_and_consecutive_edits(tmp_path):
    path = tmp_path / "a.txt"
    path.write_bytes(b"one\r\n")
    path.chmod(0o755)
    runtime = RuntimeState(workspace=tmp_path)
    read_file(runtime, "a.txt")
    assert edit_file(runtime, "a.txt", "one", "two")["ok"]
    assert edit_file(runtime, "a.txt", "two", "three")["ok"]
    preview = rewind(tmp_path, runtime.run_id)
    assert preview["ok"] and len(preview["changes"]) == 2
    assert path.read_text() == "three\n"
    restored = rewind(tmp_path, runtime.run_id, apply=True)
    assert restored["ok"] and path.read_bytes() == b"one\r\n"
    assert path.stat().st_mode & 0o777 == 0o755


def test_rewind_conflict_new_file_quota_and_lock(tmp_path):
    from mokioclaw.core.session_lock import session_writer

    runtime = RuntimeState(workspace=tmp_path)
    assert write_file(runtime, "new.txt", "agent")["ok"]
    (tmp_path / "new.txt").write_text("user")
    assert not rewind(tmp_path, runtime.run_id, apply=True)["ok"]
    assert (tmp_path / "new.txt").read_text() == "user"
    with session_writer(tmp_path), pytest.raises(RuntimeError, match="session_busy"):
        rewind(tmp_path, runtime.run_id, apply=True)
    (tmp_path / "new.txt").write_text("agent")
    assert rewind(tmp_path, runtime.run_id, apply=True)["ok"]
    assert not (tmp_path / "new.txt").exists()
    (tmp_path / "old.txt").write_text("large existing data")
    runtime.rewind_quota_bytes = 1
    read_file(runtime, "old.txt")
    assert not write_file(runtime, "old.txt", "changed")["ok"]
    assert (tmp_path / "old.txt").read_text() == "large existing data"


def test_backup_gc_only_removes_closed_runs(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("before")
    old = RuntimeState(workspace=tmp_path)
    read_file(old, "a.txt")
    assert write_file(old, "a.txt", "after")["ok"]
    ChangeStore(old).finish()
    new = RuntimeState(workspace=tmp_path, rewind_quota_bytes=5)
    read_file(new, "a.txt")
    assert write_file(new, "a.txt", "new")["ok"]
    assert not ChangeStore(old).manifest.exists()


def test_role_profile_legacy_fallback_and_token_stop(tmp_path):
    from mokioclaw.providers.model_policy import _SELECTION

    runtime = RuntimeState(workspace=tmp_path)
    assert create_role_model(lambda: _SELECTION.get(), runtime, "planner") == {}
    runtime.settings = {
        "model_profiles": {"cheap": {"context_tokens": 10000, "output_tokens": 100}},
        "model_roles": {"repoAgent": "cheap"},
    }
    assert create_role_model(lambda: _SELECTION.get(), runtime, "repoAgent")["output_tokens"] == 100
    model = Reply(
        AIMessage(
            content="ok",
            usage_metadata={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
        )
    )
    events = []
    invoke_streaming(model, [], state={"runtime": runtime}, node="planner", writer=events.append)
    assert runtime.model_metrics["total_tokens"] == 12
    assert runtime.model_metrics["roles"]["planner"]["requests"] == 1
    runtime.run_token_limit = 13
    with pytest.raises(RunBudgetExceeded):
        invoke_streaming(model, [], state={"runtime": runtime})
    assert model.calls == 1


def test_light_path_blocks_second_file_before_effect(tmp_path):
    runtime = RuntimeState(workspace=tmp_path, light_active=True)
    tools = {t.name: t for t in build_tools(runtime)}
    assert execute_tool(runtime, tools["FileWriteTool"], {"file_path": "a.txt", "content": "one"})[
        "ok"
    ]
    result = execute_tool(runtime, tools["FileWriteTool"], {"file_path": "b.txt", "content": "two"})
    assert result["error_type"] == "workflow_escalation_required"
    assert runtime.light_escalation and not (tmp_path / "b.txt").exists()


def test_runtime_configuration_precedence_covers_existing_modes(tmp_path, monkeypatch):
    from mokioclaw.core import agent
    from mokioclaw.core.agent import create_runtime

    monkeypatch.setattr(agent, "load_dotenv", lambda: None)
    for name in (
        "APPROVAL_MODE",
        "CHECKPOINT_MODE",
        "TRACE_MODE",
        "REWIND_QUOTA_BYTES",
        "RUN_TOKEN_LIMIT",
    ):
        monkeypatch.delenv("MOKIO_" + name, raising=False)
    config = tmp_path / ".mokioclaw/config.toml"
    config.parent.mkdir()
    config.write_text(
        '[runtime]\napproval_mode="deny"\ncheckpoint_mode="off"\ntrace_mode="off"\nrewind_quota_bytes=2048\nrun_token_limit=10000\n'
    )
    runtime = create_runtime(tmp_path)
    assert (runtime.approval_mode, runtime.checkpoint_mode, runtime.trace_mode) == (
        "deny",
        "off",
        "off",
    )
    monkeypatch.setenv("MOKIO_APPROVAL_MODE", "auto")
    monkeypatch.setenv("MOKIO_REWIND_QUOTA_BYTES", "4096")
    monkeypatch.setenv("MOKIO_RUN_TOKEN_LIMIT", "20000")
    runtime = create_runtime(tmp_path, approval_mode="inline", checkpoint_mode="strict")
    assert (runtime.approval_mode, runtime.checkpoint_mode, runtime.trace_mode) == (
        "inline",
        "strict",
        "off",
    )
    assert runtime.rewind_quota_bytes == 4096 and runtime.run_token_limit == 20000
    configure_runtime(runtime, explicit={"run_token_limit": 30000})
    assert runtime.run_token_limit == 30000
    config.write_text('[runtime]\nrun_control="forbidden"\n')
    with pytest.raises(ValueError, match="unknown runtime setting"):
        create_runtime(tmp_path)


def test_inbox_failed_persistence_never_acknowledges_receipt(tmp_path, monkeypatch):
    inbox = RunInbox(tmp_path / "inbox.json")
    original_save = inbox._save
    monkeypatch.setattr(inbox, "_save", lambda: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        inbox.submit("one", "不要修改数据库")
    assert not inbox.items
    monkeypatch.setattr(inbox, "_save", original_save)
    assert inbox.submit("one", "不要修改数据库")["status"] == "queued"
    assert RunInbox(inbox.path).pending()


def test_session_corrections_survive_reload_and_replay_once(tmp_path):
    from mokioclaw.core.run_inbox import merge_session_requirements
    from mokioclaw.core.session import load_or_create_session, save_session

    session = load_or_create_session(tmp_path)
    inbox = RunInbox(tmp_path / "inbox.json")
    inbox.submit("one", "不要修改数据库")
    runtime = RuntimeState(workspace=tmp_path)
    state = {"runtime": runtime, "todos": [{"id": "done", "status": "completed"}]}
    inbox.apply(state)
    restored = load_or_create_session(tmp_path)
    assert restored["user_requirements"][0]["message_id"] == "one"
    reloaded = RunInbox(inbox.path)
    assert reloaded.apply(state) == []
    merge_session_requirements(restored, reloaded)
    save_session(tmp_path, restored)
    assert len(load_or_create_session(tmp_path)["user_requirements"]) == 1
    assert state["todos"][0]["status"] == "completed"
    assert len(state["latest_user_corrections"]) == 1
    assert (
        "user_requirements" not in session
    )  # apply merged the durable session, not a stale object


def test_journal_finalization_failure_reports_unknown_outcome_and_can_rewind(tmp_path, monkeypatch):
    path = tmp_path / "app.txt"
    path.write_bytes(b"before\r\n")
    runtime = RuntimeState(workspace=tmp_path)
    read_file(runtime, "app.txt")
    monkeypatch.setattr(
        ChangeStore, "applied", lambda *_: (_ for _ in ()).throw(OSError("disk full"))
    )
    result = write_file(runtime, "app.txt", "after\n")
    assert not result["ok"] and result["error_type"] == "outcome_unknown"
    assert path.read_bytes() == b"after\n"
    assert rewind(tmp_path, runtime.run_id, apply=True)["ok"]
    assert path.read_bytes() == b"before\r\n"


def test_role_profile_output_reservation_uses_selected_window(tmp_path):
    class Profiled(Reply):
        def __init__(self):
            super().__init__()
            self.mokio_profile = {"context_tokens": 2000, "output_tokens": 64}

        def bind(self, **kwargs):
            self.options = kwargs
            return self

    runtime = RuntimeState(workspace=tmp_path, context_safety_tokens=64)
    model = Profiled()
    events = []
    invoke_streaming(
        model,
        [HumanMessage(content="hello")],
        state={"runtime": runtime},
        writer=events.append,
    )
    assert model.options == {"max_tokens": 64}
    budget = next(event for event in events if event["type"] == "context_budget")
    assert budget["window"] == 2000 and budget["reserved_tokens"] == 64
    with pytest.raises(ContextBudgetError):
        invoke_streaming(
            model,
            [HumanMessage(content="long" * 1000)],
            state={"runtime": runtime},
            allow_compaction=False,
        )
    assert model.calls == 1


def test_recursive_glob_matches_zero_directories_and_nested_files(tmp_path):
    (tmp_path / "root.txt").write_text("needle")
    (tmp_path / "src").mkdir()
    (tmp_path / "src/nested.txt").write_text("needle")
    runtime = RuntimeState(workspace=tmp_path)
    assert file_glob(runtime, "**/*.txt")["paths"] == ["root.txt", "src/nested.txt"]
    assert [m["path"] for m in grep(runtime, "needle", glob="**/*.txt")["matches"]] == [
        "root.txt",
        "src/nested.txt",
    ]


def test_directory_named_personal_is_still_scoped_to_workspace(tmp_path):
    from mokioclaw.core.instructions import instructions, restore_instructions

    folder = tmp_path / "personal"
    folder.mkdir()
    rule = folder / "AGENTS.md"
    rule.write_text("Scoped project convention")
    runtime = RuntimeState(workspace=tmp_path)
    instructions(runtime).refresh(folder / "app.py")
    restored = RuntimeState(workspace=tmp_path)
    restore_instructions(restored, instructions(runtime).manifest())
    assert "Scoped project convention" in instructions(restored).text()
    outside = tmp_path.parent / (tmp_path.name + "-external-rule")
    outside.write_text("Must not escape workspace")
    rule.unlink()
    rule.symlink_to(outside)
    with pytest.raises(ValueError, match="stay inside workspace"):
        instructions(runtime).refresh()
