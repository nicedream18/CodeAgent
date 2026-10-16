from __future__ import annotations

from mokioclaw.core import agent as api


def _graph_envelopes(workflow, inputs, *, terminal=False):
    from mokioclaw.core.run_inbox import RunRedirected, check_redirect

    api.check_run(inputs.get("runtime"))
    runtime = inputs["runtime"]
    inbox = runtime.run_control.inbox if runtime.run_control else None
    while True:
        iterator = None
        try:
            check_redirect(runtime, preserve_progress=False)
            iterator = workflow.stream(inputs, stream_mode=["updates", "custom"])
            for mode, event in iterator:
                if mode == "updates":
                    api._merge_graph_update(inputs, event)
                elif event.get("type") == "llm_stream_end" and runtime.run_control:
                    runtime.run_control.pending_stream_ends.pop(event.get("stream_id"), None)
                yield {
                    "type": "custom_event" if mode == "custom" else "graph_event",
                    "event": event,
                }
                api.check_run(runtime)
                # Custom events may arrive while a node still runs in another
                # thread. Only the producer can declare its current step done.
                if mode == "updates":
                    check_redirect(runtime, preserve_progress=False)
            if inbox and (terminal or inputs.get("intent_route") == "chat") and not inbox.seal():
                raise RunRedirected()
            return
        except RunRedirected as redirect:
            if iterator is not None and hasattr(iterator, "close"):
                iterator.close()
                iterator = None
            inputs.update(redirect.state)
            events = []
            if inbox is None or not inbox.pending():
                raise
            for stream_id, event in list(runtime.run_control.pending_stream_ends.items()):
                runtime.run_control.pending_stream_ends.pop(stream_id, None)
                yield {"type": "custom_event", "event": event}
            inbox.apply(inputs, events.append)
            if terminal and runtime.workflow_mode != "full":
                workflow = api.build_complex_workflow()
                runtime.light_active = False
                runtime.light_target = None
                inputs["workflow_upgraded"] = True
            for event in events:
                yield {"type": "custom_event", "event": event}
            yield {
                "type": "graph_event",
                "event": {
                    "run_redirect": {
                        key: value for key, value in inputs.items() if key != "runtime"
                    }
                },
            }
        finally:
            if hasattr(iterator, "close"):
                iterator.close()


def _stream_complex_workflow(
    *,
    task: str | None,
    workspace: api.Path,
    max_attempts: int,
    approval_mode: str,
    approval_handler,
    checkpoint_mode: str | None,
    resume_workspace: api.Path | None,
    trace_mode: str | None,
    session: dict[str, api.Any] | None = None,
    turn: int | None = None,
    session_context: str = "",
    _lifecycle=None,
) -> api.Iterator[dict[str, api.Any]]:
    resume_path = resume_workspace.expanduser() if resume_workspace is not None else None
    state = _lifecycle.runtime
    hint = _lifecycle.state.get("workflow_hint", {})
    targets = hint.get("target_files", [])
    simple = (
        hint.get("complexity") == "simple"
        and hint.get("cross_module") is False
        and isinstance(targets, list)
        and len(targets) == 1
        and isinstance(targets[0], str)
        and hint.get("confidence", 0) >= 0.85
    )
    selected_mode = (
        "light"
        if state.workflow_mode == "light" or (state.workflow_mode == "auto" and simple)
        else "full"
    )
    if selected_mode == "light":
        from mokioclaw.graph.light import build_light_workflow

        workflow = build_light_workflow()
    else:
        workflow = api.build_complex_workflow()
    yield {
        "type": "custom_event",
        "event": {
            "type": "workflow_selected",
            "mode": selected_mode,
            "reason": "explicit configuration or conservative router hint",
        },
    }

    resume_event: dict[str, api.Any] | None = None
    if resume_path is not None:
        inputs = dict(_lifecycle.resume_inputs)
        resume_event = _lifecycle.resume_event
        yield {"type": "custom_event", "event": resume_event}
    else:
        inputs = {
            **_lifecycle.state,
            "task": task or "",
            "runtime": state,
            "messages": [],
            "attempts": 0,
            "max_attempts": max_attempts,
        }

    if session is not None:
        inputs["session_id"] = session.get("session_id", "")
    if turn is not None:
        inputs["session_turn"] = turn
    if session_context:
        import json

        inputs["session_context"] = session_context
        session_payload = json.loads(session_context)
        inputs["latest_user_corrections"] = session_payload.get(
            "user_requirements", []
        ) + inputs.get("latest_user_corrections", [])
    state.context_version = int(inputs.get("context_version", 0))
    metadata = dict(inputs.get("metadata", {}))
    if session is not None:
        metadata["session_id"] = session.get("session_id", "")
    if turn is not None:
        metadata["session_turn"] = turn
    if metadata:
        inputs["metadata"] = metadata

    _lifecycle.state.update(inputs)
    yield from api._graph_envelopes(workflow, inputs, terminal=True)
