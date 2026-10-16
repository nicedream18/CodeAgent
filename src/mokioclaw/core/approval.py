from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

VALID_APPROVAL_MODES = {"inline", "auto", "deny"}


@dataclass(frozen=True)
class ApprovalRequest:
    id: str
    command: str
    risk_reason: str
    tool_name: str = "BashTool"
    tool_call_id: str = ""
    run_id: str = ""
    args_digest: str = ""
    context_version: int = 0
    policy_version: int = 0
    rule_preview: str = ""


@dataclass(frozen=True)
class ApprovalDecision:
    approved: bool
    reason: str = ""
    scope: Literal["once", "session", "project"] = "once"
    match_scope: Literal["exact", "directory"] = "exact"


RISK_PATTERNS = [
    (
        r"(?:^|&&|\|\||;)\s*(?:python\s+-m\s+)?pip\s+install\b",
        "Python package installation",
    ),
    (r"(?:^|&&|\|\||;)\s*uv\s+add\b", "Project dependency change with uv add"),
    (r"(?:^|&&|\|\||;)\s*uv\s+sync\b", "Dependency synchronization with uv sync"),
    (
        r"(?:^|&&|\|\||;)\s*uv\s+pip\s+install\b",
        "Python package installation with uv pip",
    ),
    (r"(?:^|&&|\|\||;)\s*npm\s+install\b", "Node package installation"),
    (r"(?:^|&&|\|\||;)\s*pnpm\s+install\b", "Node package installation"),
    (r"(?:^|&&|\|\||;)\s*yarn\s+(?:install\b|add\b)", "Node package installation"),
    (r"(?:^|&&|\|\||;)\s*(?:curl|wget)\b", "Network download command"),
    (r"(?:^|&&|\|\||;)\s*uvicorn\b", "Long-running development server"),
    (
        r"(?:^|&&|\|\||;)\s*python\s+-m\s+http\.server\b",
        "Long-running development server",
    ),
]

DANGEROUS_PATTERNS = [
    r"\brm\s+-rf\b",
    r"\bRemove-Item\b.*\b-Recurse\b.*\b-Force\b",
    r"\bdel\s+/[sq]\b",
    r"\bformat\b",
    r"\bshutdown\b",
    r"\breboot\b",
    r"(?:^|[^0-9])>\s*(?:[A-Za-z]:\\|/(?!dev/null\b))",
]


def normalize_approval_mode(mode: str | None) -> str:
    normalized = (mode or "inline").strip().lower()
    return normalized if normalized in VALID_APPROVAL_MODES else "inline"


def classify_command_risk(command: str) -> str | None:
    for pattern, reason in RISK_PATTERNS:
        if re.search(pattern, command, flags=re.IGNORECASE):
            return reason
    return None


def classify_dangerous_command(command: str) -> str | None:
    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, command, flags=re.IGNORECASE):
            return pattern
    return None


def make_approval_request(command: str, risk_reason: str) -> ApprovalRequest:
    return ApprovalRequest(
        id=f"approval-{uuid4().hex[:8]}", command=command, risk_reason=risk_reason
    )
