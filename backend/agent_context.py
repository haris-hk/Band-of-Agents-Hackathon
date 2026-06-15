from __future__ import annotations

import json
from typing import Any

from backend.schemas import IncidentState, Stage


def _dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _trim_repo_files(repo_files: dict[str, str], limit: int = 12) -> dict[str, str]:
    items = list(repo_files.items())[:limit]
    return {path: content for path, content in items}


def build_stage_prompt(state: IncidentState, stage: Stage) -> str:
    """Stage-scoped prompt: smaller, faster, and avoids shipping full incident history to LLMs."""
    alert = state.raw_alert.payload
    payload: dict[str, Any] = {
        "run_id": str(state.run_id),
        "stage": stage.value,
        "service": alert.get("service"),
        "service_short": alert.get("service_short", alert.get("service")),
        "repo_full_name": state.repo_full_name or alert.get("repo_full_name"),
        "environment": alert.get("environment"),
    }

    if stage == Stage.TRIAGE:
        payload["alert"] = {
            "error": alert.get("error"),
            "error_details": alert.get("error_details"),
            "impact": alert.get("impact"),
            "severity": alert.get("severity"),
            "commit_sha": alert.get("commit_sha"),
        }
        payload["repo_files"] = _trim_repo_files(state.repo_files, limit=15)
    elif stage == Stage.REPRO:
        payload["context"] = _dump(state.context)
        payload["repo_files"] = _trim_repo_files(state.repo_files, limit=10)
        payload["repro_command_hint"] = alert.get("repro_command") or alert.get("failing_command")
    elif stage == Stage.TEST:
        payload["context"] = _dump(state.context)
        payload["repro_execution"] = _dump(state.repro_execution)
        payload["repo_files"] = _trim_repo_files(state.repo_files, limit=8)
    elif stage == Stage.FIX:
        payload["context"] = _dump(state.context)
        payload["repro_execution"] = _dump(state.repro_execution)
        payload["tests"] = _dump(state.tests)
        payload["repo_files"] = _trim_repo_files(state.repo_files, limit=12)
    elif stage == Stage.RCA:
        payload["context"] = _dump(state.context)
        payload["validation"] = _dump(state.validation)
        payload["winning_patch"] = _dump(state.fix)
        payload["errors"] = state.errors[-5:]

    return json.dumps(payload, separators=(",", ":"))
