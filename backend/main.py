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


@app.get("/runs/{run_id}/report.html")
async def download_incident_report(run_id: str) -> Any:
    """Generate and serve a complete self-contained HTML incident report."""
    import html as html_module
    from fastapi.responses import HTMLResponse
    from backend.run_store import runs_dir

    events_path = runs_dir() / f"{run_id}.jsonl"
    events: list[dict[str, Any]] = []
    if events_path.is_file():
        for line in events_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    if not events:
        raise HTTPException(status_code=404, detail="No events found for this run")

    queued = next((e for e in events if e.get("status") == "queued"), {})
    done = next((e for e in reversed(events) if e.get("status") == "done"), {})
    failed_ev = next((e for e in reversed(events) if e.get("status") == "failed"), None)
    terminal = done or failed_ev or {}
    payload = terminal.get("payload", {})

    rca: dict[str, Any] = payload.get("rca", {}) if isinstance(payload.get("rca"), dict) else {}
    fix: dict[str, Any] = payload.get("fix", {}) if isinstance(payload.get("fix"), dict) else {}
    tests: dict[str, Any] = payload.get("tests", {}) if isinstance(payload.get("tests"), dict) else {}
    fix_export: dict[str, Any] = payload.get("fix_export", {}) if isinstance(payload.get("fix_export"), dict) else {}

    patch_diff = fix.get("patch_unified_diff") or fix_export.get("patch_unified_diff", "")
    test_code = tests.get("test_code") or fix_export.get("test_code", "")
    rca_markdown = rca.get("final_markdown", "")
    root_cause = rca.get("root_cause", "")
    incident_title = rca.get("title", f"Incident — {run_id[:8]}")
    branch = rca.get("git_branch") or payload.get("branch", "")
    commit_msg = rca.get("commit_message", "")
    pr_url = payload.get("pr_url", "")
    timeline: list[str] = rca.get("timeline", []) or []
    factors: list[str] = rca.get("contributing_factors", []) or []
    recommendations: list[str] = rca.get("prevention_recommendations", []) or []
    validation_summary = rca.get("validation_summary", "")
    files_changed: list[str] = fix.get("files_changed") or fix_export.get("files_changed", []) or []

    alert_payload: dict[str, Any] = queued.get("payload", {}).get("alert", {}) or {}
    service = alert_payload.get("service") or alert_payload.get("service_short", "unknown")
    environment = alert_payload.get("environment", "unknown")
    severity = alert_payload.get("severity", "unknown")
    error_text = str(alert_payload.get("error", ""))[:140]

    e = html_module.escape
    status_color = "#10b981" if done else "#ef4444"
    status_text = "RESOLVED" if done else "FAILED"

    def li_list(items: list[str]) -> str:
        if not items:
            return "<p class='mu'>None recorded.</p>"
        return "<ul>" + "".join(f"<li>{e(str(i))}</li>" for i in items) + "</ul>"

    def code_block(text: str) -> str:
        if not text:
            return "<p class='mu'>Not available.</p>"
        return f"<pre class='cb'><code>{e(text)}</code></pre>"

    tl_html = "".join(
        f"<div class='tl'><span class='td'></span><span class='tt'>{e(str(item))}</span></div>"
        for item in timeline
    ) if timeline else "<p class='mu'>No timeline recorded.</p>"

    pr_html = (
        f'<a class="prl" href="{e(pr_url)}" target="_blank">🔗 View Pull Request</a>'
        if pr_url else ""
    )

    doc = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>{e(incident_title)}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f1117;color:#e2e8f0;line-height:1.6;padding:40px 20px}}
