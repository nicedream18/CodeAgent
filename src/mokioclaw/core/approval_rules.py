"""User-owned, project-scoped approval memory with exact resource matching."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

_LOCK = threading.RLock()
_SESSION_RULES: dict[str, list[dict[str, Any]]] = {}


class ApprovalRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    tool: str
    resource: str
    allowed: bool
    scope: Literal["session", "project"]
    match_scope: Literal["exact", "directory"] = "exact"
    last_used: float | None = None


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    with _LOCK, path.with_suffix(".lock").open("a+b") as handle:
        path.with_suffix(".lock").chmod(0o600)
        if sys.platform == "win32":
            import msvcrt

            handle.write(b"0")
            handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if sys.platform == "win32":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def args_digest(args: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(args, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


class ApprovalRules:
    def __init__(
        self,
        workspace: Path,
        session_id: str = "",
        session_cache: dict[str, list[dict[str, Any]]] | None = None,
        execution_context: str = "local",
    ):
        self.execution_context = execution_context
        self.workspace = workspace.expanduser().resolve()
        self.project = hashlib.sha256(os.path.normcase(str(self.workspace)).encode()).hexdigest()
        self.path = Path.home() / ".mokioclaw/project-permissions" / f"{self.project}.json"
        self.session_key = self.project + ":" + session_id
        self.cache = _SESSION_RULES if session_cache is None else session_cache

    def _read(self) -> list[ApprovalRule]:
        if not self.path.exists():
            return []
        raw = json.loads(self.path.read_text())
        if raw.get("workspace") != str(self.workspace):
            raise ValueError("permission workspace mismatch")
        return [ApprovalRule.model_validate(item) for item in raw.get("rules", [])]

    def _write(self, rules: list[ApprovalRule]) -> None:
        temporary = self.path.with_name(self.path.name + "." + uuid4().hex + ".tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    {"workspace": str(self.workspace), "rules": [r.model_dump() for r in rules]},
                    handle,
                    ensure_ascii=False,
                )
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def list_rules(self) -> dict[str, Any]:
        if not self.path.exists() and not self.cache.get(self.session_key):
            return {"workspace": str(self.workspace), "rules": [], "policy_version": self.version()}
        with _locked(self.path):
            project = self._read()
            session = [ApprovalRule.model_validate(r) for r in self.cache.get(self.session_key, [])]
            return {
                "workspace": str(self.workspace),
                "rules": [r.model_dump() for r in [*session, *project]],
                "policy_version": self.version(),
            }

    def version(self) -> int:
        values = [*self.cache.get(self.session_key, []), *[r.model_dump() for r in self._read()]]
        auth = [{k: v for k, v in r.items() if k != "last_used"} for r in values]
        return int(args_digest({"rules": auth})[:12], 16)

    def resource(
        self, tool: str, args: dict[str, Any], servers: list[dict[str, Any]] | None = None
    ) -> str:
        if tool in {"FileReadTool", "FileWriteTool", "FileEditTool"}:
            path = (self.workspace / str(args.get("file_path", ""))).resolve()
            return "file:" + path.relative_to(self.workspace).as_posix()
        if tool == "BashTool":
            return (
                (
                    "bash:"
                    if self.execution_context == "local"
                    else "bash@" + self.execution_context + ":"
                )
                + str(args.get("command", "")).strip()
                + ":"
                + args_digest({k: v for k, v in args.items() if k != "command"})
            )
        if tool.startswith("MCP_"):
            from mokioclaw.tools.mcp_tools import _safe_name

            server = next(
                (
                    s
                    for s in sorted(servers or [], key=lambda s: len(str(s["name"])), reverse=True)
                    if tool.startswith("MCP_" + _safe_name(str(s["name"])) + "_")
                ),
                None,
            )
            return "mcp:" + args_digest({"server": server, "args": args})
        return "args:" + args_digest(args)

    def remember(
        self,
        tool: str,
        args: dict[str, Any],
        allowed: bool,
        scope: Literal["session", "project"],
        match_scope: Literal["exact", "directory"] = "exact",
        servers: list[dict[str, Any]] | None = None,
    ) -> ApprovalRule:
        resource = self.resource(tool, args, servers)
        if match_scope == "directory":
            if not resource.startswith("file:"):
                raise ValueError("directory scope only applies to file tools")
            resource = "file:" + str(Path(resource[5:]).parent).replace("\\", "/")
        rule = ApprovalRule(
            id=uuid4().hex,
            tool=tool,
            resource=resource,
            allowed=allowed,
            scope=scope,
            match_scope=match_scope,
        )
        with _locked(self.path):
            if rule.scope == "session":
                values = self.cache.setdefault(self.session_key, [])
                values[:] = [
                    v
                    for v in values
                    if not (
                        v["tool"] == tool
                        and v["resource"] == resource
                        and v["match_scope"] == match_scope
                    )
                ]
                values.append(rule.model_dump())
            else:
                project_values = [
                    r
                    for r in self._read()
                    if not (
                        r.tool == tool and r.resource == resource and r.match_scope == match_scope
                    )
                ]
                self._write([*project_values, rule])
        return rule

    def match(
        self, tool: str, args: dict[str, Any], servers: list[dict[str, Any]] | None = None
    ) -> ApprovalRule | None:
        if not self.path.exists() and not self.cache.get(self.session_key):
            return None
        resource = self.resource(tool, args, servers)
        with _locked(self.path):
            candidates = [
                ApprovalRule.model_validate(r) for r in self.cache.get(self.session_key, [])
            ] + self._read()
            matching = [
                r
                for r in candidates
                if r.tool == tool
                and (
                    r.resource == resource
                    or r.match_scope == "directory"
                    and resource.startswith("file:")
                    and (
                        r.resource == "file:."
                        or Path(resource[5:]).is_relative_to(Path(r.resource[5:]))
                    )
                )
            ]
            matching.sort(key=lambda r: (r.allowed, r.scope != "session"))
            if not matching:
                return None
            result = matching[0]
            result.last_used = time.time()
            if result.scope == "session":
                self.cache[self.session_key] = [
                    result.model_dump() if r["id"] == result.id else r
                    for r in self.cache[self.session_key]
                ]
            else:
                self._write([result if r.id == result.id else r for r in self._read()])
            return result

    def revoke(self, rule_id: str | None = None, *, clear: bool = False) -> bool:
        with _locked(self.path):
            project = self._read()
            session = self.cache.get(self.session_key, [])
            changed = (
                bool(project or session)
                if clear
                else any(r.id == rule_id for r in project)
                or any(r["id"] == rule_id for r in session)
            )
            self.cache[self.session_key] = (
                [] if clear else [r for r in session if r["id"] != rule_id]
            )
            if project:
                self._write([] if clear else [r for r in project if r.id != rule_id])
            return changed


def runtime_rules(runtime: Any) -> ApprovalRules:
    if not runtime.run_id:
        runtime.run_id = "local-" + uuid4().hex
    cache = getattr(getattr(runtime, "run_control", None), "approval_sessions", None)
    from mokioclaw.core.execution_backend import execution_fingerprint

    return ApprovalRules(
        runtime.workspace,
        getattr(runtime, "session_id", "") or runtime.run_id,
        cache,
        execution_context=execution_fingerprint(runtime),
    )
