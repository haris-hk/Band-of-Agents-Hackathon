from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class Stage(StrEnum):
    TRIAGE = "triage"
    REPRO = "repro"
    FIX = "fix"
    TEST = "test"
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


class RawAlert(BaseModel):
    source: str = "simulated-webhook"
    payload: dict[str, Any]
    received_at: datetime = Field(default_factory=datetime.utcnow)


class IncidentContext(BaseModel):
    service: str
    environment: str = "unknown"
    error_signature: str
    severity: Severity
    impact: str
    suspected_components: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


class ReproPlan(BaseModel):
    confirmed: bool
    assumptions: list[str] = Field(default_factory=list)
    steps: list[str] = Field(min_length=1)
    expected_failure: str
    required_data: list[str] = Field(default_factory=list)


class CodePatch(BaseModel):
    summary: str
    files_changed: list[str] = Field(default_factory=list)
    patch_unified_diff: str
    risk_notes: list[str] = Field(default_factory=list)
    rollback_plan: str


class RegressionTests(BaseModel):
    framework: str = "pytest"
    test_files: list[str] = Field(default_factory=list)
    test_code: str
    run_command: str = "pytest"
    acceptance_criteria: list[str] = Field(default_factory=list)


class RCAReport(BaseModel):
    title: str
    incident_summary: str
    customer_impact: str
    root_cause: str
    timeline: list[str] = Field(default_factory=list)
    remediation: list[str] = Field(default_factory=list)
    prevention: list[str] = Field(default_factory=list)
    final_markdown: str


class AgentHandoff(BaseModel):
    from_agent: str
    to_agent: str
    stage: Stage
    mention: str
    payload: dict[str, Any]
    created_at: datetime = Field(default_factory=datetime.utcnow)


class AgentEvent(BaseModel):
    run_id: UUID
    stage: Stage
    agent: str
    status: Literal["queued", "active", "handoff", "complete", "failed", "done"]
    payload: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class IncidentState(BaseModel):
    run_id: UUID = Field(default_factory=uuid4)
    current_stage: Stage = Stage.TRIAGE
    max_steps: int = 5
    steps_run: int = 0
    raw_alert: RawAlert
    context: IncidentContext | None = None
    repro: ReproPlan | None = None
    fix: CodePatch | None = None
    tests: RegressionTests | None = None
    rca: RCAReport | None = None
    band_thread: list[AgentHandoff] = Field(default_factory=list)
    events: list[AgentEvent] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class RunRequest(BaseModel):
    alert: dict[str, Any]


class RunResult(BaseModel):
    state: IncidentState
