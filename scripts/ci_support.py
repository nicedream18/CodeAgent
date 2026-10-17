"""Standard-library CI reporting: bounded output, redaction and isolated children."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAX_LOG = 1024 * 1024
MAX_LINE = 8192
MAX_REPORT_TOTAL = 16 * 1024 * 1024


def credential_values(values: Mapping[str, str | None]) -> list[str]:
    return [v for k, v in values.items() if v and len(v) >= 8 and (
        k.upper().endswith(("_KEY", "_TOKEN", "_PASSWORD", "_SECRET"))
        or k.upper() in {"TOKEN", "PASSWORD", "SECRET"})]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def redact(text: str, secrets: list[str]) -> str:
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)(authorization\s*:\s*)[^\r\n]*", r"\1[REDACTED]", text)
    text = re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1[REDACTED]@", text)
    return re.sub(r"(?i)((?:api[_-]?key|access[_-]?token|github_token|gh_token)\s*[=:]\s*)[^\s\"']+",
                  r"\1[REDACTED]", text)


def child_environment(home: Path, *, repository: Path | None = None) -> dict[str, str]:
    allowed = {"PATH", "SYSTEMROOT", "TMPDIR", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR",
               "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "UV_CACHE_DIR",
               "UV_PYTHON_INSTALL_DIR", "UV_SYSTEM_CERTS", "UV_OFFLINE"}
    environment = {k: v for k, v in os.environ.items() if k in allowed}
    environment.setdefault("DOCKER_CONFIG", str(Path.home() / ".docker"))
    environment.setdefault("UV_CACHE_DIR", str(Path.home() / ".cache/uv"))
    environment.update(HOME=str(home), CI="1", PYTHON_DOTENV_DISABLED="1",
                       PYTHONUNBUFFERED="1", PYTHONNOUSERSITE="1", NO_COLOR="1",
                       UV_NO_PROGRESS="1", UV_PYTHON=sys.executable)
    if repository:
        environment["PYTHONPATH"] = str(repository / "scripts")
    return environment


def run_command(command: list[str], *, cwd: Path, environment: dict[str, str], log: Path,
                timeout: float = 900, limit: int = MAX_LOG) -> dict[str, Any]:
    started = time.monotonic()
    log.parent.mkdir(parents=True, exist_ok=True)
    secrets = credential_values(os.environ)
    total, sanitized, retained, tail, long_line = 0, 0, 0, "", False
    errors: list[str] = []
    result: dict[str, Any] = {"command": [redact(arg, secrets) for arg in command],
                              "status": "running", "exit_code": None,
                              "log": log.name, "timeout_seconds": timeout}
    with log.open("wb") as handle:
        log.chmod(0o600)
        try:
            process = subprocess.Popen(command, cwd=cwd, env=environment, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as exc:
            text = redact(f"{type(exc).__name__}: {exc}", secrets)
            handle.write(text.encode()[:limit])
            return {**result, "status": "failed", "error": text,
                    "seconds": round(time.monotonic() - started, 3), "log_truncated": False}

        def drain() -> None:
            nonlocal total, sanitized, retained, tail, long_line
            assert process.stdout is not None
            dropping = False
            try:
                while raw := process.stdout.readline(MAX_LINE):
                    total += len(raw)
                    if dropping:
                        dropping = not raw.endswith(b"\n")
                        continue
                    if len(raw) == MAX_LINE and not raw.endswith(b"\n"):
                        # Never retain a partial credential/authorization line.
                        dropping, long_line = True, True
                        safe = "[oversized output line omitted]\n"
                    else:
                        safe = redact(raw.decode(errors="replace"), secrets)
                    tail = (tail + safe)[-4096:]
                    data = safe.encode()
                    sanitized += len(data)
                    accepted = data[:max(0, limit - retained)]
                    retained += handle.write(accepted)
            except OSError as exc:
                errors.append(type(exc).__name__)

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        timed_out = False
        try:
            process.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            timed_out = True
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
        reader.join(timeout=3)
        if reader.is_alive():
            # A child retaining the pipe also belongs to this command's group.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            reader.join(timeout=3)
        result.update(status="passed" if process.returncode == 0 and not timed_out and not errors
                      and not reader.is_alive() else "failed", exit_code=process.returncode,
                      timed_out=timed_out, seconds=round(time.monotonic() - started, 3),
                      output_bytes=total, log_bytes=retained,
                      log_truncated=sanitized > retained or long_line, tail=tail,
                      output_complete=not reader.is_alive() and not errors, output_errors=errors)
    return result


def metadata(repository: Path) -> dict[str, Any]:
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
    files = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard",
                                     "--", "src", "scripts", "tests", "examples",
                                     "pyproject.toml", "uv.lock", "Makefile", "README.md",
                                     ".gitignore", ".gitattributes", ".github", "docker"],
                                    cwd=repository, text=True).splitlines()
    hashes = {name: sha(repository / name) for name in files if (repository / name).is_file()}
    run = os.getenv("GITHUB_RUN_ID")
    repo = os.getenv("GITHUB_REPOSITORY")
    server = os.getenv("GITHUB_SERVER_URL", "https://github.com")
    return {"commit": commit, "source_and_checks_sha256": hashlib.sha256(
        json.dumps(hashes, sort_keys=True).encode()).hexdigest(),
        "lock_sha256": sha(repository / "uv.lock"), "python": sys.version,
        "platform": platform.platform(), "machine": platform.machine(),
        "recorded_at": datetime.now(UTC).isoformat(), "workflow": os.getenv("GITHUB_WORKFLOW"),
        "job": os.getenv("GITHUB_JOB"), "run_id": run, "run_attempt": os.getenv("GITHUB_RUN_ATTEMPT"),
        "run_url": f"{server}/{repo}/actions/runs/{run}" if repo and run else None,
        "dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=repository,
                                               text=True).strip()), "real_model_calls": 0,
        "log_limit_bytes": MAX_LOG, "total_report_limit_bytes": MAX_REPORT_TOTAL}


def save_report(output: Path, report: dict[str, Any]) -> None:
    completed = {s["name"]: s for s in report["steps"]}
    report["checks"] = [completed.get(name, {"name": name, "status": "not_started",
                                            "exit_code": None, "seconds": None})
                        for name in report.get("planned_steps", completed)]
    write_json(output / "report.json", report)
    rows = ["# CI check report", "", f"Status: {report['status']}",
            f"Commit: {report['metadata']['commit']}",
            f"Platform: {report['metadata']['platform']}", "",
            "| Check | Status | Exit | Seconds | Log truncated |", "|---|---|---|---|---|"]
    for step in report["checks"]:
        rows.append(f"| {step['name']} | {step['status']} | {step.get('exit_code')} | "
                    f"{step.get('seconds')} | {step.get('log_truncated', False)} |")
    rows += ["", "Machine-readable counts, image and wheel provenance are in report.json.",
             "No model credentials are forwarded to child checks.", ""]
    (output / "summary.md").write_text("\n".join(rows))
    total = sum(p.stat().st_size for p in output.rglob("*") if p.is_file())
    if total > MAX_REPORT_TOTAL:
        raise ValueError("CI report exceeds its 16 MiB quota")
