from __future__ import annotations

from mokioclaw.core import agent as api


def _stream_session_events(
    task: str | None = None,
    *,
    session_workspace: api.Path | None = None,
    max_attempts: int = 3,
    approval_mode: str | None = None,
    approval_handler=None,
    checkpoint_mode: str | None = None,
    resume_workspace: api.Path | None = None,
    trace_mode: str | None = None,
    use_daemon: bool = False,
    execution_backend: str | None = None,
    workflow_mode: str | None = None,
    skill: str | None = None,
    _lifecycle=None,
) -> api.Iterator[dict[str, api.Any]]:
    if (use_daemon or api.os.getenv("MOKIO_USE_DAEMON") == "1") and api.os.getenv(
        "MOKIO_DAEMON_WORKER"
    ) != "1":
        from mokioclaw.core.daemon_client import run_via_daemon

        yield from run_via_daemon(
            "session",
            task,
            options={
                "session_workspace": session_workspace,
                "max_attempts": max_attempts,
                "approval_mode": approval_mode,
                "checkpoint_mode": checkpoint_mode,
                "resume_workspace": resume_workspace,
                "trace_mode": trace_mode,
                "execution_backend": execution_backend,
                "workflow_mode": workflow_mode,
                "skill": skill,
            },
            approval_handler=approval_handler,
        )
        return
    workspace = (resume_workspace or session_workspace or api.default_workspace()).expanduser()
    workspace.mkdir(parents=True, exist_ok=True)
    session = api.load_or_create_session(workspace)
    _lifecycle.runtime.session_id = session["session_id"]
    resumed = resume_workspace is not None
    yield {
        "type": "custom_event",
        "event": api.session_started_event(workspace, session, resumed=resumed),
    }
    yield {"type": "workspace", "path": str(workspace)}

    if not task:
        return

    turn = api.append_user_turn(session, task)
    api.save_session(workspace, session)
    yield {
        "type": "custom_event",
        "event": api.session_turn_started_event(workspace, session, turn=turn, task=task),
    }
    session_context = api.build_session_context(workspace, session)

    route = "workflow"
    entry_state: dict[str, api.Any] = {
        "runtime": _lifecycle.runtime,
        "task": task or "",
        "messages": [],
        "session_id": session.get("session_id", ""),
        "session_turn": turn,
        "session_context": session_context,
    }
    _lifecycle.state.update(entry_state)
    for envelope in api._graph_envelopes(api.build_entry_workflow(), entry_state):
        event = envelope["event"]
        if envelope["type"] == "custom_event" and event.get("type") == "intent_decision":
            route = str(event.get("route") or "workflow")
        elif envelope["type"] == "graph_event":
            api._merge_graph_update(entry_state, event)
        yield envelope

    if route == "chat":
        response = str(entry_state.get("chat_response") or entry_state.get("final_answer") or "")
        api.check_run(_lifecycle.runtime)
        api.append_assistant_turn(
            session, turn=turn, route="chat", content=response, summary=response
        )
        api.save_session(workspace, session)
        yield {
            "type": "custom_event",
            "event": api.session_turn_saved_event(workspace, session, turn=turn, route="chat"),
        }
        return

    workflow_events = api._stream_complex_workflow(
        task=task,
        workspace=workspace,
        max_attempts=max_attempts,
        approval_mode=approval_mode,
        approval_handler=approval_handler,
        checkpoint_mode=checkpoint_mode,
        resume_workspace=resume_workspace,
        trace_mode=trace_mode,
        session=session,
        turn=turn,
        session_context=session_context,
        _lifecycle=_lifecycle,
    )
    final_answer = ""
    for event in workflow_events:
        final_answer = api._final_answer_from_event(event) or final_answer
        yield event

    api.check_run(_lifecycle.runtime)
    api.append_assistant_turn(
        session, turn=turn, route="workflow", content=final_answer, summary=final_answer
    )
    api.save_session(workspace, session)
    yield {
        "type": "custom_event",
        "event": api.session_turn_saved_event(workspace, session, turn=turn, route="workflow"),
    }
