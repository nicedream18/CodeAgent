from __future__ import annotations

import os
import time

import pytest

from mokioclaw.core.approval_rules import ApprovalRules
from mokioclaw.core.execution_backend import (
    BackendUnavailable,
    DockerBackend,
    ExecutionRequest,
    LocalBackend,
    doctor,
    resolve_image,
)
from mokioclaw.core.state import RuntimeState
from mokioclaw.tools.bash_tool import run_bash


def test_local_timeout_kills_descendants_and_output_is_bounded(tmp_path):
    import sys

    result = LocalBackend().run(
        ExecutionRequest(
            tmp_path,
            f"'{sys.executable}' -c \"print('x'*3000000)\"; (sleep 2; touch late) & wait",
            timeout_seconds=0.3,
            max_output_chars=100,
        )
    )
    assert result.timed_out
    assert len(result.stdout) == 100
    assert result.metadata["stdout_output_truncated"]
    assert (tmp_path / result.metadata["stdout_path"]).stat().st_size <= 1024 * 1024
    time.sleep(2.1)
    assert not (tmp_path / "late").exists()


def test_backend_rules_keep_legacy_local_and_separate_images(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    args = {"command": "echo test"}
    docker = ApprovalRules(tmp_path, execution_context="docker:sha256:one")
    rule = docker.remember("BashTool", args, True, "project")
    assert docker.match("BashTool", args).id == rule.id
    assert ApprovalRules(tmp_path).match("BashTool", args) is None
    assert (
        ApprovalRules(tmp_path, execution_context="docker:sha256:two").match("BashTool", args)
        is None
    )
    local = ApprovalRules(tmp_path)
    assert local.resource("BashTool", args).startswith("bash:")


def test_docker_unavailable_never_falls_back(monkeypatch, tmp_path):
    def fail(*args):
        raise BackendUnavailable("engine off")

    monkeypatch.setattr("mokioclaw.core.execution_backend.resolve_image", fail)
    result = run_bash(
        RuntimeState(workspace=tmp_path, execution_backend="docker"), "touch forbidden"
    )
    assert not result["ok"] and result["backend"] == "docker"
    assert not (tmp_path / "forbidden").exists()
    assert not doctor("docker")["ok"]


def test_docker_background_is_rejected_before_launch(tmp_path):
    result = run_bash(
        RuntimeState(workspace=tmp_path, execution_backend="docker"),
        "touch forbidden",
        run_in_background=True,
    )
    assert not result["ok"] and "background" in result["error"]
    assert not (tmp_path / "forbidden").exists()


@pytest.mark.skipif(
    os.getenv("MOKIO_TEST_DOCKER") != "1", reason="make check-docker runs real containers"
)
def test_real_docker_isolation_timeout_and_paths(tmp_path, monkeypatch):
    image = resolve_image(os.getenv("MOKIO_DOCKER_IMAGE", "mokioclaw-execution:0.2.0"))
    workspace = tmp_path / "中文 workspace"
    workspace.mkdir()
    workspace.chmod(0o777)
    monkeypatch.setenv("MOKIO_TEST_HOST_SECRET", "not-in-container")
    outside = tmp_path / "host-only"
    outside.write_text("host-only")
    command = """python - <<'PY'
import os, pathlib, socket
assert os.getuid() != 0
assert 'MOKIO_TEST_HOST_SECRET' not in os.environ
assert not pathlib.Path('/var/run/docker.sock').exists()
assert not os.access('/root/.ssh', os.R_OK)
assert not pathlib.Path('../host-only').exists()
assert pathlib.Path('/proc/sys/kernel/cap_last_cap').exists()
assert 'CapEff:\\t0000000000000000' in pathlib.Path('/proc/self/status').read_text()
try:
    pathlib.Path('/etc/host-write').write_text('bad')
except OSError:
    pass
else:
    raise AssertionError('writable root')
try:
    socket.create_connection(('1.1.1.1', 443), timeout=0.5)
except OSError:
    pass
else:
    raise AssertionError('external network available')
assert pathlib.Path('/sys/fs/cgroup/pids.max').read_text().strip() == '128'
assert pathlib.Path('/sys/fs/cgroup/memory.max').read_text().strip() == str(2*1024**3)
assert pathlib.Path('/sys/fs/cgroup/cpu.max').read_text().split() == ['200000','100000']
pathlib.Path('输出.txt').write_text('中文')
print('isolation checks passed')
PY"""
    backend = DockerBackend(image)
    result = backend.run(ExecutionRequest(workspace, command, owner="test-isolation"))
    assert result.ok, result.stderr
    assert (workspace / "输出.txt").read_text() == "中文"
    result = backend.run(
        ExecutionRequest(
            workspace, "(sleep 3; touch late) & wait", timeout_seconds=0.7, owner="test-timeout"
        )
    )
    assert result.timed_out
    time.sleep(3.1)
    assert not (workspace / "late").exists()
    from mokioclaw.core.execution_backend import docker_cli

    assert not docker_cli(
        ["ps", "-aq", "--filter", "label=mokioclaw.owner=test-timeout"]
    ).stdout.strip()
    result = backend.run(
        ExecutionRequest(
            workspace,
            "python -c \"print('x'*3000000)\"; echo fault >&2; exit 7",
            max_output_chars=100,
        )
    )
    assert not result.ok and result.exit_code == 7
    assert len(result.stdout) == 100 and "fault" in result.stderr
    assert (workspace / result.metadata["stdout_path"]).stat().st_size <= 1024 * 1024
    (workspace / "readonly.txt").write_text("a")
    (workspace / "readonly.txt").chmod(0o400)
    assert not backend.run(ExecutionRequest(workspace, "echo b >> readonly.txt")).ok


def test_missing_image_is_controlled(monkeypatch):
    def fail(*args, **kwargs):
        raise BackendUnavailable("No such image")

    monkeypatch.setattr("mokioclaw.core.execution_backend.docker_cli", fail)
    with pytest.raises(BackendUnavailable):
        resolve_image("missing")


def test_runtime_explicit_backend_and_config_precedence(tmp_path, monkeypatch):
    from mokioclaw.core.agent import create_runtime

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MOKIO_EXECUTION_BACKEND", "docker")
    assert create_runtime(tmp_path, execution_backend="local").execution_backend == "local"


def test_once_approval_is_invalid_after_backend_change(tmp_path):
    from mokioclaw.core.approval import ApprovalDecision
    from mokioclaw.tools.execution import execute_tool
    from mokioclaw.tools.registry import build_tools

    runtime = RuntimeState(workspace=tmp_path, tool_policies={"BashTool": "ask"})

    def approve(request):
        assert request.execution_context == "local"
        runtime.execution_backend = "docker"
        runtime.execution_image_digest = "sha256:fixture"
        return ApprovalDecision(True)

    runtime.approval_handler = approve
    tool = next(t for t in build_tools(runtime) if t.name == "BashTool")
    result = execute_tool(runtime, tool, {"command": "curl --version; touch forbidden"})
    assert not result["ok"]
    assert not (tmp_path / "forbidden").exists()


@pytest.mark.parametrize("mode", ["light", "strict"])
def test_resume_with_different_backend_redecides_and_reverifies(tmp_path, monkeypatch, mode):
    from mokioclaw.core import agent
    from mokioclaw.core.checkpoint import CheckpointManager

    runtime = RuntimeState(workspace=tmp_path, checkpoint_mode=mode)
    CheckpointManager(runtime, task="fix code").save(
        {
            "runtime": runtime,
            "task": "fix code",
            "passed": True,
            "messages": [],
            "verification_results": [{"ok": True}],
        }
    )
    observed = []

    class Workflow:
        def stream(self, inputs, **kwargs):
            observed.append(inputs)
            yield "updates", {"final": {"final_answer": "done"}}

    monkeypatch.setattr(agent, "build_complex_workflow", Workflow)
    monkeypatch.setattr(
        "mokioclaw.core.execution_backend.resolve_image", lambda image: "sha256:fixture"
    )
    list(
        agent.stream_agent_events(
            resume_workspace=tmp_path, checkpoint_mode=mode, execution_backend="docker"
        )
    )
    assert observed and observed[0]["context_next_node"] == "planner"
    assert not observed[0]["passed"] and not observed[0]["verification_results"]
    assert observed[0]["metadata"]["execution_context_changed"]
