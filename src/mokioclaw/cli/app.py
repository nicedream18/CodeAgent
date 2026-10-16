from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Literal

import typer
from rich import box
from rich.panel import Panel
from typer.core import TyperGroup

from mokioclaw.cli.formatter import print_event, safe_echo, safe_secho
from mokioclaw.core.agent import stream_agent_events
from mokioclaw.core.approval import ApprovalDecision, ApprovalRequest
from mokioclaw.core.daemon_client import (
    cancel_run,
    daemon_status,
    iter_run_events,
    iter_subagent_events,
    stop_daemon,
    subagent_cancel,
    subagent_status,
)
from mokioclaw.core.session import compact_session_history


class MokioClawGroup(TyperGroup):
    """Let ``mokioclaw "task"`` coexist with real subcommands."""

    def parse_args(self, ctx, args):  # type: ignore[no-untyped-def]
        commands = set(self.commands)
        remaining: list[str] = []
        task_parts: list[str] = []
        index = 0
        while index < len(args):
            arg = args[index]
            if arg in commands or arg == "--help":
                remaining.extend(args[index:])
                break
            if arg.startswith("-"):
                remaining.append(arg)
                if "=" not in arg and index + 1 < len(args) and not args[index + 1].startswith("-"):
                    remaining.append(args[index + 1])
                    index += 2
                    continue
                index += 1
                continue
            task_parts.extend(args[index:])
            break
        if task_parts:
            ctx.obj = dict(ctx.obj or {})
            ctx.obj["task_arg"] = " ".join(task_parts)
        return super().parse_args(ctx, remaining)


app = typer.Typer(
    cls=MokioClawGroup,
    help='mokioclaw: a teaching-first mini CodeAgent. Use `mokioclaw "task"` for Rich output or `mokioclaw tui` for Textual TUI.',
)
daemon_app = typer.Typer(help="Manage the local MokioClaw Agent daemon.")
app.add_typer(daemon_app, name="daemon")
subagent_app = typer.Typer(help="Inspect and control daemon-managed Subagents.")
app.add_typer(subagent_app, name="subagent")


@subagent_app.command("status")
def subagent_status_command(run_id: str) -> None:
    safe_echo(json.dumps(subagent_status(run_id), ensure_ascii=False, indent=2))


@subagent_app.command("result")
def subagent_result_command(
    run_id: str,
    wait_seconds: Annotated[float, typer.Option("--wait", min=0, max=30)] = 0,
) -> None:
    safe_echo(
        json.dumps(
            subagent_status(run_id, wait_seconds=wait_seconds),
            ensure_ascii=False,
            indent=2,
        )
    )


@subagent_app.command("cancel")
def subagent_cancel_command(run_id: str) -> None:
    safe_echo(json.dumps(subagent_cancel(run_id), ensure_ascii=False, indent=2))


@subagent_app.command("events")
def subagent_events_command(
    run_id: str,
    after_seq: Annotated[int, typer.Option("--after-seq", min=0)] = 0,
) -> None:
    for item in iter_subagent_events(run_id, after_seq=after_seq):
        safe_echo(json.dumps(item, ensure_ascii=False))


@daemon_app.command("status")
def daemon_status_command() -> None:
    status = daemon_status(start=False)
    if status is None:
        safe_echo("MokioClaw daemon is stopped.")
        return
    active_runs = sum(run_status == "running" for run_status in status.get("runs", {}).values())
    safe_echo(f"MokioClaw daemon is running (pid {status.get('pid')}); active runs: {active_runs}.")


