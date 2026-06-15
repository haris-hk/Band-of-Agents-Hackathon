from __future__ import annotations

import os


def _truthy(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).lower() in {"1", "true", "yes", "on"}


def is_shared_deployment() -> bool:
    return _truthy("SHARED_DEPLOYMENT")


def enforce_github_webhook_secret() -> bool:
    if is_shared_deployment():
        return True
    return _truthy("ENFORCE_GITHUB_WEBHOOK_SECRET")


def validate_deployment_config() -> list[str]:
    """Return fatal configuration errors for the current deployment mode."""
    errors: list[str] = []
    if not is_shared_deployment():
        return errors

    if not os.getenv("INCIDENT_API_KEY", "").strip():
        errors.append("SHARED_DEPLOYMENT requires INCIDENT_API_KEY")
    if not os.getenv("GITHUB_WEBHOOK_SECRET", "").strip():
        errors.append("SHARED_DEPLOYMENT requires GITHUB_WEBHOOK_SECRET")
    return errors


def require_docker_at_startup() -> bool:
    """Online / shared backends must run repro + validate in Docker."""
    explicit = os.getenv("REQUIRE_DOCKER")
    if explicit is not None and explicit.strip() != "":
        return _truthy("REQUIRE_DOCKER", default="false")
    return is_shared_deployment()


def require_incident_api_key() -> bool:
    if is_shared_deployment():
        return True
    return bool(os.getenv("INCIDENT_API_KEY", "").strip())
