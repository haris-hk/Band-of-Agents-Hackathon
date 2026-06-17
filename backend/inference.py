from __future__ import annotations

import asyncio
import json
import os
import random
from dataclasses import dataclass
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from backend.configuration import load_project_env
from backend.schemas import Provider

try:
    from openai import AsyncOpenAI
except ModuleNotFoundError:
    AsyncOpenAI = None  # type: ignore[assignment]

T = TypeVar("T", bound=BaseModel)

# Exponential back-off settings for rate-limit (429) errors.
_RATE_LIMIT_BASE_WAIT = 2.0   # seconds; doubles each retry
_MAX_RETRY_ATTEMPTS = 3       # retries per client before escalating to next tier


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
    openrouter_base_url: str

    @classmethod
    def from_env(cls) -> "ProviderSettings":
        load_project_env()
        return cls(
            live_llm_enabled=os.getenv("LIVE_LLM_ENABLED", "false").lower() == "true",
            max_run_usd=float(os.getenv("MAX_RUN_USD", "0")),
            max_run_tokens=int(os.getenv("MAX_RUN_TOKENS", "4000")),
            request_timeout_seconds=float(os.getenv("REQUEST_TIMEOUT_SECONDS", "20")),
            max_prompt_tokens=int(os.getenv("MAX_PROMPT_TOKENS", "2500")),
            max_agent_tokens=int(os.getenv("MAX_AGENT_TOKENS", "900")),
            aiml_model=os.getenv("AIML_MODEL", "gpt-4o"),
            featherless_model=os.getenv("FEATHERLESS_MODEL", "Qwen/Qwen2.5-Coder-32B-Instruct"),
            aiml_base_url=os.getenv("AIML_BASE_URL", "https://api.aimlapi.com/v1"),
            featherless_base_url=os.getenv("FEATHERLESS_BASE_URL", "https://api.featherless.ai/v1"),
            openrouter_base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
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
            print(f"DEBUG: used_tokens ({self.used_tokens}) + estimated ({estimated_tokens}) > max_run_tokens ({self.max_run_tokens})")
            raise GuardrailBlocked("LLM call blocked by MAX_RUN_TOKENS guardrail")
        estimated_usd = (estimated_tokens / 1000) * price_per_1k
        if self.max_usd <= 0 or self.spent_usd + estimated_usd > self.max_usd:
            print(f"DEBUG: spent_usd ({self.spent_usd}) + estimated_usd ({estimated_usd}) > max_usd ({self.max_usd})")
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


def _is_rate_limit_error(exc: Exception) -> bool:
    """Detect 429 rate-limit / quota-exceeded responses from any provider."""
    msg = str(exc).lower()
    return any(
        k in msg for k in ("429", "rate limit", "rate_limit", "quota", "too many requests", "capacity")
    )


async def _call_with_backoff(
    client: "AsyncOpenAI",
    model: str,
    messages: list[dict],
    max_tokens: int,
    timeout_seconds: float,
    label: str = "",
) -> object:
    """
    Call the OpenAI-compatible endpoint with exponential backoff on rate-limit
    errors. Raises the last exception if all retry attempts are exhausted.
    """
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRY_ATTEMPTS):
        try:
            async with asyncio.timeout(timeout_seconds):
                return await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=0,
                    response_format={"type": "json_object"},
                )
        except Exception as exc:
            last_exc = exc
            if _is_rate_limit_error(exc) and attempt < _MAX_RETRY_ATTEMPTS - 1:
                wait = _RATE_LIMIT_BASE_WAIT * (2 ** attempt) + random.uniform(0, 1)
                print(f"[inference] {label} rate-limited (attempt {attempt + 1}/{_MAX_RETRY_ATTEMPTS}). Waiting {wait:.1f}s...")
                await asyncio.sleep(wait)
            else:
                # Non-rate-limit error or last attempt — stop retrying this client
                break
    raise last_exc  # type: ignore[misc]


