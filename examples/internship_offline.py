"""Deterministic shared-loop demonstration, explicitly not real-model evidence."""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage

from mokioclaw.evaluation.models import EvalTrialConfig
from mokioclaw.evaluation.worker import main


class ScriptedModel:
    def __init__(self):
        self.step = 0

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        calls = [
            {"name": "FileReadTool", "id": "read", "args": {"file_path": "app.py"}},
            {
                "name": "FileEditTool",
                "id": "edit",
                "args": {
                    "file_path": "app.py",
                    "old_text": "return a - b",
                    "new_text": "return a + b",
                },
            },
        ]
        self.step += 1
        return AIMessage(
            content="" if self.step <= 2 else "AGENT_CLAIM: solved",
            tool_calls=[calls[self.step - 1]] if self.step <= 2 else [],
        )


with tempfile.TemporaryDirectory(prefix="mokio-offline-demo-") as temp:
    root = Path(temp)
    workspace = root / "workspace"
    workspace.mkdir()
    (workspace / "app.py").write_text("def add(a, b):\n    return a - b\n")
    config = EvalTrialConfig(
        task_id="offline-example",
        architecture="baseline",
        repeat=1,
        model="scripted-fixture",
        model_config_sha256="simulation",
        harness_revision="simulation",
        prompt_sha256="simulation",
    )
    spec = {
        "config": config.model_dump(),
        "workspace": str(workspace),
        "owner": "offline-example",
        "image": "sha256:not-used-file-tools-only",
        "requirement": "Fix add(a,b) so it adds the two values.",
        "events": str(root / "events.jsonl"),
        "output": str(root / "worker.json"),
    }
    (root / "spec.json").write_text(json.dumps(spec))
    with (
        patch.dict("os.environ", {"HOME": str(root / "home"), "PYTHON_DOTENV_DISABLED": "1"}),
        patch("mokioclaw.agents.code_agent.create_model", ScriptedModel),
    ):
        main(root / "spec.json")
    namespace = {}
    exec((workspace / "app.py").read_text(), namespace)
    assert namespace["add"](2, 3) == 5
    result = json.loads((root / "worker.json").read_text())
    assert result["status"] == "finished"
    print(
        json.dumps(
            {
                "simulation": True,
                "real_model": False,
                "independent_check": "add(2,3) == 5",
                "requests": result["model_metrics"]["requests"],
                "tools": result["tool_count"],
                "path": result["actual_path"],
                "note": "Scripted fixture only; excluded from real coding-task statistics.",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
