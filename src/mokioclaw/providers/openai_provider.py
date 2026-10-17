from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from pydantic import Field

from mokioclaw.core.run_control import (
    RunControl,
    RunInterrupted,
    check_run,
    run_control,
)


class ConfiguredChatOpenAI(ChatOpenAI):
    """Keep the configured output parameter on OpenAI-compatible endpoints.

    ChatOpenAI otherwise renames max_tokens to max_completion_tokens even for
    non-OpenAI base URLs. A server may ignore an unknown generation parameter.
    """

    output_parameter: str = "max_tokens"
    mokio_profile: dict = Field(default_factory=dict)

    def estimate_context_tokens(self, messages, tools):
        from mokioclaw.providers.token_estimate import compatible_estimate

        return compatible_estimate(messages, tools)

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        other = "max_completion_tokens" if self.output_parameter == "max_tokens" else "max_tokens"
        if other in payload:
            payload[self.output_parameter] = payload.pop(other)
        return payload


def create_model(*, role=None, runtime=None) -> ChatOpenAI:
    load_dotenv()
    from mokioclaw.providers.model_policy import _SELECTION, create_role_model

    if runtime is not None and role is not None:
        return create_role_model(create_model, runtime, role)
    profile = _SELECTION.get() or {}

    api_key = os.getenv(profile.get("api_key_env", "API_KEY"))
    model = profile.get("model") or os.getenv("MODEL")
    base_url = profile.get("base_url") or os.getenv("BASE_URL")

    missing = [
        name
        for name, value in {
            "API_KEY": api_key,
            "MODEL": model,
            "BASE_URL": base_url,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError(f"missing required .env setting(s): {', '.join(missing)}")

    from mokioclaw.core.context_budget import ContextBudget

    budget = ContextBudget.from_runtime()
    parameter = os.getenv("MOKIO_MODEL_OUTPUT_PARAMETER", "max_tokens")
    if parameter not in {"max_tokens", "max_completion_tokens"}:
        raise ValueError("MOKIO_MODEL_OUTPUT_PARAMETER must be max_tokens or max_completion_tokens")

    return ConfiguredChatOpenAI(
        api_key=api_key,
        model=model,
        base_url=base_url,
        temperature=0,
        max_retries=int(profile.get("max_retries", 2)),
        output_parameter=parameter,
        mokio_profile=profile,
        **{parameter: int(profile.get("output_tokens", budget.output))},
    )


def invoke_streaming(
    runnable, messages, *, writer=None, node="", state=None, allow_compaction=True
):
    """Retry one truncated generation without executing its discarded calls."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    from mokioclaw.providers.generation import ModelGenerationStopped, declared_calls

    for attempt in range(2):
        try:
            return _invoke_once(
                runnable,
                messages,
                writer=writer,
                node=node,
                state=state,
                allow_compaction=allow_compaction,
                generation_attempt=attempt,
            )
        except ModelGenerationStopped as exc:
            if exc.reason != "model_output_truncated" or attempt:
                raise
            calls = declared_calls(exc.message)
            messages.append(
                AIMessage(
                    content=_text_content(exc.message.content),
                    tool_calls=[{"id": c["id"], "name": c["name"], "args": {}} for c in calls],
                )
            )
            for call in calls:
                messages.append(
                    ToolMessage(
                        content="Not executed: output was truncated. Regenerate a smaller complete call.",
                        name=call["name"],
                        tool_call_id=call["id"],
                    )
                )
            messages.append(
                HumanMessage(
                    content="Your previous response exceeded the output limit. ALL of its tools were discarded and none ran. Do not continue the broken fragment or repeat the entire large response. Take exactly ONE SMALL complete step within the same output budget: at most one complete tool call with short arguments, or a concise complete answer of at most three sentences. If the whole deliverable cannot fit, explain the next step and remaining work instead of claiming completion. Preserve the original task and acceptance criteria."
                )
            )
    raise AssertionError("unreachable")


def _invoke_once(
    runnable: Any,
    messages: list[Any],
    *,
    writer: Callable[[dict[str, Any]], None] | None = None,
    node: str = "",
    state: dict[str, Any] | None = None,
    allow_compaction: bool = True,
    generation_attempt: int = 0,
) -> Any:
    """Stream text chunks to runtime events while returning a normal message."""
    from mokioclaw.core.context_compaction import prepare_model_context

    runtime = (state or {}).get("runtime")
    check_run(runtime)
    from mokioclaw.core.run_inbox import RunRedirected, check_redirect

    check_redirect(runtime)
    if runtime is not None:
        from mokioclaw.core.instructions import inject_instructions

        inject_instructions(runtime, messages, writer)
        if runtime.active_agent_prompt:
            from langchain_core.messages import SystemMessage

            marker = "MOKIO ROLE INSTRUCTIONS\n"
            messages[:] = [
                m
                for m in messages
                if not (isinstance(m, SystemMessage) and str(m.content).startswith(marker))
            ]
            messages.insert(0, SystemMessage(content=marker + runtime.active_agent_prompt))
        if runtime.hook_context:
            from langchain_core.messages import SystemMessage

            marker = "MOKIO HOOK FEEDBACK\n"
            messages[:] = [
                m
                for m in messages
                if not (
                    isinstance(m, SystemMessage)
                    and isinstance(m.content, str)
                    and m.content.startswith(marker)
                )
            ]
            messages.insert(0, SystemMessage(content=marker + "\n".join(runtime.hook_context)))
    from mokioclaw.core.context_budget import ContextBudget
    from mokioclaw.providers.generation import ModelGenerationStopped, declared_calls, outcome
    from mokioclaw.providers.model_policy import (
        REQUEST_BUDGET,
        begin_request,
        finish_request,
        profile_for,
    )

    profile = profile_for(runnable)
    token = REQUEST_BUDGET.set(
        ContextBudget(
            window=int(profile["context_tokens"]),
            output=int(profile["output_tokens"]),
            safety=getattr(runtime, "context_safety_tokens", 1024),
            high_ratio=getattr(runtime, "context_high_ratio", 0.85),
            target_ratio=getattr(runtime, "context_target_ratio", 0.60),
        )
        if profile
        else None
    )
    try:
        budget, _snapshot = prepare_model_context(
            runnable,
            messages,
            state,
            writer=writer,
            node=node,
            allow_compaction=allow_compaction,
        )
    finally:
        REQUEST_BUDGET.reset(token)
    role_name = getattr(runtime, "active_agent_name", None) or node
    started = begin_request(runtime, role_name, _snapshot, budget)
    check_run(runtime)
    if callable(getattr(runnable, "bind", None)):
        parameter = os.getenv("MOKIO_MODEL_OUTPUT_PARAMETER", "max_tokens")
        if parameter not in {"max_tokens", "max_completion_tokens"}:
            raise ValueError("unsupported model output parameter")
        runnable = runnable.bind(**{parameter: budget.output})
    control = run_control(runtime)
    owned = control is None
    control = control or RunControl()
    stream_id = uuid4().hex
    metadata = {
        "node": node,
        "stream_id": stream_id,
        "generation_attempt": generation_attempt,
        "role": role_name,
    }
    status = "failed"
    error = ""
    iterator = None
    assembled = None
    if writer:
        writer({"type": "llm_stream_start", **metadata})
    from mokioclaw.providers.payload_trace import payload_reference

    if writer:
        writer(
            {
                "type": "llm_request",
                **metadata,
                "message_count": len(messages),
                "input_estimate": _snapshot.input_tokens,
                "output_budget": budget.output,
                "model_config": {
                    "model": profile.get("model"),
                    "context_tokens": budget.window,
                    "output_tokens": budget.output,
                    "sdk_max_retries": profile.get("max_retries"),
                },
                **payload_reference(
                    runtime,
                    [m.model_dump() if hasattr(m, "model_dump") else str(m) for m in messages],
                ),
            }
        )
    try:
        control.check()
        if callable(getattr(runnable, "astream", None)):
            iterator = control.iter_async(lambda: runnable.astream(messages))
        elif callable(getattr(runnable, "stream", None)):
            iterator = iter(runnable.stream(messages))
        else:
            iterator = iter([runnable.invoke(messages)])
        for chunk in iterator:
            control.check()
            if not hasattr(chunk, "content"):
                raise ModelGenerationStopped("model_stream_chunk_invalid", assembled)
            text = _text_content(getattr(chunk, "content", ""))
            if text and writer:
                writer({"type": "llm_token", **metadata, "text": text})
            if assembled is None:
                assembled = chunk
            else:
                try:
                    assembled = assembled + chunk
                except (TypeError, AttributeError) as exc:
                    raise ModelGenerationStopped(
                        "model_stream_chunk_unaggregatable", assembled
                    ) from exc
        control.check()
        if assembled is not None and not hasattr(assembled, "content"):
            raise ModelGenerationStopped("model_stream_chunk_unaggregatable", assembled)
        if assembled is None:
            raise ModelGenerationStopped("model_stream_empty", assembled)
        from mokioclaw.providers.generation import validate_generation

        completion = validate_generation(assembled)
        metadata.update(finish_reason=completion.reason, raw_finish_reason=completion.raw_reason)
        status = "finished"
        if runtime and runtime.run_control and runtime.run_control.inbox.pending() and writer:
            for call in getattr(assembled, "tool_calls", []):
                writer(
                    {
                        "type": "tool_result",
                        **metadata,
                        "name": call["name"],
                        "tool_call_id": call["id"],
                        "result": {
                            "ok": False,
                            "status": "not_executed",
                            "error_type": "user_redirected",
                        },
                    }
                )
        check_redirect(runtime)
        return assembled
    except ModelGenerationStopped as exc:
        exc.stream_id = stream_id
        error = exc.reason
        metadata.update(
            finish_reason=outcome(assembled).reason, raw_finish_reason=outcome(assembled).raw_reason
        )
        for call in declared_calls(assembled):
            if writer:
                writer(
                    {
                        "type": "tool_result",
                        **metadata,
                        "name": call["name"],
                        "tool_call_id": call["id"],
                        "result": {"ok": False, "status": "not_executed", "error_type": exc.reason},
                    }
                )
        raise
    except RunRedirected:
        raise
    except (RunInterrupted, KeyboardInterrupt):
        status = "interrupted"
        raise
    except BaseException as exc:
        from openai import OpenAIError

        if runtime is not None and runtime.evaluation_mode and isinstance(exc, OpenAIError):
            error = "provider_" + type(exc).__name__
            raise ModelGenerationStopped(error) from None
        error = f"{type(exc).__name__}: {exc}"[:500]
        raise
    finally:
        try:
            if iterator is not None and hasattr(iterator, "close"):
                iterator.close()
        finally:
            try:
                if owned:
                    control.close()
            finally:
                if writer:
                    end_event = {
                        "type": "llm_stream_end",
                        **metadata,
                        "status": status,
                        "error": error,
                    }
                    control.pending_stream_ends[stream_id] = end_event
                    writer(end_event)
                if writer:
                    writer(
                        {
                            "type": "llm_response",
                            **metadata,
                            "status": status,
                            **payload_reference(
                                runtime,
                                assembled.model_dump()
                                if hasattr(assembled, "model_dump")
                                else str(assembled),
                            ),
                        }
                    )
                finish_request(
                    runtime,
                    assembled,
                    role_name,
                    _snapshot.input_tokens,
                    started,
                    writer,
                    status,
                    stream_id,
                )


def _text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )
    return ""
