from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend import main


class LifespanDockerGateTests(unittest.TestCase):
    def test_shared_deployment_fails_when_docker_unavailable(self) -> None:
        with patch.dict(
            os.environ,
            {
                "SHARED_DEPLOYMENT": "true",
                "INCIDENT_API_KEY": "secret",
                "GITHUB_WEBHOOK_SECRET": "whsec",
            },
            clear=False,
        ):
            with patch("backend.main.check_docker_available", return_value=(False, "down")):
                with self.assertRaises(RuntimeError):
                    with TestClient(main.app):
                        pass

    def test_shared_deployment_starts_when_docker_smoke_passes(self) -> None:
        with patch.dict(
            os.environ,
            {
                "SHARED_DEPLOYMENT": "true",
                "INCIDENT_API_KEY": "secret",
                "GITHUB_WEBHOOK_SECRET": "whsec",
            },
            clear=False,
        ):
            with (
                patch("backend.main.check_docker_available", return_value=(True, None)),
                patch("backend.main.check_docker_smoke", return_value=(True, None)),
            ):
                with TestClient(main.app) as client:
                    response = client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json().get("docker_required"))


if __name__ == "__main__":
    unittest.main()
