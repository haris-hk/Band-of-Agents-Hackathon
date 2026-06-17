from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from backend.agent_loop import IncidentOrchestrator, Stage
from backend.schemas import IncidentState


class FinalizeRunTests(unittest.TestCase):
    def test_failed_does_not_emit_done(self) -> None:
        async def collect() -> list[str]:
            orchestrator = IncidentOrchestrator()
            alert = {"service": "svc", "error": "x", "repo_url": "https://github.com/a/b"}
            statuses: list[str] = []

            async def fake_prepare(state: IncidentState, _alert: dict) -> None:
                state.current_stage = Stage.FAILED
                state.errors.append("repo: clone failed")

            with patch.object(
                orchestrator,
                "_prepare_repository",
                new=AsyncMock(side_effect=fake_prepare),
            ):
                async for event in orchestrator.run(alert):
                    statuses.append(event.status)

            return statuses

        import asyncio

        statuses = asyncio.run(collect())
        self.assertIn("failed", statuses)
        self.assertNotIn("done", statuses)


if __name__ == "__main__":
    unittest.main()
