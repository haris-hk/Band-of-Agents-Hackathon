# Band Incident Response — Frontend

Next.js 15 dashboard for the Band Incident Response pipeline. Connects to the FastAPI backend via REST and a real-time WebSocket stream, and lets you trigger, observe, and download the output of each pipeline run.

---

## Prerequisites

| Tool | Version | Notes |
|------|---------|-------|
| Node.js | 20 + | Check with `node -v` |
| npm | 10 + | Bundled with Node 20 |
| Backend | running on port 8000 | See root `README.md` |

---

## Quick start

### 1 — Install dependencies

```bash
cd frontend
npm ci
```

> `npm ci` uses the committed `package-lock.json` for a reproducible install. Use `npm install` only when you want to update the lockfile.

### 2 — Configure environment

```bash
cp .env.example .env.local
```

`.env.local` is git-ignored. Edit it if needed:

```env
# Where the FastAPI backend listens (default works for local dev)
NEXT_PUBLIC_API_URL=http://localhost:8000

# Only required when the backend sets INCIDENT_API_KEY / SHARED_DEPLOYMENT=true
NEXT_PUBLIC_INCIDENT_API_KEY=
```

### 3 — Start the dev server

```bash
npm run dev
```

Open **http://localhost:3000**.

---

## Starting both backend and frontend together

### Windows (PowerShell)

```powershell
# From the repo root
.\scripts\start.ps1
```

### macOS / Linux

```bash
chmod +x scripts/start.sh
./scripts/start.sh
```

Both scripts start `uvicorn` on port 8000 then `next dev` on port 3000.

### Manual (two terminals)

**Terminal 1 — backend:**

```bash
# From repo root
pip install -e ".[dev]"
uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload
```

**Terminal 2 — frontend:**

```bash
cd frontend
npm ci
npm run dev
```

---

## How it works

### API integration

| Action | Method | Endpoint |
|--------|--------|----------|
| Health / pre-flight check | `GET` | `/health` |
| Fetch demo payload | `GET` | `/demo/alert` |
| Submit incident (HTTP fallback) | `POST` | `/incidents` with `{ "alert": { ... } }` |
| Live event stream | WebSocket | `ws://localhost:8000/ws/incidents` |
| Download HTML report | `GET` | `/runs/{run_id}/report.html` |
| Download patch file | `GET` | `/runs/{run_id}/fix.patch` |

### WebSocket flow

1. **Open** `ws://localhost:8000/ws/incidents` (or with `?api_key=…` when `INCIDENT_API_KEY` is set).
2. **Send** `{ "alert": { ... } }` — the backend starts the pipeline.
3. **Receive** a stream of `AgentEvent` JSON objects:
   ```jsonc
   {
     "run_id": "uuid-string",
     "stage": "triage | repro | test | fix | validate | rca | done | failed",
     "agent": "Alert Triager",
     "status": "queued | active | handoff | complete | failed | done",
     "payload": { /* stage-specific data */ },
     "error": null,
     "created_at": "2026-06-19T10:00:00Z"
   }
   ```
4. The backend sends a `{ "type": "ping" }` heartbeat every 30 s — the frontend replies with `{ "type": "pong" }` and ignores it.
5. When `status === "done"` the pipeline succeeded; `status === "failed"` means it failed.

### HTTP fallback

If the WebSocket cannot connect (e.g. a proxy blocking WebSocket upgrades), the frontend automatically submits the alert via `POST /incidents` and shows a banner. You won't get live event streaming in that case, but the pipeline still runs on the backend.

### Health polling

`GET /health` is called once on mount and then every **30 seconds** automatically. The **Pre-flight** sidebar shows three indicators:

| Indicator | Meaning |
|-----------|---------|
| API ok / down | Backend HTTP is reachable |
| Docker ready / down | Docker daemon is reachable from backend |
| Smoke test pass / fail | A container can actually execute (`docker run hello-world`) |

---

## UI tabs

### Incident input

Configure the alert before running:

