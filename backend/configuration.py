from __future__ import annotations

from pathlib import Path

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:  # pragma: no cover - dependency is optional in some test runners.

    def load_dotenv(*_args: object, **_kwargs: object) -> bool:
        return False


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = PROJECT_ROOT / ".env"
AGENT_CONFIG_PATH = PROJECT_ROOT / "agent_config.yaml"


def load_project_env() -> bool:
    return load_dotenv(dotenv_path=ENV_PATH, override=False)
