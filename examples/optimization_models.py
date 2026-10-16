"""Deterministic offline model for the optimization learning experiments."""

import json
from uuid import uuid4

from langchain_core.messages import AIMessage


class WorkflowModel:
    """A deterministic local model exercising real graphs and file tools."""

    def __init__(self):
        self.role, self.index = "router", 0

    def bind_tools(self, tools):
        names = {tool.name for tool in tools}
        self.role = (
            "planner"
            if "TodoWriteTool" in names
            else "code"
            if "FileWriteTool" in names
            else "verifier"
            if "BashTool" in names
            else "repo"
        )
        return self

    def invoke(self, messages):
        self.index += 1

        def calls(*specs):
            return AIMessage(
                content="",
                tool_calls=[
                    {"name": name, "args": args, "id": uuid4().hex} for name, args in specs
                ],
            )

        if self.role == "router":
            return AIMessage(
                content=json.dumps(
                    {
                        "route": "workflow",
                        "confidence": 0.99,
                        "complexity": "simple",
                        "target_files": ["app.txt"],
                        "cross_module": False,
                    }
                )
            )
        if self.role == "repo":
            if self.index == 1:
                return calls(("FileReadTool", {"file_path": "app.txt"}))
            return AIMessage(content='{"summary":"app.txt is the only implementation target"}')
        if self.role == "planner":
            if self.index == 1:
                return calls(
                    (
                        "TodoWriteTool",
                        {
                            "todos": ["Update app.txt"],
                            "acceptance_criteria": ["app.txt contains after"],
                            "verification_commands": [],
                        },
                    ),
                    ("CallCodeAgentTool", {"instruction": "Update app.txt"}),
                )
            return AIMessage(content="Implemented; ready for verification")
        if self.role == "code":
            if self.index == 1:
                return calls(("FileReadTool", {"file_path": "app.txt"}))
            if self.index == 2:
                return calls(
                    (
                        "FileEditTool",
                        {
                            "file_path": "app.txt",
                            "old_text": "before",
                            "new_text": "after",
                        },
                    ),
                    ("TodoUpdateTool", {"todo_id": "todo-1", "status": "completed"}),
                )
            return AIMessage(content="Updated app.txt")
        if self.index == 1:
            return calls(("FileReadTool", {"file_path": "app.txt"}))
        assert "after" in str(messages[-1].content)
        return AIMessage(
            content='{"passed":true,"reason":"actual file verified","checks":[{"name":"file","passed":true,"detail":"after"}]}'
        )


class CrossModuleModel(WorkflowModel):
    """Two-file task; a light-path restart must preserve the first completed edit."""

    def invoke(self, messages):
        self.index += 1

        def calls(*specs):
            return AIMessage(
                content="",
                tool_calls=[
                    {"name": name, "args": args, "id": uuid4().hex} for name, args in specs
                ],
            )

        if self.role == "router":
            return AIMessage(
                content=json.dumps(
                    {
                        "route": "workflow",
                        "confidence": 0.99,
                        "complexity": "complex",
                        "target_files": ["app.txt", "config.txt"],
                        "cross_module": True,
                    }
                )
            )
        if self.role == "repo":
            if self.index == 1:
                return calls(
                    *[("FileReadTool", {"file_path": path}) for path in ("app.txt", "config.txt")]
                )
            return AIMessage(
                content='{"summary":"app.txt and config.txt must be updated together"}'
            )
        if self.role == "planner":
            if self.index == 1:
                return calls(
                    (
                        "TodoWriteTool",
                        {
                            "todos": ["Update both files"],
                            "acceptance_criteria": ["Both app.txt and config.txt contain after"],
                            "verification_commands": [],
                        },
                    ),
                    (
                        "CallCodeAgentTool",
                        {
                            "instruction": "Update app.txt and config.txt, retaining any completed edit"
                        },
                    ),
                )
            return AIMessage(content="Both files are ready for verification")
        if self.role == "code":
            if self.index == 1:
                return calls(
                    *[("FileReadTool", {"file_path": path}) for path in ("app.txt", "config.txt")]
                )
            if self.index == 2:
                reads = {
                    json.loads(message.content)["path"]: json.loads(message.content)["content"]
                    for message in messages
                    if getattr(message, "name", "") == "FileReadTool"
                }
                return calls(
                    *[
                        (
                            "FileEditTool",
                            {
                                "file_path": path,
                                "old_text": "before",
                                "new_text": "after",
                            },
                        )
                        for path in ("app.txt", "config.txt")
                        if "before" in reads.get(path, "")
                    ],
                    ("TodoUpdateTool", {"todo_id": "todo-1", "status": "completed"}),
                )
            return AIMessage(content="Updated both files")
        if self.index == 1:
            return calls(
                *[("FileReadTool", {"file_path": path}) for path in ("app.txt", "config.txt")]
            )
        assert all(
            "after" in str(message.content)
            for message in messages
            if getattr(message, "name", "") == "FileReadTool"
        )
        return AIMessage(
            content='{"passed":true,"reason":"Both files verified","checks":[{"name":"both files","passed":true}]}'
        )
