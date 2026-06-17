import asyncio
import json
import websockets
import urllib.request

alert = {
    "repo_url": "https://github.com/band-incident-response/band-of-agents-hackathon.git",
    "service": "checkout",
    "error": "TypeError: payload is required",
    "severity": "sev1",
    "auto_pr": False
}

async def run():
    print("Testing github flow via websocket...")
    async with websockets.connect("ws://localhost:8000/ws/incidents") as ws:
        await ws.send(json.dumps({"alert": alert}))
        events = []
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=300)
            except asyncio.TimeoutError:
                print("Timeout!")
                break
            ev = json.loads(raw)
            if ev.get("type") == "ping":
                await ws.send(json.dumps({"type": "pong"}))
                continue
            
            status = ev.get("status")
            agent = ev.get("agent")
            stage = ev.get("stage")
            
            print(f"EVENT: {status:10} | {agent:25} | {stage}")
            if status in ("done", "failed"):
                print("Terminal state reached:", status)
                print(ev.get("error") or "")
                break

if __name__ == "__main__":
    asyncio.run(run())
