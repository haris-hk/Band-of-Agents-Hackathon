from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from band import Agent
from band.config import load_agent_config
from band.core.simple_adapter import SimpleAdapter

from backend.agent_loop import IncidentAgent, build_agents
from backend.configuration import AGENT_CONFIG_PATH, load_project_env
from backend.inference import InferenceClients
from backend.schemas import IncidentState, RawAlert, Stage


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
        output = await self.stage_agent.run(state, self.llm)
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
    config_keys = {
        Stage.TRIAGE: "incident_triager",
        Stage.REPRO: "incident_reproducer",
        Stage.FIX: "incident_fixer",
        Stage.TEST: "incident_test_generator",
        Stage.RCA: "incident_rca_writer",
    }
    agents: list[Agent] = []
    for index, stage in enumerate(order):
        next_stage = order[index + 1] if index + 1 < len(order) else None
        agent_id, api_key = load_agent_config(config_keys[stage], config_path=AGENT_CONFIG_PATH)
        agents.append(
            Agent.create(
                adapter=CustomAdapter(
                    stages[stage],
                    stages[next_stage].mention if next_stage else None,
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
        return json.loads(content[content.index("{") :])
    except Exception:
        return {"message": content}


def _state_from_message(content: str) -> IncidentState:
    payload = _payload_from_message(content)
    if payload.get("message_type") == "band.handoff.v1":
        state_payload = payload.get("state")
        if isinstance(state_payload, dict):
            return IncidentState.model_validate(state_payload)
        handoff_payload = payload.get("payload")
        if isinstance(handoff_payload, dict):
            return IncidentState(raw_alert=RawAlert(payload=handoff_payload))
    return IncidentState(raw_alert=RawAlert(payload=payload))


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
        state.current_stage = Stage.REPRO
    elif stage == Stage.REPRO:
        state.repro = output
        state.current_stage = Stage.TEST
    elif stage == Stage.TEST:
        state.tests = output
        state.current_stage = Stage.FIX
    elif stage == Stage.FIX:
        state.candidate_patches = output
        state.fix = output.candidates[0] if output.candidates else None
        state.current_stage = Stage.RCA
    elif stage == Stage.RCA:
        state.rca = output
        state.current_stage = Stage.DONE


def _agent_name_for_stage(stage: Stage) -> str:
    return {
        Stage.TRIAGE: "Alert Triager",
        Stage.REPRO: "Repro Planner",
        Stage.TEST: "Regression Test Generator",
        Stage.FIX: "Patch Generator",
        Stage.RCA: "RCA Publisher",
    }.get(stage, stage.value)


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
