"""Offline approximation for compatible transports, never vendor token usage."""

from __future__ import annotations

import base64
import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import tiktoken


@lru_cache(maxsize=1)
def encoding() -> tiktoken.Encoding:
    data = (Path(__file__).parent / "assets/cl100k_base.tiktoken").read_bytes()
    if (
        hashlib.sha256(data).hexdigest()
        != "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"
    ):
        raise ValueError("Bundled token estimator data failed integrity verification")
    ranks = {
        base64.b64decode(token): int(rank)
        for token, rank in (line.split() for line in data.splitlines())
    }
    return tiktoken.Encoding(
        name="mokio_cl100k_estimate",
        pat_str=r"'(?i:[sdmt]|ll|ve|re)|[^\r\n\p{L}\p{N}]?+\p{L}++|\p{N}{1,3}+| ?[^\s\p{L}\p{N}]++[\r\n]*+|\s++$|\s*[\r\n]|\s+(?!\S)|\s",
        mergeable_ranks=ranks,
        special_tokens={},
    )


def compatible_estimate(messages: list[Any], tools: list[Any]) -> tuple[int, str]:
    from langchain_core.messages import convert_to_openai_messages

    payload = {"messages": convert_to_openai_messages(messages), "tools": tools}
    text = json.dumps(payload, ensure_ascii=False, default=str)
    # Include JSON wrappers and an additional per-message allowance. This is an
    # explicit approximation; the run budget still reconciles actual usage.
    return len(encoding().encode_ordinary(text)) + 16 * len(messages), "cl100k_base_estimate"
