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
    Stage.TRIAGE:   "@zealox587/alert-triager",
    Stage.REPRO:    "@zealox587/incident-reproducer",
    Stage.TEST:     "@zealox587/regression-test-generato",  # actual Band handle — no trailing 'r'
    Stage.FIX:      "@zealox587/patch-generator",
    Stage.VALIDATE: "@zealox587/qa-validator",
    Stage.RCA:      "@zealox587/rca-publisher",
}



class IncidentBandAdapter(SimpleAdapter[Any]):
    def __init__(self, agent: IncidentAgent, stage: Stage, llm: InferenceClients | None = None):
        super().__init__(history_converter=None)
        self.agent = agent
        self.stage = stage
        self.llm = llm or InferenceClients()
        self._room_states: dict[str, IncidentState] = {}
        self._room_locks: dict[str, asyncio.Lock] = {}
        # Tracks (run_id, from_stage) pairs already processed per room to
        # prevent Band's session-bootstrap double-delivery from running the
        # LLM a second time on the same handoff.
        self._processed_handoffs: dict[str, set[tuple[str, str]]] = {}
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
        # ── Stage-based routing guard ──────────────────────────────────────────
        # Band delivers every room message to every agent. We must reject messages
        # that are not intended for this stage to prevent all agents from
        # re-running the pipeline on every handoff.
        raw_payload = self._payload_from_content(msg.content)
        if raw_payload.get("message_type") == "band.handoff.v1":
            # Handoff messages carry an explicit next_stage field.
            # Only process if next_stage matches our stage.
            next_stage_in_msg = raw_payload.get("next_stage")
            if next_stage_in_msg and next_stage_in_msg != self.stage.value:
                print(
                    f"[{self.agent.name}] Ignoring handoff intended for "
                    f"stage={next_stage_in_msg!r} (I am stage={self.stage.value!r})"
                )
                return
        else:
            # Non-handoff messages are new incident alerts — only TRIAGE handles them.
            if self.stage != Stage.TRIAGE:
                print(
                    f"[{self.agent.name}] Ignoring non-handoff message "
                    f"(only TRIAGE handles new alerts)"
                )
                return
        # ──────────────────────────────────────────────────────────────────────

        # ── Deduplication guard ───────────────────────────────────────────────
        # Band delivers the same message twice: once as a session bootstrap
        # (is_session_bootstrap=True) and again from the live queue.
        # Use (run_id, from_stage) as a dedup key so we skip the duplicate
        # without blocking genuinely new incidents.
        if raw_payload.get("message_type") == "band.handoff.v1":
            state_snippet = raw_payload.get("state") or {}
            run_id_key = str(state_snippet.get("run_id", ""))
            from_stage_key = str(raw_payload.get("stage", ""))
            dedup_key = (run_id_key, from_stage_key)
            seen = self._processed_handoffs.setdefault(room_id, set())
            if dedup_key in seen:
                print(
                    f"[{self.agent.name}] Skipping duplicate handoff "
                    f"run_id={run_id_key!r} from_stage={from_stage_key!r}"
                )
                return
            seen.add(dedup_key)
        # ──────────────────────────────────────────────────────────────────────

        lock = self._room_locks.setdefault(room_id, asyncio.Lock())
        async with lock:
            try:
                state = self._room_states.get(room_id) or self._state_from_message(msg.content)

                if state.raw_alert and state.raw_alert.payload:
                    from backend.alert_normalize import normalize_alert
                    normalized = normalize_alert(state.raw_alert.payload)
                    state.raw_alert.payload = normalized
                    if not state.repo_path:
                        state.repo_path = normalized.get("repo_path")
                    if not state.repo_full_name:
                        state.repo_full_name = normalized.get("repo_full_name")

                self._room_states[room_id] = state

                if state.raw_alert and state.raw_alert.payload:
                    from backend.repo_access import ensure_repo_checkout
                    repo_path, repo_error = await ensure_repo_checkout(state.raw_alert.payload)
                    if repo_path:
                        state.repo_path = repo_path
                        state.raw_alert.payload["repo_path"] = repo_path
                    if repo_error:
                        state.errors.append(f"repo checkout: {repo_error}")

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
                    import os
                    pr_url: str | None = None
                    pr_error: str | None = None
                    auto_pr = state.raw_alert.payload.get("auto_pr")
                    if auto_pr is None:
                        auto_pr = True
                    else:
                        auto_pr = bool(auto_pr)

                    # Override auto_pr to True if a valid repository is configured and GITHUB_TOKEN is present,
                    # to make sure Band Chat triggers are fully autonomous even if the raw payload omitted auto_pr or set it to false in demo.
                    if not auto_pr and os.getenv("GITHUB_TOKEN") and state.repo_full_name and "/" in state.repo_full_name:
                        auto_pr = True

                    if auto_pr and state.rca and state.rca.patch_unified_diff and state.fix:
                        try:
                            from backend.git_output import push_fix_as_pr
                            pr_url = await push_fix_as_pr(state=state, repo_path=state.repo_path)
                            print(f"PR pushed successfully: {pr_url}")
                        except Exception as exc:
                            pr_error = str(exc)
                            state.errors.append(f"pr: {exc}")
                            print(f"PR push failed: {exc}")
                    elif state.rca and state.rca.patch_unified_diff and not auto_pr:
                        pr_error = "auto_pr disabled; patch available in RCA only"
                    elif auto_pr and not state.fix:
                        pr_error = "no validated fix to push"


                    rca_summary = getattr(output, "final_markdown", None) or str(output)
                    if len(rca_summary) > 4000:
                        rca_summary = rca_summary[:4000] + "... (truncated)"
                    
                    message_lines = ["✅ Incident analysis complete!"]
                    if pr_url:
                        message_lines.append(f"🔗 **Pull Request:** {pr_url}")
                    elif pr_error:
                        message_lines.append(f"⚠️ **PR Link Status:** {pr_error}")
                    
                    message_lines.append(f"\n**RCA Report:**\n{rca_summary}")
                    final_msg = "\n".join(message_lines)

                    max_retries = 3
                    for attempt in range(max_retries):
                        try:
                            await tools.send_message(
                                final_msg,
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
                    
                    done_payload = {
                        "room_id": room_id,
                        "pr_url": pr_url,
                        "pr_error": pr_error,
                    }
                    state.events.append(
                        AgentEvent(
                            run_id=state.run_id,
                            stage=self.stage,
                            agent=self.agent.name,
                            status="done",
                            payload=done_payload,
                        )
                    )
                    await tools.send_event(
                        json.dumps(
                            {
                                "room_id": room_id,
                                "stage": self.stage.value,
                                "agent": self.agent.name,
                                "status": "done",
                                "pr_url": pr_url,
                                "pr_error": pr_error,
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
                # Build full handoff JSON (state + output) for the next agent
                handoff_json = json.dumps(handoff_payload, separators=(',', ':'))

                # next_handle may already include the leading "@" (e.g. "@org/agent")
                # Prepend @ only if it is missing to avoid "@@mention" which breaks Band routing
                mention_text = next_handle if next_handle.startswith("@") else f"@{next_handle}"

                # Send message with retry
                max_retries = 3
                for attempt in range(max_retries):
                    try:
                        await tools.send_message(
                            f"{mention_text} {handoff_json}",
                            mentions=[next_handle],
                        )
                        print(f"Handoff to {next_handle} sent (attempt {attempt + 1})")
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
                import traceback
                print(f"[{self.agent.name}] EXCEPTION in stage={self.stage.value}: {exc}")
                traceback.print_exc()
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
                # Send visible error message to Band chat so failures are not silent
                try:
                    await tools.send_message(
                        f"❌ **{self.agent.name}** failed at stage `{self.stage.value}`:\n```\n{exc}\n```",
                        mentions=["@harishk5647"],
                    )
                except Exception:
                    pass
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
        self._processed_handoffs.pop(room_id, None)

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
                from backend.alert_normalize import normalize_alert
                normalized = normalize_alert(handoff_payload)
                return IncidentState(
                    raw_alert=RawAlert(payload=normalized),
                    repo_path=normalized.get("repo_path"),
                    repo_full_name=normalized.get("repo_full_name"),
                )

        from backend.alert_normalize import normalize_alert
        normalized = normalize_alert(payload)
        return IncidentState(
            raw_alert=RawAlert(payload=normalized),
            repo_path=normalized.get("repo_path"),
            repo_full_name=normalized.get("repo_full_name"),
        )

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
            if state.candidate_patches and output.winning_patch_id:
                idx = 0
                if output.winning_patch_id.startswith("patch_"):
                    char = output.winning_patch_id.split("_")[1]
                    if len(char) == 1 and 'a' <= char <= 'z':
                        idx = ord(char) - ord('a')
                if 0 <= idx < len(state.candidate_patches.candidates):
                    state.fix = state.candidate_patches.candidates[idx]
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