- **Local demo** — uses the built-in `demo_alert.json` checkout bug. No GitHub token or LLM keys required (set `LIVE_LLM_ENABLED=false` in backend `.env`).
- **GitHub repo** — point at any GitHub repo URL. Requires `GITHUB_TOKEN` and `LIVE_LLM_ENABLED=true` on the backend.

Click **Run demo** (or **Run pipeline**) in the header toolbar to start.

### Agent chat

Live feed of agent messages as the pipeline runs. Each agent posts:
- **active** — what it's about to do
- **handoff** — passing the baton to the next agent
- **complete** — summary of its output
- **done / failed** — terminal status with full details

### Code changes

Appears after the `fix` and `validate` stages complete:
- Interactive unified-diff viewer per file (collapsible, with line numbers)
- **Download fix.patch** — `GET /runs/{run_id}/fix.patch`
- Branch / PR status if `auto_pr` was requested

### Final report

Appears when the pipeline finishes:
- Rendered RCA markdown (formatted in-browser)
- **↗ View Report** — opens `GET /runs/{run_id}/report.html` in a new browser tab
- **⬇ Download Report** — saves the self-contained HTML file locally

---

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `NEXT_PUBLIC_API_URL` | `http://localhost:8000` | Backend base URL (no trailing slash) |
| `NEXT_PUBLIC_INCIDENT_API_KEY` | *(empty)* | Must match backend `INCIDENT_API_KEY` when `SHARED_DEPLOYMENT=true` |

> Variables prefixed with `NEXT_PUBLIC_` are inlined at **build time** and visible in the browser bundle. Never put secrets here.

---

## Production build

```bash
npm run build
npm start
```

Set `NEXT_PUBLIC_API_URL` to your hosted backend URL before building.

> **Windows note:** `npm run build` may exit with a spurious PowerShell pipe error (`0xE9`) on some machines. This is a known Windows + Next.js worker issue unrelated to the code. If you see `✓ Compiled successfully` before the crash, the build artifacts are usable. For production builds on Windows, use **WSL 2** or a CI runner.

---

## Project structure

```
frontend/
├── app/
│   ├── globals.css      # Design system: tokens, layout, all component styles
│   ├── layout.tsx       # Root layout
│   └── page.tsx         # Dashboard: health polling, WS connection, all state
├── components/
│   ├── AgentChat.tsx    # Chat feed — renders ChatMessage bubbles
│   ├── ChangesTab.tsx   # Diff viewer + fix.patch download
│   ├── InputTab.tsx     # Alert configuration form
│   ├── ReportTab.tsx    # RCA markdown + report.html view/download
│   └── types.ts         # Shared TS types (FixExportPayload, WorkspaceTab)
└── lib/
    ├── agentThread.ts   # AgentEvent types, message builders, agent-status logic
    ├── chatMessages.ts  # Converts thread messages → ChatMessage for AgentChat
    └── parseDiff.ts     # Unified-diff parser → per-file row data for diff viewer
```

---

## Troubleshooting

| Problem | Likely cause | Fix |
|---------|-------------|-----|
| "API down" in Pre-flight | Backend not running | Start `uvicorn backend.main:app --port 8000` |
| "Docker down" or "Smoke test fail" | Docker Desktop not started | Launch Docker Desktop and wait for it to be ready |
| WebSocket rejected (close code 4401) | API key mismatch | Set `NEXT_PUBLIC_INCIDENT_API_KEY` to match the backend `INCIDENT_API_KEY` |
| Demo alert stuck on "Loading…" | Backend not reachable | Check CORS: `CORS_ORIGINS=http://localhost:3000` in backend `.env` |
| Pipeline submitted but no events stream | Proxy stripping WS upgrade | Use HTTP-only mode or configure your proxy to pass `Upgrade: websocket` |
| `npm run build` crashes on Windows | PowerShell pipe error | Build inside WSL 2 or a Linux CI runner |
| Run button stays disabled | Docker status unknown + `canRun` check | Click **Health** button in toolbar to refresh status |
