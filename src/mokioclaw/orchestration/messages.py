"""Message updates shared by Flows and legacy checkpoint readers."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from langchain_core.messages import (
    BaseMessage,
    RemoveMessage,
    convert_to_messages,
    message_chunk_to_message,
)

# Preserve existing compression/checkpoint payloads without a LangGraph runtime.
REMOVE_ALL_MESSAGES = "__remove_all__"


def add_messages(left: Any, right: Any) -> list[BaseMessage]:
    def normalize(value: Any) -> list[BaseMessage]:
        values = value if isinstance(value, list) else [value] if value is not None else []
        messages = [message_chunk_to_message(m) for m in convert_to_messages(values)]
        for message in messages:
            if message.id is None:
                message.id = str(uuid4())
        return messages

    merged = normalize(left)
    updates = normalize(right)
    resets = [
        index
        for index, message in enumerate(updates)
        if isinstance(message, RemoveMessage) and message.id == REMOVE_ALL_MESSAGES
    ]
    if resets:
        merged = []
        updates = updates[resets[-1] + 1 :]
    for message in updates:
        positions = {old.id: index for index, old in enumerate(merged)}
        if isinstance(message, RemoveMessage):
            if message.id == REMOVE_ALL_MESSAGES:
                merged = []
            elif message.id not in positions:
                raise ValueError(f"Cannot remove unknown message ID: {message.id}")
            else:
                merged.pop(positions[message.id])
        elif message.id in positions:
            merged[positions[message.id]] = message
        else:
            merged.append(message)
    return merged
