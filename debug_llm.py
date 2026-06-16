import asyncio
import traceback
from backend.inference import InferenceClients, GuardrailBlocked
from backend.agent_loop import FileRewriteCandidates
from backend.schemas import Provider


async def main():
    llm = InferenceClients()
    print("live_llm:", llm.settings.live_llm_enabled)
    print("max_prompt_tokens:", llm.settings.max_prompt_tokens)
    print("max_run_tokens:", llm.settings.max_run_tokens)
    print("max_run_usd:", llm.settings.max_run_usd)
    print("featherless_model:", llm.settings.featherless_model)

    test_system = (
        "You return JSON only. "
        'Output exactly: {"candidates":[{"file_path":"README.md","new_content":"test","summary":"test","rollback_plan":"revert"},'
        '{"file_path":"README.md","new_content":"test2","summary":"test2","rollback_plan":"revert2"}]}'
    )
    try:
        result = await llm.json_call(
            provider=Provider.FEATHERLESS,
            system=test_system,
            user="test",
            output_model=FileRewriteCandidates,
        )
        print("SUCCESS:", result)
    except GuardrailBlocked as e:
        print("GUARDRAIL BLOCKED:", e)
    except Exception as e:
        print("ERROR:", type(e).__name__, e)
        traceback.print_exc()


asyncio.run(main())
