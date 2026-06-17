from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from backend.repo_stack import detect_repo_stack, enrich_alert_docker_from_repo


class RepoStackTests(unittest.TestCase):
    def test_detects_node_stack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(
                json.dumps({"scripts": {"lint": "tsc --noEmit"}}),
                encoding="utf-8",
            )
            self.assertEqual(detect_repo_stack(root), "node")

    def test_enrich_node_alert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(
                json.dumps({"scripts": {"lint": "tsc --noEmit"}}),
                encoding="utf-8",
            )
            alert: dict[str, str] = {}
            enrich_alert_docker_from_repo(alert, root)
            self.assertEqual(alert["repo_stack"], "node")
            self.assertEqual(alert["docker_image"], "node:20-bookworm-slim")
            self.assertEqual(alert["repro_command"], "npm run lint")


if __name__ == "__main__":
    unittest.main()
