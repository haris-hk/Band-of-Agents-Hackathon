from __future__ import annotations


def process(payload: dict[str, object]) -> dict[str, object]:
    return {"order_id": payload["order_id"], "status": "accepted"}


def handle(payload: dict[str, object] | None) -> dict[str, object]:
    result = process(payload)  # type: ignore[arg-type]
    return {"ok": True, "result": result}
