# from __future__ import annotations

# import asyncio
# import json
# from typing import Any

# from fastapi import FastAPI, WebSocket, WebSocketDisconnect
# from fastapi.middleware.cors import CORSMiddleware

# from backend.agent_loop import IncidentOrchestrator
# from backend.schemas import RunRequest

# app = FastAPI(title="Band Incident Response")
# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
#     allow_credentials=True,
#     allow_methods=["*"],
#     allow_headers=["*"],
# )


# class WebSocketHub:
#     def __init__(self) -> None:
#         self._connections: dict[WebSocket, asyncio.Lock] = {}
#         self._lock = asyncio.Lock()

#     async def connect(self, ws: WebSocket) -> None:
#         await ws.accept()
#         async with self._lock:
#             self._connections[ws] = asyncio.Lock()

#     async def disconnect(self, ws: WebSocket) -> None:
#         async with self._lock:
#             self._connections.pop(ws, None)

#     async def send_json(self, ws: WebSocket, payload: dict[str, Any]) -> None:
#         lock = self._connections.get(ws)
#         if lock is None:
#             return
#         async with lock:
#             await ws.send_text(json.dumps(payload, separators=(",", ":")))

#     async def broadcast(self, payload: dict[str, Any]) -> None:
#         async with self._lock:
#             sockets = list(self._connections)
#         for ws in sockets:
#             try:
#                 await self.send_json(ws, payload)
#             except Exception:
#                 await self.disconnect(ws)


# hub = WebSocketHub()


# @app.get("/health")
# async def health() -> dict[str, str]:
#     return {"status": "ok"}


# @app.post("/incidents")
# async def submit_incident(request: RunRequest) -> dict[str, str]:
#     asyncio.create_task(run_incident(request.alert))
#     return {"status": "accepted"}


# @app.websocket("/ws/incidents")
# async def incidents_ws(ws: WebSocket) -> None:
#     await hub.connect(ws)
#     heartbeat_task = asyncio.create_task(heartbeat(ws))
#     try:
#         while True:
#             message = await ws.receive_text()
#             payload = json.loads(message)
#             if payload.get("type") == "pong":
#                 continue
#             request = RunRequest.model_validate(payload)
#             asyncio.create_task(run_incident(request.alert))
#     except WebSocketDisconnect:
#         pass
#     except Exception as exc:
#         await hub.send_json(ws, failure_payload(str(exc)))
#     finally:
#         heartbeat_task.cancel()
#         await hub.disconnect(ws)
#         await asyncio.gather(heartbeat_task, return_exceptions=True)


# async def heartbeat(ws: WebSocket) -> None:
#     while True:
#         await asyncio.sleep(30)
#         await hub.send_json(ws, {"type": "ping"})


# async def run_incident(alert: dict[str, Any]) -> None:
#     orchestrator = IncidentOrchestrator()
#     try:
#         async for event in orchestrator.run(alert):
#             await hub.broadcast(event.model_dump(mode="json"))
#     except Exception as exc:
#         await hub.broadcast(failure_payload(str(exc)))


# def failure_payload(error: str) -> dict[str, Any]:
#     return {
#         "stage": "failed",
#         "agent": "orchestrator",
#         "status": "failed",
#         "error": error,
#         "payload": {},
#     }
    
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
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


class WebSocketHub:
    def __init__(self) -> None:
        self._connections: dict[WebSocket, asyncio.Lock] = {}
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._connections[ws] = asyncio.Lock()

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._connections.pop(ws, None)

    async def send_json(self, ws: WebSocket, payload: dict[str, Any]) -> None:
        lock = self._connections.get(ws)
        if lock is None:
            return
        async with lock:
            await ws.send_text(json.dumps(payload, separators=(",", ":")))

    async def broadcast(self, payload: dict[str, Any]) -> None:
        async with self._lock:
            sockets = list(self._connections)
        for ws in sockets:
            try:
                await self.send_json(ws, payload)
            except Exception:
                await self.disconnect(ws)


hub = WebSocketHub()


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/incidents")
async def submit_incident(request: RunRequest) -> dict[str, str]:
    asyncio.create_task(run_incident(request.alert))
    return {"status": "accepted"}


@app.websocket("/ws/incidents")
async def incidents_ws(ws: WebSocket) -> None:
    await hub.connect(ws)
    heartbeat_task = asyncio.create_task(heartbeat(ws))
    try:
        while True:
            message = await ws.receive_text()
            payload = json.loads(message)
            if payload.get("type") == "pong":
                continue
            request = RunRequest.model_validate(payload)
            asyncio.create_task(run_incident(request.alert))
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        await hub.send_json(ws, failure_payload(str(exc)))
    finally:
        heartbeat_task.cancel()
        await hub.disconnect(ws)
        await asyncio.gather(heartbeat_task, return_exceptions=True)


async def heartbeat(ws: WebSocket) -> None:
    while True:
        await asyncio.sleep(30)
        await hub.send_json(ws, {"type": "ping"})


async def run_incident(alert: dict[str, Any]) -> None:
    orchestrator = IncidentOrchestrator()
    try:
        async for event in orchestrator.run(alert):
            await hub.broadcast(event.model_dump(mode="json"))
    except Exception as exc:
        await hub.broadcast(failure_payload(str(exc)))


def failure_payload(error: str) -> dict[str, Any]:
    return {
        "stage": "failed",
        "agent": "orchestrator",
        "status": "failed",
        "error": error,
        "payload": {},
    }


@app.post("/webhooks/github")
async def github_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
) -> dict[str, str]:
    payload_bytes = await request.body()

    # Verify HMAC signature
    webhook_secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    if webhook_secret:
        mac = hmac.new(
            webhook_secret.encode("utf-8"),
            payload_bytes,
            hashlib.sha256,
        )
        expected = f"sha256={mac.hexdigest()}"
        if not hmac.compare_digest(expected, x_hub_signature_256 or ""):
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

    payload: dict = json.loads(payload_bytes)
    alert: dict | None = None

    # Route by event type
    if x_github_event == "check_run":
        check_run = payload.get("check_run", {})
        if check_run.get("conclusion") == "failure":
            repo = payload.get("repository", {})
            output = check_run.get("output", {})
            alert = {
                "service":       repo.get("full_name"),
                "environment":   "ci",
                "severity":      "high",
                "error_message": output.get("summary", "CI check failed"),
                "error_details": output.get("text", ""),
                "repo_url":      repo.get("clone_url"),
                "repo_path":     f"/tmp/repos/{repo.get('name')}",
                "commit_sha":    check_run.get("head_sha"),
            }

    elif x_github_event == "issues":
        if payload.get("action") == "opened":
            issue = payload.get("issue", {})
            labels = [lbl.get("name", "") for lbl in issue.get("labels", [])]
            if "bug" in labels:
                repo = payload.get("repository", {})
                alert = {
                    "service":       repo.get("full_name"),
                    "environment":   "production",
                    "severity":      "medium",
                    "error_message": issue.get("title", ""),
                    "error_details": issue.get("body", ""),
                    "repo_url":      repo.get("clone_url"),
                    "repo_path":     f"/tmp/repos/{repo.get('name')}",
                }

    # Fire the pipeline (non-blocking) using the same run_incident function
    if alert:
        asyncio.create_task(run_incident(alert))
        return {"status": "pipeline started"}

    return {"status": "ignored"}