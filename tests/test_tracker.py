from datetime import UTC, datetime, timedelta

import pytest

from app.gis import LayerManager
from app.models import AircraftUpdate
from app.tracker import AircraftTracker


@pytest.mark.asyncio
async def test_tracker_builds_track_and_delta(tmp_path) -> None:
    layers = LayerManager(tmp_path)
    layers.refresh()
    tracker = AircraftTracker(55.0, 37.0, layers, 60, 10, 1)
    timestamp = datetime.now(UTC)

    await tracker.apply(
        [AircraftUpdate(icao="abc123", lat=55.1, lon=37.1, received_at=timestamp)]
    )
    await tracker.apply(
        [
            AircraftUpdate(
                icao="abc123",
                lat=55.2,
                lon=37.2,
                altitude_ft=1000,
                received_at=timestamp + timedelta(seconds=30),
            )
        ]
    )

    delta = await tracker.consume_delta()
    assert delta is not None
    assert len(delta["upsert"]) == 1
    aircraft = delta["upsert"][0]
    assert len(aircraft["track_append"]) == 2
    assert "track" not in aircraft
    assert aircraft["calculated_track_deg"] is not None
    assert aircraft["distance_km"] > 0

    journal = await tracker.recent_events()
    assert len(journal["events"]) == 2
    assert journal["events"][0]["kind"] == "detected"
    assert journal["events"][1]["kind"] == "position"
    assert "ABC123" in journal["events"][1]["text"]
    newest = await tracker.recent_events(limit=1, newest_first=True)
    assert newest["events"][0]["kind"] == "position"
    assert newest["total"] == 2
    assert newest["has_more"] is True
    older = await tracker.recent_events(
        before_id=newest["events"][0]["id"], limit=1, newest_first=True
    )
    assert older["events"][0]["kind"] == "detected"
    assert older["has_more"] is False


@pytest.mark.asyncio
async def test_tracker_archives_expired_aircraft(tmp_path) -> None:
    layers = LayerManager(tmp_path)
    layers.refresh()
    tracker = AircraftTracker(55.0, 37.0, layers, 1, 10, 1, max_archive=2)
    stale = datetime.now(UTC) - timedelta(seconds=5)

    await tracker.apply(
        [
            AircraftUpdate(
                icao="abc123",
                lat=55.1,
                lon=37.1,
                squawk="7700",
                callsign="TEST42",
                received_at=stale,
            )
        ]
    )
    await tracker.prune()

    assert await tracker.snapshot() == []
    archived = await tracker.archived_snapshot()
    assert len(archived) == 1
    assert archived[0]["icao"] == "abc123"
    assert archived[0]["status"] == "archived"
    assert archived[0]["squawk"] == "7700"
    assert archived[0]["lost_at"] is not None

    delta = await tracker.consume_delta()
    assert delta is not None
    assert delta["remove"] == ["abc123"]
    assert delta["archive"][0]["icao"] == "abc123"

    await tracker.apply(
        [AircraftUpdate(icao="abc123", lat=55.2, lon=37.2, received_at=datetime.now(UTC))]
    )
    live = await tracker.snapshot()
    assert live[0]["status"] == "live"
    assert live[0]["squawk"] == "7700"
    assert await tracker.archived_snapshot() == []
