from __future__ import annotations

import os
import re
from typing import Any

_SEVERITY_MAP = {
    "critical": "sev1",
    "sev1": "sev1",
    "p0": "sev1",
    "high": "sev1",
    "sev2": "sev2",
    "p1": "sev2",
    "medium": "sev2",
    "sev3": "sev3",
    "low": "sev3",
    "p2": "sev3",
    "sev4": "sev4",
    "info": "sev4",
    "p3": "sev4",
}

_GITHUB_REPO_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/([^/\s]+)/([^/\s#?]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)

_DEFAULT_DOCKER_SETUP = (
    "apt-get update -qq && apt-get install -y -qq patch "
    "&& python -m pip install -q pytest"
)


def default_docker_setup_command() -> str:
    return _DEFAULT_DOCKER_SETUP


def _first_string(alert: dict[str, Any], keys: tuple[str, ...], default: str = "") -> str:
    for key in keys:
        value = alert.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return default


def _parse_github_repo(value: str) -> str | None:
    value = value.strip()
    if "/" in value and " " not in value and not value.startswith("http"):
        parts = value.split("/", 1)
        if len(parts) == 2 and parts[0] and parts[1]:
            return f"{parts[0]}/{parts[1].removesuffix('.git')}"
    match = _GITHUB_REPO_RE.match(value)
    if match:
        return f"{match.group(1)}/{match.group(2)}"
    return None


def _repo_cache_path(repo_full_name: str) -> str:
    root = os.getenv("REPOS_ROOT", os.path.join(os.path.expanduser("~"), ".band-repos"))
    safe_name = repo_full_name.replace("/", "__")
    return os.path.join(root, safe_name)


def normalize_alert(raw: dict[str, Any]) -> dict[str, Any]:
    """Unify webhook, UI, and demo alert shapes into one pipeline contract."""
    alert = dict(raw)

    repo_link = _first_string(
        alert,
        ("repo_url", "repository_url", "github_url", "repo_link", "repository"),
    )
    repo_full_name = _first_string(alert, ("repo_full_name",))
    if not repo_full_name and repo_link:
        repo_full_name = _parse_github_repo(repo_link) or ""
    service = _first_string(alert, ("service",))
    if not repo_full_name and service:
        repo_full_name = _parse_github_repo(service) or (
            service if "/" in service else ""
        )
    if repo_full_name:
        alert["repo_full_name"] = repo_full_name
    service_short = (
        alert.get("service_short")
        if isinstance(alert.get("service_short"), str)
        else None
    )
    if not service_short:
        if repo_full_name:
            service_short = repo_full_name.split("/", 1)[-1]
        elif service:
            service_short = service.split("/")[-1]
        else:
            service_short = "unknown-service"
    alert["service_short"] = service_short
    alert["service"] = repo_full_name or service_short

    error = _first_string(
        alert,
        ("error", "error_message", "message", "title"),
    )
    if not error:
        details = _first_string(alert, ("error_details", "body", "text"))
        error = details[:500] if details else "unknown-error"
    alert["error"] = error

    raw_severity = _first_string(alert, ("severity",), "sev2").lower()
    alert["severity"] = _SEVERITY_MAP.get(raw_severity, "sev2")

    if not alert.get("environment"):
        alert["environment"] = "unknown"
    if not alert.get("impact"):
        alert["impact"] = "impact requires confirmation"

    if repo_full_name and not _first_string(alert, ("repo_url",)):
        alert["repo_url"] = f"https://github.com/{repo_full_name}.git"

    github_flow = bool(repo_full_name and alert.get("repo_url"))
    if "auto_pr" not in alert:
        if github_flow:
            alert["auto_pr"] = True
        else:
            alert["auto_pr"] = os.getenv("AUTO_PR_ENABLED", "false").lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
    else:
        alert["auto_pr"] = bool(alert["auto_pr"])

    if not alert.get("repo_path"):
        if repo_full_name:
            alert["repo_path"] = _repo_cache_path(repo_full_name)
        else:
            root = os.getenv("REPOS_ROOT", os.path.join(os.path.expanduser("~"), ".band-repos"))
            alert["repo_path"] = os.path.join(root, service_short)

    uses_docker = bool(
        alert.get("docker_image")
        or alert.get("repro_command")
        or alert.get("validation_command")
    )
    if uses_docker and not alert.get("setup_command"):
        alert["setup_command"] = _DEFAULT_DOCKER_SETUP

    return alert
