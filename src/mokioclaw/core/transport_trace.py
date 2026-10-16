"""Bounded transport metadata; authentication and request bodies are never logged."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

_LOCK = threading.RLock()


class TraceRecord(BaseModel):
    model_config = ConfigDict(extra="allow")
    timestamp: str
    layer: str
    direction: str
    type: str
    request_id: str | int | None = None
    run_id: str | None = None
    stream_id: str | None = None
    tool_call_id: str | None = None
    method: str | None = None
    error_code: int | None = None
    event_id: str | None = None
    event_seq: int | None = None


def transport_record(
    root: Path,
    direction: str,
    kind: str,
    *,
    request: Any = None,
    response: Any = None,
    run_id: str | None = None,
    event: dict[str, Any] | None = None,
) -> None:
    request = request if isinstance(request, dict) else {}
    response = response if isinstance(response, dict) else {}
    result = response.get("result", {})
    params = request.get("params", {}) if isinstance(request.get("params"), dict) else {}
    event = event or {}
    detail = event.get("event", event)
    detail = detail.get("event", detail) if isinstance(detail, dict) else {}
    try:

        def short(value: Any) -> str | None:
            return value[:256] if isinstance(value, str) else None

        record = TraceRecord(
            timestamp=datetime.now(UTC).isoformat(),
            layer="ipc",
            direction=direction,
            type=kind,
            request_id=request.get("id")
            if isinstance(request.get("id"), (str, int)) and not isinstance(request.get("id"), bool)
            else None,
            run_id=run_id
            or (result.get("run_id") if isinstance(result, dict) else None)
            or params.get("run_id"),
            method=short(request.get("method")),
            event_id=short(detail.get("event_id")),
            event_seq=event.get("seq"),
            stream_id=short(detail.get("stream_id")),
            tool_call_id=short(detail.get("tool_call_id")),
            error_code=(response.get("error") or {}).get("code"),
        )
        with _LOCK:
            directory = root / "transport"
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / "current.jsonl"
            if path.exists() and path.stat().st_size >= 1024 * 1024:
                path.replace(directory / f"transport-{uuid4().hex}.jsonl")
            with path.open("a", encoding="utf-8") as handle:
                handle.write(record.model_dump_json() + "\n")
            path.chmod(0o600)
            files = sorted(
                directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime_ns, reverse=True
            )
            total = 0
            for index, item in enumerate(files):
                total += item.stat().st_size
                if index >= 32 or total > 16 * 1024 * 1024:
                    item.unlink(missing_ok=True)
    except (OSError, ValueError, TypeError, AttributeError):
        pass  # Observability cannot change the RPC outcome.


def iter_trace_records(
    workspace: Path | None = None, *, daemon_root: Path | None = None
) -> Iterator[tuple[Path, dict[str, Any]]]:
    paths: list[Path] = []
    if workspace is not None:
        paths.extend(workspace.glob(".mokioclaw/traces/*/events.jsonl"))
    if daemon_root is not None:
        paths.extend((daemon_root / "transport").glob("*.jsonl"))
    for path in sorted(paths, key=lambda p: p.stat().st_mtime_ns):
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                    if isinstance(item, dict):
                        yield path, item
                except json.JSONDecodeError:
                    continue


class TraceReader:
    """Incremental reader with one cursor per file, never one key per event."""

    def __init__(self) -> None:
        self.offsets: dict[str, tuple[int, int]] = {}

    def read(
        self, workspace: Path | None = None, *, daemon_root: Path | None = None
    ) -> Iterator[tuple[Path, dict[str, Any]]]:
        paths: list[Path] = []
        if workspace is not None:
            paths.extend(workspace.glob(".mokioclaw/traces/*/events.jsonl"))
        if daemon_root is not None:
            paths.extend((daemon_root / "transport").glob("*.jsonl"))
        by_inode = dict(self.offsets.values())
        alive = {str(p) for p in paths}
        self.offsets = {k: v for k, v in self.offsets.items() if k in alive}
        for path in sorted(paths):
            try:
                info = path.stat()
                inode, offset = self.offsets.get(
                    str(path), (info.st_ino, by_inode.get(info.st_ino, 0))
                )
                if inode != info.st_ino or offset > info.st_size:
                    offset = 0
                with path.open("rb") as handle:
                    handle.seek(offset)
                    while line := handle.readline():
                        if not line.endswith(b"\n"):
                            break
                        offset = handle.tell()
                        try:
                            record = json.loads(line)
                            if isinstance(record, dict):
                                yield path, record
                        except (ValueError, UnicodeDecodeError):
                            continue
                self.offsets[str(path)] = (info.st_ino, offset)
            except OSError:
                continue
