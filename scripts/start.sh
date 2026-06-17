#!/usr/bin/env bash
# Start backend + frontend for hackathon demo (macOS/Linux).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example (demo defaults: LIVE_LLM_ENABLED=false)"
fi

PYTHON="${ROOT}/venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON=python3
fi

if ! docker info >/dev/null 2>&1; then
  echo "WARNING: Docker is not running. Start Docker before running the pipeline." >&2
fi

"$PYTHON" -m pip install -q -e ".[dev]" 2>/dev/null || true

echo "Starting backend on http://127.0.0.1:8000 ..."
"$PYTHON" -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 &
BACKEND_PID=$!

for i in $(seq 1 45); do
  if curl -sf http://127.0.0.1:8000/health >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

curl -sf http://127.0.0.1:8000/health | head -c 200
echo ""

if [[ ! -d frontend/node_modules ]]; then
  (cd frontend && npm ci)
fi

echo "Starting frontend on http://localhost:3000 ..."
(cd frontend && npm run dev) &
FRONTEND_PID=$!

echo ""
echo "=== Band Incident Response ==="
echo "  UI:      http://localhost:3000  (use Local demo tab)"
echo "  API:     http://127.0.0.1:8000/health"
echo "  Backend PID: $BACKEND_PID  Frontend PID: $FRONTEND_PID"
echo ""
echo "Headless demo: python scripts/e2e_ws_pipeline.py"
wait
