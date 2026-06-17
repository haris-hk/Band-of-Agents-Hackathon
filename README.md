# Band Incident Response

Autonomous incident pipeline: clone a GitHub repo, reproduce in Docker, write a regression test, generate and validate fixes, publish an RCA, then push a branch and open a PR.

## Hackathon judge demo (recommended)

**Prerequisites:** Docker Desktop running, Python 3.11+, Node 20+.

### One command (Windows)

```powershell
.\scripts\start.ps1
```

Open http://localhost:3000 → **Local demo** tab is selected by default → click **Run local demo**.

Headless proof (no browser):

```powershell
.\scripts\start.ps1 -RunE2E
```

### One command (macOS / Linux)

```bash
chmod +x scripts/start.sh
./scripts/start.sh
```

### Manual steps

```bash
cp .env.example .env          # demo defaults: LIVE_LLM_ENABLED=false
pip install -e ".[dev]"

# Terminal 1 — backend
uvicorn backend.main:app --host 127.0.0.1 --port 8000

# Terminal 2 — frontend
cd frontend && npm ci && npm run dev
```

1. Confirm `curl http://localhost:8000/health` shows `"docker_available": true`
2. Open http://localhost:3000 → **Run local demo**
3. Watch the **Band agent thread** (triage → repro → test → fix → validate → RCA)

The local demo uses `demo_alert.json` and the bundled `services/checkout/handler.py` bug — no GitHub token, no LLM API keys required.

## Triggers

| Source | Endpoint |
|--------|----------|
| Web UI | WebSocket `ws://localhost:8000/ws/incidents` with `{ "alert": { ... } }` |
| REST | `POST /incidents` |
| Demo payload | `GET /demo/alert` |
| GitHub | `POST /webhooks/github` (issues opened/labeled, failed check runs) |

## GitHub repo mode (live LLM)

In the UI, switch to **GitHub repo**. Requires:

| Variable | Purpose |
|----------|---------|
| `GITHUB_TOKEN` | Clone private repos + push PRs |
| `LIVE_LLM_ENABLED=true` | Real agent inference (not demo fallbacks) |
| `MAX_RUN_USD` | Must be &gt; 0 or LLM calls are blocked |
| `AIML_API_KEY` or `FEATHERLESS_API_KEY` | At least one provider |

After clone, the orchestrator detects **Node** vs **Python** repos and sets Docker image/commands automatically (`backend/repo_stack.py`).

## Environment

See [.env.example](.env.example) and [frontend/.env.example](frontend/.env.example).

| Variable | Purpose |
|----------|---------|
| `GITHUB_TOKEN` | Clone private repos + push PRs |
| `GITHUB_WEBHOOK_SECRET` | Verify GitHub webhooks |
| `INCIDENT_API_KEY` | Protect `/incidents` and WebSocket |
| `NEXT_PUBLIC_INCIDENT_API_KEY` | Frontend WebSocket auth (match above) |
| `SHARED_DEPLOYMENT=true` | **Requires** both API key and webhook secret |
| `LIVE_LLM_ENABLED` | `false` for judge demo; `true` for real repos |
| `REPOS_ROOT` | Clone cache directory |
| `RUNS_PERSIST` | Write event log to `RUNS_DIR` (default on) |

## Shared / production deployment

Use a **backend host that exposes Docker** to the API process (Docker socket mount or DinD). Repro and validate stages create ephemeral containers; without Docker the pipeline cannot complete.

```env
SHARED_DEPLOYMENT=true
INCIDENT_API_KEY=<long-random-secret>
GITHUB_WEBHOOK_SECRET=<from-github-webhook-settings>
GITHUB_TOKEN=<pat-or-app-token-for-clone-and-pr>
LIVE_LLM_ENABLED=true
MAX_RUN_USD=1.0
AIML_API_KEY=...
CORS_ORIGINS=https://your-frontend.example.com
```

Set `NEXT_PUBLIC_API_URL` and `NEXT_PUBLIC_INCIDENT_API_KEY` in the frontend build to point at the hosted API.

On startup the API runs a **Docker smoke test** when `SHARED_DEPLOYMENT=true` (or `REQUIRE_DOCKER=true`). The process exits if Docker is unavailable — so misconfigured hosts fail fast instead of mid-incident.

| Host requirement | Why |
|------------------|-----|
| Docker daemon reachable from the API | Repro + validation sandboxes |
| Writable container storage | Avoid read-only overlayfs errors |
| Outbound network from containers | `npm install` / `pip install` in cloned repos |
| Persistent `REPOS_ROOT` / `RUNS_DIR` (recommended) | Clone cache + fix export artifacts |

`GET /health` returns `docker_required`, `docker_smoke_ok`, and `docker_remediation` for UI pre-flight.

The API refuses to start if API key or webhook secret are missing in shared mode.

## Pipeline stages

1. **Triage** — normalize alert, clone/pull repo  
2. **Repro** — Docker sandbox, must observe failure  
3. **Test** — regression test artifact  
4. **Fix** — candidate patches  
5. **Validate** — apply patch + tests in Docker  
6. **RCA** — report + branch name  
7. **Push** — commit, push, open PR when `auto_pr` is true

Failures emit a single terminal `failed` event — never `done` after `failed`.

## Band SDK runtime

See [BAND_RUNTIME.md](BAND_RUNTIME.md). The Band path does **not** run Docker validation or open PRs.

## Observability

- `GET /health` — liveness + Docker status (UI pre-flight uses this)  
- `GET /metrics` — incident counters  
- Run logs — `~/.band-runs/<run_id>.jsonl` when `RUNS_PERSIST=true`

## Security notes

- `agent_config.yaml` is gitignored — copy from `agent_config.example.yaml` and **rotate** any keys that were ever committed or shared.  
- Per-request `github_token` from the UI is stripped from WebSocket broadcasts.  
- Never commit `.env`.

## Development

```bash
pytest tests -v
ruff check backend tests
cd frontend && npm run build
python scripts/e2e_ws_pipeline.py   # requires backend + Docker
```

CI runs ruff, pytest, and `next build` on push/PR.

## API examples

**PowerShell — run demo via REST:**

```powershell
$alert = Invoke-RestMethod http://localhost:8000/demo/alert
$body = @{ alert = $alert } | ConvertTo-Json -Depth 10
Invoke-RestMethod -Uri http://localhost:8000/incidents -Method POST -ContentType application/json -Body $body
```

**bash:**

```bash
curl -s http://localhost:8000/demo/alert | jq .
curl -X POST http://localhost:8000/incidents -H "Content-Type: application/json" \
  -d "$(jq -n --slurpfile a demo_alert.json '{alert: $a[0]}')"
```
