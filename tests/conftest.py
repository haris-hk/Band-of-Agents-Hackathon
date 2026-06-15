"""Shared pytest hooks — keep TestClient startup independent of local Docker."""
from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _mock_docker_startup_for_app_tests() -> None:
    """
    Lifespan runs a Docker smoke test when SHARED_DEPLOYMENT / REQUIRE_DOCKER is set.
    API tests should not depend on a local Docker Desktop install.
    """
    with (
        patch("backend.main.check_docker_available", return_value=(True, None)),
        patch("backend.main.check_docker_smoke", return_value=(True, None)),
    ):
        yield
