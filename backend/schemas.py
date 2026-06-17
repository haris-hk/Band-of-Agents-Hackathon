from __future__ import annotations

from datetime import datetime, timezone
try:
    from enum import StrEnum
except ImportError:  # pragma: no cover - compatibility for local Python 3.9 runners.
    from enum import Enum

    class StrEnum(str, Enum):
        pass
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, model_validator


def _utcnow() -> datetime:
    """Timezone-aware UTC timestamp (replaces deprecated datetime.utcnow)."""
    return datetime.now(timezone.utc)


class Stage(StrEnum):
    TRIAGE = "triage"
    REPRO = "repro"
    TEST = "test"
    FIX = "fix"
    VALIDATE = "validate"
    RCA = "rca"
    DONE = "done"
    FAILED = "failed"


class Severity(StrEnum):
    SEV1 = "sev1"
    SEV2 = "sev2"
    SEV3 = "sev3"
    SEV4 = "sev4"


class Provider(StrEnum):
    AIML = "aimlapi"
    FEATHERLESS = "featherless"
    OPENROUTER = "openrouter"


class RawAlert(BaseModel):
    source: str = "simulated-webhook"
    payload: dict[str, Any]
    received_at: datetime = Field(default_factory=_utcnow)


class IncidentContext(BaseModel):
    service: str
    environment: str = "unknown"
    error_signature: str
    severity: Severity
    impact: str
    suspected_components: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    interpretations: list[str] = Field(default_factory=list, description="Different possible interpretations of what the user is trying to say or what the root cause could be based on the repo context.")
    investigation_plan: list[str] = Field(default_factory=list, description="Step-by-step plan for how the agents should investigate and fix the problem.")


class ReproPlan(BaseModel):
    confirmed: bool
    assumptions: list[str] = Field(default_factory=list)
    steps: list[str] = Field(min_length=1)
    expected_failure: str
    required_data: list[str] = Field(default_factory=list)


class ReproExecution(BaseModel):
    image: str
    command: str
    exit_code: int | None = None
    timed_out: bool = False
    failure_observed: bool = False
    logs: str = ""
    stack_trace: str = ""
    error: str | None = None


class CodePatch(BaseModel):
    summary: str
    files_changed: list[str] = Field(default_factory=list)
    patch_unified_diff: str
    risk_notes: list[str] = Field(default_factory=list)
    rollback_plan: str


class CandidatePatches(BaseModel):
    candidates: list[CodePatch] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def validate_distinct_diffs(self) -> "CandidatePatches":
        diffs = [candidate.patch_unified_diff.strip() for candidate in self.candidates]
        if len(set(diffs)) != len(diffs):
            raise ValueError("candidate patches must contain distinct unified diffs")
        return self


class RegressionTests(BaseModel):
    framework: str = "pytest"
    test_files: list[str] = Field(default_factory=list)
    test_code: str
    run_command: str = "pytest"
    acceptance_criteria: list[str] = Field(default_factory=list)


class PatchValidationResult(BaseModel):
    candidate_index: int
    validation_passed: bool
    timed_out: bool = False
    exit_code: int | None = None
    logs: str = ""
    error: str | None = None
    patch_summary: str | None = None


class ValidationSwarmResult(BaseModel):
    winning_candidate_index: int | None = None
    winning_patch: CodePatch | None = None
    results: list[PatchValidationResult] = Field(default_factory=list)


class RCAReport(BaseModel):
    title: str
    incident_summary: str
    customer_impact: str
    root_cause: str
    timeline: list[str] = Field(default_factory=list)
    remediation: list[str] = Field(default_factory=list)
    prevention: list[str] = Field(default_factory=list)
    git_branch: str | None = None
    commit_message: str | None = None
    patch_unified_diff: str | None = None
    validation_summary: str | None = None
    final_markdown: str


class AgentHandoff(BaseModel):
    from_agent: str
    to_agent: str
    stage: Stage
    mention: str
    payload: dict[str, Any]
    message_type: str = "band.handoff.v1"
    summary: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)


class AgentEvent(BaseModel):
    run_id: UUID
    stage: Stage
    agent: str
    status: Literal["queued", "active", "handoff", "complete", "failed", "done"]
    payload: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)


class IncidentState(BaseModel):
    run_id: UUID = Field(default_factory=uuid4)
    current_stage: Stage = Stage.TRIAGE
    max_steps: int = 8
    steps_run: int = 0
    raw_alert: RawAlert
    context: IncidentContext | None = None
    repro: ReproPlan | None = None
    repro_execution: ReproExecution | None = None
    candidate_patches: CandidatePatches | None = None
    fix: CodePatch | None = None
    tests: RegressionTests | None = None
    validation: ValidationSwarmResult | None = None
    rca: RCAReport | None = None
    band_thread: list[AgentHandoff] = Field(default_factory=list)
    events: list[AgentEvent] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    repo_path: str | None = None
    repo_full_name: str | None = None
    repo_files: dict[str, str] = Field(default_factory=dict)
    fix_export: dict[str, Any] | None = None

class PatchResult(BaseModel):
    patch_id: str
    applies_cleanly: bool
    test_exit_code: int
    test_output_summary: str
    side_effects: list[str]
    correctness_score: int  # 0-10

class ValidationReport(BaseModel):
    patch_results: list[PatchResult]
    winning_patch_id: str | None
    confidence: Literal["high", "medium", "low"]
    validation_notes: str
    regression_risk: Literal["none", "low", "medium", "high"]
    suggested_followup_tests: list[str]

class RunRequest(BaseModel):
    alert: dict[str, Any]


class RunResult(BaseModel):
    state: IncidentState
