from __future__ import annotations

import threading
from dataclasses import dataclass, field


@dataclass
class RunMetrics:
    incidents_started: int = 0
    incidents_done: int = 0
    incidents_failed: int = 0
    webhook_accepted: int = 0
    webhook_rejected: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def inc(self, name: str, amount: int = 1) -> None:
        with self._lock:
            current = getattr(self, name, 0)
            setattr(self, name, current + amount)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "incidents_started": self.incidents_started,
                "incidents_done": self.incidents_done,
                "incidents_failed": self.incidents_failed,
                "webhook_accepted": self.webhook_accepted,
                "webhook_rejected": self.webhook_rejected,
            }


METRICS = RunMetrics()
