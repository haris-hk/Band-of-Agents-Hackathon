from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable

from pydantic import BaseModel

from backend.inference import GuardrailBlocked, InferenceClients
from backend.schemas import (
    AgentEvent,
    AgentHandoff,
    CodePatch,
    IncidentContext,
    IncidentState,
    Provider,
    RCAReport,
    RawAlert,
    RegressionTests,
    ReproPlan,
    Severity,
    Stage,
)

Fallback = Callable[[IncidentState], BaseModel]


@dataclass(frozen=True)
class IncidentAgent:
    name: str
    mention: str
    stage: Stage
    provider: Provider
    output_model: type[BaseModel]
    system_prompt: str
    fallback: Fallback

    async def run(self, state: IncidentState, llm: InferenceClients) -> BaseModel:
        prompt = json.dumps(state.model_dump(mode="json"), separators=(",", ":"))
        try:
            return await llm.json_call(
                provider=self.provider,
                system=self.system_prompt,
                user=prompt,
                output_model=self.output_model,
            )
        except (GuardrailBlocked, TimeoutError, Exception) as exc:
            state.errors.append(f"{self.name}: {exc}")
            return self.fallback(state)


class IncidentOrchestrator:
    # Bounded deterministic state machine: one pass per stage, no back edges.
    transitions = {
        Stage.TRIAGE: Stage.REPRO,
        Stage.REPRO: Stage.FIX,
        Stage.FIX: Stage.TEST,
        Stage.TEST: Stage.RCA,
        Stage.RCA: Stage.DONE,
    }

    def __init__(self, llm: InferenceClients | None = None) -> None:
        self.llm = llm or InferenceClients()
        self.agents = build_agents()

    async def run(self, alert: dict[str, Any]) -> AsyncIterator[AgentEvent]:
        state = IncidentState(raw_alert=RawAlert(payload=alert))
        yield self._event(state, Stage.TRIAGE, "orchestrator", "queued", {"alert": alert})

        while state.current_stage not in {Stage.DONE, Stage.FAILED}:
            if state.steps_run >= state.max_steps:
                state.current_stage = Stage.FAILED
                yield self._event(state, Stage.FAILED, "orchestrator", "failed", error="max_steps exceeded")
                return

            agent = self.agents[state.current_stage]
            yield self._event(state, agent.stage, agent.name, "active", self._stage_payload(state))

            output = await agent.run(state, self.llm)
            self._merge_output(state, agent.stage, output)
            state.steps_run += 1

            next_stage = self.transitions[agent.stage]
            if next_stage != Stage.DONE:
                next_agent = self.agents[next_stage]
                handoff = AgentHandoff(
                    from_agent=agent.name,
                    to_agent=next_agent.name,
                    stage=next_stage,
                    mention=next_agent.mention,
                    payload=output.model_dump(mode="json"),
                )
                state.band_thread.append(handoff)
                yield self._event(
                    state,
                    next_stage,
                    agent.name,
                    "handoff",
                    {"mention": next_agent.mention, "payload": handoff.payload},
                )

            state.current_stage = next_stage
            yield self._event(state, agent.stage, agent.name, "complete", output.model_dump(mode="json"))

        yield self._event(
            state,
            Stage.DONE,
            "orchestrator",
            "done",
            {
                "rca": state.rca.model_dump(mode="json") if state.rca else None,
                "fix": state.fix.model_dump(mode="json") if state.fix else None,
            },
        )

    def _merge_output(self, state: IncidentState, stage: Stage, output: BaseModel) -> None:
        if stage == Stage.TRIAGE:
            state.context = output  # type: ignore[assignment]
        elif stage == Stage.REPRO:
            state.repro = output  # type: ignore[assignment]
        elif stage == Stage.FIX:
            state.fix = output  # type: ignore[assignment]
        elif stage == Stage.TEST:
            state.tests = output  # type: ignore[assignment]
        elif stage == Stage.RCA:
            state.rca = output  # type: ignore[assignment]

    def _event(
        self,
        state: IncidentState,
        stage: Stage,
        agent: str,
        status: str,
        payload: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> AgentEvent:
        event = AgentEvent(
            run_id=state.run_id,
            stage=stage,
            agent=agent,
            status=status,  # type: ignore[arg-type]
            payload=payload or {},
            error=error,
        )
        state.events.append(event)
        return event

    def _stage_payload(self, state: IncidentState) -> dict[str, Any]:
        return {
            "context": state.context.model_dump(mode="json") if state.context else None,
            "repro": state.repro.model_dump(mode="json") if state.repro else None,
            "fix": state.fix.model_dump(mode="json") if state.fix else None,
            "tests": state.tests.model_dump(mode="json") if state.tests else None,
            "errors": state.errors,
        }


def build_agents() -> dict[Stage, IncidentAgent]:
    return {
        Stage.TRIAGE: IncidentAgent(
            name="Alert Triager",
            mention="@incident-triager",
            stage=Stage.TRIAGE,
            provider=Provider.AIML,
            output_model=IncidentContext,
            system_prompt="Extract incident context from webhook data. Return strict JSON only.",
            fallback=_fallback_triage,
        ),
        Stage.REPRO: IncidentAgent(
            name="Reproducer",
            mention="@incident-reproducer",
            stage=Stage.REPRO,
            provider=Provider.AIML,
            output_model=ReproPlan,
            system_prompt="Confirm incident shape and produce deterministic repro steps. Return JSON only.",
            fallback=_fallback_repro,
        ),
        Stage.FIX: IncidentAgent(
            name="Fix Agent",
            mention="@incident-fixer",
            stage=Stage.FIX,
            provider=Provider.FEATHERLESS,
            output_model=CodePatch,
            system_prompt="Propose minimal production-safe code patch as unified diff. Return JSON only.",
            fallback=_fallback_fix,
        ),
        Stage.TEST: IncidentAgent(
            name="Test Generator",
            mention="@incident-test-generator",
            stage=Stage.TEST,
            provider=Provider.FEATHERLESS,
            output_model=RegressionTests,
            system_prompt="Write focused regression tests for the proposed patch. Return JSON only.",
            fallback=_fallback_tests,
        ),
        Stage.RCA: IncidentAgent(
            name="RCA Writer",
            mention="@incident-rca-writer",
            stage=Stage.RCA,
            provider=Provider.AIML,
            output_model=RCAReport,
            system_prompt="Consolidate the Band thread into a concise postmortem/RCA. Return JSON only.",
            fallback=_fallback_rca,
        ),
    }


def _alert_value(state: IncidentState, key: str, default: str) -> str:
    value = state.raw_alert.payload.get(key, default)
    return value if isinstance(value, str) else json.dumps(value)


def _fallback_triage(state: IncidentState) -> IncidentContext:
    return IncidentContext(
        service=_alert_value(state, "service", "unknown-service"),
        environment=_alert_value(state, "environment", "unknown"),
        error_signature=_alert_value(state, "error", _alert_value(state, "message", "unknown-error")),
        severity=Severity.SEV2,
        impact=_alert_value(state, "impact", "impact requires manual confirmation"),
        suspected_components=[_alert_value(state, "service", "unknown-service")],
        evidence=[json.dumps(state.raw_alert.payload, separators=(",", ":"))],
    )


def _fallback_repro(state: IncidentState) -> ReproPlan:
    service = state.context.service if state.context else "service"
    error = state.context.error_signature if state.context else "reported error"
    return ReproPlan(
        confirmed=False,
        assumptions=["Generated from alert payload because live inference was unavailable."],
        steps=[
            f"Deploy or target the same environment for {service}.",
            "Replay the webhook payload captured in raw_alert.",
            f"Observe logs and metrics for signature: {error}.",
        ],
        expected_failure=error,
        required_data=["raw webhook payload", "service logs", "recent deploy diff"],
    )


def _fallback_fix(state: IncidentState) -> CodePatch:
    service = state.context.service if state.context else "service"
    return CodePatch(
        summary=f"Add defensive validation around failing path in {service}.",
        files_changed=[f"services/{service}/handler.py"],
        patch_unified_diff=(
            "--- a/services/{service}/handler.py\n"
            "+++ b/services/{service}/handler.py\n"
            "@@\n"
            "-    result = process(payload)\n"
            "+    if payload is None:\n"
            "+        raise ValueError('payload is required')\n"
            "+    result = process(payload)\n"
        ).format(service=service),
        risk_notes=["Placeholder patch; replace with repository-specific diff after repro."],
        rollback_plan="Revert the patch commit and redeploy the previous stable artifact.",
    )


def _fallback_tests(state: IncidentState) -> RegressionTests:
    service = state.context.service if state.context else "service"
    return RegressionTests(
        framework="pytest",
        test_files=[f"tests/{service}/test_incident_regression.py"],
        test_code=(
            "import pytest\n\n"
            "def test_rejects_missing_payload():\n"
            "    with pytest.raises(ValueError, match='payload is required'):\n"
            "        handle(None)\n"
        ),
        run_command="pytest tests/{service}/test_incident_regression.py".format(service=service),
        acceptance_criteria=["Regression test fails before the patch and passes after it."],
    )


def _fallback_rca(state: IncidentState) -> RCAReport:
    context = state.context or _fallback_triage(state)
    title = f"{context.service} incident RCA"
    final = (
        f"# {title}\n\n"
        f"## Summary\n{context.error_signature}\n\n"
        f"## Impact\n{context.impact}\n\n"
        "## Root Cause\nPending confirmation from repro and code review.\n\n"
        "## Remediation\nApply the proposed patch, run regression tests, and monitor the service.\n"
    )
    return RCAReport(
        title=title,
        incident_summary=context.error_signature,
        customer_impact=context.impact,
        root_cause="Pending confirmation from repro and code review.",
        timeline=[handoff.created_at.isoformat() for handoff in state.band_thread],
        remediation=[state.fix.summary if state.fix else "Patch pending."],
        prevention=["Add alert-linked regression tests.", "Review deploy health gates."],
        final_markdown=final,
    )
