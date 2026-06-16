import asyncio
import json
import traceback
from backend.inference import InferenceClients, GuardrailBlocked, estimate_tokens
from backend.agent_loop import FileRewriteCandidates, _build_verified_patches
from backend.agent_context import select_relevant_files, _extract_keywords
from backend.schemas import Provider, IncidentState, RawAlert, IncidentContext, Severity
from backend.repo_access import load_repo_files


async def main():
    llm = InferenceClients()

    alert = {
        "repo_url": "https://github.com/sufyann004/Follant",
        "error": "Login screen doesnt show id password as test credentials as mentioned in the readme",
        "impact": "People cant login and test",
        "source": "web-ui",
        "repo_full_name": "sufyann004/Follant",
        "service_short": "Follant",
        "service": "sufyann004/Follant",
        "severity": "sev2",
        "environment": "unknown",
        "auto_pr": True,
        "repo_path": "C:/Users/sufya/.band-repos/sufyann004__Follant",
        "docker_image": "node:20-bookworm-slim",
        "setup_command": "apt-get update -qq && apt-get install -y -qq patch && (npm ci 2>/dev/null || npm install --no-audit --no-fund)",
        "repro_command": "npm run lint",
        "validation_command": "npm run lint",
        "container_timeout_seconds": 300,
        "repo_stack": "node",
    }

    state = IncidentState(raw_alert=RawAlert(payload=alert))
    state.repo_path = "C:/Users/sufya/.band-repos/sufyann004__Follant"
    state.repo_full_name = "sufyann004/Follant"
    state.repo_files = load_repo_files("C:/Users/sufya/.band-repos/sufyann004__Follant")
    state.context = IncidentContext(
        service="Follant",
        environment="unknown",
        error_signature="LoginCredentialsNotDisplayed",
        severity=Severity.SEV2,
        impact="Users cannot test app",
    )

    keywords = _extract_keywords(state)
    relevant = select_relevant_files(state.repo_files, keywords, max_chars=14_000)
    file_manifest = "\n".join(f"  - {p}" for p in sorted(state.repo_files.keys()))

    system = (
        "You are an expert software engineer. You must fix a production incident by rewriting "
        "one or more files in the repository. "
        "Your ONLY output is a valid JSON object matching the FileRewriteCandidates schema. "
        "CRITICAL RULES:\n"
        "1. candidates: provide EXACTLY 2 entries.\n"
        "2. file_path: MUST be an exact path from the repository file manifest.\n"
        f"Repository file manifest:\n{file_manifest}"
    )

    user_payload = {
        "incident": {"error": alert["error"], "impact": alert["impact"]},
        "repo_files": relevant,
    }
    user_str = json.dumps(user_payload, separators=(",", ":"))

    sys_tokens = estimate_tokens(system)
    usr_tokens = estimate_tokens(user_str)
    schema_tokens = estimate_tokens(json.dumps(FileRewriteCandidates.model_json_schema()))
    total = sys_tokens + usr_tokens + schema_tokens
    print(f"system_tokens={sys_tokens} user_tokens={usr_tokens} schema_tokens={schema_tokens}")
    print(f"TOTAL_PROMPT_TOKENS={total} (max={llm.settings.max_prompt_tokens})")
    print(f"relevant_files selected: {list(relevant.keys())}")

    if total > llm.settings.max_prompt_tokens:
        print(">>> BLOCKED BY GUARDRAIL <<<")
    else:
        print(">>> WITHIN LIMIT, calling LLM <<<")
        try:
            result = await llm.json_call(
                provider=Provider.FEATHERLESS,
                system=system,
                user=user_str,
                output_model=FileRewriteCandidates,
            )
            print("LLM SUCCESS:", [(c.file_path, c.summary) for c in result.candidates])
        except GuardrailBlocked as e:
            print("GUARDRAIL:", e)
        except Exception as e:
            print("LLM ERROR:", type(e).__name__, e)
            traceback.print_exc()


asyncio.run(main())
