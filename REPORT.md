# Band-of-Agents - Project Report

Generated: 2026-06-14

**Product Description**
- **Summary:** Multi-agent incident response system for alert triage, Docker-based reproduction, regression test generation, patch generation, sandbox validation, and RCA/Git output.
- **Runtime shape:** The local FastAPI orchestrator now runs a Best-of-N validation architecture with N=2 candidate patches. It uses the Python Docker SDK for local sandbox execution and keeps Band/thenvoi integration available for stage-agent operation.
- **Provider strategy:** The orchestrator supports per-agent model routing through environment-selected model names while preserving the existing `agent.run()` style abstraction.

**Current Implementation**
- **Backend service:** `backend/main.py` exposes `/ws/incidents` over WebSocket and streams `AgentEvent` updates from `IncidentOrchestrator`.
- **Orchestrator:** `backend/agent_loop.py` now runs `triage -> repro -> test -> patch -> validate -> rca`.
- **Docker execution:** Repro Pass 1 and validation jobs use local Docker containers through the Python Docker SDK.
- **Validation swarm:** The validation stage runs up to 2 concurrent Docker containers, one per candidate patch. The first passing candidate wins and the remaining container is killed and removed.
- **LLM clients:** `backend/inference.py` still centralizes JSON calls and spend guards, now with optional per-call model override support.
- **Band runtime adapter:** `backend/band_runtime.py` preserves Band SDK agent creation and reorders stage handoff to match the new model-agent sequence.
- **Schemas:** `backend/schemas.py` now models repro execution, candidate patches, validation results, winning patches, and RCA/Git metadata.
- **Packaging:** `pyproject.toml` now includes `docker>=7.1.0`.

**Changes Made This Session**
- Refactored `backend/agent_loop.py` from a linear patch/test flow into a Docker-backed incident pipeline.
- Added `truncate_logs(log_string, max_lines=200)` and applied truncation to Docker stdout/stderr before logs enter downstream state.
- Added Docker sandbox configuration and lifecycle helpers:
  - Workspace mounted read-only from the host.
  - Per-container isolated writable copy under `/workspace`.
  - Optional setup command.
  - Strict cleanup on normal completion, failures, and timeouts.
- Added Repro Pass 1 execution:
  - Runs one Docker container.
  - Executes the configured repro command.
  - Captures truncated logs and stack-trace text.
  - Enforces the same 60 second container timeout policy.
- Reordered the local orchestrator flow:
  1. Alert Triager
  2. Repro Planner
  3. Repro Sandbox Pass 1
  4. Regression Test Generator
  5. Patch Generator
  6. Validation Swarm
  7. RCA Publisher
- Updated Patch Generator behavior:
  - It now asks for exactly 2 distinct candidate unified diffs.
  - `CandidatePatches` enforces exactly 2 candidates.
  - Fallback patch generation now emits 2 distinct diffs.
- Updated Validation Swarm behavior:
  - It slices candidates to `MAX_VALIDATION_CANDIDATES = 2`.
  - It starts a maximum of 2 concurrent Docker validation jobs.
  - It preserves fail-fast behavior: first `validation_passed: true` result wins.
  - It preserves the 60 second timeout per validation container.
  - It preserves loser cleanup by killing/removing pending containers after a winner.
  - It preserves strict patch application using `patch --batch --forward --fuzz=0`.
- Added RCA/Git output fields:
  - `git_branch`
  - `commit_message`
  - `patch_unified_diff`
  - `validation_summary`
- Added per-agent model routing:
  - `TRIAGE_MODEL`
  - `REPRO_MODEL`
  - `REGRESSION_TEST_MODEL`
  - `PATCH_GENERATOR_MODEL`
  - `RCA_MODEL`
- Updated Band runtime ordering from `triage -> repro -> fix -> test -> rca` to `triage -> repro -> test -> fix -> rca`.
- Added `docker>=7.1.0` to backend dependencies.

**Functional Features Completed**
- **AlertTriager:** Fast/cheap model route by default via `TRIAGE_MODEL` or `gpt-4o-mini`.
- **Repro Pass 1:** Medium model plans repro, then one Docker sandbox executes the failing command and captures truncated logs.
- **RegressionTestGenerator:** Heavy/smart model route generates one strict pytest-style regression test from Pass 1 logs.
- **PatchGenerator:** Heavy/smart model route generates exactly 2 distinct candidate unified diffs.
- **Validation Swarm:** Async Best-of-2 Docker validation race with fail-fast cleanup.
- **RCAPublisher:** Medium model route produces final RCA/Git JSON from the winning patch and validation logs.
- **Event stream:** The orchestrator continues yielding `AgentEvent` updates for WebSocket consumers.
- **Fallback behavior:** Existing no-live-LLM fallback path remains available, updated for the new two-candidate patch contract.

