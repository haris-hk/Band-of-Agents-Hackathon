from __future__ import annotations

import asyncio
import json
from typing import Any

from band.core.protocols import AgentToolsProtocol
from band.core.simple_adapter import SimpleAdapter
from band.core.types import PlatformMessage

from backend.agent_loop import IncidentAgent
from backend.inference import InferenceClients
from backend.schemas import AgentEvent, AgentHandoff, IncidentState, RawAlert, Stage


STAGE_TRANSITIONS: dict[Stage, Stage | None] = {
    Stage.TRIAGE: Stage.REPRO,
    Stage.REPRO: Stage.TEST,
    Stage.TEST: Stage.FIX,
    Stage.FIX: Stage.VALIDATE,
    Stage.VALIDATE: Stage.RCA,
    Stage.RCA: None,
}

STAGE_HANDLES: dict[Stage, str] = {
    Stage.TRIAGE: "@zealox587/alert-triager",           
    Stage.REPRO: "@zealox587/incident-reproducer",      
    Stage.TEST: "@zealox587/regression-test-generato", 
    Stage.FIX: "@zealox587/patch-generator", 
    Stage.VALIDATE: "@zealox587/qa-validator",           
    Stage.RCA: "@zealox587/rca-publisher",             
}


class IncidentBandAdapter(SimpleAdapter[Any]):
    def __init__(self, agent: IncidentAgent, stage: Stage, llm: InferenceClients | None = None):
        super().__init__(history_converter=None)
        self.agent = agent
        self.stage = stage
        self.llm = llm or InferenceClients()
        self._room_states: dict[str, IncidentState] = {}
        self._room_locks: dict[str, asyncio.Lock] = {}
        self.agent_name = agent.name
        self.agent_description = agent.system_prompt
        self.system_prompt = agent.system_prompt

    async def on_started(self, agent_name: str, agent_description: str) -> None:
        self.agent_name = agent_name
        self.agent_description = agent_description
        self.system_prompt = agent_description or self.agent.system_prompt

    async def on_message(
        self,
        msg: PlatformMessage,
        tools: AgentToolsProtocol,
        history: Any,
        participants_msg: str | None,
        contacts_msg: str | None,
        *,
        is_session_bootstrap: bool,
        room_id: str,
    ) -> None:
        lock = self._room_locks.setdefault(room_id, asyncio.Lock())
        async with lock:
            try:
                state = self._room_states.get(room_id) or self._state_from_message(msg.content)
                self._room_states[room_id] = state

                state.events.append(
                    AgentEvent(
                        run_id=state.run_id,
                        stage=self.stage,
                        agent=self.agent.name,
                        status="active",
                        payload={
                            "room_id": room_id,
                            "is_session_bootstrap": is_session_bootstrap,
                        },
                    )
                )
                await tools.send_event(
                    json.dumps(
                        {
                            "room_id": room_id,
                            "stage": self.stage.value,
                            "agent": self.agent.name,
                            "status": "active",
                        },
                        separators=(",", ":"),
                    ),
                    message_type="task",
                )

                output = await self.agent.run(state, self.llm)
                self._merge_stage_output(state, output)
                state.steps_run += 1

                next_stage = self._get_next_stage(self.stage)
                if next_stage is None:
                    rca_summary = getattr(output, "final_markdown", None) or str(output)
                    if len(rca_summary) > 4000:
                        rca_summary = rca_summary[:4000] + "... (truncated)"
                        # Send final message with retry
                    max_retries = 3
                    for attempt in range(max_retries):
                        try:
                            await tools.send_message(
                                f"✅ Incident analysis complete!\n\n**RCA Report:**\n{rca_summary}",
                                mentions=["@harishk5647"],
                            )
                            print(f"RCA final message sent successfully (attempt {attempt + 1})")
                            break
                        except Exception as e:
                            if attempt < max_retries - 1:
                                print(f"RCA send failed, retrying ({attempt + 1}/{max_retries}): {e}")
                                await asyncio.sleep(2)
                                continue
                            raise
                    state.events.append(
                        AgentEvent(
                            run_id=state.run_id,
                            stage=self.stage,
                            agent=self.agent.name,
                            status="done",
                            payload={"room_id": room_id},
                        )
                    )
                    await tools.send_event(
                        json.dumps(
                            {
                                "room_id": room_id,
                                "stage": self.stage.value,
                                "agent": self.agent.name,
                                "status": "done",
                            },
                            separators=(",", ":"),
                        ),
                        message_type="task",
                    )
                    return

                next_handle = self._get_agent_handle(next_stage)
                handoff_payload = self._build_handoff_payload(state, output, next_stage)
                state.band_thread.append(
                    AgentHandoff(
                        from_agent=self.agent.name,
                        to_agent=next_handle,
                        stage=self.stage,
                        mention=next_handle,
                        payload=handoff_payload["payload"],
                        summary=getattr(output, "summary", None),
                    )
                )
                state.events.append(
                    AgentEvent(
                        run_id=state.run_id,
                        stage=self.stage,
                        agent=self.agent.name,
                        status="handoff",
                        payload={"to": next_handle, "room_id": room_id},
                    )
                )
                # Replace the send_message call (around line 105) with:
                import time

                # Send message with retry
                max_retries = 3
                for attempt in range(max_retries):
                    try:
                        await tools.send_message(
                            f"@{next_handle} Stage '{self.stage.value}' complete. Result: {json.dumps(output.model_dump(), separators=(',', ':'))}",
                            mentions=[next_handle],
                        )
                        break
                    except Exception as e:
                        if attempt < max_retries - 1:
                            print(f"Send message failed, retrying ({attempt + 1}/{max_retries}): {e}")
                            await asyncio.sleep(2)
                            continue
                        raise
                await tools.send_event(
                    json.dumps(
                        {
                            "room_id": room_id,
                            "stage": self.stage.value,
                            "agent": self.agent.name,
                            "status": "handoff",
                            "to": next_handle,
                        },
                        separators=(",", ":"),
                    ),
                    message_type="task",
                )
            except Exception as exc:
                state = self._room_states.get(room_id)
                if state is not None:
                    state.errors.append(f"{self.agent.name}: {exc}")
                    state.events.append(
                        AgentEvent(
                            run_id=state.run_id,
                            stage=self.stage,
                            agent=self.agent.name,
                            status="failed",
                            payload={"room_id": room_id},
                            error=str(exc),
                        )
                    )
                await tools.send_event(
                    json.dumps(
                        {
                            "room_id": room_id,
                            "stage": self.stage.value,
                            "agent": self.agent.name,
                            "error": str(exc),
                        },
                        separators=(",", ":"),
                    ),
                    message_type="error",
                )

    async def on_cleanup(self, room_id: str) -> None:
        self._room_states.pop(room_id, None)
        self._room_locks.pop(room_id, None)

    def _get_next_stage(self, stage: Stage) -> Stage | None:
        return STAGE_TRANSITIONS[stage]

    def _get_agent_handle(self, stage: Stage) -> str:
        return STAGE_HANDLES[stage]

    def _state_from_message(self, content: str) -> IncidentState:
        payload = self._payload_from_content(content)
        if payload.get("message_type") == "band.handoff.v1":
            state_payload = payload.get("state")
            if isinstance(state_payload, dict):
                return IncidentState.model_validate(state_payload)

            handoff_payload = payload.get("payload")
            if isinstance(handoff_payload, dict):
                return IncidentState(raw_alert=RawAlert(payload=handoff_payload))

        return IncidentState(raw_alert=RawAlert(payload=payload))

    def _payload_from_content(self, content: str) -> dict[str, Any]:
        content = content.strip()
        if not content:
            return {}

        try:
            return json.loads(content)
        except json.JSONDecodeError:
            brace_index = content.find("{")
            if brace_index >= 0:
                return json.loads(content[brace_index:])
            return {"message": content}

    def _merge_stage_output(self, state: IncidentState, output: Any) -> None:
        if self.stage == Stage.TRIAGE:
            state.context = output
            state.current_stage = Stage.REPRO
        elif self.stage == Stage.REPRO:
            state.repro = output
            state.current_stage = Stage.TEST
        elif self.stage == Stage.TEST:
            state.tests = output
            state.current_stage = Stage.FIX
        elif self.stage == Stage.FIX:
            state.candidate_patches = output
            state.fix = output.candidates[0] if getattr(output, "candidates", None) else None
            state.current_stage = Stage.VALIDATE  # ← was Stage.RCA
        elif self.stage == Stage.VALIDATE:          # ← add this block
            state.validation = output
            state.fix = (
                next(
                    (c for c in state.candidate_patches.candidates
                    if c.summary == output.winning_patch_id),
                    state.fix,
                )
                if state.candidate_patches and output.winning_patch_id
                else state.fix
            )
            state.current_stage = Stage.RCA
        elif self.stage == Stage.RCA:
            state.rca = output
            state.current_stage = Stage.DONE

    def _build_handoff_payload(
        self,
        state: IncidentState,
        output: Any,
        next_stage: Stage,
    ) -> dict[str, Any]:
        return {
            "message_type": "band.handoff.v1",
            "stage": self.stage.value,
            "agent": self.agent.name,
            "next_stage": next_stage.value,
            "mention": self._get_agent_handle(next_stage),
            "payload": output.model_dump(mode="json"),
            "state": state.model_dump(mode="json"),
        }