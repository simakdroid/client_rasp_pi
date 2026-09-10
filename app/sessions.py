from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from .models import AircraftUpdate

MAX_SESSION_EVENTS = 20_000


class SessionReplayInput(BaseModel):
    events: list[dict[str, Any]] = Field(default_factory=list)


class SessionRecorder:
    """Opt-in capture of ADS-B batches for download and replay. Not a 7-day archive."""

    def __init__(self, max_events: int = MAX_SESSION_EVENTS) -> None:
        self.max_events = max_events
        self._recording = False
        self._started_at: datetime | None = None
        self._stopped_at: datetime | None = None
        self._events: deque[dict[str, Any]] = deque()
        self._batches = 0
        self._updates = 0

    def status(self) -> dict[str, Any]:
        return {
            "recording": self._recording,
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "stopped_at": self._stopped_at.isoformat() if self._stopped_at else None,
            "events": len(self._events),
            "batches": self._batches,
            "updates": self._updates,
        }

    def start(self) -> dict[str, Any]:
        self._recording = True
        self._started_at = datetime.now(UTC)
        self._stopped_at = None
        self._events.clear()
        self._batches = 0
        self._updates = 0
        return self.status()

    def stop(self) -> dict[str, Any]:
        self._recording = False
        self._stopped_at = datetime.now(UTC)
        return self.status()

    def record_batch(self, updates: list[AircraftUpdate]) -> None:
        if not self._recording or not updates:
            return
        payload = {
            "type": "batch",
            "received_at": datetime.now(UTC).isoformat(),
            "updates": [item.model_dump(mode="json") for item in updates],
        }
        self._events.append(payload)
        self._batches += 1
        self._updates += len(updates)
        overflow = len(self._events) - self.max_events
        if overflow > 0:
            dropped = [self._events.popleft() for _ in range(overflow)]
            self._batches = max(0, self._batches - len(dropped))
            self._updates = max(
                0,
                self._updates - sum(len(item.get("updates") or []) for item in dropped),
            )

    def export(self) -> dict[str, Any]:
        return {
            **self.status(),
            "events": list(self._events),
        }

    def load(self, payload: dict[str, Any]) -> int:
        events = payload.get("events")
        if not isinstance(events, list):
            raise ValueError("session payload must contain an events array")
        loaded: deque[dict[str, Any]] = deque()
        for item in events[-self.max_events :]:
            if not isinstance(item, dict) or item.get("type") != "batch":
                continue
            updates = item.get("updates")
            if not isinstance(updates, list):
                continue
            loaded.append(
                {
                    "type": "batch",
                    "received_at": item.get("received_at"),
                    "updates": updates,
                }
            )
        self._recording = False
        self._events = loaded
        self._batches = len(loaded)
        self._updates = sum(len(item["updates"]) for item in loaded)
        self._started_at = None
        self._stopped_at = datetime.now(UTC)
        return self._batches

    async def replay(self, tracker: Any) -> dict[str, Any]:
        applied = 0
        for event in list(self._events):
            raw_updates = event.get("updates") or []
            batch: list[AircraftUpdate] = []
            for item in raw_updates:
                try:
                    batch.append(AircraftUpdate.model_validate(item))
                except (TypeError, ValueError):
                    continue
            if batch:
                await tracker.apply(batch)
                applied += len(batch)
        return {"ok": True, "applied": applied, "batches": len(self._events)}
