"""Opt-in, bounded model payload diagnostics. No credentials in trace files."""

from __future__ import annotations

import json
import os
import re
from typing import Any

from mokioclaw.core.artifacts import DiagnosticArtifact

_SECRET_KEY = re.compile(
    r"(api.?key|authorization|password|secret|access.?token|auth.?token|credential|^token$)", re.I
)


def redact(value: Any) -> Any:
    secrets = [v for k, v in os.environ.items() if _SECRET_KEY.search(k) and len(v) >= 8]

    def clean(item: Any) -> Any:
        if isinstance(item, dict):
            return {
                str(k): "[REDACTED]" if _SECRET_KEY.search(str(k)) else clean(v)
                for k, v in item.items()
            }
        if isinstance(item, (list, tuple)):
            return [clean(v) for v in item]
        if isinstance(item, str):
            for secret in secrets:
                item = item.replace(secret, "[REDACTED]")
            return item
        return item if item is None or isinstance(item, (int, float, bool)) else clean(str(item))

    return clean(value)


def payload_reference(runtime: Any, payload: Any) -> dict[str, Any]:
    if runtime is None or not runtime.settings.get("trace", {}).get("include_model_payload", False):
        return {}
    try:
        artifact = DiagnosticArtifact(runtime.workspace, "model-payload")
        try:
            artifact.write(json.dumps(redact(payload), ensure_ascii=False, default=str).encode())
        finally:
            result = artifact.finish()
        return result
    except (OSError, ValueError) as exc:
        return {"trace_diagnostic": type(exc).__name__}
