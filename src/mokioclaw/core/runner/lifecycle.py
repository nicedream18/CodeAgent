from __future__ import annotations

from mokioclaw.core import agent as api
from mokioclaw.core.loop_guard import LoopRecovery


class _RunLifecycle:
    def __init__(self, runtime, task, max_attempts):
        self.runtime = runtime
        self.state = {
            "runtime": runtime,
            "task": task or "",
            "messages": [],
            "explicit_skill": runtime.explicit_skill,
            "max_attempts": max_attempts,
        }
        self.latest_node = "start"
        self.resume_inputs = None
        self.resume_event = None
        self.trace = api.TraceRecorder(runtime, task=task or "")
        runtime.run_control.event_sink = self.record_emitted
        self.manager = api.CheckpointManager(runtime, task=task or "")

    def record_emitted(self, event):
        event.setdefault("event_id", api.uuid4().hex)
        event["run_id"] = event.get("run_id") or self.runtime.run_id
        event.setdefault("context_version", self.runtime.context_version)
        if event.get("type") == "llm_stream_start":
            self.latest_node = event.get("node") or self.latest_node
        self.trace.record_emitted_event(event)

    def record(self, envelope):
        event = envelope.get("event", {})
        if envelope.get("type") == "custom_event" and isinstance(event, dict):
            event.setdefault("event_id", api.uuid4().hex)
            event["run_id"] = event.get("run_id") or self.runtime.run_id
            event.setdefault("context_version", self.runtime.context_version)
        if envelope.get("type") == "graph_event":
            self.latest_node = api._latest_graph_node(event) or self.latest_node
            api._merge_graph_update(self.state, event)
            self.trace.record_graph_update(event)
        elif envelope.get("type") == "custom_event":
            if event.get("type") == "llm_stream_start":
                self.latest_node = event.get("node") or self.latest_node
            self.trace.record_custom_event(event)
        if envelope.get("type") == "graph_event" or api._custom_event_needs_checkpoint(event):
            saved = self.manager.save(self.state, status="running", latest_node=self.latest_node)
            if saved:
                self.trace.record_custom_event(saved)


