from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import TypeVar

from dotenv import load_dotenv
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from backend.schemas import Provider

T = TypeVar("T", bound=BaseModel)


class GuardrailBlocked(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderSettings:
    live_llm_enabled: bool
    max_run_usd: float
    request_timeout_seconds: float
    max_agent_tokens: int
    aiml_model: str
    featherless_model: str
    aiml_base_url: str
    featherless_base_url: str

    @classmethod
    def from_env(cls) -> "ProviderSettings":
        load_dotenv()
        return cls(
            live_llm_enabled=os.getenv("LIVE_LLM_ENABLED", "false").lower() == "true",
            max_run_usd=float(os.getenv("MAX_RUN_USD", "0")),
            request_timeout_seconds=float(os.getenv("REQUEST_TIMEOUT_SECONDS", "20")),
            max_agent_tokens=int(os.getenv("MAX_AGENT_TOKENS", "900")),
            aiml_model=os.getenv("AIML_MODEL", "gpt-4o"),
            featherless_model=os.getenv("FEATHERLESS_MODEL", "Qwen/Qwen2.5-Coder-7B-Instruct"),
            aiml_base_url=os.getenv("AIML_BASE_URL", "https://api.aimlapi.com/v1"),
            featherless_base_url=os.getenv("FEATHERLESS_BASE_URL", "https://api.featherless.ai/v1"),
        )


class SpendGuard:
    def __init__(self, max_usd: float, max_tokens: int) -> None:
        self.max_usd = max_usd
        self.max_tokens = max_tokens
        self.spent_usd = 0.0

    def reserve(self, estimated_tokens: int, price_per_1k: float) -> None:
        estimated_usd = (min(estimated_tokens, self.max_tokens) / 1000) * price_per_1k
        if self.max_usd <= 0 or self.spent_usd + estimated_usd > self.max_usd:
            raise GuardrailBlocked("LLM call blocked by MAX_RUN_USD guardrail")
        self.spent_usd += estimated_usd


class InferenceClients:
    def __init__(self, settings: ProviderSettings | None = None) -> None:
        self.settings = settings or ProviderSettings.from_env()
        self.guard = SpendGuard(self.settings.max_run_usd, self.settings.max_agent_tokens)
        self.aiml = AsyncOpenAI(
            base_url=self.settings.aiml_base_url,
            api_key=os.getenv("AIML_API_KEY") or "missing",
            timeout=self.settings.request_timeout_seconds,
        )
        self.featherless = AsyncOpenAI(
            base_url=self.settings.featherless_base_url,
            api_key=os.getenv("FEATHERLESS_API_KEY") or "missing",
            timeout=self.settings.request_timeout_seconds,
            default_headers={
                "HTTP-Referer": "https://github.com/band-incident-response",
                "X-Title": "Band Incident Response",
            },
        )

    async def json_call(
        self,
        *,
        provider: Provider,
        system: str,
        user: str,
        output_model: type[T],
    ) -> T:
        if not self.settings.live_llm_enabled:
            raise GuardrailBlocked("LIVE_LLM_ENABLED=false")

        client = self.aiml if provider == Provider.AIML else self.featherless
        model = self.settings.aiml_model if provider == Provider.AIML else self.settings.featherless_model
        price = 0.01 if provider == Provider.AIML else 0.002
        self.guard.reserve(self.settings.max_agent_tokens, price)

        schema_hint = output_model.model_json_schema()
        messages = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": (
                    f"{user}\n\nReturn only JSON matching this schema:\n"
                    f"{json.dumps(schema_hint, separators=(',', ':'))}"
                ),
            },
        ]

        async with asyncio.timeout(self.settings.request_timeout_seconds):
            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=self.settings.max_agent_tokens,
                temperature=0,
                response_format={"type": "json_object"},
            )

        content = response.choices[0].message.content or "{}"
        try:
            return output_model.model_validate_json(content)
        except ValidationError:
            return output_model.model_validate(json.loads(content))