**Non-Functional Features Completed**
- **Strict log control:** Docker output is truncated to the last 200 lines before being stored or sent to later agents.
- **Timeout safety:** Docker repro and validation jobs are bounded by a hard 60 second timeout.
- **Container cleanup:** Containers are removed after normal execution and force-removed after failures, timeouts, or fail-fast cancellation.
- **Strict diff application:** Validation uses the system `patch` command with no fuzzy matching.
- **Typed state:** Pydantic schemas now cover repro execution, candidate patch batches, individual validation results, validation swarm results, and final RCA/Git output.
- **Modular execution:** Docker sandbox concerns are isolated from agent prompting and state transition code.

**Potential Errors and Bugs (observed / likely)**
- **Docker daemon availability:** The Docker SDK is installed, but local execution requires Docker Desktop or a reachable Docker daemon. A daemon ping during this session failed because the Windows Docker named pipe was unavailable.
- **Container image assumptions:** The default image is `python:3.11-slim`. Repos that need system packages, project dependencies, or `patch` installed may require a custom image or `setup_command`.
- **Patch path assumptions:** Unified diffs are applied with `patch -p1` by default. Alerts can override `patch_strip` if generated paths require a different strip level.
- **Generated test path safety:** Test files are written into the container workspace. Unsafe absolute or parent-traversal paths are rejected.
- **LLM gating:** If `LIVE_LLM_ENABLED=false`, every model call falls back to local placeholder behavior.
- **Credentials:** Missing provider API keys still allow client construction but live calls will fail.
- **Band stage limitation:** `backend/band_runtime.py` still runs individual model stages through Band messages. Docker repro/validation is part of the local `IncidentOrchestrator` path, not the per-stage Band adapter path.
- **No persistence:** Incident state, handoffs, validation logs, and RCA output are still in-memory only.

**Status of Band Integration**
- **Implemented:** `backend/band_runtime.py` still creates thenvoi `Agent` instances through `Agent.create(...)`.
- **Updated:** Band model-stage order now follows `TRIAGE -> REPRO -> TEST -> FIX -> RCA`.
- **Preserved:** Stage agents still use the standard `IncidentAgent.run(state, llm)` abstraction.
- **Caveat:** The local Docker validation swarm is orchestrator-managed. Running only the Band stage agents does not by itself perform the local Docker validation stage.

**System Architecture**
- **Components:**
  - **Frontend (Next.js):** submits sample alerts and renders event feed, RCA, and patch output.
  - **Backend (FastAPI):** WebSocket entrypoint and orchestrator runtime.
  - **Agent layer:** `IncidentAgent` definitions, prompts, fallbacks, and model routing.
  - **Docker sandbox layer:** local container repro and validation execution.
  - **Providers:** AIML and Featherless-compatible OpenAI clients.
  - **Band/thenvoi:** optional agent runtime adapter.
- **Dataflow:**
  1. Alert arrives over WebSocket or Band message.
  2. Alert Triager creates structured incident context.
  3. Repro Planner creates repro expectations.
  4. Repro Sandbox runs Pass 1 in Docker and truncates logs.
  5. Regression Test Generator writes one strict test from Pass 1 logs.
  6. Patch Generator emits exactly 2 distinct unified diffs.
  7. Validation Swarm runs both candidates concurrently in Docker.
  8. First passing patch wins; the other container is killed and removed.
  9. RCA Publisher emits final RCA/Git JSON.

**Complete Process Workflow**
- **1) Trigger:** Web UI or Band message provides JSON alert payload.
- **2) TRIAGE:** `Alert Triager` extracts `IncidentContext`.
- **3) REPRO:** `Repro Planner` produces `ReproPlan`.
- **4) REPRO SANDBOX:** One Docker container executes the failing state and captures truncated logs.
- **5) TEST:** `Regression Test Generator` produces one regression test.
- **6) PATCH:** `Patch Generator` produces exactly 2 candidate unified diffs.
- **7) VALIDATE:** Two Docker containers race candidate patches against the same regression test.
- **8) FAIL-FAST:** First passing validation wins; the losing container is killed and removed.
- **9) RCA:** `RCA Publisher` creates final RCA/Git JSON.
- **10) Completion:** Final `done` event includes `rca`, `fix`, and `validation` payloads.