def _controlled_stream(stream, task, options, control, *, session, execution_control=None):

    from mokioclaw.core.context_control import CURRENT_CONTROL, ContextControl
    from mokioclaw.core.session_lock import session_writer

    remote = (
        options.get("use_daemon") or api.os.getenv("MOKIO_USE_DAEMON") == "1"
    ) and api.os.getenv("MOKIO_DAEMON_WORKER") != "1"
    if remote:
        yield from stream(task, **options)
        return
    selected = (
        options.get("resume_workspace")
        or options.get("session_workspace")
        or options.get("workspace")
        or api.default_workspace()
    ).expanduser()
    options["session_workspace" if session else "workspace"] = selected
    previous = CURRENT_CONTROL.get()
    active_control = control or previous or ContextControl()
    context_token = CURRENT_CONTROL.set(active_control)
    active_run = execution_control or api.CURRENT_RUN.get() or api.RunControl()
    run_token = api.CURRENT_RUN.set(active_run)
    lifecycle = None
    try:
        with session_writer(selected):
            runtime = api.create_runtime(
                selected,
                approval_mode=options["approval_mode"],
                approval_handler=options["approval_handler"],
                checkpoint_mode=options["checkpoint_mode"],
                trace_mode=options["trace_mode"],
                resume_from=options["resume_workspace"],
                execution_backend=options.get("execution_backend"),
                workflow_mode=options.get("workflow_mode"),
                skill=options.get("skill"),
            )
            lifecycle = api._RunLifecycle(runtime, task, options["max_attempts"])
            if options["resume_workspace"]:
                # Read the previous checkpoint before writing this run's start snapshot.
                lifecycle.resume_inputs, lifecycle.resume_event = api.load_resume_inputs(
                    runtime, task=task, max_attempts=options["max_attempts"]
                )
                lifecycle.state.update(lifecycle.resume_inputs)
                import json

                from mokioclaw.core.checkpoint import checkpoint_dir
                from mokioclaw.core.execution_backend import execution_fingerprint

                saved_context = json.loads(
                    (checkpoint_dir(selected) / "checkpoint.json").read_text()
                ).get("execution_context", {"backend": "local", "image_digest": ""})
                current_context = execution_fingerprint(runtime)
                previous_context = (
                    "local"
                    if saved_context.get("backend", "local") == "local"
                    else "docker:" + saved_context.get("image_digest", "")
                )
                if current_context != previous_context:
                    lifecycle.resume_inputs.update(
                        messages=[],
                        passed=False,
                        verification_results=[],
                        context_next_node="planner",
                    )
                    lifecycle.resume_inputs.setdefault("metadata", {})[
                        "execution_context_changed"
                    ] = True
                    runtime.read_files.clear()
                    lifecycle.state.update(lifecycle.resume_inputs)

                runtime.explicit_skill = options.get("skill") or lifecycle.resume_inputs.get(
                    "explicit_skill"
                )
                if (selected / ".mokioclaw/changes/reverify.json").exists():
                    lifecycle.resume_inputs.update(
                        passed=False,
                        verification_results=[],
                        context_next_node="verifier",
                    )
                    lifecycle.state.update(lifecycle.resume_inputs)
                reference = lifecycle.resume_inputs.get("inbox_reference")
                if reference:
                    from mokioclaw.core.run_inbox import RunInbox

                    old_path = runtime.assert_workspace_path(runtime.workspace / reference)
                    known = {
                        m.get("message_id")
                        for m in lifecycle.state.get("latest_user_corrections", [])
                        if isinstance(m, dict)
                    }
                    for item in RunInbox(old_path).items:
                        if item["message_id"] not in known:
                            active_run.inbox.submit(item["message_id"], item["text"])
                restored_task = str(lifecycle.state.get("task", ""))
                lifecycle.trace.task = lifecycle.manager.task = restored_task
            lifecycle.trace.start(
                lifecycle.state,
                resumed=bool(options["resume_workspace"]),
                resume_event=lifecycle.resume_event,
            )
            initial = lifecycle.manager.save(lifecycle.state, status="started", latest_node="start")
            if initial:
                lifecycle.trace.record_custom_event(initial)
            status, failure = "finished", None
            hook_events = []
            try:
                if runtime.parent_run_id is None:
                    started = {
                        "type": "custom_event",
                        "event": {
                            "type": "run_started",
                            "orchestration_backend": "crewai",
                            "run_id": runtime.run_id,
                        },
                    }
                    lifecycle.record(started)
                    yield started
                active_run.check()
                for envelope in stream(task, **options, _lifecycle=lifecycle):
                    lifecycle.record(envelope)
                    yield envelope
                active_run.check()
            except (api.RunInterrupted, KeyboardInterrupt):
                active_run.cancel()
                status = "interrupted"
            except api.ModelGenerationStopped as exc:
                status = "failed"
                lifecycle.state.update(exc.state)
                if runtime.foreground_state:
                    lifecycle.state.update(
                        {
                            k: v
                            for k, v in runtime.foreground_state.items()
                            if k not in {"runtime", "messages"}
                        }
                    )
                lifecycle.state["stop_reason"] = exc.reason
                lifecycle.state["final_answer"] = (
                    f"模型生成未完整完成，当前进展已保存：{exc.reason}"
                )
                event = {"type": "model_generation_stopped", "reason": exc.reason}
                lifecycle.record_emitted(event)
                yield {"type": "custom_event", "event": event}
            except LoopRecovery as exc:
                status = "failed"
                lifecycle.state.update(exc.state)
                lifecycle.state.update(
                    passed=False,
                    stop_reason=exc.reason,
                    final_answer="协作循环保护已停止任务，已有修改和证据已保存："
                    + exc.decision.reason,
                )
                from mokioclaw.core.loop_guard import event as loop_event

                event = loop_event(runtime, "stop", exc.decision.reason)
                event["stop_reason"] = exc.reason
                lifecycle.record_emitted(event)
                yield {"type": "custom_event", "event": event}
            except api.RunBudgetExceeded as exc:
                status = "failed"
                lifecycle.state.update(exc.state)
                lifecycle.state["stop_reason"] = exc.reason
                lifecycle.state["final_answer"] = (
                    f"预留验收阶段未能确认任务完成，当前进展已保存：{exc.reason}"
                    if exc.reason == "completion_verification_failed"
                    else f"运行预算已耗尽，当前进展已保存：{exc.reason}"
                )
                event = {
                    "type": "run_budget_exhausted",
                    "event_id": api.uuid4().hex,
                    "run_id": runtime.run_id,
                    "message": lifecycle.state["final_answer"],
                    "stop_reason": exc.reason,
                }
                lifecycle.record_emitted(event)
                yield {"type": "custom_event", "event": event}
            except Exception as exc:  # noqa: BLE001 — fault boundary; control signals are handled separately
                status, failure = "failed", exc
            finally:
                # Model/MCP cleanup completes before the terminal status is published.
                from mokioclaw.core.hooks import HookContext, run_hooks

                run_hooks(
                    runtime,
                    HookContext(
                        "RunFinished",
                        status=status,
                        result={"model_metrics": runtime.model_metrics},
                    ),
                    hook_events.append,
                )
                for event in hook_events:
                    lifecycle.record_emitted(event)
                active_run.close()
            for stream_id, event in list(active_run.pending_stream_ends.items()):
                active_run.pending_stream_ends.pop(stream_id, None)
                envelope = {"type": "custom_event", "event": event}
                lifecycle.record(envelope)
                yield envelope
            for event in hook_events:
                yield {"type": "custom_event", "event": event}
            status = active_run.finish(status)
            if not runtime.session_id:
                from mokioclaw.core.approval_rules import runtime_rules

                permissions = runtime_rules(runtime)
                permissions.cache.pop(permissions.session_key, None)
            from mokioclaw.core.changes import ChangeStore

            ChangeStore(runtime).finish()
            if session and (selected / ".mokioclaw/session/session.json").exists():
                saved_session = api.load_or_create_session(selected)
                from mokioclaw.core.run_inbox import merge_session_requirements

                merge_session_requirements(saved_session, active_run.inbox)
                saved_session["last_run_status"] = status
                api.save_session(selected, saved_session)
            saved = lifecycle.manager.save(
                lifecycle.state, status=status, latest_node=lifecycle.latest_node
            )
            if saved and status == "finished" and lifecycle.state.get("passed"):
                (selected / ".mokioclaw/changes/reverify.json").unlink(missing_ok=True)
            if saved:
                lifecycle.trace.record_custom_event(saved)
                yield {"type": "custom_event", "event": saved}
            trace_event = lifecycle.trace.end(
                status=status,
                latest_node=lifecycle.latest_node,
                final_state=lifecycle.state,
            )
            if trace_event:
                yield {"type": "custom_event", "event": trace_event}
            if lifecycle.trace.errors:
                yield {
                    "type": "custom_event",
                    "event": {
                        "type": "trace_diagnostic",
                        "event_id": api.uuid4().hex,
                        "context_version": runtime.context_version,
                        "run_id": runtime.run_id,
                        "errors": lifecycle.trace.errors[-8:],
                    },
                }
            yield {
                "type": "custom_event",
                "event": {
                    "type": "run_finished",
                    "event_id": api.uuid4().hex,
                    "run_id": runtime.run_id,
                    "context_version": runtime.context_version,
                    "status": status,
                    "stop_reason": lifecycle.state.get("stop_reason"),
                    "model_metrics": runtime.model_metrics,
                },
            }
            if failure is not None:
                raise failure
    finally:
        active_run.event_sink = None
        api.CURRENT_RUN.reset(run_token)
        CURRENT_CONTROL.reset(context_token)
        active_run.close()
        if previous is None:
            active_control.close()
