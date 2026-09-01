from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.gis import LayerManager
from app.models import AircraftUpdate
from app.sessions import SessionLog
from app.tracker import AircraftTracker


def test_session_log_upserts_and_purges_old_days(tmp_path: Path) -> None:
    log = SessionLog(tmp_path, keep_days=7)
    now = datetime.now(UTC)
    first = {
        "id": "abc123-20260101T000000",
        "icao": "abc123",
        "callsign": "TEST42",
        "started_at": now.isoformat(),
        "lost_at": now.isoformat(),
        "track": [[55.1, 37.1, 1000, now.isoformat()]],
    }
    log.record(first)
    longer = dict(first)
    longer["track"] = [
        [55.1, 37.1, 1000, now.isoformat()],
        [55.2, 37.2, 1200, now.isoformat()],
    ]
    log.record(longer)
    listed = log.list()
    assert len(listed) == 1
    assert listed[0]["track"] == longer["track"]

    old_day = (now.date() - timedelta(days=8)).isoformat()
    stale = tmp_path / f"{old_day}.jsonl"
    stale.write_text(
        '{"id":"old-1","started_at":"2019-01-01T00:00:00+00:00","track":[]}\n',
        encoding="utf-8",
    )
    log.purge()
    assert not stale.exists()
    assert log.list()[0]["id"] == first["id"]


@pytest.mark.asyncio
async def test_tracker_writes_session_when_contact_is_lost(tmp_path: Path) -> None:
    layers = LayerManager(tmp_path)
    layers.refresh()
    log = SessionLog(tmp_path / "sessions", keep_days=7)
    tracker = AircraftTracker(
        55.0, 37.0, layers, 1, 10, 1, session_log=log
    )
    stale = datetime.now(UTC) - timedelta(seconds=5)
    await tracker.apply(
        [
            AircraftUpdate(icao="abc123", lat=55.1, lon=37.1, received_at=stale),
            AircraftUpdate(
                icao="abc123",
                lat=55.2,
                lon=37.2,
                callsign="TEST42",
                received_at=stale + timedelta(seconds=1),
            ),
        ]
    )
    await tracker.prune()
    sessions = log.list()
    assert len(sessions) == 1
    assert sessions[0]["icao"] == "abc123"
    assert sessions[0]["callsign"] == "TEST42"
    assert len(sessions[0]["track"]) >= 2
    assert sessions[0]["lost_at"] is not None
