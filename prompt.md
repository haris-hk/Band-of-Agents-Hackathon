You are an expert Python/FastAPI engineer. I am building a multi-agent production incident response system called Band-of-Agents. Here is everything you need to know:

## Project Structure
- `backend/main.py` — FastAPI app with WebSocket `/ws/incidents` and REST POST `/incidents`
- `backend/agent_loop.py` — IncidentOrchestrator, DockerContainerExecutor, build_agents()
- `backend/schemas.py` — Pydantic models: IncidentContext, ReproPlan, RegressionTests, CandidatePatches, ValidationSwarmResult, RCAReport, IncidentState
- `backend/inference.py` — InferenceClients with json_call()
- `backend/band_runtime.py` — Band/thenvoi agent adapter
- `services/checkout/handler.py` — mock buggy microservice for demo
- `demo_alert.json` — sample alert payload

## What I need you to implement

### 1. GitHub Webhook Endpoint
Add `POST /webhooks/github` to `backend/main.py`:
- Verify the request signature using `GITHUB_WEBHOOK_SECRET` env var with HMAC SHA256 against `X-Hub-Signature-256` header
- Handle two GitHub event types from the `X-GitHub-Event` header:
  - `check_run` where `payload["check_run"]["conclusion"] == "failure"` → construct alert with service, environment="ci", severity="high", error_message from check run output summary, repo_url, repo_path, commit_sha
  - `issues` where `payload["action"] == "opened"` and "bug" is in the issue labels → construct alert with service, environment="production", severity="medium", error_message from issue title, error_details from issue body, repo_url, repo_path
- If an alert is constructed, fire `asyncio.create_task(orchestrator.run(alert))` and return `{"status": "pipeline started"}`
- Otherwise return `{"status": "ignored"}`

### 2. GitHub PR Push Module
Create a new file `backend/git_output.py`:
- Install dependency: `PyGithub>=2.1.0` (add to pyproject.toml)
- Async function `push_fix_as_pr(state: IncidentState, repo_path: str) -> str` that:
  - Uses `GITHUB_TOKEN` env var to authenticate with PyGithub
  - Gets the repo using `state.incident.service` as the repo name (format: "org/repo")
  - Reads `state.rca.git_branch`, `state.rca.commit_message`, `state.rca.patch_unified_diff`
  - Runs these shell commands in sequence using `subprocess.run(..., check=True)`:
    - `git checkout -b <branch_name>`
    - writes the diff to a temp file then `patch -p1 --input <tmpfile>`
    - `git add -A`
    - `git commit -m "<commit_message>"`
    - `git push origin <branch_name>`
  - Opens a GitHub PR using `gh_repo.create_pull()` with title=commit_message, body=rca_summary + validation_summary, head=branch_name, base="main"
  - Returns the PR URL as a string
  - Wraps subprocess calls in try/except and yields meaningful error messages
- Call `push_fix_as_pr` at the end of `IncidentOrchestrator.run()` after RCA completes, include `pr_url` in the final `done` AgentEvent payload


