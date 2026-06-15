# Band SDK runtime vs FastAPI orchestrator

This repo ships two execution paths:

| Capability | FastAPI orchestrator (`backend/agent_loop.py`) | Band SDK runtime (`backend/band_runtime.py`) |
|------------|-----------------------------------------------|-----------------------------------------------|
| Docker repro | Yes | No |
| Docker validation swarm | Yes | No |
| Regression test in container | Yes | No |
| Git branch + PR push | Yes | No |
| Multi-agent Band thread handoffs | Partial (orchestrator simulates sandbox/swarm) | Yes |

Use the **orchestrator** for the full repo-link → fix → PR workflow.

Use the **Band runtime** only when you need native Band agent threads; it runs triage → repro plan → test → fix → RCA **without** Docker validation or automatic PR output. Treat fixes from Band mode as **unvalidated suggestions** until run through the orchestrator path.

```bash
python -m backend.band_runtime
```
