from types import SimpleNamespace

import pytest

from backend.inference import GuardrailBlocked, InferenceClients, ProviderSettings
from backend.schemas import IncidentContext, Provider


def settings(**overrides):
    values = {
        "live_llm_enabled": True,
        "max_run_usd": 1.0,
        "max_run_tokens": 2000,
        "request_timeout_seconds": 1,
        "max_prompt_tokens": 500,
        "max_agent_tokens": 100,
        "aiml_model": "test-aiml",
        "featherless_model": "test-featherless",
        "aiml_base_url": "http://unused",
        "featherless_base_url": "http://unused",
    }
    values.update(overrides)
    return ProviderSettings(**values)


class FakeCompletions:
    def __init__(self, total_tokens=42):
        self.total_tokens = total_tokens

    async def create(self, **_kwargs):
        return SimpleNamespace(
            usage=SimpleNamespace(total_tokens=self.total_tokens),
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=(
                            '{"service":"api","environment":"prod","error_signature":"500",'
                            '"severity":"sev2","impact":"degraded","suspected_components":[],'
                            '"evidence":[]}'
                        )
                    )
                )
            ],
        )


class FakeClient:
    def __init__(self, total_tokens=42):
        self.chat = SimpleNamespace(completions=FakeCompletions(total_tokens))


@pytest.mark.anyio
async def test_blocks_when_prompt_budget_exceeded():
    llm = InferenceClients(settings(max_prompt_tokens=5))
    llm.aiml = FakeClient()

    with pytest.raises(GuardrailBlocked, match="MAX_PROMPT_TOKENS"):
        await llm.json_call(
            provider=Provider.AIML,
            system="x" * 100,
            user="{}",
            output_model=IncidentContext,
        )


@pytest.mark.anyio
async def test_tracks_actual_usage_without_network_call():
    llm = InferenceClients(settings())
    llm.aiml = FakeClient(total_tokens=77)

    result = await llm.json_call(
        provider=Provider.AIML,
        system="Return JSON.",
        user="{}",
        output_model=IncidentContext,
    )

    assert result.service == "api"
    assert llm.guard.used_tokens == 77


@pytest.mark.anyio
async def test_blocks_cumulative_run_token_budget():
    llm = InferenceClients(settings(max_run_tokens=300))
    llm.aiml = FakeClient(total_tokens=77)

    await llm.json_call(
        provider=Provider.AIML,
        system="Return JSON.",
        user="{}",
        output_model=IncidentContext,
    )

    with pytest.raises(GuardrailBlocked, match="MAX_RUN_TOKENS"):
        await llm.json_call(
            provider=Provider.AIML,
            system="Return JSON.",
            user="{}",
            output_model=IncidentContext,
        )
