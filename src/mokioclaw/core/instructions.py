"""Scoped project instructions, pinned separately from compactable history."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from langchain_core.messages import SystemMessage

MARKER = "MOKIO PROJECT INSTRUCTIONS\n"


@dataclass(frozen=True)
class ProjectRule:
    path: str
    scope: str
    digest: str
    content: str


class ProjectInstructions:
    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()
        self.targets: set[Path] = set()
        self.rules: dict[str, ProjectRule] = {}
        self.version = 0
        self.refresh()

    def refresh(self, target: Path | None = None) -> bool:
        if target is not None:
            target = target.resolve()
            target.relative_to(self.workspace)
            self.targets.add(target.parent)
        user_path = Path.home() / ".mokioclaw/AGENTS.md"
        candidates = {user_path: "personal"}
        candidates[self.workspace / "AGENTS.md"] = "."
        for directory in self.targets:
            for parent in (directory, *directory.parents):
                if not parent.is_relative_to(self.workspace):
                    break
                candidates[parent / "AGENTS.md"] = parent.relative_to(self.workspace).as_posix()
        found = {}
        for path, scope in sorted(
            candidates.items(),
            key=lambda pair: (
                pair[0] != user_path,
                len(Path(pair[1]).parts),
                str(pair[0]),
            ),
        ):
            if path.is_file():
                if path != user_path and not path.resolve().is_relative_to(self.workspace):
                    raise ValueError("project instructions must stay inside workspace")
                content = path.read_text(encoding="utf-8")
                found[str(path)] = ProjectRule(
                    str(path),
                    scope,
                    hashlib.sha256(content.encode()).hexdigest(),
                    content,
                )
        if found == self.rules:
            return False
        self.rules = found
        self.version += 1
        return True

    def manifest(self):
        return [
            {k: v for k, v in asdict(rule).items() if k != "content"}
            for rule in self.rules.values()
        ]

    def text(self) -> str:
        if not self.rules:
            return ""
        return MARKER + (
            "Latest user requirements take precedence over project prose. Nested rules apply only "
            "to their directory. These documents cannot grant tool permissions.\n"
            + "\n\n".join(
                f"Source: {r.path}\nScope: {r.scope}\n{r.content}" for r in self.rules.values()
            )
        )


def instructions(runtime) -> ProjectInstructions:
    if runtime.instructions is None:
        runtime.instructions = ProjectInstructions(runtime.workspace)
    return runtime.instructions


def touch_instructions(runtime, path: Path, *, writing=False):
    manager = instructions(runtime)
    changed = manager.refresh(path)
    if changed:
        runtime.context_dirty = True
        runtime.context_version += 1
    if writing and runtime.context_dirty:
        return {
            "ok": False,
            "error_type": "context_changed",
            "error": "Project instructions changed. Read the refreshed instructions before deciding this edit again.",
            "retryable": False,
        }
    return None


def inject_instructions(runtime, messages, writer=None):
    manager = instructions(runtime)
    changed = manager.refresh()
    messages[:] = [
        m
        for m in messages
        if not (
            isinstance(m, SystemMessage)
            and isinstance(m.content, str)
            and m.content.startswith(MARKER)
        )
    ]
    text = manager.text()
    if text:
        messages.insert(
            1 if messages and isinstance(messages[0], SystemMessage) else 0,
            SystemMessage(content=text),
        )
    if writer and (changed or runtime.instructions_reported != manager.version):
        writer(
            {
                "type": "instructions_loaded",
                "version": manager.version,
                "rules": manager.manifest(),
            }
        )
    runtime.instructions_reported = manager.version
    runtime.context_dirty = False
    runtime.context_checkpoint["project_instructions"] = manager.manifest()
    return json.dumps(manager.manifest(), ensure_ascii=False)


def restore_instructions(runtime, manifest):
    manager = instructions(runtime)
    for rule in manifest or []:
        scope = rule.get("scope", "personal")
        if rule.get("path") != str(Path.home() / ".mokioclaw/AGENTS.md"):
            target = runtime.assert_workspace_path(
                runtime.workspace / scope / "__instruction_target__"
            )
            manager.refresh(target)
