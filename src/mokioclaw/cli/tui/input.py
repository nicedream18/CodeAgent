"""Multiline task input with explicit submission and discoverable commands."""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual import events
from textual.message import Message
from textual.widgets import Static, TextArea


class TaskInput(TextArea):
    DEFAULT_CSS = "TaskInput { height: auto; min-height: 3; max-height: 10; border: none; }"

    class Submitted(Message, namespace="input"):
        def __init__(self, editor: TaskInput) -> None:
            super().__init__()
            self.input = editor
            self.value = editor.text

    def __init__(self, placeholder: str = "", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.border_subtitle = placeholder
        self._matches: list[tuple[str, str]] = []
        self._cursor = 0
        self._dismissed = False

    @property
    def value(self) -> str:
        return self.text

    @value.setter
    def value(self, value: str) -> None:
        self.load_text(value)

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area is not self:
            return
        self._dismissed = False
        self._cursor = 0
        self.refresh_completion()

    def refresh_completion(self) -> None:
        popup = self.app.query_one("#command-completion", Static)
        query = self.text[1:].lower()
        candidates = getattr(self.app, "command_candidates", lambda: [])()
        self._matches = (
            [item for item in candidates if query in item[0].lower()]
            if self.text.startswith("/")
            and " " not in self.text
            and "\n" not in self.text
            and not self._dismissed
            else []
        )
        self._cursor = min(self._cursor, max(0, len(self._matches) - 1))
        popup.display = bool(self._matches)
        popup.update(
            Text(
                "\n".join(
                    ("❯ " if i == self._cursor else "  ") + "/" + name + "  " + description
                    for i, (name, description) in enumerate(self._matches[:8])
                )
            )
        )

    async def _on_key(self, event: events.Key) -> None:
        key = event.key
        if key in {"shift+enter", "alt+enter", "ctrl+j", "super+enter"}:
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        if self._matches and key in {"up", "down", "tab", "escape"}:
            event.stop()
            event.prevent_default()
            if key == "escape":
                self._dismissed = True
            elif key == "tab":
                self.value = "/" + self._matches[self._cursor][0] + " "
            else:
                self._cursor = (self._cursor + (1 if key == "down" else -1)) % min(
                    len(self._matches), 8
                )
            self.refresh_completion()
            return
        if key == "enter":
            event.stop()
            event.prevent_default()
            if self.text.strip():
                self.post_message(self.Submitted(self))
            return
        await super()._on_key(event)
