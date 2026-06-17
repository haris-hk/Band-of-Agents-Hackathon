from __future__ import annotations

import hashlib
import hmac
import json
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend import main


async def no_op_run_incident(_alert):
    return None


class WebhookAuthTests(unittest.TestCase):
    def test_rejects_invalid_signature_when_enforced(self) -> None:
        body = json.dumps({"action": "opened", "issue": {"title": "x"}}).encode()
        with patch.dict(
            os.environ,
            {
                "SHARED_DEPLOYMENT": "true",
                "INCIDENT_API_KEY": "k",
                "GITHUB_WEBHOOK_SECRET": "topsecret",
            },
            clear=False,
        ):
            with patch("backend.main.run_incident", no_op_run_incident):
                with TestClient(main.app) as client:
                    response = client.post(
                        "/webhooks/github",
                        content=body,
                        headers={
                            "X-GitHub-Event": "issues",
                            "X-Hub-Signature-256": "sha256=deadbeef",
                        },
                    )
        self.assertEqual(response.status_code, 401)

    def test_accepts_valid_signature(self) -> None:
        secret = "topsecret"
        payload = {
            "action": "opened",
            "issue": {"title": "bug", "body": "", "labels": [], "number": 1},
            "repository": {
                "full_name": "org/repo",
                "clone_url": "https://github.com/org/repo.git",
            },
        }
        body = json.dumps(payload).encode()
        digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        with patch.dict(
            os.environ,
            {
                "SHARED_DEPLOYMENT": "true",
                "INCIDENT_API_KEY": "k",
                "GITHUB_WEBHOOK_SECRET": secret,
            },
            clear=False,
        ):
            with patch("backend.main.run_incident", no_op_run_incident):
                with TestClient(main.app) as client:
                    response = client.post(
                        "/webhooks/github",
                        content=body,
                        headers={
                            "X-GitHub-Event": "issues",
                            "X-Hub-Signature-256": f"sha256={digest}",
                        },
                    )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "pipeline started")


if __name__ == "__main__":
    unittest.main()
