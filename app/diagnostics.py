from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def read_adsb_status(path: Path) -> dict[str, Any]:
    """Return readsb JSON health without treating zero aircraft as a failure."""
    if not path.is_file():
        return {
            "status": "unavailable",
            "detail": f"{path} does not exist",
            "aircraft": 0,
        }

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "status": "invalid",
            "detail": str(exc),
            "aircraft": 0,
        }

    generated_at = _generated_at(payload, path)
    age_s = max(0.0, (datetime.now(UTC) - generated_at).total_seconds())
    aircraft = payload.get("aircraft")
    aircraft_count = len(aircraft) if isinstance(aircraft, list) else 0
    return {
        "status": "online" if age_s <= 5 else "stale",
        "json_age_s": round(age_s, 1),
        "messages": payload.get("messages"),
        "aircraft": aircraft_count,
    }


def _generated_at(payload: dict[str, Any], path: Path) -> datetime:
    try:
        return datetime.fromtimestamp(float(payload["now"]), UTC)
    except (KeyError, TypeError, ValueError, OSError, OverflowError):
        return datetime.fromtimestamp(path.stat().st_mtime, UTC)
