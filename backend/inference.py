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

try:
    import httpx
except ModuleNotFoundError:
    httpx = None  # type: ignore[assignment]

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
            # FIX 1: Default to "true" so agents actually run
            live_llm_enabled=os.getenv("LIVE_LLM_ENABLED", "true").lower() == "true",
            max_run_usd=float(os.getenv("MAX_RUN_USD", "10.0")),
            # FIX 2: Raised token limits — repo-injected prompts easily exceed 4000
            max_run_tokens=int(os.getenv("MAX_RUN_TOKENS", "200000")),
            # FIX 3: Raised timeout — local Ollama models need more time
            request_timeout_seconds=float(os.getenv("REQUEST_TIMEOUT_SECONDS", "300")),
            # FIX 4: Raised prompt token limit — repo files blow past 2500
            max_prompt_tokens=int(os.getenv("MAX_PROMPT_TOKENS", "100000")),
            max_agent_tokens=int(os.getenv("MAX_AGENT_TOKENS", "4000")),
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
        if self.max_run_tokens > 0 and self.used_tokens + estimated_tokens > self.max_run_tokens:
            print(f"DEBUG: used_tokens ({self.used_tokens}) + estimated ({estimated_tokens}) > max_run_tokens ({self.max_run_tokens})")
            raise GuardrailBlocked("LLM call blocked by MAX_RUN_TOKENS guardrail")
        estimated_usd = (estimated_tokens / 1000) * price_per_1k
        if self.max_usd > 0 and self.spent_usd + estimated_usd > self.max_usd:
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
                break
    raise last_exc  # type: ignore[misc]


def _parse_output(content: str, output_model: type[T]) -> T:
    """Parse LLM JSON response into the target Pydantic model."""
    # Strip markdown code fences if present
    cleaned = content.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        # Remove opening fence (```json or ```)
        lines = lines[1:] if lines[0].startswith("```") else lines
        # Remove closing fence
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    try:
        return output_model.model_validate_json(cleaned)
    except ValidationError:
        pass
    try:
        return output_model.model_validate(json.loads(cleaned))
    except (ValidationError, json.JSONDecodeError) as exc:
        raise GuardrailBlocked(f"LLM response could not be parsed: {exc}\nRaw content: {content[:500]}") from exc


class InferenceClients:
    def __init__(self, settings: ProviderSettings | None = None) -> None:
        self.settings = settings or ProviderSettings.from_env()
        self.guard = SpendGuard(self.settings.max_run_usd, self.settings.max_run_tokens)
        if AsyncOpenAI is None:
            self.aiml = self.aiml_2 = self.featherless = self.featherless_2 = self.openrouter = self.ollama_openai = None
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
        # FIX 5: Correct Ollama base URL must include /v1
        self.ollama_openai = AsyncOpenAI(
            base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
            api_key="ollama",  # Ollama requires a non-empty string but ignores the value
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

    async def _ollama_call(self, model: str, system: str, user: str, output_model: type[T]) -> T:
        """
        Call a local Ollama model via its OpenAI-compatible /v1 endpoint.
        Falls back to raw httpx if the openai client isn't available.
        """
        ollama_model = model or os.getenv("OLLAMA_MODEL", "qwen2.5-coder:32b")
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

        # Try OpenAI-compatible client first (cleaner)
        if self.ollama_openai is not None:
            try:
                print(f"[inference] Ollama/OpenAI-compat calling model={ollama_model}")
                async with asyncio.timeout(self.settings.request_timeout_seconds):
                    response = await self.ollama_openai.chat.completions.create(
                        model=ollama_model,
                        messages=messages,
                        max_tokens=self.settings.max_agent_tokens,
                        temperature=0.1,
                        # Note: Ollama doesn't support response_format for all models
                        # so we omit it and parse manually
                    )
                content = response.choices[0].message.content or "{}"
                print(f"[inference] Ollama succeeded via OpenAI-compat client.")
                return _parse_output(content, output_model)
            except Exception as exc:
                print(f"[inference] Ollama OpenAI-compat failed: {exc}, trying raw httpx...")

        # Raw httpx fallback
        if httpx is None:
            raise GuardrailBlocked("Ollama call failed: httpx not installed. Run: pip install httpx")

        ollama_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1").rstrip("/v1").rstrip("/")
        async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
            resp = await client.post(
                f"{ollama_url}/api/chat",
                json={
                    "model": ollama_model,
                    "stream": False,
                    "messages": messages,
                    "options": {
                        "temperature": 0.1,
                        "num_ctx": int(os.getenv("OLLAMA_NUM_CTX", "16384")),
                    },
                },
            )
            resp.raise_for_status()
            content = resp.json()["message"]["content"]
            print(f"[inference] Ollama succeeded via raw httpx.")
            return _parse_output(content, output_model)

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
            raise GuardrailBlocked("LIVE_LLM_ENABLED=false — set LIVE_LLM_ENABLED=true in your .env to enable LLM calls")

        # Ollama is handled separately — no spend guard, no OpenAI client tiers
        if provider == Provider.OLLAMA:
            return await self._ollama_call(model or "", system, user, output_model)

        if provider == Provider.AIML:
            primary_client = self.aiml
            model = model or self.settings.aiml_model
            price = 0.01
        elif provider == Provider.OPENROUTER:
            primary_client = self.openrouter
            model = model or os.getenv("OPENROUTER_MODEL", "openai/gpt-oss-120b:free")
            price = 0.00
        else:
            # Default: Featherless
            primary_client = self.featherless
            model = model or self.settings.featherless_model
            price = 0.002

        if primary_client is None:
            raise GuardrailBlocked("LIVE_LLM_ENABLED=true but openai package is not installed")

        schema_hint = output_model.model_json_schema()
        schema_json = json.dumps(schema_hint, separators=(",", ":"))
        prompt_tokens = estimate_tokens(system) + estimate_tokens(user) + estimate_tokens(schema_json)

        if self.settings.max_prompt_tokens > 0 and prompt_tokens > self.settings.max_prompt_tokens:
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
        # 4-Tier Fallback Chain:
        #   Tier 1: Primary key for the requested provider
        #   Tier 2: Secondary key for the same provider
        #   Tier 3: Cross-provider fallback
        #   Tier 4: OpenRouter fallback (free models)
        # ---------------------------------------------------------------
        if provider == Provider.OPENROUTER:
            tier2_client = self.featherless
            tier3_client = self.aiml
            tier3_model = self.settings.aiml_model
        elif provider == Provider.AIML:
            tier2_client = self.aiml_2
            tier3_client = self.featherless
            tier3_model = self.settings.featherless_model
        else:
            tier2_client = self.featherless_2
            tier3_client = self.aiml
            tier3_model = self.settings.aiml_model

        response = None
        errors: list[str] = []

        for c, m, label in [
            (primary_client,  model,        f"Tier1/{provider.value}/primary-key"),
            (tier2_client,    model,        f"Tier2/{provider.value}/secondary-key"),
            (tier3_client,    tier3_model,  "Tier3/cross-provider"),
            (self.openrouter, os.getenv("OPENROUTER_MODEL", "openai/gpt-oss-120b:free"), "Tier4/openrouter-fallback"),
        ]:
            if c is None:
                errors.append(f"{label} skipped: client not initialized")
                continue
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
                f"All inference tiers exhausted. Errors: {'; '.join(errors)}"
            )

        usage = getattr(response, "usage", None)
        actual_tokens = getattr(usage, "total_tokens", None) if usage is not None else None
        self.guard.reconcile(reserved_tokens, actual_tokens, price)
        content = response.choices[0].message.content or "{}"
        return _parse_output(content, output_model)