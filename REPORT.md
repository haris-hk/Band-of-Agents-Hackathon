# Band-of-Agents — Project Report

Generated: 2026-06-14

**Product Description**
- **Summary:** Multi-agent orchestration for production incident detection, repro, automated patch proposal, regression test synthesis, and RCA generation. Uses multi-agent orchestration via Band (thenvoi adapter), plus two LLM providers (`AIML` and `FEATHERLESS`) to split responsibilities between reasoning and code generation.

**Current Implementation**
- **Backend service:** FastAPI app exposing a WebSocket at `/ws/incidents` implemented in `backend/main.py` to run the orchestrator.
- **Orchestrator:** `backend/agent_loop.py` implements `IncidentOrchestrator` and stage `IncidentAgent`s (triage → repro → fix → test → rca). State is modeled with Pydantic types in `backend/schemas.py`.
- **LLM clients & guards:** `backend/inference.py` contains `InferenceClients`, `ProviderSettings`, and a `SpendGuard` to limit cost/tokens and gate LLM usage via env `LIVE_LLM_ENABLED`.
- **Band runtime adapter:** `backend/band_runtime.py` contains `BandStageAdapter` and `create_band_agents()` which wire the stage agents into Band (`thenvoi.Agent`).
- **Frontend:** Minimal Next.js UI at `frontend/app/page.tsx` that opens a WebSocket to the backend, sends a JSON alert payload, and renders the live event feed, RCA, and proposed patch.
- **Packaging & deps:** Project metadata in `pyproject.toml` and frontend `package.json` (Next, React, TypeScript). Backend code is packaged under `backend`.

**Functional Features Completed**
- **Incident orchestration state machine:** deterministic stage transitions and event emission (`AgentEvent`).
- **Stage agent abstraction:** reusable `IncidentAgent` wrapper supporting provider, system prompt, output Pydantic model and fallback behavior.
- **Provider switch & guardrails:** pluggable provider selection between `AIML` and `FEATHERLESS` and spend guard to prevent runaway costs.
- **WebSocket interface & demo UI:** WebSocket runner in backend and a runnable UI to exercise the flow locally.
- **Band adapter present:** adapter to run each stage as a Band agent using `thenvoi` library.

**Non-Functional Features Completed**
- **Typed models & validation:** Pydantic models for alerts, plans, patches, tests, and RCA ensure structured outputs.
- **Configuration via dotenv:** `ProviderSettings.from_env()` reads runtime configuration from environment.
- **Modular design:** clear separation of concerns between orchestration, inference, and Band integration.

**Potential Errors and Bugs (observed / likely)**
- **LLM gating / no-op behavior:** If `LIVE_LLM_ENABLED` is false, `InferenceClients.json_call` raises `GuardrailBlocked` and the orchestrator falls back to local fallback outputs — this is intended but may be surprising during local runs.
- **Missing / placeholder credentials:** `InferenceClients` uses `os.getenv(... ) or "missing"` for API keys. If keys are missing, the clients may still instantiate and then fail on network calls.
- **SpendGuard logic:** `SpendGuard.reserve()` raises `GuardrailBlocked` when budget exceeded (or budget <= 0) — unexpected runs can be blocked silently.
- **Fragile JSON parsing:** `band_runtime._payload_from_message` extracts JSON by slicing at the first `{` — malformed messages may produce unhelpful payloads.
- **Broad exception handling:** WebSocket loop in `backend/main.py` catches generic Exception and returns a single failed payload; this may hide underlying issues.
- **Patch safety & automation:** `CodePatch` output is a proposed unified diff string, but there is no implementation of automated apply/review/CI — applying patches automatically would be risky.
- **No persistence / audit log:** State is in-memory only; there is no database or durable audit trail for incidents, handoffs, or approvals.
- **Limited testing:** No test harness or unit tests included in repository; regression tests are produced by agents but not run or validated by CI.

**Status of Band Integration**
- **Implemented:** `backend/band_runtime.py` implements `BandStageAdapter` and `create_band_agents()` to create `thenvoi.Agent` instances for each stage. Agents send messages to the next stage mention when available.
- **Requirements:** Band integration depends on valid `thenvoi` configuration (agent ids and API keys via `load_agent_config`) and environment variables `THENVOI_WS_URL` / `THENVOI_REST_URL`.
- **Remarks:** Integration is present and callable, but requires external Band/thenvoi service credentials and networking; no end-to-end test is included in the repo.

**System Architecture**
- **Components:**
  - **Frontend (Next.js):** lightweight UI to submit sample alerts and view live events via WebSocket.
  - **Backend (FastAPI):** orchestrator entrypoint (`/ws/incidents`), orchestrator & agents, inference clients, Band runtime adapter.
  - **Providers:** external LLM endpoints (AIML, FEATHERLESS) behind `InferenceClients`.
  - **Band (thenvoi):** optional runtime to run agents inside Band rooms using `BandStageAdapter`.
