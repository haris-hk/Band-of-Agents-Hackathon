from __future__ import annotations

import json
import re
from typing import Any

from backend.schemas import IncidentState, Stage

# Files that are never useful for an LLM fix — they're generated or huge lock files
_SKIP_PATTERNS = re.compile(
    r"(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|\.min\.(js|css)$"
    r"|__pycache__|\.pyc$|dist/|build/)",
    re.IGNORECASE,
)

# Roughly 4 chars per token; stay under 5,500 tokens of repo context per call
# so total prompt stays well under 8,000 tokens ($0.02 @ GPT-4o rates)
_MAX_REPO_CHARS = 20_000


def _dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _relevance_score(path: str, content: str, keywords: list[str]) -> float:
    """Score a file 0-1 based on how many incident keywords appear in it."""
    if not keywords:
        return 0.5
    path_lower = path.lower()
    content_lower = content.lower()
    hits = sum(
        1
        for kw in keywords
        if kw in path_lower or kw in content_lower
    )
    return hits / len(keywords)


def _extract_keywords(state: IncidentState) -> list[str]:
    """Pull key terms from the alert and triage context to rank files by."""
    alert = state.raw_alert.payload
    terms: list[str] = []

    for field in ("error", "impact", "error_details"):
        val = alert.get(field, "")
        if isinstance(val, str):
            # split into lowercase words >= 4 chars
            terms += [w.lower() for w in re.findall(r"[a-zA-Z]{4,}", val)]

    if state.context:
        ctx = state.context
        for attr in ("error_signature", "impact", "service"):
            val = getattr(ctx, attr, "") or ""
            terms += [w.lower() for w in re.findall(r"[a-zA-Z]{4,}", val)]
        terms += [c.lower() for comp in (ctx.suspected_components or []) for c in comp.split("/")]

    # Deduplicate while preserving order
    seen: set[str] = set()
    result: list[str] = []
    for t in terms:
        if t not in seen:
            seen.add(t)
            result.append(t)
    return result


def _priority_files(path: str) -> int:
    """Return a numeric priority: lower = more important. Docs > source > config > misc."""
    p = path.lower()
    if p in ("readme.md", "readme.txt", "readme"):
        return 0  # Always first — often contains credentials / usage examples
    if p.endswith(".md"):
        return 1
    if any(p.endswith(ext) for ext in (".tsx", ".ts", ".jsx", ".js", ".py", ".go", ".java", ".rb")):
        return 2
    if any(p.endswith(ext) for ext in (".html", ".css", ".json", ".yml", ".yaml", ".toml")):
        return 3
    return 4


def select_relevant_files(
    repo_files: dict[str, str],
    keywords: list[str],
    max_chars: int = _MAX_REPO_CHARS,
) -> dict[str, str]:
    """
    Select the most relevant files from the repo that fit within max_chars total.

    Strategy:
    1. Skip generated / lock files entirely.
    2. Score remaining files by keyword relevance + priority tier.
    3. Take top-scoring files until we hit the char budget.
    Always include README* regardless of budget — it's tiny and critical.
    """
    filtered = {
        path: content
        for path, content in repo_files.items()
        if not _SKIP_PATTERNS.search(path)
    }

    # Score every file
    scored = [
        (path, content, _relevance_score(path, content, keywords), _priority_files(path))
        for path, content in filtered.items()
    ]

    # Sort: highest relevance first, then by priority tier (lower = better)
    scored.sort(key=lambda x: (-x[2], x[3], x[0]))

    selected: dict[str, str] = {}
    used_chars = 0

    for path, content, _score, _prio in scored:
        is_readme = path.lower().startswith("readme")
        content_chars = len(content)

        if is_readme or used_chars + content_chars <= max_chars:
            selected[path] = content
            if not is_readme:
                used_chars += content_chars

    return selected


def build_stage_prompt(state: IncidentState, stage: Stage) -> str:
    """Stage-scoped prompt with smart relevance-based file selection.
    
    Each prompt is capped at ~6K tokens so it fits within a $0.02 per-request
    API budget at GPT-4o pricing (~$2.50 / million input tokens).
    """
    alert = state.raw_alert.payload
    keywords = _extract_keywords(state)

    payload: dict[str, Any] = {
        "run_id": str(state.run_id),
        "stage": stage.value,
        "service": alert.get("service"),
        "service_short": alert.get("service_short", alert.get("service")),
        "repo_full_name": state.repo_full_name or alert.get("repo_full_name"),
        "environment": alert.get("environment"),
        "repo_stack": alert.get("repo_stack", "unknown"),
    }

    if stage == Stage.TRIAGE:
        payload["alert"] = {
            "error": alert.get("error"),
            "error_details": alert.get("error_details"),
            "impact": alert.get("impact"),
            "severity": alert.get("severity"),
            "commit_sha": alert.get("commit_sha"),
        }
        # Triage: docs + a few source files to understand the service
        payload["repo_files"] = select_relevant_files(
            state.repo_files, keywords, max_chars=15_000
        )

    elif stage == Stage.REPRO:
        payload["context"] = _dump(state.context)
        payload["repro_command_hint"] = alert.get("repro_command") or alert.get("failing_command")
        # Repro: focus on source files that match the error keywords
        payload["repo_files"] = select_relevant_files(
            state.repo_files, keywords, max_chars=12_000
        )

    elif stage == Stage.TEST:
        payload["context"] = _dump(state.context)
        payload["repro_execution"] = _dump(state.repro_execution)
        # Test generator: it needs to know what files exist and their exact paths
        payload["repo_files"] = select_relevant_files(
            state.repo_files, keywords, max_chars=10_000
        )
        # Also pass a manifest of all file paths so the LLM can reference correct paths
        payload["all_file_paths"] = sorted(state.repo_files.keys())

    elif stage == Stage.FIX:
        payload["context"] = _dump(state.context)
        payload["repro_execution"] = _dump(state.repro_execution)
        payload["tests"] = _dump(state.tests)
        # Patch generator: most critical to have the right files - max relevance budget
        payload["repo_files"] = select_relevant_files(
            state.repo_files, keywords, max_chars=14_000
        )
        payload["all_file_paths"] = sorted(state.repo_files.keys())

    elif stage == Stage.RCA:
        payload["context"] = _dump(state.context)
        payload["validation"] = _dump(state.validation)
        payload["winning_patch"] = _dump(state.fix)
        payload["errors"] = state.errors[-5:]
        # RCA doesn't need repo files at all

    return json.dumps(payload, separators=(",", ":"))
