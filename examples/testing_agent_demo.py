"""Offline testAgent demonstration: a false success claim cannot hide a failed check."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from langchain_core.messages import AIMessage

from mokioclaw.agents import test_agent
from mokioclaw.agents.registry import run_subagent
from mokioclaw.core.state import RuntimeState


class ScriptedModel:
    def __init__(self):
        self.step = 0

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        self.step += 1
        if self.step == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "check",
                        "name": "BashTool",
                        "args": {"command": "python -m pytest -q test_add.py"},
                    }
                ],
            )
        return AIMessage(content="All tests passed!")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execution-backend", choices=["local", "docker"], default="local")
    parser.add_argument("--docker-image", default="mokioclaw-execution:0.2.0")
    args = parser.parse_args()
    outcomes = []
    for fixed in (False, True):
        with TemporaryDirectory(prefix="mokio-testing-demo-") as temp:
            workspace = Path(temp)
            operation = "+" if fixed else "-"
            (workspace / "app.py").write_text(f"def add(a, b):\n    return a {operation} b\n")
            (workspace / "test_add.py").write_text(
                "from app import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
            )
            runtime = RuntimeState(
                workspace=workspace,
                approval_mode="auto",
                execution_backend=args.execution_backend,
                docker_image=args.docker_image,
            )
            with patch.object(test_agent, "create_model", ScriptedModel):
                result = run_subagent(
                    {"runtime": runtime, "task": "Verify add(2,3) equals 5"},
                    "testAgent",
                    "Run the existing test",
                )
            report = result["state_updates"]["test_report"]
            assert report["status"] == ("passed" if fixed else "failed")
            outcomes.append(
                {
                    "fixed": fixed,
                    "status": report["status"],
                    "exit_code": report["commands"][0]["exit_code"],
                }
            )
    print(
        json.dumps(
            {
                "simulation": True,
                "real_model": False,
                "real_checks": True,
                "execution_backend": args.execution_backend,
                "outcomes": outcomes,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
