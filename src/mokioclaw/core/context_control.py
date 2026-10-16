"""Thread-safe requests; only the execution thread applies compaction."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import RLock
from typing import Any
from uuid import uuid4

CURRENT_CONTROL: ContextVar[Any] = ContextVar("mokioclaw_context_control", default=None)


@dataclass
class ContextControl:
    version: int = 0
    closed: bool = False
    requests: dict[str, dict[str, Any]] = field(default_factory=dict)
    lock: Any = field(default_factory=RLock)

    def request(self) -> dict[str, Any]:
        with self.lock:
            if self.closed:
                return {"ok": False, "status": "failed", "error_type": "run_finished"}
            for item in self.requests.values():
                if item["status"] in {"pending", "applying"}:
                    return dict(item)
                if item["status"] == "noop" and item["context_version"] == self.version:
                    return dict(item)
            request_id = "compact-" + uuid4().hex[:12]
            item = {
                "ok": True,
                "request_id": request_id,
                "status": "pending",
                "context_version": self.version,
                "scope": "run",
            }
            self.requests[request_id] = item
            # Bound control history while preserving the active request.
            for key in list(self.requests)[:-64]:
                del self.requests[key]
            return dict(item)

    def take(self) -> dict[str, Any] | None:
        with self.lock:
            for item in self.requests.values():
                if item["status"] == "pending":
                    item["status"] = "applying"
                    return dict(item)
        return None

    def finish(self, request_id: str, status: str, **details: Any) -> dict[str, Any]:
        with self.lock:
            item = self.requests[request_id]
            item.update({**details, "status": status, "context_version": self.version})
            return dict(item)

    def get(self, request_id: str) -> dict[str, Any]:
        with self.lock:
            return dict(
                self.requests.get(
                    request_id, {"ok": False, "error_type": "unknown_compact_request"}
                )
            )

    def cancel(self, request_id: str) -> dict[str, Any]:
        with self.lock:
            item = self.requests.get(request_id)
            if item is None:
                return {"ok": False, "error_type": "unknown_compact_request"}
            if item["status"] == "applying":
                return {**item, "ok": False, "error_type": "compaction_in_progress"}
            if item["status"] == "pending":
                item.update(status="cancelled")
            return dict(item)

    def close(self) -> list[dict[str, Any]]:
        with self.lock:
            self.closed = True
            failed = []
            for item in self.requests.values():
                if item["status"] in {"pending", "applying"}:
                    item.update(
                        ok=False,
                        status="failed",
                        error_type="run_finished_before_compaction",
                    )
                    failed.append(dict(item))
            return failed
