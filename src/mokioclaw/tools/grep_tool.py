from __future__ import annotations

import asyncio
import fnmatch
import itertools
import json
import os
import re
import shutil
import time
from pathlib import Path

from pathspec import GitIgnoreSpec

from mokioclaw.core.run_control import RunControl, check_run, run_control
from mokioclaw.tools.file_tools import display_path, resolve_workspace_path

SKIP_DIRS = {
    ".git",
    ".mokioclaw",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
}


def _iter_files(root: Path, glob_pattern=None, *, workspace=None):
    """Prune before descending; both backends share this file scope."""
    root = root.resolve()
    workspace = (workspace or root).resolve()
    rules = {}

    def ignored(path, directory=False):
        result = False
        for base, spec in rules.items():
            if path.is_relative_to(base):
                match = spec.check_file(
                    path.relative_to(base).as_posix() + ("/" if directory else "")
                )
                if match.include is not None:
                    result = match.include
        return result

    for base in reversed((root, *root.parents)):
        if base.is_relative_to(workspace) and (base / ".gitignore").is_file():
            rules[base] = GitIgnoreSpec.from_lines(
                (base / ".gitignore").read_text(errors="replace").splitlines()
            )
    for directory, dirs, files in os.walk(root, followlinks=False):
        base = Path(directory)
        ignore = base / ".gitignore"
        if ignore.is_file():
            rules[base] = GitIgnoreSpec.from_lines(ignore.read_text(errors="replace").splitlines())
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not ignored(base / d, True))
        for name in sorted(files):
            path = base / name
            if not path.resolve().is_relative_to(workspace) or ignored(path):
                continue
            if glob_pattern and not (
                fnmatch.fnmatch(name, glob_pattern)
                or path.relative_to(workspace).full_match(glob_pattern)
            ):
                continue
            yield path


async def _rg_lines(command):
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=16 * 1024 * 1024,
    )

    async def drain():
        tail = b""
        while chunk := await process.stderr.read(4096):
            tail = (tail + chunk)[-4096:]
        return tail

    stderr = asyncio.create_task(drain())
    try:
        while line := await process.stdout.readline():
            yield json.loads(line)
        code = await process.wait()
        detail = await stderr
        if code not in {0, 1}:
            raise RuntimeError(detail.decode(errors="replace"))
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()
        if not stderr.done():
            stderr.cancel()
        await asyncio.gather(stderr, return_exceptions=True)


def file_glob(state, pattern="**/*", path=".", limit=100):
    root = resolve_workspace_path(state, path)
    if not root.is_dir() or not 1 <= int(limit) <= 1000:
        return {"ok": False, "error": "expected directory and limit between 1 and 1000"}
    paths = []
    for item in _iter_files(root, pattern, workspace=state.workspace.resolve()):
        check_run(state)
        rel = display_path(state, item)
        paths.append(rel)
        if len(paths) > int(limit):
            break
    return {
        "ok": True,
        "paths": paths[: int(limit)],
        "truncated": len(paths) > int(limit),
    }


def grep(state, pattern, path=".", glob=None, head_limit=50, ignore_case=False):
    started = time.monotonic()
    try:
        limit = int(head_limit)
        if not pattern or limit <= 0:
            raise ValueError("pattern and positive head_limit are required")
        regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        root = resolve_workspace_path(state, path)
        if not root.exists():
            raise ValueError(f"path does not exist: {path}")
    except (ValueError, re.error) as exc:
        return {"ok": False, "error": str(exc)}

    def candidates():
        return (
            iter([root])
            if root.is_file()
            else _iter_files(root, glob, workspace=state.workspace.resolve())
        )

    matches = []
    backend = "python"
    rg = (
        shutil.which("rg")
        if state.settings.get("search", {}).get("backend", "auto") != "python"
        else None
    )
    control = run_control(state) or RunControl()
    owned = run_control(state) is None
    try:
        if rg:
            backend = "rg"
            files = candidates()
            try:
                while batch := list(itertools.islice(files, 128)):
                    command = [
                        rg,
                        "--json",
                        "--no-ignore",
                        "--hidden",
                        "--sort",
                        "path",
                    ]
                    if ignore_case:
                        command.append("-i")
                    command += ["-e", pattern, "--", *map(str, batch)]
                    stream = control.iter_async(lambda command=command: _rg_lines(command))
                    try:
                        for item in stream:
                            if item.get("type") == "match":
                                data = item["data"]
                                matches.append(
                                    {
                                        "path": display_path(state, Path(data["path"]["text"])),
                                        "line": data["line_number"],
                                        "text": data["lines"]["text"].rstrip("\r\n"),
                                    }
                                )
                                if len(matches) >= limit:
                                    break
                    finally:
                        stream.close()
                    if len(matches) >= limit:
                        break
            except RuntimeError:
                backend, matches = "python", []
        if backend == "python":
            for file in candidates():
                check_run(state)
                with file.open("rb") as probe:
                    if b"\0" in probe.read(4096):
                        continue
                with file.open(encoding="utf-8", errors="replace") as handle:
                    for idx, line in enumerate(handle, 1):
                        check_run(state)
                        if regex.search(line):
                            matches.append(
                                {
                                    "path": display_path(state, file),
                                    "line": idx,
                                    "text": line.rstrip("\r\n"),
                                }
                            )
                            if len(matches) >= limit:
                                break
                if len(matches) >= limit:
                    break
    finally:
        if owned:
            control.close()
    return {
        "ok": True,
        "pattern": pattern,
        "matches": matches,
        "truncated": len(matches) >= limit,
        "backend": backend,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
    }
