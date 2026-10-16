"""Bounded diagnostic artifacts shared by stream traces and MCP stderr."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

_LOCK = threading.RLock()
_ACTIVE: set[Path] = set()
MAX_FILE_BYTES = 1024 * 1024
MAX_FILES = 32
MAX_TOTAL_BYTES = 16 * 1024 * 1024


def read_diagnostic_text(workspace: Path, reference: str) -> str | None:
    path = (workspace / reference).resolve()
    if not path.is_relative_to(workspace.resolve()) or not path.is_relative_to(
        (workspace / ".mokioclaw/diagnostics").resolve()
    ):
        return None
    try:
        with path.open("rb") as handle:
            return handle.read(MAX_FILE_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return None


class DiagnosticArtifact:
    def __init__(self, workspace: Path, kind: str):
        self.workspace = workspace
        self.root = workspace / ".mokioclaw" / "diagnostics"
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / f"{kind}-{uuid4().hex}.log"
        self._handle = self.path.open("xb")
        self.path.chmod(0o600)
        with _LOCK:
            _ACTIVE.add(self.path)
        self.total = 0
        self.retained = 0
        self.tail = b""
        self.closed = False

    def write(self, data: bytes):
        self.total += len(data)
        self.tail = (self.tail + data)[-4096:]
        allowed = max(0, MAX_FILE_BYTES - self.retained)
        if allowed:
            retained = data[:allowed]
            if len(data) > allowed:
                retained = retained.decode("utf-8", errors="ignore").encode("utf-8")
            self.retained += self._handle.write(retained)

    def finish(self) -> dict[str, Any]:
        if not self.closed:
            self._handle.close()
            self.closed = True
            with _LOCK:
                _ACTIVE.discard(self.path)
                files = []
                for path in self.root.glob("*.log"):
                    try:
                        info = path.stat()
                        files.append((info.st_mtime_ns, path, info.st_size))
                    except FileNotFoundError:
                        pass
                total = 0
                for index, (_, path, size) in enumerate(sorted(files, reverse=True)):
                    total += size
                    if (index >= MAX_FILES or total > MAX_TOTAL_BYTES) and path not in _ACTIVE:
                        path.unlink(missing_ok=True)
        return {
            "full_output_path": self.path.relative_to(self.workspace).as_posix(),
            "output_bytes": self.total,
            "retained_bytes": self.retained,
            "output_truncated": self.total > self.retained,
        }
