from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def runs_dir() -> Path:
    return Path(os.getenv("RUNS_DIR", Path.home() / ".band-runs")).expanduser()


def persistence_enabled() -> bool:
    return os.getenv("RUNS_PERSIST", "true").lower() in {"1", "true", "yes", "on"}


def append_run_event(run_id: str, event: dict[str, Any]) -> None:
    if not persistence_enabled():
        return
    root = runs_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{run_id}.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, separators=(",", ":")) + "\n")