@daemon_app.command("stop")
def daemon_stop_command() -> None:
    safe_echo(
        "MokioClaw daemon stopped." if stop_daemon() else "MokioClaw daemon is already stopped."
    )


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    workspace: Annotated[
        Path | None,
        typer.Option(
            "--workspace",
            "-w",
            help="Workspace for generated files. Defaults to a fresh .mokioclaw/workspaces/workspace-* directory.",
        ),
    ] = None,
    max_attempts: Annotated[
        int,
        typer.Option(
            "--max-attempts",
            help="Maximum planner/actor/verifier attempts before finalizing.",
        ),
    ] = 3,
    approval_mode: Annotated[
        Literal["inline", "auto", "deny"] | None,
        typer.Option(
            "--approval-mode",
            help="Human approval mode for high-risk BashTool commands: inline, auto, or deny.",
        ),
    ] = None,
    checkpoint_mode: Annotated[
        Literal["light", "strict", "off"] | None,
        typer.Option("--checkpoint-mode", help="Checkpoint mode: light, strict, or off."),
    ] = None,
    trace_mode: Annotated[
        Literal["on", "off"] | None,
        typer.Option("--trace-mode", help="Trace logging mode: on or off."),
    ] = None,
    resume: Annotated[
        Path | None,
        typer.Option("--resume", help="Resume from an existing MokioClaw workspace."),
    ] = None,
    skill: str | None = typer.Option(None, "--skill"),
    execution_backend: Literal["local", "docker"] | None = typer.Option(None, "--execution-backend"),
    workflow_mode: Literal["full", "light", "auto"] | None = typer.Option(None, "--workflow-mode"),
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    configure_console()
    task = None
    if isinstance(ctx.obj, dict):
        task = ctx.obj.get("task_arg")
    if not task and resume is None:
        safe_echo(ctx.get_help())
        raise typer.Exit()

    safe_secho(
        "mokioclaw stage 5: MultiAgent + context/harness engineering",
        fg=typer.colors.MAGENTA,
    )
    approval_handler = _inline_approval_handler if approval_mode in {None, "inline"} else None
    iterator = stream_agent_events(
        task,
        **({"skill": skill} if skill else {}),
        workspace=workspace,
        max_attempts=max_attempts,
        approval_mode=approval_mode,
        approval_handler=approval_handler,
        checkpoint_mode=checkpoint_mode,
        resume_workspace=resume,
        trace_mode=trace_mode,
        use_daemon=True,
        **({"execution_backend": execution_backend} if execution_backend is not None else {}),
        **({"workflow_mode": workflow_mode} if workflow_mode is not None else {}),
    )
    run_id, status = None, "finished"
    while True:
        try:
            for event in iterator:
                payload = event.get("event", {})
                if isinstance(payload, dict) and payload.get("type") == "run_started":
                    run_id = payload.get("run_id")
                if event.get("type") == "daemon_run_finished":
                    status = event.get("status", status)
                elif isinstance(payload, dict) and payload.get("type") == "run_finished":
                    status = payload.get("status", status)
                print_event(event)
            break
        except KeyboardInterrupt:
            if not run_id:
                raise typer.Exit(130) from None
            cancel_run(run_id)
            safe_echo("cancelling")
            iterator = iter_run_events(run_id, approval_handler=lambda _: ApprovalDecision(False))
    if status != "finished":
        raise typer.Exit(130 if status == "interrupted" else 1)


