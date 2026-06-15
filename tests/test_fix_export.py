from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from backend.docker_health import humanize_docker_error
from backend.fix_export import apply_patch_to_repo, build_replication_steps, export_validated_fix
from backend.schemas import CodePatch, IncidentState, RawAlert, RCAReport, RegressionTests

class HumanizeDockerErrorTests(unittest.TestCase):
    def test_read_only_overlayfs(self) -> None:
        raw = (
            'commit failed: write /var/lib/desktop-containerd/daemon/'
            'io.containerd.snapshotter.v1.overlayfs/metadata.db: read-only file system'
        )
        msg = humanize_docker_error(raw)
        self.assertIn("read-only", msg.lower())
        self.assertIn("Docker Desktop", msg)

    def test_connection_refused(self) -> None:
        msg = humanize_docker_error("Cannot connect to the Docker daemon connection refused")
        self.assertIn("not running", msg.lower())


class FixExportTests(unittest.TestCase):
    def test_apply_patch_to_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "handler.py"
            target.write_text("def handle(x):\n    return x\n", encoding="utf-8")
            patch = (
                "--- a/handler.py\n"
                "+++ b/handler.py\n"
                "@@ -1,2 +1,3 @@\n"
                " def handle(x):\n"
                "+    if x is None:\n"
                "+        return {}\n"
                "     return x\n"
            )
            ok, detail = apply_patch_to_repo(root, patch)
            self.assertTrue(ok, detail)
            self.assertIn("if x is None", target.read_text(encoding="utf-8"))

    def test_export_validated_fix_writes_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            handler = root / "services" / "checkout"
            handler.mkdir(parents=True)
            (handler / "handler.py").write_text("def handle(x):\n    return x\n", encoding="utf-8")

            patch = (
                "--- a/services/checkout/handler.py\n"
                "+++ b/services/checkout/handler.py\n"
                "@@ -1,2 +1,3 @@\n"
                " def handle(x):\n"
                "+    if x is None:\n"
                "+        return {}\n"
                "     return x\n"
            )
            state = IncidentState(
                run_id=uuid4(),
                raw_alert=RawAlert(source="test", payload={"patch_strip": 1}),
                repo_path=str(root),
            )
            state.fix = CodePatch(
                patch_unified_diff=patch,
                files_changed=["services/checkout/handler.py"],
                summary="Guard None input",
                rollback_plan="Revert handler.py",
            )
            state.tests = RegressionTests(
                test_code="def test_handle_none():\n    assert handle(None) == {}\n",
                test_files=["tests/checkout/test_incident_regression.py"],
                run_command="pytest tests/checkout/test_incident_regression.py",
            )
            state.rca = RCAReport(
                title="Checkout None crash",
                incident_summary="handle(None) raised TypeError",
                customer_impact="Checkout failures",
                root_cause="Missing None guard",
                final_markdown="# RCA",
                git_branch="fix/demo",
                commit_message="Fix None handling",
                patch_unified_diff=patch,
            )

            exported = export_validated_fix(state)
            self.assertTrue(Path(exported["patch_path"]).is_file())
            self.assertTrue(Path(exported["guide_path"]).is_file())
            self.assertIn("services/checkout/handler.py", exported["files_changed"])
            self.assertGreaterEqual(len(exported["replication_steps"]), 4)

    def test_build_replication_steps_includes_branch_and_test(self) -> None:
        state = IncidentState(
            run_id=uuid4(),
            raw_alert=RawAlert(source="test", payload={}),
            repo_path="/tmp/repo",
        )
        state.rca = RCAReport(
            title="Incident",
            incident_summary="summary",
            customer_impact="impact",
            root_cause="cause",
            final_markdown="# RCA",
            git_branch="fix/incident-abc",
            commit_message="Apply fix",
            patch_unified_diff="",
        )
        state.tests = RegressionTests(
            test_code="pass",
            test_files=["tests/test_x.py"],
            run_command="pytest tests/test_x.py",
        )
        steps = build_replication_steps(state)
        self.assertTrue(any("fix/incident-abc" in step for step in steps))
        self.assertTrue(any("pytest" in step for step in steps))


if __name__ == "__main__":
    unittest.main()
