from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Architecture = Literal["baseline", "full", "light", "auto"]


class Versioned(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1


class EvalTask(Versioned):
    id: str
    version: str = "internship-v1"
    source: str
    base_commit: str
    source_sha256: str
    dependency_sha256: str
    image_digest: str
    requirement: str
    allowed_paths: list[str]
    target_tests: list[str]
    regression_tests: list[str]
    reference_patch_sha256: str
    test_patch_sha256: str
    dataset_revision: str | None = None
    upstream_license: str | None = None


class EvalTrialConfig(Versioned):
    task_id: str
    architecture: Architecture
    repeat: int = Field(ge=1, le=3)
    seed: int = 20261016
    temperature: float = 0
    context_tokens: int = 32768
    output_tokens: int = 4096
    token_limit: int = 200000
    request_limit: int = 40
    tool_limit: int = 80
    generation_seconds: int = 900
    bash_seconds: int = 60
    grading_seconds: int = 120
    model: str
    model_config_sha256: str
    harness_revision: str
    prompt_sha256: str

    @property
    def trial_id(self) -> str:
        return f"{self.task_id}-{self.architecture}-{self.repeat}"


class Grade(Versioned):
    valid: bool = False
    solved: bool = False
    target_passed: int = 0
    target_failed: int = 0
    regression_failed: int = 0
    tests_run: int = 0
    out_of_scope: list[str] = Field(default_factory=list)
    tampered_tests: list[str] = Field(default_factory=list)
    infrastructure_error: str | None = None
    seconds: float = 0
    report: str | None = None

    @model_validator(mode="after")
    def credible_success(self) -> Grade:
        if self.solved and (
            not self.valid or self.tests_run == 0 or self.target_passed == 0
            or self.target_failed or self.regression_failed or self.out_of_scope
            or self.tampered_tests or self.infrastructure_error
        ):
            raise ValueError("Solved requires a valid, nonempty independent passing grade")
        return self


class EvalTrialResult(Versioned):
    experiment_id: str
    trial_id: str
    attempt: int = 1
    config: EvalTrialConfig
    task_sha256: str
    real_model: bool = False
    execution_status: Literal["not_started", "running", "finished", "failed", "interrupted", "infrastructure_failed"] = "not_started"
    stop_reason: str | None = None
    grade: Grade = Field(default_factory=Grade)
    safety_assertions: dict[str, bool | None] = Field(default_factory=dict)
    model_claim: bool | None = None
    model_metrics: dict[str, Any] = Field(default_factory=dict)
    tool_count: int = 0
    generation_seconds: float = 0
    actual_path: list[str] = Field(default_factory=list)
    run_id: str | None = None
    stream_ids: list[str] = Field(default_factory=list)
    tool_call_ids: list[str] = Field(default_factory=list)
    artifacts: dict[str, str] = Field(default_factory=dict)
    infrastructure_error: str | None = None
    started_at: str | None = None
    ended_at: str | None = None


def legacy_result(raw: dict[str, Any]) -> dict[str, Any]:
    """Old safety demos cannot be converted into independent task success."""
    if raw.get("schema_version") == 1 and "grade" in raw:
        return EvalTrialResult.model_validate(raw).model_dump()
    return {"schema_version": 0, "legacy": raw, "task_solved": None, "grade_valid": False}