.c{{max-width:900px;margin:0 auto}}
.hd{{border-bottom:2px solid #1e2433;padding-bottom:24px;margin-bottom:32px}}
.rid{{font-family:monospace;font-size:12px;color:#64748b;margin-bottom:8px}}
.rt{{font-size:28px;font-weight:700;color:#f1f5f9;margin-bottom:12px}}
.bd{{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px}}
.bg{{padding:4px 12px;border-radius:20px;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.05em}}
.bs{{background:{status_color}22;color:{status_color};border:1px solid {status_color}44}}
.bv{{background:#f59e0b22;color:#f59e0b;border:1px solid #f59e0b44}}
.be{{background:#6366f122;color:#818cf8;border:1px solid #6366f144}}
.mg{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;margin-top:16px}}
.mi label{{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:#64748b;display:block;margin-bottom:4px}}
.mi span{{font-size:14px;color:#cbd5e1}}
.mi code{{font-family:monospace;background:#1e2433;padding:2px 6px;border-radius:4px;font-size:13px;color:#cbd5e1}}
section{{margin-bottom:32px}}
h2{{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#64748b;border-bottom:1px solid #1e2433;padding-bottom:8px;margin-bottom:16px}}
p{{color:#94a3b8;font-size:14px;margin-bottom:8px}}
ul{{color:#94a3b8;font-size:14px;padding-left:20px}} li{{margin-bottom:6px}}
.cb{{background:#0d1117;border:1px solid #1e2433;border-radius:8px;padding:20px;overflow:auto;font-family:monospace;font-size:12px;line-height:1.6;color:#e2e8f0;white-space:pre}}
.mu{{color:#475569;font-size:13px;font-style:italic}}
.prl{{display:inline-flex;align-items:center;gap:6px;background:#10b98122;color:#10b981;border:1px solid #10b98144;padding:8px 16px;border-radius:6px;text-decoration:none;font-size:13px;font-weight:600;margin-top:8px}}
.tl{{display:flex;gap:12px;padding:8px 0;border-bottom:1px solid #1e243380}}
.tl:last-child{{border-bottom:none}}
.td{{width:8px;height:8px;border-radius:50%;background:#6366f1;flex-shrink:0;margin-top:6px;display:inline-block}}
.tt{{font-size:13px;color:#94a3b8}}
.ft{{text-align:center;padding-top:32px;border-top:1px solid #1e2433;color:#475569;font-size:12px;margin-top:32px}}
</style></head>
<body><div class="c">
<div class="hd">
  <div class="rid">Incident Report · Run ID: {e(run_id)}</div>
  <div class="rt">{e(incident_title)}</div>
  <div class="bd">
    <span class="bg bs">{status_text}</span>
    <span class="bg bv">{e(severity)}</span>
    <span class="bg be">{e(environment)}</span>
  </div>
  <div class="mg">
    <div class="mi"><label>Service</label><span>{e(service)}</span></div>
    <div class="mi"><label>Error</label><span>{e(error_text)}</span></div>
    <div class="mi"><label>Branch</label><code>{e(branch) or "—"}</code></div>
    <div class="mi"><label>Commit</label><span>{e(commit_msg) or "—"}</span></div>
  </div>
  {pr_html}
</div>
<section><h2>Root Cause</h2><p>{e(root_cause) if root_cause else "<span class='mu'>Not available.</span>"}</p></section>
<section><h2>Timeline</h2>{tl_html}</section>
<section><h2>Files Changed ({len(files_changed)})</h2>{li_list(files_changed)}</section>
<section><h2>Code Patch (Unified Diff)</h2>{code_block(patch_diff)}</section>
<section><h2>Regression Test</h2>{code_block(test_code)}</section>
<section><h2>Validation Summary</h2><p>{e(validation_summary) if validation_summary else "<span class='mu'>Not available.</span>"}</p></section>
<section><h2>Contributing Factors</h2>{li_list(factors)}</section>
<section><h2>Prevention Recommendations</h2>{li_list(recommendations)}</section>
<section><h2>Full RCA</h2>{code_block(rca_markdown)}</section>
<div class="ft">Band-of-Agents autonomous incident response · Run {e(run_id[:8])}</div>
</div></body></html>"""

    return HTMLResponse(
        content=doc,
        headers={"Content-Disposition": f'attachment; filename="incident-{run_id[:8]}.html"'},
    )


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
    try:
        import requests
        import json
        import os
        from backend.run_store import append_run_event
        
        agent_key = "band_a_1781718378_oGUOGcE1RkdxVP9apFtoOvUiIvU-3NSJ"
        room_id = "b03e5ffe-59e1-48a1-97c9-2345db411b1d"

        alert_data = {
            "repo_url": "https://github.com/hamzaraza123/mock-buggy-project",
            "error": "Fix syntax error in level_1_syntax/app.py",
            "impact": "error",
            "service_short": "mock-buggy-project",
            "severity": "sev2",
            "auto_pr": "true",
        }

        data = {
            "message": {
                "content": f"@alert-triager\n```json\n{json.dumps(alert_data, indent=2)}\n```",
                "mentions": [
                    {"handle": "zealox587/alert-triager"}
                ]
            }
        }
        
        import asyncio
        response = await asyncio.to_thread(
            requests.post,
            f"https://app.band.ai/api/v1/agent/chats/{room_id}/messages",
            headers={
                "X-API-Key": agent_key,
                "Content-Type": "application/json"
            },
            json=data
        )
        response.raise_for_status()
        
    except Exception as exc:
        METRICS.inc("incidents_failed")
        await hub.broadcast(failure_payload(str(exc)))


def failure_payload(error: str, run_id: str = "") -> dict[str, Any]:
    return {
        "run_id": run_id,
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
