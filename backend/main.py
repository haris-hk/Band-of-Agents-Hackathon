from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from backend.agent_loop import IncidentOrchestrator
from backend.alert_normalize import normalize_alert
from backend.configuration import load_project_env
from backend.deploy_security import (
    enforce_github_webhook_secret,
    is_shared_deployment,
    require_docker_at_startup,
    require_incident_api_key,
    validate_deployment_config,
)
from backend.docker_health import check_docker_available, check_docker_smoke, humanize_docker_error
from backend.metrics import METRICS
from backend.run_store import append_run_event
from backend.schemas import RunRequest

load_project_env()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    errors = validate_deployment_config()
    if errors:
        message = "; ".join(errors)
        logger.error("Invalid deployment configuration: %s", message)
        raise RuntimeError(message)
    if is_shared_deployment():
        logger.info("Shared deployment mode: API key and webhook secret are required")
    if require_docker_at_startup():
        docker_ok, docker_message = await asyncio.to_thread(check_docker_available)
        smoke_ok = False
        smoke_message = docker_message
        if docker_ok:
            smoke_ok, smoke_message = await asyncio.to_thread(check_docker_smoke)
        if not docker_ok or not smoke_ok:
            detail = humanize_docker_error(smoke_message or docker_message or "unknown")
            message = f"Docker is required for this deployment but is not ready: {detail}"
            logger.error(message)
            raise RuntimeError(message)
        logger.info("Docker smoke test passed (repro/validate enabled)")
    yield


app = FastAPI(title="Band Incident Response", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv(
        "CORS_ORIGINS",
        "http://localhost:3000,http://127.0.0.1:3000",
    ).split(","),
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


def _require_incident_api_key(x_api_key: str | None) -> None:
    if not require_incident_api_key():
        return
    expected = os.getenv("INCIDENT_API_KEY", "").strip()
    if not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


@app.get("/health")
async def health() -> dict[str, Any]:
    docker_ok, docker_message = await asyncio.to_thread(check_docker_available)
    smoke_ok: bool | None = None
    smoke_message: str | None = None
    remediation: str | None = None
    if docker_ok:
        smoke_ok, smoke_message = await asyncio.to_thread(check_docker_smoke)
        if not smoke_ok and smoke_message:
            remediation = humanize_docker_error(smoke_message)
    elif docker_message:
        remediation = humanize_docker_error(docker_message)

    return {
        "status": "ok",
        "shared_deployment": is_shared_deployment(),
        "docker_required": require_docker_at_startup(),
        "docker_available": docker_ok and (smoke_ok is not False),
        "docker_ping_ok": docker_ok,
        "docker_smoke_ok": smoke_ok,
        "docker_message": smoke_message or docker_message,
        "docker_remediation": remediation,
    }


@app.get("/runs/{run_id}/fix.patch")
async def download_fix_patch(run_id: str) -> Any:
    from fastapi.responses import FileResponse

    from backend.run_store import runs_dir

    path = runs_dir() / run_id / "fix" / "fix.patch"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="fix.patch not found for this run")
    return FileResponse(path, media_type="text/x-diff", filename="fix.patch")


@app.get("/metrics")
async def metrics() -> dict[str, int]:
    return METRICS.snapshot()


@app.get("/demo/alert")
async def demo_alert_payload() -> dict[str, Any]:
    """Built-in checkout demo alert for hackathon / offline judges."""
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "demo_alert.json"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="demo_alert.json not found")
    return json.loads(path.read_text(encoding="utf-8"))


@app.post("/incidents")
async def submit_incident(
    request: RunRequest,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, str]:
    _require_incident_api_key(x_api_key)
    alert = normalize_alert(request.alert)
    asyncio.create_task(run_incident(alert))
    return {"status": "accepted"}


@app.websocket("/ws/incidents")
async def incidents_ws(ws: WebSocket) -> None:
    if require_incident_api_key():
        expected = os.getenv("INCIDENT_API_KEY", "").strip()
        api_key = ws.headers.get("x-api-key") or ws.query_params.get("api_key")
        if not api_key or not hmac.compare_digest(api_key, expected):
            await ws.close(code=4401)
            return

    await hub.connect(ws)
    heartbeat_task = asyncio.create_task(heartbeat(ws))
    try:
        while True:
            message = await ws.receive_text()
            payload = json.loads(message)
            if payload.get("type") == "pong":
                continue
            request = RunRequest.model_validate(payload)
            alert = normalize_alert(request.alert)
            asyncio.create_task(run_incident(alert))
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
    METRICS.inc("incidents_started")
    orchestrator = IncidentOrchestrator()
    try:
        async for event in orchestrator.run(alert):
            payload = event.model_dump(mode="json")
            append_run_event(str(event.run_id), payload)
            await hub.broadcast(payload)
            if event.status == "done":
                METRICS.inc("incidents_done")
            elif event.status == "failed" and event.agent == "orchestrator":
                METRICS.inc("incidents_failed")
    except Exception as exc:
        METRICS.inc("incidents_failed")
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
    webhook_secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "").strip()
    enforce = enforce_github_webhook_secret()

    if enforce and not webhook_secret:
        METRICS.inc("webhook_rejected")
        raise HTTPException(status_code=503, detail="GITHUB_WEBHOOK_SECRET is not configured")

    if webhook_secret:
        mac = hmac.new(webhook_secret.encode("utf-8"), payload_bytes, hashlib.sha256)
        expected = f"sha256={mac.hexdigest()}"
        if not hmac.compare_digest(expected, x_hub_signature_256 or ""):
            METRICS.inc("webhook_rejected")
            raise HTTPException(status_code=401, detail="Invalid webhook signature")
    elif enforce:
        METRICS.inc("webhook_rejected")
        raise HTTPException(status_code=401, detail="Missing webhook signature")

    try:
        payload: dict[str, Any] = json.loads(payload_bytes)
    except json.JSONDecodeError as exc:
        METRICS.inc("webhook_rejected")
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}") from exc

    alert: dict[str, Any] | None = None

    if x_github_event == "check_run":
        check_run = payload.get("check_run", {})
        if check_run.get("conclusion") == "failure":
            repo = payload.get("repository", {})
            output = check_run.get("output", {})
            alert = normalize_alert(
                {
                    "service": repo.get("full_name"),
                    "environment": "ci",
                    "severity": "high",
                    "error": output.get("summary", "CI check failed"),
                    "error_details": output.get("text", ""),
                    "repo_url": repo.get("clone_url"),
                    "commit_sha": check_run.get("head_sha"),
                    "source": "github-check_run",
                }
            )

    elif x_github_event == "issues":
        if payload.get("action") in {"opened", "labeled"}:
            issue = payload.get("issue", {})
            labels = [lbl.get("name", "").lower() for lbl in issue.get("labels", [])]
            is_bug = "bug" in labels or issue.get("title", "").lower().startswith("[bug]")
            if payload.get("action") == "opened" or is_bug:
                repo = payload.get("repository", {})
                alert = normalize_alert(
                    {
                        "service": repo.get("full_name"),
                        "environment": "production",
                        "severity": "medium",
                        "error": issue.get("title", ""),
                        "error_details": issue.get("body", ""),
                        "repo_url": repo.get("clone_url"),
                        "issue_number": issue.get("number"),
                        "issue_url": issue.get("html_url"),
                        "source": "github-issues",
                        "auto_pr": True,
                    }
                )

    if alert:
        METRICS.inc("webhook_accepted")
        asyncio.create_task(run_incident(alert))
        return {"status": "pipeline started"}

    return {"status": "ignored"}
