from __future__ import annotations

import os

from backend.schemas import Stage

AGENT_CONFIG_KEYS: dict[Stage, str] = {
    Stage.TRIAGE: "incident_triager",
    Stage.REPRO: "incident_reproducer",
    Stage.FIX: "incident_fixer",
    Stage.TEST: "incident_test_generator",
    Stage.RCA: "incident_rca_writer",
}

AGENT_SLUGS: dict[Stage, str] = {
    Stage.TRIAGE: "incident-triager",
    Stage.REPRO: "incident-reproducer",
    Stage.TEST: "incident-test-generator",
    Stage.FIX: "incident-patch-generator",
    Stage.RCA: "incident-rca-writer",
}

AGENT_DISPLAY_NAMES: dict[Stage, str] = {
    Stage.TRIAGE: "Alert Triager",
    Stage.REPRO: "Repro Planner",
    Stage.TEST: "Regression Test Generator",
    Stage.FIX: "Patch Generator",
    Stage.RCA: "RCA Publisher",
}

def agent_mention(stage: Stage) -> str:
    username = os.getenv("BAND_USERNAME", "").strip().lstrip("@")
    slug = AGENT_SLUGS[stage]
    if not username:
        return f"@{slug}"
    return f"@{username}/{slug}"
