from __future__ import annotations

import unittest
from unittest.mock import patch

from backend.agent_loop import IncidentOrchestrator
from backend.schemas import (
    CandidatePatches,
    CodePatch,
    IncidentState,
    PatchValidationResult,
    RawAlert,
    RegressionTests,
    Stage,
    ValidationSwarmResult,
)


class ValidationFailureFinalizeTests(unittest.IsolatedAsyncioTestCase):
    async def test_validation_failure_finalize_emits_failed_not_done(self) -> None:
        orchestrator = IncidentOrchestrator()
        state = IncidentState(raw_alert=RawAlert(payload={"error": "boom"}))
        state.current_stage = Stage.VALIDATE
        state.candidate_patches = CandidatePatches(
            candidates=[
                CodePatch(
                    summary="fix-a",
                    patch_unified_diff="--- a/a.py\n+++ b/a.py\n",
                    rollback_plan="revert",
                ),
                CodePatch(
                    summary="fix-b",
                    patch_unified_diff="--- a/b.py\n+++ b/b.py\n",
                    rollback_plan="revert",
                ),
            ]
        )
        state.tests = RegressionTests(
            test_code="def test_x(): assert False",
            run_command="pytest",
        )

        async def no_winner(_state, _patches, _tests):
            return ValidationSwarmResult(
                results=[
                    PatchValidationResult(
                        candidate_index=0,
                        validation_passed=False,
                        error="failed",
                        patch_summary="fix-a",
                    )
                ]
            )

        with patch("backend.agent_loop.run_validation_swarm", no_winner):
            stage_statuses = [
                event.status
                async for event in orchestrator._run_validation_stage(state)
            ]
            finalize_statuses = [
                event.status
                async for event in orchestrator._finalize_run(state, {"auto_pr": False})
            ]

        self.assertEqual(state.current_stage, Stage.FAILED)
        self.assertIn("failed", stage_statuses)
        self.assertIn("failed", finalize_statuses)
        self.assertNotIn("done", finalize_statuses)


if __name__ == "__main__":
    unittest.main()
