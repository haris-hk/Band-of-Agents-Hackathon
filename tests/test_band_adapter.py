from __future__ import annotations

import json
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.band_adapter import IncidentBandAdapter
from backend.schemas import (
    CodePatch,
    IncidentState,
    RawAlert,
    RCAReport,
    Stage,
)
from backend.agent_loop import IncidentAgent


class FakeMessage:
    def __init__(self, content: str, sender_name: str = "test-user"):
        self.content = content
        self.sender_name = sender_name


def _make_adapter(stage: Stage) -> IncidentBandAdapter:
    agent = MagicMock(spec=IncidentAgent)
    agent.name = f"{stage.value} Agent"
    agent.system_prompt = "Prompt"
    return IncidentBandAdapter(agent=agent, stage=stage)


def _make_tools() -> MagicMock:
    tools = MagicMock()
    tools.send_event = AsyncMock()
    tools.send_message = AsyncMock()
    return tools


def _handoff_json(from_stage: Stage, to_stage: Stage, state: IncidentState) -> str:
    """Build a band.handoff.v1 JSON message as sent between agents."""
    return json.dumps({
        "message_type": "band.handoff.v1",
        "stage": from_stage.value,
        "next_stage": to_stage.value,
        "agent": from_stage.value,
        "state": state.model_dump(mode="json"),
        "payload": {},
    })


