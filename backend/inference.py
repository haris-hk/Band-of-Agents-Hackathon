from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import TypeVar

from pydantic import BaseModel, ValidationError

try:
    from openai import AsyncOpenAI
except ModuleNotFoundError:
    AsyncOpenAI = None  # type: ignore[assignment]

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv() -> None:
        return None

from backend.schemas import Provider

T = TypeVar("T", bound=BaseModel)


class GuardrailBlocked(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderSettings:
    live_llm_enabled: bool
    max_run_usd: float
    max_run_tokens: int
    request_timeout_seconds: float
    max_prompt_tokens: int
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
            max_run_tokens=int(os.getenv("MAX_RUN_TOKENS", "4000")),
            request_timeout_seconds=float(os.getenv("REQUEST_TIMEOUT_SECONDS", "20")),
            max_prompt_tokens=int(os.getenv("MAX_PROMPT_TOKENS", "2500")),
            max_agent_tokens=int(os.getenv("MAX_AGENT_TOKENS", "900")),
            aiml_model=os.getenv("AIML_MODEL", "gpt-4o"),
            featherless_model=os.getenv("FEATHERLESS_MODEL", "Qwen/Qwen2.5-Coder-7B-Instruct"),
            aiml_base_url=os.getenv("AIML_BASE_URL", "https://api.aimlapi.com/v1"),
            featherless_base_url=os.getenv("FEATHERLESS_BASE_URL", "https://api.featherless.ai/v1"),
        )


class SpendGuard:
    def __init__(self, max_usd: float, max_run_tokens: int) -> None:
        self.max_usd = max_usd
        self.max_run_tokens = max_run_tokens
        self.spent_usd = 0.0
        self.used_tokens = 0

    def reserve(self, estimated_tokens: int, price_per_1k: float) -> int:
        if estimated_tokens <= 0:
            raise GuardrailBlocked("LLM call blocked: token estimate must be positive")
        if self.max_run_tokens <= 0 or self.used_tokens + estimated_tokens > self.max_run_tokens:
            raise GuardrailBlocked("LLM call blocked by MAX_RUN_TOKENS guardrail")
        estimated_usd = (estimated_tokens / 1000) * price_per_1k
        if self.max_usd <= 0 or self.spent_usd + estimated_usd > self.max_usd:
            raise GuardrailBlocked("LLM call blocked by MAX_RUN_USD guardrail")
        self.spent_usd += estimated_usd
        self.used_tokens += estimated_tokens
        return estimated_tokens

    def reconcile(self, reserved_tokens: int, actual_tokens: int | None, price_per_1k: float) -> None:
        if actual_tokens is None or actual_tokens <= 0:
            return
        self.used_tokens += actual_tokens - reserved_tokens
        self.spent_usd += ((actual_tokens - reserved_tokens) / 1000) * price_per_1k


def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


class InferenceClients:
    def __init__(self, settings: ProviderSettings | None = None) -> None:
        self.settings = settings or ProviderSettings.from_env()
        self.guard = SpendGuard(self.settings.max_run_usd, self.settings.max_run_tokens)
        if AsyncOpenAI is None:
            self.aiml = self.featherless = None
            return
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
        model: str | None = None,
    ) -> T:
        if not self.settings.live_llm_enabled:
            raise GuardrailBlocked("LIVE_LLM_ENABLED=false")

        client = self.aiml if provider == Provider.AIML else self.featherless
        if client is None:
            raise GuardrailBlocked("LIVE_LLM_ENABLED=true but openai package is not installed")
        model = model or (
            self.settings.aiml_model if provider == Provider.AIML else self.settings.featherless_model
        )
        price = 0.01 if provider == Provider.AIML else 0.002

        schema_hint = output_model.model_json_schema()
        schema_json = json.dumps(schema_hint, separators=(",", ":"))
        prompt_tokens = estimate_tokens(system) + estimate_tokens(user) + estimate_tokens(schema_json)
        if prompt_tokens > self.settings.max_prompt_tokens:
            raise GuardrailBlocked("LLM call blocked by MAX_PROMPT_TOKENS guardrail")
        reserved_tokens = self.guard.reserve(prompt_tokens + self.settings.max_agent_tokens, price)
        messages = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": (
                    f"{user}\n\nReturn only JSON matching this schema:\n"
                    f"{schema_json}"
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

        usage = getattr(response, "usage", None)
        actual_tokens = getattr(usage, "total_tokens", None) if usage is not None else None
        self.guard.reconcile(reserved_tokens, actual_tokens, price)
        content = response.choices[0].message.content or "{}"
        try:
            return output_model.model_validate_json(content)
        except ValidationError:
            return output_model.model_validate(json.loads(content))
