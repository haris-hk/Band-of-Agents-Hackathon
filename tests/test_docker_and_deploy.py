from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from backend.alert_normalize import normalize_alert
from backend.deploy_security import (
    enforce_github_webhook_secret,
    is_shared_deployment,
    require_docker_at_startup,
    validate_deployment_config,
)
from backend.docker_health import check_docker_available, docker_unavailable_message, humanize_docker_error


class DockerHealthTests(unittest.TestCase):
    def test_humanize_read_only_overlayfs(self) -> None:
        raw = (
            "commit failed: write /var/lib/desktop-containerd/daemon/"
            "io.containerd.snapshotter.v1.overlayfs/metadata.db: read-only file system"
        )
        msg = humanize_docker_error(raw)
        self.assertIn("Docker Desktop", msg)

    def test_docker_unavailable_message_includes_hint(self) -> None:
        message = docker_unavailable_message("connection refused")
        self.assertIn("Docker is required", message)
        self.assertIn("not running", message.lower())

    @patch("docker.from_env")
    def test_check_docker_available_success(self, mock_from_env: MagicMock) -> None:
        client = MagicMock()
        mock_from_env.return_value = client
        ok, err = check_docker_available()
        self.assertTrue(ok)
        self.assertIsNone(err)
        client.ping.assert_called_once()

    @patch("docker.from_env", side_effect=Exception("daemon not running"))
    def test_check_docker_available_failure(self, _mock_from_env: MagicMock) -> None:
        ok, err = check_docker_available()
        self.assertFalse(ok)
        self.assertIn("daemon not running", err or "")


class DeploySecurityTests(unittest.TestCase):
    @patch.dict("os.environ", {"SHARED_DEPLOYMENT": "true"}, clear=False)
    def test_shared_deployment_requires_secrets(self) -> None:
        with patch.dict("os.environ", {"INCIDENT_API_KEY": "", "GITHUB_WEBHOOK_SECRET": ""}, clear=False):
            errors = validate_deployment_config()
        self.assertEqual(len(errors), 2)

    @patch.dict(
        "os.environ",
        {
            "SHARED_DEPLOYMENT": "true",
            "INCIDENT_API_KEY": "secret",
            "GITHUB_WEBHOOK_SECRET": "whsec",
        },
        clear=False,
    )
    def test_shared_deployment_valid(self) -> None:
        self.assertEqual(validate_deployment_config(), [])
        self.assertTrue(enforce_github_webhook_secret())
        self.assertTrue(is_shared_deployment())


class RequireDockerStartupTests(unittest.TestCase):
    @patch.dict("os.environ", {"SHARED_DEPLOYMENT": "false"}, clear=False)
    def test_docker_not_required_locally_by_default(self) -> None:
        os.environ.pop("REQUIRE_DOCKER", None)
        self.assertFalse(require_docker_at_startup())

    @patch.dict(
        "os.environ",
        {
            "SHARED_DEPLOYMENT": "true",
            "INCIDENT_API_KEY": "secret",
            "GITHUB_WEBHOOK_SECRET": "whsec",
        },
        clear=False,
    )
    def test_docker_required_when_shared(self) -> None:
        os.environ.pop("REQUIRE_DOCKER", None)
        self.assertTrue(require_docker_at_startup())

    @patch.dict(
        "os.environ",
        {"SHARED_DEPLOYMENT": "true", "REQUIRE_DOCKER": "false"},
        clear=False,
    )
    def test_docker_can_be_disabled_explicitly(self) -> None:
        self.assertFalse(require_docker_at_startup())


class NormalizeDockerSetupTests(unittest.TestCase):
    def test_demo_alert_gets_patch_in_setup(self) -> None:
        alert = normalize_alert(
            {
                "repo_path": ".",
                "docker_image": "python:3.11-slim",
                "repro_command": "pytest",
                "validation_command": "pytest tests/checkout/test_incident_regression.py",
            }
        )
        self.assertIn("patch", alert["setup_command"])


if __name__ == "__main__":
    unittest.main()
