"""End-to-end WebSocket pipeline smoke test (local demo_alert)."""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import urllib.error
import urllib.request

try:
    import websockets
except ImportError:
    import subprocess

    subprocess.check_call([sys.executable, "-m", "pip", "install", "websockets", "-q"])
    import websockets

API_BASE = "http://localhost:8000"
HEALTH_URL = f"{API_BASE}/health"
WS_URL = "ws://localhost:8000/ws/incidents"


async def main() -> int:
    try:
        health = await asyncio.to_thread(
            lambda: urllib.request.urlopen(HEALTH_URL, timeout=5).read()
        )
        health_data = json.loads(health.decode("utf-8"))
    except Exception as exc:
        print(f"FAIL: backend not reachable at {HEALTH_URL}: {exc}")
        print("Start with: uvicorn backend.main:app --host 127.0.0.1 --port 8000")
        return 1

    if not health_data.get("docker_available"):
        print(f"WARN: docker_available=false — {health_data.get('docker_message')}")
        print("Start Docker Desktop before expecting repro/validate to pass.")

    demo_path = pathlib.Path("demo_alert.json")
    if demo_path.is_file():
        alert = json.loads(demo_path.read_text(encoding="utf-8"))
    else:
        with urllib.request.urlopen(f"{API_BASE}/demo/alert", timeout=10) as response:
            alert = json.loads(response.read().decode("utf-8"))
    events: list[dict] = []
    async with websockets.connect(WS_URL) as ws:
        await ws.send(json.dumps({"alert": alert}))
        while len(events) < 50:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=300)
            except asyncio.TimeoutError:
                print("TIMEOUT waiting for events")
                return 1
            ev = json.loads(raw)
            if ev.get("type") == "ping":
                await ws.send(json.dumps({"type": "pong"}))
                continue
            events.append(ev)
            print(
                f"EVENT: {ev.get('status', ''):10} | "
                f"{ev.get('agent', ''):25} | {ev.get('stage')}"
            )
            if ev.get("status") in ("done", "failed"):
                break

    print("\n=== SUMMARY ===")
    print(f"Total events: {len(events)}")
    statuses = [e.get("status") for e in events]
    print(f"Has queued: {'queued' in statuses}")
    print(f"Has handoff: {'handoff' in statuses}")
    terminal = events[-1] if events else {}
    print(f"Terminal: {terminal.get('status')} @ {terminal.get('stage')}")
    if terminal.get("status") == "done":
        payload = terminal.get("payload", {})
        print(f"PR URL: {payload.get('pr_url', payload.get('pr', 'n/a'))}")
        return 0
    if terminal.get("status") == "failed":
        print(f"Error: {terminal.get('error', terminal.get('payload'))}")
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
