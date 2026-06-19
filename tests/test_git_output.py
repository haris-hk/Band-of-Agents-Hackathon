from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from backend.git_output import _push_fix_as_pr_sync
from backend.schemas import CodePatch, IncidentContext, IncidentState, RCAReport, RawAlert


class PushFixAsPrTests(unittest.TestCase):
    def test_push_fix_as_pr_uses_rca_title_and_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            (repo_path / "README.md").write_text("hello\n", encoding="utf-8")

            state = IncidentState(
                run_id=uuid4(),
                raw_alert=RawAlert(source="test", payload={"repo_path": str(repo_path)}),
                repo_path=str(repo_path),
                repo_full_name="acme/checkout",
                context=IncidentContext(
                    service="acme/checkout",
                    environment="prod",
                    error_signature="sig",
                    severity="sev2",
                    impact="impact",
                ),
            )
            state.fix_export = {"applied_to_repo": True}
            state.fix = CodePatch(
                summary="Fix",
                files_changed=["README.md"],
                patch_unified_diff="--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-hello\n+hello world\n",
                rollback_plan="Revert README",
            )
            state.rca = RCAReport(
                title="Checkout README fix",
                incident_summary="summary",
                customer_impact="impact",
                root_cause="cause",
                final_markdown="# RCA Summary",
                git_branch="fix/checkout-readme",
                commit_message="fix(checkout): update README",
                patch_unified_diff=state.fix.patch_unified_diff,
                validation_summary="Validated with pytest",
            )

            fake_repo = MagicMock()
            fake_pr = SimpleNamespace(html_url="https://github.com/acme/checkout/pull/1")
            fake_repo.create_pull.return_value = fake_pr

            with patch.dict(
                "os.environ",
                {"GITHUB_TOKEN": "token", "GITHUB_PR_BASE_BRANCH": "main"},
                clear=False,
            ), patch("backend.git_output.Github") as github_cls, patch(
                "backend.git_output._run_cmd"
            ) as run_cmd:
                github_cls.return_value.get_repo.return_value = fake_repo
                run_cmd.return_value = ""

                url = _push_fix_as_pr_sync(state, str(repo_path))

        self.assertEqual(url, fake_pr.html_url)
        fake_repo.create_pull.assert_called_once()
        kwargs = fake_repo.create_pull.call_args.kwargs
        self.assertEqual(kwargs["title"], "Checkout README fix")
        self.assertIn("## RCA Summary", kwargs["body"])
        self.assertIn("## Patch Diff", kwargs["body"])
        self.assertIn("Validated with pytest", kwargs["body"])
        self.assertEqual(kwargs["base"], "main")
        self.assertEqual(kwargs["head"], "fix/checkout-readme")


if __name__ == "__main__":
    unittest.main()