- **Dataflow:**
  1. Alert arrives (WebSocket-run sample from UI, or Band message).
  2. `IncidentOrchestrator` runs through stages: TRIAGE → REPRO → FIX → TEST → RCA.
  3. At each stage the associated `IncidentAgent` calls an LLM via `InferenceClients` (or falls back to built-in fallback result) and the orchestrator merges outputs into `IncidentState`.
  4. The orchestrator emits `AgentEvent`s over the WebSocket and accumulates `AgentHandoff`s in `state.band_thread` for Band posting.

**Complete Process Workflow**
- **1) Trigger** — Web UI or Band message provides a JSON `alert` payload.
- **2) TRIAGE** — `Alert Triager` extracts `IncidentContext` (service, environment, error_signature, severity, evidence).
- **3) REPRO** — `Reproducer` produces a `ReproPlan` with deterministic reproduction steps and required data.
- **4) FIX** — `Fix Agent` proposes a `CodePatch` as a unified-diff string (provider: `FEATHERLESS`).
- **5) TEST** — `Test Generator` synthesizes `RegressionTests` to validate the proposed patch.
- **6) RCA** — `RCA Writer` consolidates conversation into an `RCAReport` final markdown.
- **7) Handoff & Band thread** — orchestrator appends `AgentHandoff` entries for each stage-to-stage handoff; `BandStageAdapter` can publish/forward payloads to Band rooms.
- **8) Completion** — orchestrator emits a final `done` event with `rca` and `fix` payloads.

**Descriptions of Folders and Notable Functions**
- **`backend/`**
  - **`__init__.py`**: package marker.
  - **`agent_loop.py`**: core orchestrator and `IncidentAgent` definitions, `build_agents()` stage registry, fallback behaviours.
    - `IncidentOrchestrator.run(alert)` — asynchronous generator that yields `AgentEvent`s for each stage.
    - `IncidentAgent.run(state, llm)` — runs a single stage by calling the LLM or fallback.
  - **`band_runtime.py`**: Band adapter and agent creation helpers.
    - `BandStageAdapter.on_message(...)` — executes the stage agent in response to Band messages and forwards payload to the next mention.
    - `create_band_agents()` — reads thenvoi config and instantiates Band agents for each stage.
  - **`inference.py`**: LLM clients, base URLs, model names, and spend guard.
    - `InferenceClients.json_call(...)` — centralized LLM schema-driven call that enforces schema-based JSON responses and timeout.
  - **`main.py`**: FastAPI app with `/ws/incidents` WebSocket endpoint and `/health`.
  - **`schemas.py`**: Pydantic models describing alert, state, events, patches, tests, and RCA report.
- **`frontend/`**
  - **`app/page.tsx`**: small React UI that sends a sample alert to the backend WS and shows the event feed, RCA and proposed patch.
  - **`package.json`**: Next dev script and dependencies.

**How to Run (local development)**
1. Create a Python virtualenv (Python 3.11+).
   - `python -m venv .venv && .venv\Scripts\activate` (Windows)
2. Install backend dependencies:
   - `pip install -e .`
3. Install frontend dependencies and run Next dev server:
   - `cd frontend`
   - `npm install`
   - `npm run dev`
4. Run backend (in project root):
   - `uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000`
5. Open the frontend UI at `http://localhost:3000` and press Run to execute the sample `alert` via WebSocket.
6. To run Band agents (requires thenvoi agent config & env):
   - Ensure `THENVOI_WS_URL` and `THENVOI_REST_URL` are set and `thenvoi` agent config is available.
   - `python -m backend.band_runtime` or run `d:/Band_Of_Agents/Band-of-Agents-Hackathon/backend/band_runtime.py`

**Environment / Required Variables**
- `LIVE_LLM_ENABLED` — true/false to permit live LLM calls.
- `AIML_API_KEY`, `FEATHERLESS_API_KEY` — provider API keys.
- Optional base overrides: `AIML_BASE_URL`, `FEATHERLESS_BASE_URL`.
- Spend control: `MAX_RUN_USD`, `MAX_AGENT_TOKENS`.
- Band / thenvoi: `THENVOI_WS_URL`, `THENVOI_REST_URL`, plus agent config accessible via `thenvoi.config.load_agent_config`.

**Next Steps & Recommendations**
- Add a test suite and CI to run generated regression tests in a sandbox before recommending patches.
- Add persistent storage (insert incident records / handoffs into a DB) and an audit log.
- Harden JSON parsing and increase observability (structured logging, tracing, metrics).
- Add fine-grained RBAC / approval workflow before applying any code patches automatically.
- Provide end-to-end integration tests for Band agents with a mocked thenvoi endpoint.

**Questions / Clarifications Needed**
- Do you want automated application of `CodePatch` proposals (CI + merge) or only human-reviewed PRs?
- Which provider(s) will you supply credentials for in production (AIML, FEATHERLESS, both)?
- Do you want me to add a basic test harness that executes the orchestrator with a mocked LLM client to validate flows?
