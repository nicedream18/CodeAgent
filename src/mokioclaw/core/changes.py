"""Write-ahead, byte-exact file edit journal and conflict-aware rewind."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import stat
import threading
import time
from pathlib import Path
from uuid import uuid4

from mokioclaw.core.context_compaction import _atomic_json
from mokioclaw.core.session_lock import session_writer
from mokioclaw.core.tool_context import current_tool_call_context

_LOCK = threading.RLock()


class JournalCommitError(OSError):
    """The edit happened, but its journal completion could not be confirmed."""


def digest(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


def atomic_bytes(path: Path, content: bytes, mode=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            temporary.chmod(mode)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class ChangeStore:
    def __init__(self, runtime):
        self.runtime = runtime
        if not runtime.run_id:
            runtime.run_id = "local-" + uuid4().hex
        if not re.fullmatch(r"[A-Za-z0-9_-]+", runtime.run_id):
            raise ValueError("invalid run_id")
        self.root = runtime.assert_workspace_path(
            runtime.workspace / ".mokioclaw/changes" / runtime.run_id
        )
        self.manifest = self.root / "manifest.json"

    def read(self):
        return (
            json.loads(self.manifest.read_text())
            if self.manifest.exists()
            else {
                "run_id": self.runtime.run_id,
                "closed": False,
                "created": time.time(),
                "changes": [],
            }
        )

    def prepare(self, path, before, after):
        with _LOCK:
            data = self.read()
            base = self.root.parent
            size = len(before or b"")
            used = sum(p.stat().st_size for p in base.glob("*/*.bin"))
            if used + size > self.runtime.rewind_quota_bytes:
                candidates = []
                for manifest in base.glob("*/manifest.json"):
                    old = json.loads(manifest.read_text())
                    if manifest.parent != self.root and old.get("closed"):
                        candidates.append((old.get("created", 0), manifest.parent))
                for _, directory in sorted(candidates):
                    used -= sum(p.stat().st_size for p in directory.glob("*.bin"))
                    shutil.rmtree(directory)
                    if used + size <= self.runtime.rewind_quota_bytes:
                        break
            if used + size > self.runtime.rewind_quota_bytes:
                raise ValueError("rewind backup quota exhausted; edit was not applied")
            change_id = uuid4().hex
            backup = self.root / f"{change_id}.bin"
            mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
            atomic_bytes(backup, before or b"", 0o600)
            context = current_tool_call_context()
            item = {
                "change_id": change_id,
                "path": path.relative_to(self.runtime.workspace.resolve()).as_posix(),
                "before_hash": digest(before),
                "after_hash": digest(after),
                "mode": mode,
                "backup": backup.name,
                "tool_call_id": context.tool_call_id if context else change_id,
                "status": "prepared",
            }
            data["changes"].append(item)
            try:
                _atomic_json(self.manifest, data)
            except BaseException:
                backup.unlink(missing_ok=True)
                raise
            return item

    def applied(self, item):
        with _LOCK:
            data = self.read()
            for entry in data["changes"]:
                if entry["change_id"] == item["change_id"]:
                    entry["status"] = "applied"
            _atomic_json(self.manifest, data)

    def finish(self):
        with _LOCK:
            if self.manifest.exists():
                data = self.read()
                data["closed"] = True
                _atomic_json(self.manifest, data)


def journal_write(runtime, path, content):
    path = runtime.assert_workspace_path(path)
    before = path.read_bytes() if path.exists() else None
    after = content.encode("utf-8")
    store = ChangeStore(runtime)
    item = store.prepare(path, before, after)
    # Recheck after durable backup: never replace a concurrent edit silently.
    current = path.read_bytes() if path.exists() else None
    if digest(current) != item["before_hash"]:
        raise ValueError("file changed while preparing edit; read it again")
    atomic_bytes(path, after, item["mode"])
    try:
        store.applied(item)
    except OSError as exc:
        raise JournalCommitError(
            f"File changed; journal finalization failed. change_id={item['change_id']}"
        ) from exc
    context = current_tool_call_context()
    if context and context.event_writer:
        context.event_writer(
            {
                "type": "file_changed",
                "run_id": runtime.run_id,
                **{k: v for k, v in item.items() if k != "backup"},
            }
        )
    return item["change_id"]


def rewind(workspace: Path, run_id=None, *, files=None, apply=False):
    workspace = workspace.resolve()
    root = (workspace / ".mokioclaw/changes").resolve()
    if not root.is_relative_to(workspace):
        raise ValueError("change store escaped workspace")
    if run_id is not None and not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise ValueError("invalid run_id")
    candidates = sorted(root.glob("*/manifest.json"), key=lambda p: p.stat().st_mtime_ns)
    manifest = root / run_id / "manifest.json" if run_id else candidates[-1] if candidates else None
    if manifest is None or not manifest.exists():
        return {"ok": False, "error_type": "unknown_change_run", "changes": []}
    manifest = manifest.resolve()
    if not manifest.is_relative_to(root):
        raise ValueError("change journal escaped its store")
    with session_writer(workspace):
        data = json.loads(manifest.read_text())
        results, blocked, virtual = [], set(), {}
        for item in reversed(data["changes"]):
            if item["status"] == "restored" or (files and item["path"] not in files):
                continue
            target = (workspace / item["path"]).resolve()
            backup = (manifest.parent / item["backup"]).resolve()
            if not target.is_relative_to(workspace) or not backup.is_relative_to(
                manifest.parent.resolve()
            ):
                raise ValueError("change journal path escaped its workspace")
            current = virtual.get(item["path"], target.read_bytes() if target.exists() else None)
            original = backup.read_bytes()
            if digest(original if item["before_hash"] is not None else None) != item["before_hash"]:
                raise ValueError("backup integrity check failed")
            if item["status"] == "prepared" and digest(current) == item["before_hash"]:
                results.append(
                    {
                        "change_id": item["change_id"],
                        "path": item["path"],
                        "status": "not_applied",
                    }
                )
                continue
            if item["path"] in blocked or digest(current) != item["after_hash"]:
                blocked.add(item["path"])
                results.append(
                    {
                        "change_id": item["change_id"],
                        "path": item["path"],
                        "status": "conflict",
                    }
                )
                continue
            difference = "\n".join(
                difflib.unified_diff(
                    (current or b"").decode(errors="replace").splitlines(),
                    original.decode(errors="replace").splitlines(),
                    fromfile=item["path"],
                    tofile="restored/" + item["path"],
                    lineterm="",
                )
            )
            result = {
                "change_id": item["change_id"],
                "path": item["path"],
                "status": "ready",
                "diff": difference[:12000],
                "diff_truncated": len(difference) > 12000,
            }
            if result["diff_truncated"]:
                from mokioclaw.core.artifacts import DiagnosticArtifact

                artifact = DiagnosticArtifact(workspace, "rewind-diff")
                artifact.write(difference.encode())
                result.update(artifact.finish())
            virtual[item["path"]] = original if item["before_hash"] is not None else None
            if apply:
                if item["before_hash"] is None:
                    target.unlink()
                else:
                    atomic_bytes(target, original, item["mode"])
                item["status"] = result["status"] = "restored"
                _atomic_json(manifest, data)
                _atomic_json(
                    workspace / ".mokioclaw/changes/reverify.json",
                    {"required": True, "run_id": data["run_id"]},
                )
            results.append(result)
        return {
            "ok": not blocked,
            "run_id": data["run_id"],
            "changes": results,
            "applied": apply,
            "coverage": "FileWriteTool/FileEditTool only; Bash/MCP changes are not covered",
        }
