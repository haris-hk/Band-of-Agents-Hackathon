from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from band import Agent
from band.config import load_agent_config

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.agent_loop import build_agents
from backend.agent_names import AGENT_CONFIG_KEYS
from backend.band_adapter import IncidentBandAdapter
from backend.configuration import AGENT_CONFIG_PATH, load_project_env
from backend.inference import InferenceClients
from backend.schemas import Stage

import logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

def _default_ws_url() -> str:
    return os.getenv("BAND_WS_URL") or os.getenv("THENVOI_WS_URL") or "wss://app.band.ai/api/v1/socket/websocket"


def _default_rest_url() -> str:
    return os.getenv("BAND_REST_URL") or os.getenv("THENVOI_REST_URL") or "https://app.band.ai"

def create_band_agents() -> list[Agent]:
    load_project_env()
    stage_agents = build_agents()
    shared_llm = InferenceClients()
    order = [Stage.TRIAGE, Stage.REPRO, Stage.TEST, Stage.FIX, Stage.VALIDATE, Stage.RCA]
    agents: list[Agent] = []

    for stage in order:
        agent_id, api_key = load_agent_config(
            AGENT_CONFIG_KEYS[stage],
            config_path=AGENT_CONFIG_PATH,
        )
        agents.append(
            Agent.create(
                adapter=IncidentBandAdapter(stage_agents[stage], stage, llm=shared_llm),
                agent_id=agent_id,
                api_key=api_key,
                ws_url=_default_ws_url(),
                rest_url=_default_rest_url(),
            )
        )

    return agents


async def run_band_agents() -> None:
    await asyncio.gather(*(agent.run() for agent in create_band_agents()))


if __name__ == "__main__":
    asyncio.run(run_band_agents())