class InferenceClients:
    def __init__(self, settings: ProviderSettings | None = None) -> None:
        self.settings = settings or ProviderSettings.from_env()
        self.guard = SpendGuard(self.settings.max_run_usd, self.settings.max_run_tokens)
        if AsyncOpenAI is None:
            self.aiml = self.aiml_2 = self.featherless = self.featherless_2 = self.openrouter = None
            return
        self.aiml = AsyncOpenAI(
            base_url=self.settings.aiml_base_url,
            api_key=os.getenv("AIML_API_KEY") or "missing",
            timeout=self.settings.request_timeout_seconds,
        )
        self.aiml_2 = AsyncOpenAI(
            base_url=self.settings.aiml_base_url,
            api_key=os.getenv("AIML_API_KEY_2") or os.getenv("AIML_API_KEY") or "missing",
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
        self.featherless_2 = AsyncOpenAI(
            base_url=self.settings.featherless_base_url,
            api_key=os.getenv("FEATHERLESS_API_KEY_2") or os.getenv("FEATHERLESS_API_KEY") or "missing",
            timeout=self.settings.request_timeout_seconds,
            default_headers={
                "HTTP-Referer": "https://github.com/band-incident-response",
                "X-Title": "Band Incident Response (Fallback)",
            },
        )
        self.openrouter = AsyncOpenAI(
            base_url=self.settings.openrouter_base_url,
            api_key=os.getenv("OPENROUTER_API_KEY") or "missing",
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

        if provider == Provider.AIML:
            primary_client = self.aiml
            model = model or self.settings.aiml_model
            price = 0.01
        elif provider == Provider.OPENROUTER:
            primary_client = self.openrouter
            model = model or os.getenv("OPENROUTER_MODEL", "openai/gpt-oss-120b:free")
            price = 0.00
        else:
            primary_client = self.featherless
            model = model or self.settings.featherless_model
            price = 0.002

        if primary_client is None:
            raise GuardrailBlocked("LIVE_LLM_ENABLED=true but openai package is not installed")

        schema_hint = output_model.model_json_schema()
        schema_json = json.dumps(schema_hint, separators=(",", ":"))
        prompt_tokens = estimate_tokens(system) + estimate_tokens(user) + estimate_tokens(schema_json)
        if prompt_tokens > self.settings.max_prompt_tokens:
            print(f"DEBUG: prompt_tokens ({prompt_tokens}) > max_prompt_tokens ({self.settings.max_prompt_tokens})")
            raise GuardrailBlocked(f"LLM call blocked by MAX_PROMPT_TOKENS guardrail (prompt_tokens={prompt_tokens})")
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

        # ---------------------------------------------------------------
        # 4-Tier Fallback Chain (each tier uses full backoff internally):
        #   Tier 1: Primary key for the requested provider
        #   Tier 2: Secondary key for the same provider
        #   Tier 3: Cross-provider fallback (other provider, primary key)
        #   Tier 4: OpenRouter fallback (free models)
        # ---------------------------------------------------------------
        tier2_client = self.aiml_2 if provider == Provider.AIML else self.featherless_2
        tier3_client = self.featherless if provider == Provider.AIML else self.aiml
        tier3_model = self.settings.featherless_model if provider == Provider.AIML else self.settings.aiml_model
        
        # If openrouter is the requested provider, just do a basic fallback to Featherless
        if provider == Provider.OPENROUTER:
            tier2_client = self.featherless
            tier3_client = self.aiml
            tier3_model = self.settings.aiml_model

        response = None
        errors: list[str] = []

        for c, m, label in [
            (primary_client, model, f"Tier1/{provider.value}/primary-key"),
            (tier2_client,   model, f"Tier2/{provider.value}/secondary-key"),
            (tier3_client,   tier3_model, "Tier3/cross-provider"),
            (self.openrouter, os.getenv("OPENROUTER_MODEL", "openai/gpt-oss-120b:free"), "Tier4/openrouter-fallback"),
        ]:
            try:
                response = await _call_with_backoff(
                    c, m, messages,
                    max_tokens=self.settings.max_agent_tokens,
                    timeout_seconds=self.settings.request_timeout_seconds,
                    label=label,
                )
                print(f"[inference] {label} succeeded.")
                break
            except Exception as exc:
                err_msg = f"{label} failed: {exc}"
                errors.append(err_msg)
                print(f"[inference] {err_msg} — escalating to next tier...")

        if response is None:
            raise GuardrailBlocked(
                f"All 3 inference tiers exhausted. Errors: {'; '.join(errors)}"
            )

        usage = getattr(response, "usage", None)
        actual_tokens = getattr(usage, "total_tokens", None) if usage is not None else None
        self.guard.reconcile(reserved_tokens, actual_tokens, price)
        content = response.choices[0].message.content or "{}"
        try:
            return output_model.model_validate_json(content)
        except ValidationError:
            pass
        try:
            return output_model.model_validate(json.loads(content))
        except (ValidationError, json.JSONDecodeError) as exc:
            raise GuardrailBlocked(f"LLM response could not be parsed: {exc}") from exc
