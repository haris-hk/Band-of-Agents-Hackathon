from __future__ import annotations

from typing import Any

_SENSITIVE_ALERT_KEYS = frozenset(
    {
        "github_token",
        "github_pat",
        "gh_token",
        "token",
    }
)


def public_alert(alert: dict[str, Any]) -> dict[str, Any]:
    """Return alert safe to broadcast over WebSocket (no tokens)."""
    return {key: value for key, value in alert.items() if key not in _SENSITIVE_ALERT_KEYS}
