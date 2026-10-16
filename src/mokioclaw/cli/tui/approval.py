from __future__ import annotations

from dataclasses import dataclass
from threading import Event

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Container, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Static

from mokioclaw.core.approval import ApprovalDecision, ApprovalRequest


@dataclass
class ApprovalGate:
    request: ApprovalRequest
    decision: ApprovalDecision | None = None

    def __post_init__(self) -> None:
        self._ready = Event()

    def resolve(self, approved: bool | ApprovalDecision) -> None:
        reason = "" if approved else "Rejected by human operator."
        self.decision = (
            approved
            if isinstance(approved, ApprovalDecision)
            else ApprovalDecision(approved=approved, reason=reason)
        )
        self._ready.set()

    def wait(self) -> ApprovalDecision:
        self._ready.wait()
        return self.decision or ApprovalDecision(approved=False, reason="Approval dialog closed.")


class ApprovalModal(ModalScreen[bool | ApprovalDecision]):
    BINDINGS = [
        ("y", "approve", "Approve"),
        ("enter", "approve", "Approve"),
        ("n", "deny", "Deny"),
        ("escape", "deny", "Deny"),
    ]

    DEFAULT_CSS = """
    ApprovalModal {
        align: center middle;
    }

    ApprovalModal #approval-dialog {
        width: 74;
        max-width: 90%;
        max-height: 90%;
        overflow-y: auto;
        height: auto;
        border: round $warning;
        background: $surface;
        padding: 1 2;
    }

    ApprovalModal #approval-title {
        text-style: bold;
        color: $warning;
        margin-bottom: 1;
    }

    ApprovalModal #approval-command {
        border: tall $panel;
        padding: 1;
        margin: 1 0;
        max-height: 10;
    }

    ApprovalModal Horizontal {
        height: 3;
    }

    ApprovalModal #approval-buttons {
        height: auto;
        margin-top: 1;
    }
    """

    def __init__(self, request: ApprovalRequest, workspace: str, *, remember: bool = False) -> None:
        super().__init__()
        self.request = request
        self.workspace = workspace
        self.remember = remember

    def compose(self) -> ComposeResult:
        with Container(id="approval-dialog"):
            yield Static(f"Human Approval · {self.request.tool_name}", id="approval-title")
            yield Static(f"Risk: {self.request.risk_reason}")
            yield Static(f"Workspace: {self.workspace}")
            yield Static(Text(self.request.command, overflow="fold"), id="approval-command")
            yield Static("Approve this tool call?  y/Enter = approve · n/Esc = deny")
            with Horizontal(id="approval-buttons"):
                yield Button("Approve", variant="success", id="approve")
                yield Button("Deny", variant="error", id="deny")
            if self.remember:
                yield Static(Text("Rule scope: " + self.request.rule_preview))
                if self.request.tool_name in {"FileReadTool", "FileWriteTool", "FileEditTool"}:
                    from pathlib import PurePosixPath

                    directory = str(
                        PurePosixPath(self.request.rule_preview.removeprefix("file:")).parent
                    )
                    yield Checkbox("保存到此目录及其子目录：" + directory, id="approval-directory")
                with Horizontal():
                    yield Button("允许本会话", id="allow-session")
                    yield Button("拒绝本会话", id="deny-session")
                with Horizontal():
                    yield Button("允许此项目资源", id="allow-project")
                    yield Button("拒绝此项目资源", id="deny-project")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        identity = event.button.id or "deny"
        if identity in {"allow-session", "deny-session", "allow-project", "deny-project"}:
            self.dismiss(
                ApprovalDecision(identity.startswith("allow"), scope=identity.split("-")[1])
            )
        else:
            self.dismiss(identity == "approve")

    def on_key(self, event: events.Key) -> None:
        if event.key in {"y", "enter"}:
            self.dismiss(True)
        elif event.key in {"n", "escape"}:
            self.dismiss(False)

    def action_approve(self) -> None:
        self.dismiss(True)

    def action_deny(self) -> None:
        self.dismiss(False)
