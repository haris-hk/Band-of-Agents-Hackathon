from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_DOCKER_FIELDS = (
    "docker_image",
    "setup_command",
    "repro_command",
    "validation_command",
    "container_timeout_seconds",
    "repo_stack",
)


def _has_explicit_docker_overrides(alert: dict[str, Any]) -> bool:
    return any(
        alert.get(key)
        for key in ("repro_command", "failing_command", "validation_command", "docker_image")
    )


def _read_package_scripts(repo_path: Path) -> dict[str, str]:
    package_json = repo_path / "package.json"
    if not package_json.is_file():
        return {}
    try:
        data = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    scripts = data.get("scripts")
    if not isinstance(scripts, dict):
        return {}
    return {key: str(value) for key, value in scripts.items() if isinstance(value, str)}


def _pick_node_repro_command(scripts: dict[str, str]) -> str:
    for name in ("test", "test:unit", "lint", "typecheck", "check"):
        if name in scripts:
            return f"npm run {name}"
    return "npm run lint" if "lint" in scripts else "npm run build"


def detect_repo_stack(repo_path: Path) -> str | None:
    """Return a coarse stack id: demo-python, python, node, or None."""
    root = repo_path.resolve()
    if (root / "services" / "checkout" / "handler.py").is_file():
        return "demo-python"
    if (root / "package.json").is_file():
        return "node"
    if any((root / name).is_file() for name in ("pyproject.toml", "requirements.txt", "setup.py")):
        return "python"
    return None


def enrich_alert_docker_from_repo(alert: dict[str, Any], repo_path: Path) -> None:
    """
    After checkout, set Docker image/commands from repository layout when the alert
    did not already specify repro/validation commands.
    """
    if _has_explicit_docker_overrides(alert):
        return

    stack = detect_repo_stack(repo_path)
    if stack is None:
        return

    if stack == "demo-python":
        alert.setdefault("docker_image", "python:3.11-slim")
        alert.setdefault(
            "setup_command",
            "python -m pip install -q pytest && "
            "(command -v patch >/dev/null || "
            "(apt-get update -qq && apt-get install -y -qq patch))",
        )
        alert.setdefault(
            "repro_command",
            'python -c "from services.checkout.handler import handle; handle(None)"',
        )
        alert.setdefault(
            "validation_command",
            "python -m pytest tests/checkout/test_incident_regression.py",
        )
        alert.setdefault("container_timeout_seconds", 180)
        alert["repo_stack"] = stack
        return

    if stack == "node":
        scripts = _read_package_scripts(repo_path)
        repro = _pick_node_repro_command(scripts)
        alert["docker_image"] = "node:20-bookworm-slim"
        alert["setup_command"] = (
            "apt-get update -qq && apt-get install -y -qq patch "
            "&& npm ci --ignore-scripts 2>/dev/null || npm install"
        )
        alert["repro_command"] = repro
        alert["validation_command"] = repro
        alert["container_timeout_seconds"] = 300
        alert["repo_stack"] = stack
        return

    if stack == "python":
        alert["docker_image"] = "python:3.11-slim"
        alert["setup_command"] = (
            "python -m pip install -q pytest && "
            "(command -v patch >/dev/null || "
            "(apt-get update -qq && apt-get install -y -qq patch))"
        )
        alert["repro_command"] = "pytest"
        alert["validation_command"] = "pytest"
        alert["container_timeout_seconds"] = 180
        alert["repo_stack"] = stack


def docker_fields_from_alert(alert: dict[str, Any]) -> dict[str, Any]:
    return {key: alert[key] for key in _DOCKER_FIELDS if key in alert}
