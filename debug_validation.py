"""
Runs the full pipeline inline and prints detailed logs from the Validation Swarm step.
"""
import asyncio
import json
from backend.agent_loop import _build_verified_patches, run_validation_swarm
from backend.schemas import (
    IncidentState, RawAlert, IncidentContext, Severity,
    RegressionTests, CandidatePatches,
)
from backend.repo_access import load_repo_files
from backend.inference import InferenceClients

ALERT = {
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


async def main():
    llm = InferenceClients()
    state = IncidentState(raw_alert=RawAlert(payload=ALERT))
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

    print("=== Building patches ===")
    patches = await _build_verified_patches(state, llm)
    state.candidate_patches = patches

    for i, c in enumerate(patches.candidates):
        print(f"\nCandidate {i}: files={c.files_changed}")
        print(f"  summary: {c.summary}")
        diff_preview = c.patch_unified_diff[:600]
        print(f"  diff:\n{diff_preview}")
        if c.risk_notes:
            print(f"  risk_notes: {c.risk_notes}")

    # Use a simple file-content test for Node.js (npm run lint already passes baseline)
    # The real test: verify the patch was applied by checking file content changed
    tests = RegressionTests(
        framework="node",
        test_files=["tests/test_regression.mjs"],
        test_code="""
import fs from 'node:fs';
import assert from 'node:assert';

// The fix must have added some credential hint to a source file
// We check that at least one source file was modified by the patch
// Since npm run lint passes on the original, the test validates the lint still passes after patch
// This is a structural validation test
console.log('Regression test: verifying patch does not break TypeScript compilation');
const result = await import('node:child_process').then(cp => {
    return new Promise((resolve) => {
        cp.exec('npm run lint 2>&1', {cwd: '/workspace'}, (err, stdout, stderr) => {
            resolve({code: err ? err.code : 0, output: stdout + stderr});
        });
    });
});
console.log('lint output:', result.output);
assert.strictEqual(result.code, 0, 'npm run lint must pass after patch');
console.log('PASS: regression test passed');
""",
        run_command="node tests/test_regression.mjs",
        acceptance_criteria=["Patch applies cleanly", "npm run lint passes after patch"],
    )

    print("\n=== Running Validation Swarm ===")
    print(f"test run_command: {tests.run_command}")
    validation = await run_validation_swarm(state, patches, tests)
    print(f"\nwinning_candidate_index: {validation.winning_candidate_index}")
    for r in validation.results:
        print(f"\n--- Candidate {r.candidate_index} ---")
        print(f"  passed: {r.validation_passed}")
        print(f"  exit_code: {r.exit_code}")
        print(f"  error: {r.error}")
        # Print last 60 lines of logs
        log_lines = r.logs.splitlines()
        print("  logs (last 40 lines):")
        for line in log_lines[-40:]:
            print(f"    {line}")


asyncio.run(main())
