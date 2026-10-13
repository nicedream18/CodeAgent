from __future__ import annotations

import os
from typing import Any, Callable

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI


def create_model() -> ChatOpenAI:
    load_dotenv()

    api_key = os.getenv("API_KEY")
    model = os.getenv("MODEL")
    base_url = os.getenv("BASE_URL")

    missing = [name for name, value in {"API_KEY": api_key, "MODEL": model, "BASE_URL": base_url}.items() if not value]
    if missing:
        raise RuntimeError(f"missing required .env setting(s): {', '.join(missing)}")

    return ChatOpenAI(
        api_key=api_key,
        model=model,
        base_url=base_url,
        temperature=0,
    )


def invoke_streaming(runnable: Any, messages: list[Any], *, writer: Callable[[dict[str, Any]], None] | None = None, node: str = "") -> Any:
    """Stream text chunks to runtime events while returning a normal message."""
    stream = getattr(runnable, "stream", None)
    if not callable(stream):
        return runnable.invoke(messages)
    if writer:
        writer({"type": "llm_stream_start", "node": node})
    assembled = None
    try:
        for chunk in stream(messages):
            content = getattr(chunk, "content", "")
            text = _text_content(content)
            if text and writer:
                writer({"type": "llm_token", "node": node, "text": text})
            if assembled is None:
                assembled = chunk
            else:
                try:
                    assembled = assembled + chunk
                except (TypeError, AttributeError):
                    assembled = chunk
    except Exception:
        if writer:
            writer({"type": "llm_stream_end", "node": node, "status": "failed"})
        raise
    if assembled is None:
        assembled = runnable.invoke(messages)
    if writer:
        writer({"type": "llm_stream_end", "node": node, "status": "finished"})
    return assembled


def _text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(item.get("text", "")) for item in content if isinstance(item, dict) and item.get("type") == "text")
    return ""
