"""Foreground command execution. Docker has no implicit host fallback.

The Docker CLI runs on the host; its environment is never passed to the container.
Containers are created before attachment so timeout cleanup owns an exact ID.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from mokioclaw.core.artifacts import DiagnosticArtifact


class BackendUnavailable(ValueError):
    """An explicit backend cannot run; callers must not fall back."""


@dataclass(frozen=True)
class ExecutionRequest:
    workspace: Path
    command: str
    timeout_seconds: float = 60
    max_output_chars: int = 6000
    env: dict[str, str] = field(default_factory=dict)
    owner: str = ""


@dataclass
class ExecutionResult:
    ok: bool
    exit_code: int | None
    timed_out: bool
    stdout: str
    stderr: str
    duration_ms: int
    backend: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {**vars(self), **self.metadata, "metadata": self.metadata}


class ExecutionBackend(Protocol):
    def run(self, request: ExecutionRequest) -> ExecutionResult: ...


class _Capture:
    def __init__(self, workspace: Path, limit: int, kind: str):
        self.limit = limit
        self.head = b""
        self.tail = b""
        self.count = 0
        self.error = ""
        self.artifact: DiagnosticArtifact | None = None
        try:
            self.artifact = DiagnosticArtifact(workspace, kind)
        except OSError as exc:
            self.error = str(exc)

    def drain(self, pipe: Any) -> None:
        try:
            while chunk := pipe.read(8192):
                self.count += len(chunk)
                self.head = (self.head + chunk)[: self.limit * 4]
                self.tail = (self.tail + chunk)[-self.limit * 4 :]
                if self.artifact is not None and not self.error:
                    try:
                        self.artifact.write(chunk)
                    except OSError as exc:
                        self.error = str(exc)
        finally:
            pipe.close()

    def finish(self, key: str) -> tuple[str, dict[str, Any]]:
        text = self.head.decode("utf-8", errors="replace")
        if self.count > len(self.head) or len(text) > self.limit:
            tail = self.tail.decode("utf-8", errors="replace")
            half = max(1, (self.limit - 3) // 2)
            text = text[:half] + "\n…\n" + tail[-(self.limit - 3 - half) :]
        info: dict[str, Any] = {key + "_truncated": self.count > len(text.encode())}
        if self.artifact is not None:
            try:
                saved = self.artifact.finish()
                info.update({key + "_" + k: v for k, v in saved.items()})
                if self.count > len(text.encode()):
                    info[key + "_path"] = saved["full_output_path"]
            except OSError as exc:
                self.error = str(exc)
        if self.error:
            info[key + "_artifact_error"] = self.error
        return text, info


def _collect(
    argv: list[str],
    request: ExecutionRequest,
    backend: str,
    *,
    stop: Any = None,
    env: dict[str, str] | None = None,
) -> ExecutionResult:
    started = time.monotonic()
    captures = [
        _Capture(request.workspace, request.max_output_chars, f"bash-{s}")
        for s in ("stdout", "stderr")
    ]
    try:
        process = subprocess.Popen(
            argv,
            cwd=request.workspace,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name != "nt",
        )
    except OSError as exc:
        for i, capture in enumerate(captures):
            capture.finish(str(i))
        raise BackendUnavailable(str(exc)) from exc
    threads = [
        threading.Thread(target=c.drain, args=(p,), daemon=True)
        for c, p in zip(captures, [process.stdout, process.stderr], strict=True)
    ]
    for thread in threads:
        thread.start()
    timed_out = False

    def kill_group() -> None:
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                if process.poll() is None:
                    process.kill()
        else:
            process.kill()

    try:
        process.wait(timeout=request.timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        if stop:
            stop()
        kill_group()
        process.wait(timeout=10)
    finally:
        # Foreground shells must not leave detached descendants holding pipes/writing later.
        if backend == "local":
            kill_group()
        if stop:
            stop()
        for thread in threads:
            thread.join(timeout=10)
    stdout, out = captures[0].finish("stdout")
    stderr, err = captures[1].finish("stderr")
    return ExecutionResult(
        process.returncode == 0 and not timed_out,
        None if timed_out else process.returncode,
        timed_out,
        stdout,
        stderr,
        round((time.monotonic() - started) * 1000),
        backend,
        {**out, **err},
    )


class LocalBackend:
    def run(self, request: ExecutionRequest) -> ExecutionResult:
        argv = (
            [os.environ.get("COMSPEC", "cmd.exe"), "/c", request.command]
            if os.name == "nt"
            else ["/bin/sh", "-c", request.command]
        )
        return _collect(argv, request, "local", env=request.env or None)


def docker_cli(args: list[str], timeout: float = 20) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackendUnavailable(f"Docker unavailable: {exc}") from exc
    if result.returncode:
        raise BackendUnavailable(
            (result.stderr or result.stdout or "Docker command failed")[-4096:]
        )
    return result


def resolve_image(image: str) -> str:
    docker_cli(["info", "--format", "{{.OSType}}"])
    value = docker_cli(["image", "inspect", "--format", "{{.Id}}", image]).stdout.strip()
    if not value.startswith("sha256:") or len(value) != 71:
        raise BackendUnavailable("Docker image has no immutable ID")
    return value


class DockerBackend:
    def __init__(self, image_id: str, *, cpus: int = 2, memory_mib: int = 2048, pids: int = 128):
        if not image_id.startswith("sha256:"):
            raise BackendUnavailable("Resolve and pin the image before execution")
        self.image_id, self.cpus, self.memory_mib, self.pids = image_id, cpus, memory_mib, pids

    def run(self, request: ExecutionRequest) -> ExecutionResult:
        path = request.workspace.resolve()
        if "," in str(path):
            raise BackendUnavailable("Docker --mount does not support commas in workspace paths")
        uid = os.getuid() if hasattr(os, "getuid") and os.getuid() else 65534
        gid = os.getgid() if hasattr(os, "getgid") and os.getgid() else 65534
        name = "mokio-" + uuid4().hex
        args = [
            "create",
            "--name",
            name,
            "--label",
            "mokioclaw.execution=true",
            "--label",
            "mokioclaw.owner=" + request.owner,
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            f"{uid}:{gid}",
            "--cpus",
            str(self.cpus),
            "--memory",
            f"{self.memory_mib}m",
            "--memory-swap",
            f"{self.memory_mib}m",
            "--pids-limit",
            str(self.pids),
            "--log-driver",
            "none",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=64m,mode=1777",
            "--mount",
            f"type=bind,src={path},dst={path}",
            "--workdir",
            str(path),
            "--env",
            "HOME=/tmp",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--entrypoint",
            "/bin/sh",
            self.image_id,
            "-c",
            request.command,
        ]
        container_id = ""

        def cleanup() -> None:
            try:
                docker_cli(["rm", "--force", container_id or name])
            except BackendUnavailable:
                pass  # Already removed by the owning evaluation watchdog.

        try:
            container_id = docker_cli(args).stdout.strip()
            result = _collect(
                ["docker", "start", "--attach", container_id], request, "docker", stop=cleanup
            )
            result.metadata.update(
                image_digest=self.image_id,
                container_id=container_id,
                limits={"cpus": self.cpus, "memory_mib": self.memory_mib, "pids": self.pids},
                network="none",
                read_only_root=True,
            )
            return result
        finally:
            cleanup()


def execution_fingerprint(runtime: Any) -> str:
    backend = runtime.execution_backend
    if backend == "local":
        return "local"
    if backend != "docker":
        raise BackendUnavailable("execution_backend must be local or docker")
    if not runtime.execution_image_digest:
        runtime.execution_image_digest = resolve_image(runtime.docker_image)
    return "docker:" + str(runtime.execution_image_digest)


def backend_for(runtime: Any) -> ExecutionBackend:
    fingerprint = execution_fingerprint(runtime)
    return (
        LocalBackend() if fingerprint == "local" else DockerBackend(runtime.execution_image_digest)
    )


def doctor(backend: str, image: str = "mokioclaw-execution:0.2.0") -> dict[str, Any]:
    try:
        if backend == "local":
            return {"ok": True, "backend": "local"}
        if backend != "docker":
            raise BackendUnavailable("execution_backend must be local or docker")
        image_id = resolve_image(image)
        info = json.loads(docker_cli(["info", "--format", "{{json .}}"]).stdout)
        return {
            "ok": True,
            "backend": "docker",
            "image_digest": image_id,
            "platform": str(info["OSType"]) + "/" + str(info["Architecture"]),
        }
    except (BackendUnavailable, ValueError) as exc:
        return {"ok": False, "backend": backend, "error": str(exc)}
