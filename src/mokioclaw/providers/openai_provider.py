from __future__ import annotations

import os
from typing import Any, Callable
from uuid import uuid4

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

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

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        other = (
            "max_completion_tokens"
            if self.output_parameter == "max_tokens"
            else "max_tokens"
        )
        if other in payload:
            payload[self.output_parameter] = payload.pop(other)
        return payload


def create_model() -> ChatOpenAI:
    load_dotenv()

    api_key = os.getenv("API_KEY")
    model = os.getenv("MODEL")
    base_url = os.getenv("BASE_URL")

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
        raise ValueError(
            "MOKIO_MODEL_OUTPUT_PARAMETER must be max_tokens or max_completion_tokens"
        )

    return ConfiguredChatOpenAI(
        api_key=api_key,
        model=model,
        base_url=base_url,
        temperature=0,
        output_parameter=parameter,
        **{parameter: budget.output},
    )


def invoke_streaming(
    runnable: Any,
    messages: list[Any],
    *,
    writer: Callable[[dict[str, Any]], None] | None = None,
    node: str = "",
    state: dict[str, Any] | None = None,
    allow_compaction: bool = True,
) -> Any:
    """Stream text chunks to runtime events while returning a normal message."""
    from mokioclaw.core.context_compaction import prepare_model_context

    runtime = (state or {}).get("runtime")
    check_run(runtime)
    budget, _snapshot = prepare_model_context(
        runnable,
        messages,
        state,
        writer=writer,
        node=node,
        allow_compaction=allow_compaction,
    )
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
    metadata = {"node": node, "stream_id": stream_id}
    status = "failed"
    error = ""
    iterator = None
    assembled = None
    if writer:
        writer({"type": "llm_stream_start", **metadata})
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
                raise RuntimeError("Model stream chunk must contain message content")
            text = _text_content(getattr(chunk, "content", ""))
            if text and writer:
                writer({"type": "llm_token", **metadata, "text": text})
            if assembled is None:
                assembled = chunk
            else:
                try:
                    assembled = assembled + chunk
                except (TypeError, AttributeError) as exc:
                    raise RuntimeError(
                        "Model stream chunks cannot be assembled"
                    ) from exc
        control.check()
        if assembled is None:
            raise RuntimeError("Model returned an empty stream")
        status = "finished"
        return assembled
    except (RunInterrupted, KeyboardInterrupt):
        status = "interrupted"
        raise
    except BaseException as exc:
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
                    writer(
                        {
                            "type": "llm_stream_end",
                            **metadata,
                            "status": status,
                            "error": error,
                        }
                    )
                if status == "finished":
                    _report_usage(assembled, writer, node)


def _report_usage(response, writer, node) -> None:
    usage = getattr(response, "usage_metadata", None)
    if writer and isinstance(usage, dict):
        writer(
            {
                "type": "model_usage",
                "agent": node,
                "scope": "run",
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "total_tokens": usage.get("total_tokens"),
                "reported": True,
            }
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
