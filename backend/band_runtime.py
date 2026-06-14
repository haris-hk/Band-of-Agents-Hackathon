from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from dotenv import load_dotenv
from thenvoi import Agent
from thenvoi.config import load_agent_config
from thenvoi.core.simple_adapter import SimpleAdapter

from backend.agent_loop import IncidentAgent, build_agents
from backend.inference import InferenceClients
from backend.schemas import IncidentState, RawAlert, Stage


class BandStageAdapter(SimpleAdapter[Any]):
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
        *,
        is_session_bootstrap: bool,
        room_id: str,
    ) -> None:
        # Band delivers only @mentioned messages to this execution; keep local state per room.
        raw = _payload_from_message(msg.content)
        state = IncidentState(raw_alert=RawAlert(payload=raw))
        output = await self.stage_agent.run(state, self.llm)
        payload = json.dumps(output.model_dump(mode="json"), separators=(",", ":"))

        if self.next_mention:
            await tools.thenvoi_send_message(f"{self.next_mention} {payload}")
        else:
            await tools.thenvoi_send_message(payload)


def create_band_agents() -> list[Agent]:
    load_dotenv()
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
        agent_id, api_key = load_agent_config(config_keys[stage])
        agents.append(
            Agent.create(
                adapter=BandStageAdapter(
                    stages[stage],
                    stages[next_stage].mention if next_stage else None,
                ),
                agent_id=agent_id,
                api_key=api_key,
                ws_url=os.getenv("THENVOI_WS_URL"),
                rest_url=os.getenv("THENVOI_REST_URL"),
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


if __name__ == "__main__":
    asyncio.run(run_band_agents())
