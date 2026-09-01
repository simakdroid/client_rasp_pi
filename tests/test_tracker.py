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
                type_code="B738",
                type_desc="Boeing 737-800",
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
    from pyproj import Geod

    azimuth, _, _ = Geod(ellps="WGS84").inv(37.0, 55.0, 37.2, 55.2)
    expected = round(azimuth % 360, 1)
    if expected == 360.0:
        expected = 0.0
    assert aircraft["azimuth_deg"] == expected
    snapshot = await tracker.snapshot()
    assert snapshot[0]["type_code"] == "B738"
    assert snapshot[0]["type_desc"] == "Boeing 737-800"
    assert snapshot[0]["started_at"] is not None

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
    coverage = await tracker.coverage_snapshot()
    assert coverage["samples"] >= 1
    assert coverage["filled_bins"] >= 1
    assert coverage["points"]


@pytest.mark.asyncio
async def test_tracker_attaches_distance_to_mode_s(tmp_path) -> None:
    layers = LayerManager(tmp_path)
    layers.refresh()
    tracker = AircraftTracker(55.0, 37.0, layers, 60, 10, 1)
    await tracker.apply(
        [
            AircraftUpdate(
                icao="40621d",
                lat=55.1,
                lon=37.1,
                callsign="BAW123",
                received_at=datetime.now(UTC),
            )
        ]
    )
    enriched = await tracker.attach_mode_s_context(
        [{"icao": "40621d", "df_label": "ADS-B, позиция в воздухе", "callsign": None}]
    )
    assert enriched[0]["known"] is True
    assert enriched[0]["callsign"] == "BAW123"
    assert enriched[0]["distance_km"] > 0
    assert "км" in enriched[0]["text"]
    snapshot = await tracker.snapshot()
    assert snapshot[0]["azimuth_deg"] is not None
    assert 0 <= snapshot[0]["azimuth_deg"] < 360


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
    assert archived[0]["started_at"] is not None
    started_at = archived[0]["started_at"]

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
    assert live[0]["started_at"] == started_at
    assert live[0]["lost_at"] is None
    assert await tracker.archived_snapshot() == []
