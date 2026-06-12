from __future__ import annotations

import json

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from backend.agent_loop import IncidentOrchestrator
from backend.schemas import RunRequest

app = FastAPI(title="Band Incident Response")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.websocket("/ws/incidents")
async def incidents_ws(ws: WebSocket) -> None:
    await ws.accept()
    orchestrator = IncidentOrchestrator()
    try:
        while True:
            request = RunRequest.model_validate_json(await ws.receive_text())
            async for event in orchestrator.run(request.alert):
                await ws.send_text(json.dumps(event.model_dump(mode="json"), separators=(",", ":")))
    except WebSocketDisconnect:
        return
    except Exception as exc:
        await ws.send_text(
            json.dumps(
                {
                    "stage": "failed",
                    "agent": "orchestrator",
                    "status": "failed",
                    "error": str(exc),
                    "payload": {},
                },
                separators=(",", ":"),
            )
        )
