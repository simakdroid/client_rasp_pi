from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

LOGGER = logging.getLogger(__name__)

TaskFactory = Callable[[], Awaitable[None]]


class RuntimeStatus:
    """Process liveness vs readiness of background ingestion/broadcast tasks."""

    def __init__(self) -> None:
        self.tasks: dict[str, str] = {}
        self.source: dict[str, Any] = {"status": "starting"}
        self.last_batch_at: datetime | None = None
        self._exiting = False

    def note_batch(self) -> None:
        self.last_batch_at = datetime.now(UTC)

    def set_source(self, **fields: Any) -> None:
        self.source = {**self.source, **fields}

    def snapshot(self, *, adsb: dict[str, Any], source_mode: str) -> dict[str, Any]:
        critical = ("adsb-ingest", "ws-broadcast", "maintenance")
        tasks_ok = all(self.tasks.get(name) == "running" for name in critical)
        source_ok = self._source_ready(adsb, source_mode)
        ready = tasks_ok and source_ok
        return {
            "status": "ok" if ready else "degraded",
            "live": True,
            "ready": ready,
            "time": datetime.now(UTC).isoformat(),
            "tasks": dict(self.tasks),
            "source": dict(self.source),
            "last_batch_at": self.last_batch_at.isoformat() if self.last_batch_at else None,
            "adsb": adsb,
        }

    def _source_ready(self, adsb: dict[str, Any], source_mode: str) -> bool:
        if source_mode == "sbs":
            return self.source.get("status") == "live"
        return adsb.get("status") in {"online", "stale"}

    def request_restart(self, name: str) -> None:
        if self._exiting:
            return
        self._exiting = True
        LOGGER.critical(
            "Critical background task %s failed; exiting so systemd can restart", name
        )
        os._exit(1)


async def run_supervised(
    name: str,
    factory: TaskFactory,
    status: RuntimeStatus,
    *,
    fatal: bool = True,
) -> None:
    status.tasks[name] = "running"
    try:
        await factory()
        status.tasks[name] = "stopped"
        if fatal:
            status.request_restart(name)
    except asyncio.CancelledError:
        status.tasks[name] = "cancelled"
        raise
    except Exception:
        LOGGER.exception("Background task %s crashed", name)
        status.tasks[name] = "failed"
        if fatal:
            status.request_restart(name)