@app.command("cancel")
def cancel_command(run_id: str) -> None:
    """Cancel a foreground run while keeping detached Subagents running."""
    result = cancel_run(run_id)
    safe_echo(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok"):
        raise typer.Exit(1)


@app.command("send")
def send_command(run_id: str, text: str, message_id: str | None = None) -> None:
    """Queue a correction for a running foreground task."""
    from uuid import uuid4

    from mokioclaw.core.daemon_client import submit_run_message

    result = submit_run_message(run_id, message_id or uuid4().hex, text)
    safe_echo(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok"):
        raise typer.Exit(1)


@app.command("rewind")
def rewind_command(
    workspace: Annotated[Path, typer.Option("--workspace", "-w")],
    run_id: str | None = None,
    file: Annotated[list[str] | None, typer.Option("--file")] = None,
    apply: bool = typer.Option(False, "--apply"),
) -> None:
    """Preview file-tool changes; --apply restores matching files."""
    from mokioclaw.core.changes import rewind

    try:
        result = rewind(workspace, run_id, files=file, apply=apply)
    except (OSError, ValueError, RuntimeError) as exc:
        result = {"ok": False, "error": str(exc)}
    safe_echo(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok"):
        raise typer.Exit(1)


@app.command("tui")
def tui(
    task: Annotated[
        str | None, typer.Argument(help="Optional initial task for the Textual TUI.")
    ] = None,
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", "-w", help="Workspace for the persistent TUI coding session."),
    ] = None,
    max_attempts: Annotated[
        int,
        typer.Option(
            "--max-attempts",
            help="Maximum planner/actor/verifier attempts before finalizing.",
        ),
    ] = 3,
    approval_mode: Annotated[
        Literal["inline", "auto", "deny"] | None,
        typer.Option(
            "--approval-mode",
            help="Human approval mode for high-risk BashTool commands: inline, auto, or deny.",
        ),
    ] = None,
    checkpoint_mode: Annotated[
        Literal["light", "strict", "off"] | None,
        typer.Option("--checkpoint-mode", help="Checkpoint mode: light, strict, or off."),
    ] = None,
    trace_mode: Annotated[
        Literal["on", "off"] | None,
        typer.Option("--trace-mode", help="Trace logging mode: on or off."),
    ] = None,
    resume: Annotated[
        Path | None,
        typer.Option("--resume", help="Resume from an existing MokioClaw workspace."),
    ] = None,
    skill: str | None = typer.Option(None, "--skill"),
    execution_backend: Literal["local", "docker"] | None = typer.Option(None, "--execution-backend"),
    workflow_mode: Literal["full", "light", "auto"] | None = typer.Option(None, "--workflow-mode"),
) -> None:
    """Open the Textual terminal interface."""
    configure_console()
    from mokioclaw.cli.tui import MokioClawTuiApp

    MokioClawTuiApp(
        initial_task=task,
        skill=skill,
        workspace=workspace,
        max_attempts=max_attempts,
        approval_mode=approval_mode,
        checkpoint_mode=checkpoint_mode,
        trace_mode=trace_mode,
        resume=resume,
        **({"execution_backend": execution_backend} if execution_backend is not None else {}),
        **({"workflow_mode": workflow_mode} if workflow_mode is not None else {}),
    ).run()


@app.command("compact")
def compact_command(
    workspace: Annotated[
        Path | None,
        typer.Option(
            "--workspace",
            "-w",
            help="Workspace whose saved session history should be compacted.",
        ),
    ] = None,
    keep_recent: Annotated[
        int,
        typer.Option("--keep-recent", min=1, help="Number of recent turns to retain."),
    ] = 4,
    run_id: Annotated[
        str | None,
        typer.Option("--run-id", help="Request compaction of an active daemon run."),
    ] = None,
    request_id: Annotated[
        str | None,
        typer.Option("--request-id", help="Inspect a prior compact request."),
    ] = None,
    wait_seconds: Annotated[float, typer.Option("--wait", min=0, max=30)] = 0,
    cancel: Annotated[
        bool,
        typer.Option(
            "--cancel",
            help="Cancel a pending compact request; requires --run-id and --request-id.",
        ),
    ] = False,
) -> None:
    """Compact a saved session or request compaction of a running Agent."""
    if (workspace is None) == (run_id is None):
        raise typer.BadParameter("provide exactly one of --workspace or --run-id")
    if cancel and (not run_id or not request_id):
        raise typer.BadParameter("--cancel requires --run-id and --request-id")
    if run_id:
        import time

        from mokioclaw.core.daemon_client import (
            compact_cancel,
            compact_run,
            compact_status,
        )

        result = (
            compact_cancel(run_id, request_id)
            if cancel
            else compact_status(run_id, request_id)
            if request_id
            else compact_run(run_id)
        )
        deadline = time.monotonic() + wait_seconds
        while (
            result.get("ok", True)
            and result.get("status") in {"pending", "applying"}
            and time.monotonic() < deadline
        ):
            time.sleep(min(0.2, max(0, deadline - time.monotonic())))
            result = compact_status(run_id, result["request_id"])
        safe_echo(json.dumps(result, ensure_ascii=False, indent=2))
        if not result.get("ok", True) or result.get("status") == "failed":
            raise typer.Exit(1)
        return
    if request_id:
        raise typer.BadParameter("--request-id requires --run-id")
    try:
        result = compact_session_history(workspace.expanduser(), keep_recent=keep_recent)
    except RuntimeError as exc:
        raise typer.BadParameter(str(exc)) from exc
    safe_echo(
        f"Compacted {result['compacted']} turn(s); kept {result['kept']} recent turn(s).\n"
        f"scope: session; status: {result['status']}; estimated tokens: "
        f"{result['before_tokens']} -> {result['after_tokens']}"
    )


def _inline_approval_handler(request: ApprovalRequest) -> ApprovalDecision:
    from mokioclaw.cli.formatter import console

    console.print(
        Panel(
            f"Requested operation:\n{request.command}\n\nReason:\n{request.risk_reason}\nResource: {request.rule_preview}\nExecution: {request.execution_context}",
            title=f"Human Approval · {request.tool_name}",
            border_style="yellow",
            box=box.ROUNDED,
        )
    )
    try:
        answer = (
            typer.prompt(
                "Approve? [y/N; s=session allow, d=session deny, p=project allow, x=project deny; append -dir for file parent directory]",
                default="n",
                show_default=False,
            )
            .strip()
            .lower()
        )
    except typer.Abort:
        # Click converts SIGINT at a prompt into Abort; forward the run signal.
        raise KeyboardInterrupt from None
    console.print()
    choices = {
        "s": (True, "session"),
        "d": (False, "session"),
        "p": (True, "project"),
        "x": (False, "project"),
    }
    directory = answer.endswith("-dir")
    selected = answer.removesuffix("-dir")
    if selected in choices:
        if directory and request.tool_name not in {"FileReadTool", "FileWriteTool", "FileEditTool"}:
            return ApprovalDecision(False, "Directory rules apply only to file tools.")
        approved, scope = choices[selected]
        return ApprovalDecision(
            approved, scope=scope, match_scope="directory" if directory else "exact"
        )
    approved = answer in {"y", "yes"}
    return ApprovalDecision(
        approved=approved, reason="" if approved else "Rejected by human operator."
    )


@app.command("trace")
def trace_command(
    workspace: Path | None = None,
    run_id: str | None = None,
    layer: str | None = None,
    direction: str | None = None,
    follow: bool = False,
    raw: bool = False,
) -> None:
    """Read existing traces, optionally following new records; never execute work."""
    import time

    from mokioclaw.core.daemon_client import daemon_state_dir
    from mokioclaw.core.transport_trace import TraceReader

    reader = TraceReader()
    try:
        while True:
            for _path, record in reader.read(workspace, daemon_root=daemon_state_dir()):
                payload = record.get("payload", {})
                if run_id and (record.get("run_id") or payload.get("run_id")) != run_id:
                    continue
                if layer and record.get("layer", "event") != layer:
                    continue
                if direction and record.get("direction", "CORE") != direction:
                    continue
                if raw:
                    typer.echo(json.dumps(record, ensure_ascii=False))
                else:
                    safe_echo(
                        f"{record.get('timestamp', '')} {record.get('layer', 'event')} {record.get('direction', 'CORE')} {record.get('type')} run={record.get('run_id', '')}"
                    )
            if not follow:
                return
            time.sleep(0.2)
    except KeyboardInterrupt:
        return


permissions_app = typer.Typer(help="Inspect or revoke user-owned project approval rules.")
app.add_typer(permissions_app, name="permissions")


@permissions_app.command("list")
def permissions_list(
    workspace: Path = typer.Option(..., "--workspace", "-w"), session_id: str = ""
) -> None:
    from mokioclaw.core.daemon_client import permissions

    safe_echo(
        json.dumps(permissions(workspace, session_id=session_id), ensure_ascii=False, indent=2)
    )


@permissions_app.command("revoke")
def permissions_revoke(
    rule_id: str, workspace: Path = typer.Option(..., "--workspace", "-w"), session_id: str = ""
) -> None:
    from mokioclaw.core.daemon_client import permissions

    safe_echo(
        json.dumps(
            permissions(workspace, action="revoke", rule_id=rule_id, session_id=session_id),
            ensure_ascii=False,
            indent=2,
        )
    )


@permissions_app.command("clear")
def permissions_clear(
    workspace: Path = typer.Option(..., "--workspace", "-w"), session_id: str = ""
) -> None:
    from mokioclaw.core.daemon_client import permissions

    safe_echo(
        json.dumps(
            permissions(workspace, action="clear", session_id=session_id),
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("doctor")
def doctor_command(
    execution_backend: Literal["local", "docker"] = typer.Option("local", "--execution-backend"),
    image: str = typer.Option("mokioclaw-execution:0.2.0", "--image"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Check backend availability without executing a model request."""
    import json

    from mokioclaw.core.execution_backend import doctor

    result = doctor(execution_backend, image)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2) if json_output else str(result))
    if not result["ok"]:
        raise typer.Exit(1)
