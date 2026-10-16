from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from pathlib import Path
from threading import Lock
from typing import Any, ClassVar, Literal

from rich.pretty import Pretty
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Collapsible, Footer, Header, Input, Static

from mokioclaw.cli.event_summary import EventSummary, shorten, summarize_event
from mokioclaw.cli.tui.approval import ApprovalGate, ApprovalModal
from mokioclaw.cli.tui.input import TaskInput
from mokioclaw.cli.tui.logo import render_logo
from mokioclaw.core.agent import stream_session_events
from mokioclaw.core.approval import ApprovalDecision, ApprovalRequest
from mokioclaw.core.daemon_client import cancel_run, subagent_status, submit_run_message
from mokioclaw.core.paths import default_workspace
from mokioclaw.core.session import compact_session_history

StreamFactory = Callable[..., Iterable[dict[str, Any]]]


class AgentEventMessage(Message):
    def __init__(self, event: dict[str, Any]) -> None:
        super().__init__()
        self.event = event


class RunFinishedMessage(Message):
    def __init__(self, status: str) -> None:
        super().__init__()
        self.status = status


class ApprovalRequestedMessage(Message):
    def __init__(self, gate: ApprovalGate) -> None:
        super().__init__()
        self.gate = gate


class MokioClawTuiApp(App[None]):
    CSS = """
    Screen {
        background: #101113;
        color: #d7d1c9;
    }

    #root {
        height: 1fr;
    }

    #top {
        height: 8;
        border-bottom: solid #2f3437;
        padding: 0 2;
        background: #151719;
    }

    #logo {
        width: 32;
        height: 7;
        content-align: center middle;
    }

    #title-block {
        width: 1fr;
        height: 7;
        padding-left: 2;
        content-align: left middle;
    }

    #title {
        text-style: bold;
        color: #f3ede3;
    }

    #status {
        color: #9aa4a6;
    }

    #subtitle {
        color: #7fd6c2;
    }

    #body {
        height: 1fr;
    }

    #events {
        width: 1fr;
        height: 100%;
        border-right: solid #2f3437;
        padding: 1 1;
        background: #101113;
    }

    #sidebar {
        width: 36;
        min-width: 30;
        height: 100%;
        padding: 1 1;
        background: #151719;
    }

    #side-title {
        text-style: bold;
        color: #f4bf75;
        margin-bottom: 1;
    }

    #side-state {
        color: #d7d1c9;
    }

    #input-row {
        height: auto;
        min-height: 3;
        max-height: 12;
        border: round #4a8f86;
        padding: 0 1;
        background: #151719;
    }

    #prompt {
        width: 3;
        height: 1;
        content-align: center middle;
        color: #7fd6c2;
        text-style: bold;
    }

    #task-input {
        width: 1fr;
        height: 1;
        border: none;
        background: #151719;
        color: #f3ede3;
    }

    #hint {
        color: #8a9294;
        width: 32;
        height: 1;
        padding-left: 1;
        content-align: right middle;
    }

    .event-card {
        height: auto;
        min-height: 1;
        margin: 0 0 1 0;
        padding: 0 1;
        border-left: solid #3f474b;
    }

    .event-summary {
        height: auto;
        min-height: 1;
    }

    .event-running {
        border-left: solid #f4bf75;
    }

    .event-success {
        border-left: solid #7fd68a;
    }

    .event-error {
        border-left: solid #ef6f6c;
    }

    .event-info {
        border-left: solid #7fd6c2;
    }

    .event-user {
        border-left: solid #f4bf75;
        background: #222426;
    }

    .detail {
        height: auto;
        max-height: 12;
        color: #b7b0a8;
        padding: 0 1 1 1;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("ctrl+k", "compact_context", "Compact", priority=True),
        Binding("ctrl+c", "cancel_or_quit", "Cancel/Quit", priority=True),
        ("ctrl+l", "clear_events", "Clear"),
        ("ctrl+q", "quit", "Quit"),
    ]

    def __init__(
        self,
        *,
        initial_task: str | None = None,
        workspace: Path | None = None,
        max_attempts: int = 3,
        approval_mode: Literal["inline", "auto", "deny"] | None = None,
        checkpoint_mode: Literal["light", "strict", "off"] | None = None,
        trace_mode: Literal["on", "off"] | None = None,
        resume: Path | None = None,
        stream_factory: StreamFactory = stream_session_events,
        execution_backend: str | None = None,
        workflow_mode: str | None = None,
        skill: str | None = None,
    ) -> None:
        super().__init__()
        self.initial_task = initial_task
        self.workspace = resume or workspace or default_workspace()
        self.session_workspace = self.workspace
        self.max_attempts = max_attempts
        self.approval_mode = approval_mode
        self.checkpoint_mode = checkpoint_mode
        self.trace_mode = trace_mode
        self.resume = resume
        self.stream_factory = stream_factory
        self.execution_backend = execution_backend
        self.workflow_mode = workflow_mode
        self._initial_skill = skill
        self.running = False
        self.run_count = 0
        self.approval_count = 0
        self.failed_tool_count = 0
        self.tool_count = 0
        self.latest_workspace = str(self.session_workspace)
        self.latest_checkpoint = ""
        self.latest_trace = ""
        self.session_id = ""
        self.session_turn = 0
        self.last_route = ""
        self.sidebar_text = ""
        self.todos: list[dict[str, Any]] = []
        self._state_lock = Lock()
        self._llm_streams: dict[str, list[str]] = {}
        self._stream_cards = {}
        self._stream_widgets = {}
        self._dirty_streams = set()
        self._approval_gates = []
        self._cancel_requested = False
        self._known_subagents: dict[str, dict[str, Any]] = {}
        self.active_run_id: str | None = None
        self.latest_context_budget = "(waiting)"
        self.latest_context_compression = "(none)"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="root"):
            with Horizontal(id="top"):
                yield Static(render_logo(max_width=28, max_rows=7), id="logo")
                with Vertical(id="title-block"):
                    yield Static("MokioClaw", id="title")
                    yield Static("ready", id="status")
                    yield Static("coding session with context + harness", id="subtitle")
            with Horizontal(id="body"):
                yield VerticalScroll(id="events")
                with Vertical(id="sidebar"):
                    yield Static("Session", id="side-title")
                    yield Static("", id="side-state")
            with Horizontal(id="input-row"):
                yield Static("❯", id="prompt")
                yield TaskInput(
                    placeholder="Chat or ask for coding work, then press Enter",
                    id="task-input",
                )
                yield Static(
                    "Enter send · /new · /compact · Ctrl+K compact run · /subagents",
                    id="hint",
                )
            yield Static("", id="command-completion")
        yield Footer()

    def command_candidates(self):
        return getattr(self, "_command_items", [])

    def on_mount(self) -> None:
        self._command_items = [
            ("help", "命令说明"),
            ("new", "新建会话"),
            ("compact", "压缩上下文"),
            ("skills", "可用 Skill"),
            ("skill", "显式选择 Skill"),
            ("agents", "角色目录"),
            ("permissions", "审批规则"),
            ("changes", "文件变更"),
            ("rewind", "回退预览"),
            ("subagents", "后台任务"),
        ]
        self._skill_names = set()
        self._skill_diagnostics = []
        self._selected_skill = self._initial_skill
        try:
            from mokioclaw.core.agent import create_runtime
            from mokioclaw.tools.skills import discover_skills

            self._catalog_runtime = create_runtime(self.session_workspace)
            reserved = {name for name, _ in self._command_items}
            self._skill_diagnostics = []
            for skill in discover_skills(self._catalog_runtime):
                if skill.get("valid"):
                    self._skill_names.add(skill["name"])
                    import re

                    if skill["name"] in reserved:
                        self._skill_diagnostics.append(
                            {
                                "name": skill["name"],
                                "reason": "alias_conflicts_with_builtin; use /skill",
                                "source": skill.get("path"),
                            }
                        )
                    elif re.fullmatch(r"[\w-]+", skill["name"]):
                        self._command_items.append((skill["name"], skill["description"]))
        except (ValueError, OSError) as exc:
            self.notify(str(exc), severity="error")
        self.query_one("#command-completion", Static).display = False
        self.set_interval(0.05, self._flush_model_streams)
        self._write_welcome()
        self._refresh_sidebar()
        self.query_one("#task-input", TaskInput).focus()
        if self.initial_task:
            self.call_after_refresh(self.start_task, self.initial_task, self.resume)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "task-input":
            return
        task = event.value.strip()
        if task == "/compact":
            event.input.value = ""
            self.action_compact_context()
            return
        if not task:
            return
        if task == "/changes" or task.startswith("/rewind"):
            if self.running:
                self.notify("请先等待当前任务结束或取消，再回退文件。")
                return
            from mokioclaw.core.approval import make_approval_request
            from mokioclaw.core.changes import rewind

            workspace = Path(self.latest_workspace or self.workspace or self.session_workspace)
            run_id = task.split(maxsplit=1)[1] if " " in task else None
            try:
                result = rewind(workspace, run_id)
            except (OSError, ValueError, RuntimeError) as exc:
                self.notify(str(exc), severity="error")
                return
            event.input.value = ""
            self._mount_event_card(
                "文件变更 · FileWrite/FileEdit",
                json.dumps(result, ensure_ascii=False, indent=2),
                category="info",
                collapsed=False,
            )
            if task.startswith("/rewind") and result.get("changes"):

                def restore(approved):
                    if approved:
                        try:
                            restored = rewind(workspace, result["run_id"], apply=True)
                            self._mount_event_card(
                                "文件回退",
                                json.dumps(restored, ensure_ascii=False, indent=2),
                                category="info",
                                collapsed=False,
                            )
                        except (OSError, ValueError, RuntimeError) as exc:
                            self.notify(str(exc), severity="error")

                self.push_screen(
                    ApprovalModal(
                        make_approval_request(
                            "Restore file-tool edits",
                            "已展示差异；冲突文件不会被覆盖。",
                        ),
                        str(workspace),
                    ),
                    restore,
                )
            return
        if task in {"/help", "/skills", "/agents", "/permissions"} or task.startswith(
            "/permissions "
        ):
            if task == "/help":
                content = self.command_candidates()
            elif task == "/skills":
                from mokioclaw.tools.skills import discover_skills

                content = {
                    "skills": discover_skills(self._catalog_runtime),
                    "diagnostics": self._skill_diagnostics,
                }
            elif task == "/agents":
                content = {
                    "roles": [
                        {
                            "name": name,
                            "description": self._catalog_runtime.subagent_registry.get(
                                name
                            ).description,
                            "tools": sorted(
                                self._catalog_runtime.subagent_registry.get(name).allowed_tools
                            ),
                            "source": self._catalog_runtime.agent_profiles.get(name, {}).get(
                                "source", "Python registration"
                            ),
                        }
                        for name in self._catalog_runtime.subagent_registry.names()
                    ],
                    "profiles": self._catalog_runtime.agent_profiles,
                    "diagnostics": self._catalog_runtime.agent_profile_diagnostics,
                }
            else:
                from mokioclaw.core.daemon_client import permissions

                parts = task.split()
                action = parts[1] if len(parts) > 1 else "list"
                if action not in {"list", "revoke", "clear"} or (
                    action == "revoke" and len(parts) != 3
                ):
                    self.notify("用法：/permissions [list|revoke ID|clear]", severity="error")
                    return
                content = permissions(
                    self.session_workspace,
                    action=action,
                    rule_id=parts[2] if action == "revoke" else None,
                    session_id=self.session_id,
                )
            self._mount_event_card(
                task,
                json.dumps(content, ensure_ascii=False, indent=2),
                category="info",
                collapsed=False,
            )
            event.input.value = ""
            return
        if task == "/new" and self.running:
            self.notify("当前运行结束后才能新建会话。")
            return
        if task == "/skill":
            self.notify("用法：/skill 名称 任务参数")
            return
        selected_skill = None
        if task.startswith("/skill "):
            parts = task.split(maxsplit=2)
            selected_skill = parts[1]
            task = parts[2] if len(parts) > 2 else f"使用 {selected_skill} Skill 完成任务"
        elif task.startswith("/") and task[1:].split(maxsplit=1)[0] in self._skill_names:
            parts = task[1:].split(maxsplit=1)
            selected_skill = parts[0]
            task = parts[1] if len(parts) > 1 else f"使用 {selected_skill} Skill 完成任务"
        if selected_skill:
            if self.running:
                self.notify("当前运行结束后才能选择新的 Skill。", severity="warning")
                return
            if selected_skill not in self._skill_names:
                self.notify("Skill 不存在或配置无效。", severity="error")
                return
            self._selected_skill = selected_skill
        else:
            self._selected_skill = None
        if self.running:
            if not self.active_run_id:
                self.notify("等待运行建立后即可发送补充要求。")
                return
            from uuid import uuid4

            run_id, message_id = self.active_run_id, uuid4().hex
            event.input.value = ""

            def send():
                try:
                    result = submit_run_message(run_id, message_id, task)
                except Exception as exc:  # noqa: BLE001 — fault boundary; control signals are handled separately
                    result = {"ok": False, "error": str(exc)}
                self._post_from_worker(
                    AgentEventMessage(
                        {
                            "type": "custom_event",
                            "event": {
                                "type": "user_message_response",
                                "run_id": run_id,
                                **result,
                            },
                        }
                    )
                )

            self.run_worker(send, thread=True, group="run-input")
            return
        event.input.value = ""
        if task == "/new":
            self.start_new_session()
            return
        if task == "/subagents":
            if not self._known_subagents:
                self.notify("No background Subagents observed in this TUI session.")
            for run_id in list(self._known_subagents):
                try:
                    snapshot = subagent_status(run_id)
                    self._known_subagents[run_id] = snapshot
                    self._mount_event_card(
                        f"Subagent · {run_id}",
                        json.dumps(snapshot, ensure_ascii=False, indent=2),
                        category="info",
                        collapsed=False,
                    )
                except Exception as exc:  # noqa: BLE001 — fault boundary; control signals are handled separately
                    self.notify(f"Could not query {run_id}: {exc}")
            return
        if task.startswith("/subagent "):
            run_id = task.split(maxsplit=1)[1].strip()
            try:
                snapshot = subagent_status(run_id)
                self._known_subagents[run_id] = snapshot
                self._mount_event_card(
                    f"Subagent · {run_id}",
                    json.dumps(snapshot, ensure_ascii=False, indent=2),
                    category="info",
                    collapsed=False,
                )
            except Exception as exc:  # noqa: BLE001 — fault boundary; control signals are handled separately
                self.notify(f"Could not query {run_id}: {exc}")
            return
        self.start_task(task, None)

    def _flush_model_streams(self) -> None:
        for stream_id in list(self._dirty_streams):
            self._dirty_streams.discard(stream_id)
            parts = self._llm_streams.get(stream_id)
            card = self._stream_cards.get(stream_id)
            if parts is not None and card is not None:
                content = "".join(parts)
                preview, full = self._stream_widgets[stream_id]
                preview.update(Text(content[-180:] or "Waiting for text…"))
                full.update(Text(content))

    def _release_approval_gates(self) -> None:
        with self._state_lock:
            gates, self._approval_gates = self._approval_gates, []
        for gate in gates:
            gate.resolve(False)
        if self.screen_stack and isinstance(self.screen, ApprovalModal):
            self.screen.dismiss(False)

    def on_unmount(self) -> None:
        self._release_approval_gates()

    def on_agent_event_message(self, message: AgentEventMessage) -> None:
        envelope = message.event
        if envelope.get("type") in {"run_cancel_requested", "ui_cancel_result"}:
            if envelope.get("run_id") and envelope["run_id"] != self.active_run_id:
                return
            if envelope.get("ok", True):
                self._release_approval_gates()
                self.query_one("#status", Static).update("cancelling")
            else:
                self._cancel_requested = False
                self.notify(str(envelope.get("error", "Cancel failed")), severity="error")
            return
        payload = envelope.get("event") if envelope.get("type") == "custom_event" else None
        if isinstance(payload, dict) and payload.get("type") in {
            "approvals_superseded",
            "user_message_response",
        }:
            if payload.get("run_id") and payload["run_id"] != self.active_run_id:
                return
            if payload.get("ok", True):
                self._release_approval_gates()
        if isinstance(payload, dict) and payload.get("type") == "run_started":
            self.active_run_id = payload.get("run_id")
            if self._cancel_requested:
                self._send_cancel()
        if isinstance(payload, dict) and payload.get("type") in {
            "llm_stream_start",
            "llm_token",
            "llm_stream_end",
        }:
            node = str(payload.get("node", "model"))
            stream_id = str(payload.get("stream_id") or node)
            if stream_id not in self._stream_cards:
                preview = Static(Text("Waiting for text…"), classes="model-preview")
                full = Static(Text(""), classes="model-full")
                self._stream_widgets[stream_id] = (preview, full)
                card = Collapsible(full, title=f"Model · {node} · running", collapsed=True)
                container = Vertical(preview, card, classes="event-card event-info")
                container.styles.height = "auto"
                self._stream_cards[stream_id] = card
                self._llm_streams[stream_id] = []
                self.query_one("#events", VerticalScroll).mount(container)
            if payload["type"] == "llm_token":
                self._llm_streams[stream_id].append(str(payload.get("text", "")))
                self._dirty_streams.add(stream_id)
            elif payload["type"] == "llm_stream_end":
                if payload.get("full_output_path"):
                    from mokioclaw.core.artifacts import read_diagnostic_text

                    text = read_diagnostic_text(
                        Path(self.latest_workspace), payload["full_output_path"]
                    )
                    if text is not None:
                        self._llm_streams[stream_id] = [text]
                self._dirty_streams.add(stream_id)
                self._flush_model_streams()
                card = self._stream_cards.pop(stream_id)
                self._stream_widgets.pop(stream_id, None)
                card.title = f"Model · {node} · {payload.get('status', 'finished')}"
                self._llm_streams.pop(stream_id, None)
                status = payload.get("status", "finished")
                self.query_one("#status", Static).update(
                    "cancelling"
                    if self._cancel_requested
                    else ("running" if status == "finished" else status)
                )
            return
        self._handle_event(envelope)

    def on_run_finished_message(self, message: RunFinishedMessage) -> None:
        self.running = False
        self._release_approval_gates()
        self._flush_model_streams()
        for card in self._stream_cards.values():
            card.title = f"{card.title.rsplit(' · ', 1)[0]} · {message.status}"
        self._stream_cards.clear()
        self._stream_widgets.clear()
        self._llm_streams.clear()
        self.resume = None
        self.query_one("#task-input", TaskInput).disabled = False
        self.query_one("#task-input", TaskInput).focus()
        self.query_one("#status", Static).update(f"{message.status}; ready for next task")
        self._refresh_sidebar()

    def on_approval_requested_message(self, message: ApprovalRequestedMessage) -> None:
        if self._cancel_requested:
            message.gate.resolve(False)
            return
        with self._state_lock:
            if message.gate not in self._approval_gates:
                self._approval_gates.append(message.gate)
        workspace = self.latest_workspace or str(self.workspace or "")
        self.push_screen(
            ApprovalModal(message.gate.request, workspace, remember=True),
            self._resolve_approval(message.gate),
        )

    def action_cancel_or_quit(self) -> None:
        if not self.running:
            self.exit()
            return
        if self._cancel_requested:
            return
        self._cancel_requested = True
        self.query_one("#status", Static).update("cancelling")
        if self.active_run_id:
            self._send_cancel()

    def _send_cancel(self) -> None:
        run_id = self.active_run_id

        def cancel():
            try:
                result = cancel_run(run_id)
            except Exception as exc:  # noqa: BLE001 — fault boundary; control signals are handled separately
                result = {"ok": False, "error": str(exc)}
            self._post_from_worker(
                AgentEventMessage({**result, "run_id": run_id, "type": "ui_cancel_result"}),
            )

        self.run_worker(cancel, thread=True, group="cancel-run")

    def action_clear_events(self) -> None:
        self.query_one("#events", VerticalScroll).remove_children()
        self._write_welcome()

    def action_compact_context(self) -> None:
        if self.running and not self.active_run_id:
            self.notify("Run is starting; request compact again once its ID is available.")
            return
        run_id = self.active_run_id if self.running else None
        workspace = self.session_workspace

        def compact() -> None:
            try:
                if run_id:
                    from mokioclaw.core.daemon_client import compact_run

                    result = compact_run(run_id)
                    event = {**result, "type": "context_compact_request"}
                else:
                    result = compact_session_history(workspace)
                    event = {
                        **result,
                        "type": "context_compression",
                        "reason": "manual",
                        "agent": "session",
                    }
            except Exception as exc:  # noqa: BLE001 - surface worker failures as UI events
                event = {
                    "type": "context_compression_failed",
                    "status": "failed",
                    "scope": "run" if run_id else "session",
                    "error": str(exc),
                }
            self.call_from_thread(
                self.post_message,
                AgentEventMessage({"type": "custom_event", "event": event}),
            )

        self.run_worker(compact, thread=True, exclusive=True, group="context-compact")

    def start_task(self, task: str, resume: Path | None = None) -> None:
        if self.running:
            self.notify("MokioClaw is already running a task.", severity="warning")
            return
        self.running = True
        self.active_run_id = None
        self._cancel_requested = False
        self.run_count += 1
        self.todos = []
        self.failed_tool_count = 0
        self.tool_count = 0
        self.query_one("#task-input", TaskInput).disabled = False
        self.query_one("#status", Static).update("running")
        self._refresh_sidebar()
        self._write_run_start(task, resume)
        self.run_worker(
            lambda: self._run_stream(task, resume),
            thread=True,
            exclusive=False,
            name=f"mokioclaw-run-{self.run_count}",
        )

    def _post_from_worker(self, message) -> bool:
        if not self.is_running:
            return False
        try:
            self.call_from_thread(self.post_message, message)
        except RuntimeError:
            if self.is_running:
                raise
            return False
        return True

    def _run_stream(self, task: str, resume: Path | None) -> None:
        status = "finished"
        try:
            approval_handler = (
                self._approval_handler if self.approval_mode in {None, "inline"} else None
            )
            for event in self.stream_factory(
                task,
                session_workspace=self.session_workspace,
                **({"skill": self._selected_skill} if self._selected_skill else {}),
                max_attempts=self.max_attempts,
                approval_mode=self.approval_mode,
                approval_handler=approval_handler,
                checkpoint_mode=self.checkpoint_mode,
                resume_workspace=resume,
                trace_mode=self.trace_mode,
                **({"execution_backend": self.execution_backend} if self.execution_backend is not None else {}),
                **({"workflow_mode": self.workflow_mode} if self.workflow_mode is not None else {}),
                use_daemon=True,
            ):
                payload = event.get("event", {})
                if event.get("type") == "daemon_run_finished":
                    status = event.get("status", status)
                elif isinstance(payload, dict) and payload.get("type") == "run_finished":
                    status = payload.get("status", status)
                if not self._post_from_worker(AgentEventMessage(event)):
                    return
        except KeyboardInterrupt:
            status = "interrupted"
        except Exception as exc:  # noqa: BLE001 — fault boundary; control signals are handled separately
            status = "failed"
            error_event = {
                "type": "custom_event",
                "event": {"type": "tui_error", "error": f"{type(exc).__name__}: {exc}"},
            }
            self._post_from_worker(AgentEventMessage(error_event))
        finally:
            self._post_from_worker(RunFinishedMessage(status))

    def _approval_handler(self, request: ApprovalRequest) -> ApprovalDecision:
        gate = ApprovalGate(request)
        with self._state_lock:
            self._approval_gates.append(gate)
        if not self._post_from_worker(ApprovalRequestedMessage(gate)):
            gate.resolve(False)
        return gate.wait()

    def _resolve_approval(self, gate: ApprovalGate) -> Callable[[bool | None], None]:
        def resolve(result: bool | None) -> None:
            approved = bool(result)
            gate.resolve(approved)
            with self._state_lock:
                if gate in self._approval_gates:
                    self._approval_gates.remove(gate)
            self._refresh_sidebar()

        return resolve

    def _handle_event(self, event: dict[str, Any]) -> None:
        self._update_state_from_event(event)
        if self._should_hide_event(event):
            self._refresh_sidebar()
            return
        summary = summarize_event(event)
        self._write_summary(summary)
        self._refresh_sidebar()

    def _update_state_from_event(self, event: dict[str, Any]) -> None:
        with self._state_lock:
            if event.get("type") == "workspace":
                self.active_run_id = event.get("run_id") or self.active_run_id
                self.latest_workspace = str(event.get("path", ""))
                self.session_workspace = Path(self.latest_workspace)
                return
            payload = event.get("event")
            if event.get("type") == "graph_event" and isinstance(payload, dict):
                for update in payload.values():
                    if isinstance(update, dict):
                        self._update_from_payload(update)
            elif event.get("type") == "custom_event" and isinstance(payload, dict):
                self._update_from_payload(payload)
            elif isinstance(event.get("subagent_run_id"), str):
                self._update_from_payload(event)

    def _update_from_payload(self, payload: dict[str, Any]) -> None:
        if payload.get("type") == "context_budget":
            self.latest_context_budget = f"{payload.get('input_tokens')} / {payload.get('token_limit')} est. · {payload.get('agent', '')}"
        if payload.get("type") == "context_compression":
            self.latest_context_compression = f"{payload.get('before_tokens')} → {payload.get('after_tokens')} est. · {payload.get('scope', 'run')}"
        if payload.get("type") == "context_compression_failed":
            self.latest_context_compression = "failed: " + str(payload.get("error", ""))[:80]
        if isinstance(payload.get("todos"), list):
            self.todos = payload["todos"]
        if payload.get("type") == "tool_call":
            self.tool_count += 1
        if payload.get("type") == "tool_result":
            result = payload.get("result")
            if isinstance(result, dict):
                if result.get("ok") is False:
                    self.failed_tool_count += 1
                if result.get("requires_approval"):
                    self.approval_count += 1
        if payload.get("type") == "checkpoint_saved":
            self.latest_checkpoint = str(payload.get("path", ""))
        if payload.get("type") == "trace_summary":
            self.latest_trace = str(payload.get("trace_dir", ""))
        if payload.get("type") == "session_started":
            self.session_id = str(payload.get("session_id", ""))
            self.session_turn = int(payload.get("turn_index", 0) or 0)
            self.latest_workspace = str(payload.get("workspace", self.latest_workspace))
        if payload.get("type") == "session_turn_started":
            self.session_turn = int(payload.get("turn", self.session_turn) or self.session_turn)
        if payload.get("type") == "session_turn_saved":
            self.session_turn = int(payload.get("turn", self.session_turn) or self.session_turn)
            self.last_route = str(payload.get("route", self.last_route))
        if payload.get("subagent_run_id"):
            run_id = str(payload["subagent_run_id"])
            self._known_subagents[run_id] = {
                **self._known_subagents.get(run_id, {}),
                **payload,
            }

    def _write_welcome(self) -> None:
        self._mount_event_card(
            "MokioClaw",
            "Ask for a quick answer or coding work. Use /new for a fresh workspace, /compact to summarize older session turns.",
            category="info",
            collapsed=True,
            detail="Persistent TUI sessions keep one workspace across turns. Workflow turns still use approval, checkpoint, trace, and layered memory.",
        )

    def _write_run_start(self, task: str, resume: Path | None) -> None:
        mode = f"resume: {resume}" if resume is not None else f"workspace: {self.session_workspace}"
        self._mount_event_card(
            f"You · turn {self.run_count}",
            shorten(task, 500),
            category="user",
            collapsed=False,
            detail=mode,
        )

    def _write_summary(self, summary: EventSummary) -> None:
        self._mount_event_card(
            summary.title,
            self._compact_body(summary),
            category=self._event_category(summary),
            collapsed=self._should_collapse(summary),
            detail=summary.body,
        )

    def _refresh_sidebar(self) -> None:
        status = "running" if self.running else "ready"
        workspace_path = self.latest_workspace or str(self.session_workspace)
        workspace = f"{Path(workspace_path).name} · {shorten(workspace_path, 64)}"
        checkpoint = shorten(self.latest_checkpoint or "(waiting)", 80)
        trace_path = self.latest_trace or "(waiting)"
        trace = (
            f"{Path(trace_path).name} · {shorten(trace_path, 64)}"
            if self.latest_trace
            else trace_path
        )
        tools = f"{self.tool_count} total / {self.failed_tool_count} failed"
        approvals = str(self.approval_count)
        todos = self._todo_sidebar_text()
        self.sidebar_text = "\n".join(
            [
                f"status {status}",
                f"turns {self.run_count}",
                f"session {self.session_id}",
                f"route {self.last_route or '(none)'}",
                f"workspace {workspace}",
                f"checkpoint {checkpoint}",
                f"trace {trace}",
                f"tools {tools}",
                f"approvals {approvals}",
                f"todos {todos}",
                f"context {self.latest_context_budget}",
                f"compact {self.latest_context_compression}",
            ]
        )
        table = Table.grid(padding=(0, 1))
        table.add_column(style="bold cyan", no_wrap=True)
        table.add_column()
        table.add_row("status", status)
        table.add_row("turns", str(self.run_count))
        table.add_row("session", shorten(self.session_id or "(starting)", 24))
        table.add_row("route", self.last_route or "(none)")
        table.add_row("workspace", workspace)
        table.add_row("checkpoint", checkpoint)
        table.add_row("trace", trace)
        table.add_row("tools", tools)
        table.add_row("approvals", approvals)
        table.add_row("todos", todos)
        table.add_row("context", self.latest_context_budget)
        table.add_row("compact", self.latest_context_compression)
        self.query_one("#side-state", Static).update(table)

    def _todo_sidebar_text(self) -> str:
        if not self.todos:
            return "(none yet)"
        counts: dict[str, int] = {}
        for todo in self.todos:
            status = str(todo.get("status", "pending"))
            counts[status] = counts.get(status, 0) + 1
        current = next((todo for todo in self.todos if todo.get("status") == "in_progress"), None)
        count_text = ", ".join(f"{key}:{value}" for key, value in sorted(counts.items()))
        if current:
            return f"{count_text}\n{shorten(current.get('content', current.get('description', '')), 120)}"
        return count_text

    def _mount_event_card(
        self,
        title: str,
        body: str,
        *,
        category: str = "info",
        collapsed: bool = True,
        detail: str | None = None,
    ) -> None:
        events = self.query_one("#events", VerticalScroll)
        title_text = f"{self._category_marker(category)} {title}"
        summary = Static(
            Text(body or " ", style=self._category_style(category)),
            classes="event-summary",
        )
        detail_text = detail if detail is not None else body
        if collapsed:
            card = Collapsible(
                summary,
                Static(self._detail_renderable(detail_text), classes="detail"),
                title=title_text,
                collapsed=True,
                classes=f"event-card {self._category_class(category)}",
            )
        else:
            card = Vertical(
                Static(Text(title_text, style=f"bold {self._category_style(category)}")),
                summary,
                classes=f"event-card {self._category_class(category)}",
            )
            card.styles.height = "auto"
        events.mount(card)
        events.scroll_end(animate=False)

    def _compact_body(self, summary: EventSummary) -> str:
        title = summary.title
        body = summary.body or ""
        if summary.category == "session":
            return self._first_matching_line(
                body, ("route:", "turn:", "workspace:", "session:")
            ) or shorten(body, 140)
        if summary.category == "intent":
            route = self._line_value(body, "route")
            reason = self._line_value(body, "reason")
            return f"route {route or 'workflow'}" + (f" · {shorten(reason, 90)}" if reason else "")
        if summary.category == "chat":
            return shorten(body.split("\nmode:")[0], 2400)
        if summary.category == "plan":
            todos = self._line_value(body, "todos")
            first = body.splitlines()[0] if body.splitlines() else title
            return shorten(first + (f" · todos {todos}" if todos else ""), 180)
        if summary.category == "tool_call":
            return shorten(body, 160)
        if summary.category == "tool_result":
            ok = self._line_value(body, "ok")
            path = self._line_value(body, "path") or self._line_value(body, "stdout_path")
            pieces = [f"ok={ok}" if ok else "tool result"]
            if path:
                pieces.append(path)
            return shorten(" · ".join(pieces), 180)
        if summary.category == "handoff":
            return shorten(body, 180)
        if summary.category == "memory":
            return "memory snapshot updated"
        if summary.category == "context":
            primary = self._first_matching_line(body, ("tokens:", "compress:", "next:"))
            scope = self._first_matching_line(body, ("scope:", "status:"))
            return shorten(" · ".join(part for part in (primary, scope) if part), 200) or shorten(
                body, 160
            )
        if summary.category == "checkpoint":
            status = self._line_value(body, "status") or self._line_value(body, "mode")
            return f"checkpoint {status}" if status else "checkpoint updated"
        if summary.category == "trace":
            status = self._line_value(body, "status")
            tools = self._line_value(body, "tools")
            return " · ".join(
                part
                for part in [
                    f"status {status}" if status else "",
                    f"tools {tools}" if tools else "",
                ]
                if part
            )
        if summary.category == "final":
            return shorten(body.splitlines()[0] if body.splitlines() else body, 220)
        if summary.category == "verifier":
            return shorten(body.splitlines()[0] if body.splitlines() else body, 180)
        return shorten(body, 180)

    def _should_collapse(self, summary: EventSummary) -> bool:
        return summary.category not in {"chat", "final", "verifier"}

    def _event_category(self, summary: EventSummary) -> str:
        if summary.category in {"final", "trace"}:
            return "success"
        if summary.category in {"verifier", "tool_result"} and "FAIL" in summary.body:
            return "error"
        if summary.category in {
            "plan",
            "tool_call",
            "handoff",
            "context",
            "checkpoint",
        }:
            return "running"
        return "info"

    def _should_hide_event(self, event: dict[str, Any]) -> bool:
        if event.get("type") == "workspace":
            return True
        payload = event.get("event")
        if event.get("type") == "graph_event" and isinstance(payload, dict):
            hidden_nodes = {"intent_router", "chat_responder"}
            return all(node in hidden_nodes for node in payload)
        if event.get("type") == "custom_event" and isinstance(payload, dict):
            return payload.get("type") in {
                "session_started",
                "session_turn_started",
                "memory_snapshot",
            }
        return False

    def _detail_renderable(self, detail: str) -> Any:
        text = detail or "(no details)"
        if len(text) > 1600:
            text = text[:1597] + "..."
        try:
            parsed = json.loads(text)
        except (TypeError, json.JSONDecodeError):
            return Text(text)
        return Pretty(parsed, max_depth=4)

    def _category_marker(self, category: str) -> str:
        return {
            "running": "•",
            "success": "✓",
            "error": "!",
            "info": "·",
            "user": ">",
        }.get(category, "·")

    def _category_style(self, category: str) -> str:
        return {
            "running": "#f4bf75",
            "success": "#7fd68a",
            "error": "#ef6f6c",
            "info": "#7fd6c2",
            "user": "#f3ede3",
        }.get(category, "#d7d1c9")

    def _category_class(self, category: str) -> str:
        return {
            "running": "event-running",
            "success": "event-success",
            "error": "event-error",
            "info": "event-info",
            "user": "event-user",
        }.get(category, "event-info")

    def _line_value(self, body: str, key: str) -> str:
        prefix = f"{key}:"
        for line in body.splitlines():
            if line.strip().startswith(prefix):
                return line.split(":", 1)[1].strip()
        return ""

    def _first_matching_line(self, body: str, prefixes: tuple[str, ...]) -> str:
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith(prefixes):
                return stripped
        return ""

    def start_new_session(self) -> None:
        if self.running:
            self.notify("MokioClaw is already running a task.", severity="warning")
            return
        self.workspace = default_workspace()
        self.session_workspace = self.workspace
        self.resume = None
        self.latest_workspace = str(self.session_workspace)
        self.latest_checkpoint = ""
        self.latest_trace = ""
        self.session_id = ""
        self.session_turn = 0
        self.last_route = ""
        self.todos = []
        self.failed_tool_count = 0
        self.tool_count = 0
        self.approval_count = 0
        self.latest_context_budget = "(waiting)"
        self.latest_context_compression = "(none)"
        self._refresh_sidebar()
        self._mount_event_card(
            "New Session",
            str(self.session_workspace),
            category="info",
            collapsed=False,
        )
