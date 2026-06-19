from __future__ import annotations

import os

from backend.schemas import Stage

AGENT_CONFIG_KEYS: dict[Stage, str] = {
    Stage.TRIAGE: "alert_triager",
    Stage.REPRO: "incident_reproducer",
    Stage.FIX: "patch_generator",
    Stage.VALIDATE: "qa_validator",
    Stage.TEST: "regression_test_generator",
    Stage.RCA: "rca_publisher",
}

AGENT_SLUGS: dict[Stage, str] = {
    Stage.TRIAGE: "alert-triager",
    Stage.REPRO: "incident-reproducer",
    Stage.FIX: "patch-generator",
    Stage.VALIDATE: "qa-validator",
    Stage.TEST: "regression-test-generato",
    Stage.RCA: "rca-publisher",
}

AGENT_DISPLAY_NAMES: dict[Stage, str] = {
    Stage.TRIAGE: "Alert Triager",
    Stage.REPRO: "Incident Reproducer",
    Stage.FIX: "Patch Generator",
    Stage.VALIDATE: "QA Validator",
    Stage.TEST: "Regression Test Generator",
    Stage.RCA: "RCA Publisher",
}

def agent_mention(stage: Stage) -> str:
    username = os.getenv("BAND_USERNAME", "").strip().lstrip("@")
    slug = AGENT_SLUGS[stage]
    if not username:
        return f"@{slug}"
    return f"@{username}/{slug}"
