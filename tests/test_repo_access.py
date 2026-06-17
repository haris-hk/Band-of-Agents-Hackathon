from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from backend.alert_normalize import normalize_alert
from backend.repo_access import ensure_repo_checkout


class RepoCheckoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_normalize_and_clone_with_alert_token(self) -> None:
        alert = normalize_alert(
            {
                "repo_url": "https://github.com/org/repo.git",
                "error": "boom",
                "github_token": "ghp_test",
            }
        )
        captured: dict[str, str | None] = {}

        def fake_clone(repo_url: str, destination: Path, token: str | None, commit_sha: str | None) -> None:
            captured["repo_url"] = repo_url
            captured["token"] = token
            destination.mkdir(parents=True, exist_ok=True)

        with (
            patch("backend.repo_access._clone_repo", side_effect=fake_clone),
            patch(
                "backend.repo_access.resolve_safe_repo_path",
                side_effect=[FileNotFoundError(), Path(alert["repo_path"])],
            ),
        ):
            path, error = await ensure_repo_checkout(alert)

        self.assertIsNone(error)
        self.assertEqual(captured["token"], "ghp_test")
        self.assertIn("github.com", captured["repo_url"])
        self.assertTrue(path)


if __name__ == "__main__":
    unittest.main()
