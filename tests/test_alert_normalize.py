from __future__ import annotations

import unittest

from backend.alert_normalize import normalize_alert
from backend.alert_sanitize import public_alert


class AlertNormalizeTests(unittest.TestCase):
    def test_maps_error_message_and_severity(self) -> None:
        alert = normalize_alert(
            {
                "service": "acme/checkout",
                "error_message": "payment failed",
                "severity": "high",
            }
        )
        self.assertEqual(alert["error"], "payment failed")
        self.assertEqual(alert["severity"], "sev1")
        self.assertEqual(alert["service_short"], "checkout")
        self.assertEqual(alert["repo_full_name"], "acme/checkout")

    def test_parses_github_repo_url(self) -> None:
        alert = normalize_alert(
            {
                "repo_url": "https://github.com/org/repo.git",
                "error": "boom",
            }
        )
        self.assertEqual(alert["repo_full_name"], "org/repo")
        self.assertEqual(alert["repo_url"], "https://github.com/org/repo.git")
        self.assertTrue(alert["auto_pr"])
        self.assertIn("repo_path", alert)

    def test_auto_pr_defaults_false_without_github_repo(self) -> None:
        alert = normalize_alert({"service": "svc", "error": "x"})
        self.assertFalse(alert["auto_pr"])

    def test_public_alert_strips_github_token(self) -> None:
        alert = normalize_alert(
            {
                "repo_url": "https://github.com/org/repo",
                "error": "boom",
                "github_token": "ghp_secret",
            }
        )
        public = public_alert(alert)
        self.assertNotIn("github_token", public)
        self.assertIn("repo_url", public)


if __name__ == "__main__":
    unittest.main()
