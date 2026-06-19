from __future__ import annotations

import unittest
from unittest.mock import patch

from pydantic import BaseModel

from backend.agent_names import AGENT_DISPLAY_NAMES, agent_mention
from backend.agent_loop import IncidentAgent, IncidentOrchestrator, _fallback_fix, _fallback_rca
from backend.agent_loop import _fallback_repro, _fallback_tests, _fallback_triage
from backend.schemas import (
    CandidatePatches,
    IncidentContext,
    IncidentState,
    Provider,
    RCAReport,
    RegressionTests,
    ReproExecution,
    ReproPlan,
    Stage,
    ValidationSwarmResult,
)


class FallbackOnlyAgent(IncidentAgent):
    async def run(self, state: IncidentState, _llm) -> BaseModel:
        return self.fallback(state)


def fake_agents() -> dict[Stage, IncidentAgent]:
    agents = {
        Stage.TRIAGE: IncidentAgent(
            name=AGENT_DISPLAY_NAMES[Stage.TRIAGE],
            mention=agent_mention(Stage.TRIAGE),
            stage=Stage.TRIAGE,
            provider=Provider.AIML,
            output_model=IncidentContext,
            system_prompt="",
            fallback=_fallback_triage,
            model_env="",
            default_model="",
        ),
        Stage.REPRO: IncidentAgent(
            name=AGENT_DISPLAY_NAMES[Stage.REPRO],
            mention=agent_mention(Stage.REPRO),
            stage=Stage.REPRO,
            provider=Provider.AIML,
            output_model=ReproPlan,
            system_prompt="",
            fallback=_fallback_repro,
            model_env="",
            default_model="",
        ),
        Stage.TEST: IncidentAgent(
            name=AGENT_DISPLAY_NAMES[Stage.TEST],
            mention=agent_mention(Stage.TEST),
            stage=Stage.TEST,
            provider=Provider.AIML,
            output_model=RegressionTests,
            system_prompt="",
            fallback=_fallback_tests,
            model_env="",
            default_model="",
        ),
        Stage.FIX: IncidentAgent(
            name=AGENT_DISPLAY_NAMES[Stage.FIX],
            mention=agent_mention(Stage.FIX),
            stage=Stage.FIX,
            provider=Provider.FEATHERLESS,
            output_model=CandidatePatches,
            system_prompt="",
            fallback=_fallback_fix,
            model_env="",
            default_model="",
        ),
        Stage.RCA: IncidentAgent(
            name=AGENT_DISPLAY_NAMES[Stage.RCA],
            mention=agent_mention(Stage.RCA),
            stage=Stage.RCA,
            provider=Provider.AIML,
            output_model=RCAReport,
            system_prompt="",
            fallback=_fallback_rca,
            model_env="",
            default_model="",
        ),
    }
    return {
        stage: FallbackOnlyAgent(**agent.__dict__)
        for stage, agent in agents.items()
    }


async def fake_repro_pass1(_state, _plan):
    return ReproExecution(
        image="python:3.11",
        command="python -c 'boom'",
        exit_code=1,
        failure_observed=True,
        logs="TypeError: payload missing",
        stack_trace="TypeError: payload missing",
    )


async def fake_validation_swarm(_state, patches, _tests):
    return ValidationSwarmResult(
        winning_candidate_index=0,
        winning_patch=patches.candidates[0],
        results=[],
    )


class BandCollaborationTests(unittest.IsolatedAsyncioTestCase):
    async def test_sandbox_and_validation_are_band_thread_participants(self) -> None:
        orchestrator = IncidentOrchestrator()
        orchestrator.agents = fake_agents()

        async def fake_prepare_repository(state, alert):
            state.repo_path = "/fake/repo/path"
            state.repo_files = {"services/checkout/handler.py": "def handle(x):\n    return x\n"}

        with (
            patch("backend.agent_loop.run_repro_pass1", fake_repro_pass1),
            patch("backend.agent_loop.run_validation_swarm", fake_validation_swarm),
            patch.object(orchestrator, "_prepare_repository", fake_prepare_repository),
        ):
            events = [
                event
                async for event in orchestrator.run(
                    {
                        "service": "checkout",
                        "environment": "demo",
                        "error": "missing payload",
                        "impact": "checkout failures",
                    }
                )
            ]

        done = events[-1]
        self.assertEqual(done.status, "done")
        state_events = [
            event for event in events if event.status == "handoff"
        ]
        self.assertIn("Repro Sandbox", [event.payload["to_agent"] for event in state_events])
        self.assertIn("Validation Swarm", [event.payload["to_agent"] for event in state_events])

        rca_payload = done.payload["rca"]
        self.assertGreaterEqual(len(rca_payload["timeline"]), 5)


if __name__ == "__main__":
    unittest.main()