**Descriptions of Folders and Notable Functions**
- **`backend/agent_loop.py`:**
  - `truncate_logs(...)` keeps only the last 200 log lines.
  - `DockerSandboxConfig` reads Docker settings from alert payload.
  - `DockerContainerExecutor` starts containers, copies workspace, uploads tests/patches, runs commands, and cleans up.
  - `run_repro_pass1(...)` runs one Docker repro container.
  - `run_validation_swarm(...)` runs the async Best-of-2 validation race.
  - `_run_validation_job(...)` enforces per-container timeout behavior.
  - `_terminate_pending_validations(...)` kills/removes losing containers after a winner.
  - `IncidentOrchestrator.run(...)` coordinates the full local pipeline.
  - `build_agents()` defines model routing, prompts, output schemas, and fallbacks.
- **`backend/schemas.py`:**
  - Adds `ReproExecution`, `CandidatePatches`, `PatchValidationResult`, and `ValidationSwarmResult`.
  - Extends `IncidentState` with repro execution, candidate patches, validation, and winning patch state.
  - Extends `RCAReport` with Git and validation summary fields.
- **`backend/inference.py`:**
  - `InferenceClients.json_call(...)` accepts an optional `model` override per agent call.
- **`backend/band_runtime.py`:**
  - Updates Band stage order to `TRIAGE, REPRO, TEST, FIX, RCA`.
- **`pyproject.toml`:**
  - Adds `docker>=7.1.0`.

**How to Run (local development)**
1. Create a Python virtualenv:
   - `python -m venv .venv`
   - `.venv\Scripts\activate`
2. Install backend dependencies:
   - `pip install -e .`
3. Start Docker Desktop or otherwise expose a Docker daemon to the Python Docker SDK.
4. Run backend:
   - `uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000`
5. Install and run frontend:
   - `cd frontend`
   - `npm install`
   - `npm run dev`
6. Open `http://localhost:3000`.
7. To run Band agents:
   - Configure thenvoi agent IDs/API keys.
   - Set `THENVOI_WS_URL` and `THENVOI_REST_URL`.
   - Run `python -m backend.band_runtime`.

**Alert Payload Runtime Options**
- `docker_image` / `container_image` / `image`: Docker image to run.
- `repo_path` / `repository_path` / `workspace_path`: host repository path mounted into containers.
- `container_workdir` / `workdir`: container workspace path, default `/workspace`.
- `setup_command`: optional setup command before repro or validation.
- `repro_command` / `failing_command` / `test_command`: command used by Pass 1 repro.
- `validation_command`: optional command overriding generated test run command for Pass 2.
- `patch_strip`: `patch -pN` strip level, default `1`.
- `container_timeout_seconds` / `docker_timeout_seconds`: timeout capped at 60 seconds.
- `docker_network_disabled` / `network_disabled`: disables container networking when true.

**Environment / Required Variables**
- `LIVE_LLM_ENABLED`: true/false to permit live LLM calls.
- `AIML_API_KEY`, `FEATHERLESS_API_KEY`: provider API keys.
- `AIML_BASE_URL`, `FEATHERLESS_BASE_URL`: optional provider base URL overrides.
- `MAX_RUN_USD`, `MAX_AGENT_TOKENS`: spend and token controls.
- `TRIAGE_MODEL`: default `gpt-4o-mini`.
- `REPRO_MODEL`: default `gpt-4o`.
- `REGRESSION_TEST_MODEL`: default `gpt-4o`.
- `PATCH_GENERATOR_MODEL`: default `Qwen/Qwen2.5-Coder-32B-Instruct`.
- `RCA_MODEL`: default `gpt-4o`.
- `THENVOI_WS_URL`, `THENVOI_REST_URL`: Band/thenvoi runtime configuration.

**Verification Performed**
- `python -m compileall backend` passed.
- Import/schema smoke test passed for:
  - `truncate_logs(...)`
  - fallback generation of exactly 2 distinct candidate patches
  - Pydantic construction of updated models
- Docker SDK import succeeded with installed version `7.1.0`.
- Docker daemon ping failed in this session because the Windows Docker named pipe was not available.
- `git diff --check` passed.

**Changed Files**
- `backend/agent_loop.py`
- `backend/band_runtime.py`
- `backend/inference.py`
- `backend/schemas.py`
- `pyproject.toml`
- `REPORT.md`

**Next Steps & Recommendations**
- Start Docker Desktop and run a real end-to-end validation using a small sample repo and known patch.
- Add unit tests for `truncate_logs`, candidate schema validation, safe test path handling, and swarm fail-fast behavior.
- Add a mocked Docker client test to verify loser cleanup without requiring a live daemon.
- Add persistent storage for incident state, validation logs, RCA output, and selected winning patches.
- Decide whether winning patches should become human-reviewed PRs or automated commits.
