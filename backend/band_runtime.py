"""Band SDK runtime — see BAND_RUNTIME.md for Docker/PR limitations."""

from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path
from typing import Any

from band import Agent
from band.config import load_agent_config
from band.core.simple_adapter import SimpleAdapter

from backend.agent_names import AGENT_CONFIG_KEYS, AGENT_DISPLAY_NAMES, agent_mention
from backend.agent_loop import IncidentAgent, build_agents
from backend.configuration import AGENT_CONFIG_PATH, load_project_env
from backend.inference import InferenceClients
from backend.schemas import IncidentState, RawAlert, Stage


def _default_repo_path() -> str:
    return os.getenv("REPO_PATH", str(Path.cwd()))


def _default_repo_full_name() -> str | None:
    value = os.getenv("REPO_FULL_NAME", "").strip()
    return value or None


class CustomAdapter(SimpleAdapter[Any]):
    def __init__(self, stage_agent: IncidentAgent, next_mention: str | None) -> None:
        super().__init__(history_converter=None)
        self.stage_agent = stage_agent
        self.next_mention = next_mention
        self.llm = InferenceClients()

    async def on_message(
        self,
        msg,
        tools,
        history,
        participants_msg,
        contacts_msg,
        *,
        is_session_bootstrap: bool,
        room_id: str,
    ) -> None:
        state = _state_from_message(msg.content)
        
        if self.stage_agent.stage == Stage.TRIAGE:
            alert = state.raw_alert.payload
            repo_url = alert.get("repo_url")
            if repo_url:
                from backend.repo_access import ensure_repo_checkout
                try:
                    repo_path, repo_full_name = await ensure_repo_checkout(alert)
                    state.repo_path = repo_path
                    state.repo_full_name = repo_full_name
                except Exception as e:
                    print(f"Failed to clone repository: {e}")

        output = await self.stage_agent.run(state, self.llm)
        
        if self.stage_agent.stage == Stage.REPRO:
            from backend.agent_loop import run_repro_pass1
            state.repro_execution = await run_repro_pass1(state, output)

        import requests
        try:
            requests.post("http://localhost:8000/webhooks/local-events", json={
                "run_id": room_id,
                "stage": self.stage_agent.stage.value,
                "agent": self.stage_agent.name,
                "status": "done",
                "payload": output.model_dump(mode="json")
            })
        except Exception as e:
            print(f"🔍 [DEBUG] Failed to broadcast local event: {e}")

        _merge_stage_output(state, self.stage_agent.stage, output)
        payload = json.dumps(
            _handoff_payload(state, self.stage_agent.stage, output),
            separators=(",", ":"),
        )

        if self.next_mention:
            await tools.send_message(payload, mentions=[self.next_mention])
        else:
            await tools.send_message(payload, mentions=_reply_mentions(msg, tools))


def create_band_agents() -> list[Agent]:
    load_project_env()
    stages = build_agents()
    order = [Stage.TRIAGE, Stage.REPRO, Stage.TEST, Stage.FIX, Stage.RCA]
    agents: list[Agent] = []
    for index, stage in enumerate(order):
        next_stage = order[index + 1] if index + 1 < len(order) else None
        agent_id, api_key = load_agent_config(
            AGENT_CONFIG_KEYS[stage],
            config_path=AGENT_CONFIG_PATH,
        )
        agents.append(
            Agent.create(
                adapter=CustomAdapter(
                    stages[stage],
                    agent_mention(next_stage) if next_stage else None,
                ),
                agent_id=agent_id,
                api_key=api_key,
                ws_url=os.getenv("THENVOI_WS_URL")
                or "wss://app.band.ai/api/v1/socket/websocket",
                rest_url=os.getenv("THENVOI_REST_URL") or "https://app.band.ai",
            )
        )
    return agents


async def run_band_agents() -> None:
    await asyncio.gather(*(agent.run() for agent in create_band_agents()))


def _payload_from_message(content: str) -> dict[str, Any]:
    try:
        start_idx = content.find("{")
        if start_idx == -1:
            raise ValueError("No JSON object found")
        
        # Find the last closing brace to handle trailing markdown/text
        end_idx = content.rfind("}")
        if end_idx == -1 or end_idx < start_idx:
            raise ValueError("No closing brace found")
            
        json_str = content[start_idx : end_idx + 1]
        
        # If the string has unescaped quotes inside quotes (e.g., from a chat UI stripping backslashes),
        # we try to parse it. If it fails, we fall back to the safe message dict.
        return json.loads(json_str)
    except Exception as e:
        print(f"🔍 [DEBUG] Failed to parse payload JSON: {e}")
        return {"message": content}


def _state_from_message(content: str) -> IncidentState:
    payload = _payload_from_message(content)
    default_path = _default_repo_path()
    default_full_name = _default_repo_full_name()

    if payload.get("message_type") == "band.handoff.v1":
        state_payload = payload.get("state")
        if isinstance(state_payload, dict):
            state = IncidentState.model_validate(state_payload)
            if not state.repo_path:
                state.repo_path = default_path
            if not state.repo_full_name and default_full_name:
                state.repo_full_name = default_full_name
            return state

        handoff_payload = payload.get("payload")
        if isinstance(handoff_payload, dict):
            return IncidentState(
                raw_alert=RawAlert(payload=handoff_payload),
                repo_path=default_path,
                repo_full_name=default_full_name,
            )

    return IncidentState(
        raw_alert=RawAlert(payload=payload),
        repo_path=default_path,
        repo_full_name=default_full_name,
    )


def _handoff_payload(
    state: IncidentState,
    stage: Stage,
    output: Any,
) -> dict[str, Any]:
    return {
        "message_type": "band.handoff.v1",
        "stage": stage.value,
        "agent": _agent_name_for_stage(stage),
        "payload": output.model_dump(mode="json"),
        "state": state.model_dump(mode="json"),
    }


def _merge_stage_output(state: IncidentState, stage: Stage, output: Any) -> None:
    if stage == Stage.TRIAGE:
        state.context = output
    elif stage == Stage.REPRO:
        state.repro = output
        state.current_stage = Stage.TEST
    elif stage == Stage.TEST:
        state.tests = output
        state.current_stage = Stage.FIX
    elif stage == Stage.FIX:
        state.candidate_patches = output
        state.fix = output.candidates[0] if output.candidates else None
    elif stage == Stage.RCA:
        state.rca = output
        state.current_stage = Stage.DONE


def _agent_name_for_stage(stage: Stage) -> str:
    return AGENT_DISPLAY_NAMES.get(stage, stage.value)


def _reply_mentions(msg: Any, tools: Any) -> list[str]:
    sender_name = getattr(msg, "sender_name", None)
    if sender_name:
        return [sender_name]

    participants = getattr(tools, "participants", [])
    for participant in participants:
        mention = participant.get("handle") or participant.get("name")
        if mention:
            return [mention]
    return []


if __name__ == "__main__":
    asyncio.run(run_band_agents())