class RoutingGuardTests(unittest.IsolatedAsyncioTestCase):
    """Each agent must ignore messages intended for a different stage."""

    async def test_triage_ignores_validate_to_rca_handoff(self) -> None:
        adapter = _make_adapter(Stage.TRIAGE)
        tools = _make_tools()

        # A validate→rca handoff: TRIAGE must not run
        state = IncidentState(raw_alert=RawAlert(payload={"error": "boom"}))
        msg = FakeMessage(_handoff_json(Stage.VALIDATE, Stage.RCA, state))

        await adapter.on_message(
            msg=msg, tools=tools, history=None,
            participants_msg=None, contacts_msg=None,
            is_session_bootstrap=False, room_id="room-1",
        )

        # Agent must NOT have run anything (no send_message, no send_event)
        tools.send_message.assert_not_called()
        tools.send_event.assert_not_called()

    async def test_rca_ignores_triage_to_repro_handoff(self) -> None:
        adapter = _make_adapter(Stage.RCA)
        tools = _make_tools()

        state = IncidentState(raw_alert=RawAlert(payload={"error": "boom"}))
        msg = FakeMessage(_handoff_json(Stage.TRIAGE, Stage.REPRO, state))

        await adapter.on_message(
            msg=msg, tools=tools, history=None,
            participants_msg=None, contacts_msg=None,
            is_session_bootstrap=False, room_id="room-2",
        )

        tools.send_message.assert_not_called()
        tools.send_event.assert_not_called()

    async def test_repro_ignores_plain_new_incident_message(self) -> None:
        """Only TRIAGE should pick up a raw (non-handoff) alert message."""
        adapter = _make_adapter(Stage.REPRO)
        tools = _make_tools()

        msg = FakeMessage(json.dumps({
            "repo_url": "https://github.com/org/repo.git",
            "error": "boom",
        }))

        await adapter.on_message(
            msg=msg, tools=tools, history=None,
            participants_msg=None, contacts_msg=None,
            is_session_bootstrap=False, room_id="room-3",
        )

        tools.send_message.assert_not_called()
        tools.send_event.assert_not_called()

    async def test_rca_processes_validate_to_rca_handoff_and_pushes_pr(self) -> None:
        """RCA agent must process a validate→rca handoff and open a PR."""
        fake_rca = RCAReport(
            title="Fix Bug",
            incident_summary="summary",
            customer_impact="none",
            root_cause="bug",
            final_markdown="RCA Report Markdown",
            git_branch="fix-branch",
            commit_message="fix bug",
            patch_unified_diff="diff --git a/file b/file",
        )

        agent = MagicMock(spec=IncidentAgent)
        agent.name = "RCA Agent"
        agent.system_prompt = "Prompt"
        agent.run = AsyncMock(return_value=fake_rca)

        adapter = IncidentBandAdapter(agent=agent, stage=Stage.RCA)
        tools = _make_tools()

        initial_state = IncidentState(
            raw_alert=RawAlert(payload={
                "repo_url": "https://github.com/org/repo.git",
                "auto_pr": True,
                "repo_full_name": "org/repo",
            }),
            repo_path="/fake/repo/path",
            repo_full_name="org/repo",
            fix=CodePatch(
                summary="fix summary",
                patch_unified_diff="diff --git a/file b/file",
                rollback_plan="none",
            ),
        )

        msg = FakeMessage(_handoff_json(Stage.VALIDATE, Stage.RCA, initial_state))

        with (
            patch("backend.repo_access.ensure_repo_checkout",
                  AsyncMock(return_value=("/fake/repo/path", None))),
            patch("backend.git_output.push_fix_as_pr",
                  AsyncMock(return_value="https://github.com/org/repo/pull/1")) as mock_push,
        ):
            await adapter.on_message(
                msg=msg, tools=tools, history=None,
                participants_msg=None, contacts_msg=None,
                is_session_bootstrap=False, room_id="room-rca",
            )

            mock_push.assert_called_once()
            tools.send_message.assert_called_once()
            sent_text = tools.send_message.call_args[0][0]
            self.assertIn("https://github.com/org/repo/pull/1", sent_text)

    async def test_state_from_message_normalization(self) -> None:
        adapter = _make_adapter(Stage.TRIAGE)

        msg_content = json.dumps({
            "repo_url": "https://github.com/org/repo.git",
            "error": "boom"
        })
        state = adapter._state_from_message(msg_content)
        self.assertEqual(state.repo_full_name, "org/repo")
        self.assertTrue(state.raw_alert.payload.get("auto_pr"))

    async def test_auto_pr_overridden_when_token_present(self) -> None:
        """If auto_pr=False but GITHUB_TOKEN is set and repo is valid, PR should be created."""
        fake_rca = RCAReport(
            title="Fix Bug", incident_summary="summary", customer_impact="none",
            root_cause="bug", final_markdown="RCA Markdown",
            git_branch="fix-branch", commit_message="fix bug",
            patch_unified_diff="diff --git a/file b/file",
        )
        agent = MagicMock(spec=IncidentAgent)
        agent.name = "RCA Agent"
        agent.system_prompt = "Prompt"
        agent.run = AsyncMock(return_value=fake_rca)
        adapter = IncidentBandAdapter(agent=agent, stage=Stage.RCA)
        tools = _make_tools()

        initial_state = IncidentState(
            raw_alert=RawAlert(payload={
                "repo_url": "https://github.com/org/repo.git",
                "auto_pr": False,
                "repo_full_name": "org/repo",
            }),
            repo_path="/fake/repo/path",
            repo_full_name="org/repo",
            fix=CodePatch(
                summary="fix summary",
                patch_unified_diff="diff --git a/file b/file",
                rollback_plan="none",
            ),
        )

        msg = FakeMessage(_handoff_json(Stage.VALIDATE, Stage.RCA, initial_state))

        with (
            patch.dict(os.environ, {"GITHUB_TOKEN": "some_token"}),
            patch("backend.repo_access.ensure_repo_checkout",
                  AsyncMock(return_value=("/fake/repo/path", None))),
            patch("backend.git_output.push_fix_as_pr",
                  AsyncMock(return_value="https://github.com/org/repo/pull/2")) as mock_push,
        ):
            await adapter.on_message(
                msg=msg, tools=tools, history=None,
                participants_msg=None, contacts_msg=None,
                is_session_bootstrap=False, room_id="room-pr2",
            )

            mock_push.assert_called_once()
            sent_text = tools.send_message.call_args[0][0]
            self.assertIn("https://github.com/org/repo/pull/2", sent_text)


if __name__ == "__main__":
    unittest.main()
