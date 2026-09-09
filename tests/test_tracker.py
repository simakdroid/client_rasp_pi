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
    cleared = await tracker.clear_events()
    assert cleared["ok"] is True
    assert cleared["last_id"] == journal["last_id"]
    assert (await tracker.recent_events())["events"] == []
    await tracker.apply(
        [
            AircraftUpdate(
                icao="abc123",
                lat=55.3,
                lon=37.3,
                received_at=timestamp + timedelta(seconds=60),
            )
        ]
    )
    after_clear = await tracker.recent_events()
    assert len(after_clear["events"]) == 1
    assert after_clear["events"][0]["id"] > journal["last_id"]
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
async def test_tracker_does_not_mix_adsb_into_acas_reply(tmp_path) -> None:
    layers = LayerManager(tmp_path)
    layers.refresh()
    tracker = AircraftTracker(55.0, 37.0, layers, 60, 10, 1)
    await tracker.apply(
        [
            AircraftUpdate(
                icao="8b7a9a",
                lat=55.0,
                lon=37.0,
                altitude_ft=21575,
                squawk="1200",
                received_at=datetime.now(UTC),
            )
        ]
    )
    enriched = await tracker.attach_mode_s_context(
        [
            {
                "icao": "8b7a9a",
                "df": 16,
                "df_label": "ACAS long",
                "altitude_ft": None,
                "squawk": None,
            }
        ]
    )
    assert enriched[0]["known"] is True
    assert "altitude_ft" not in enriched[0] or enriched[0]["altitude_ft"] is None
    assert "distance_km" not in enriched[0]
    assert "squawk" not in enriched[0] or not enriched[0]["squawk"]


@pytest.mark.asyncio
async def test_tracker_archives_expired_aircraft(tmp_path) -> None:
    layers = LayerManager(tmp_path)
    layers.refresh()
    tracker = AircraftTracker(55.0, 37.0, layers, 1, 10, 1, max_archive=2)
    first_seen = datetime.now(UTC) - timedelta(minutes=10)

    await tracker.apply(
        [
            AircraftUpdate(
                icao="abc123",
                lat=55.1,
                lon=37.1,
                squawk="7700",
                callsign="TEST42",
                received_at=first_seen,
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
    first_started = archived[0]["started_at"]
    first_lost = archived[0]["lost_at"]

    delta = await tracker.consume_delta()
    assert delta is not None
    assert delta["remove"] == ["abc123"]
    assert delta["archive"][0]["icao"] == "abc123"

    second_seen = datetime.now(UTC) - timedelta(seconds=5)
    await tracker.apply(
        [AircraftUpdate(icao="abc123", lat=55.2, lon=37.2, received_at=second_seen)]
    )
    live = await tracker.snapshot()
    assert live[0]["status"] == "live"
    assert live[0]["squawk"] == "7700"
    assert live[0]["callsign"] == "TEST42"
    assert live[0]["started_at"] != first_started
    assert live[0]["lost_at"] is None
    assert live[0]["track"][-1][0] == 55.2
    assert all(point[0] != 55.1 for point in live[0]["track"])
    assert await tracker.archived_snapshot() == []

    await tracker.prune()
    second_archive = await tracker.archived_snapshot()
    assert second_archive[0]["started_at"] != first_started
    assert second_archive[0]["lost_at"] != first_lost
    assert second_archive[0]["started_at"] == live[0]["started_at"]


@pytest.mark.asyncio
async def test_tracker_keeps_archive_when_squawk_or_callsign_changes(tmp_path) -> None:
    layers = LayerManager(tmp_path)
    layers.refresh()
    tracker = AircraftTracker(55.0, 37.0, layers, 1, 10, 1, max_archive=3)
    first_seen = datetime.now(UTC) - timedelta(minutes=10)
    await tracker.apply(
        [
            AircraftUpdate(
                icao="abc123",
                lat=55.1,
                lon=37.1,
                squawk="7700",
                callsign="TEST42",
                received_at=first_seen,
            )
        ]
    )
    await tracker.prune()
    first_archive = (await tracker.archived_snapshot())[0]
    await tracker.consume_delta()

    second_seen = datetime.now(UTC) - timedelta(seconds=5)
    await tracker.apply(
        [
            AircraftUpdate(
                icao="abc123",
                lat=55.4,
                lon=37.4,
                squawk="1200",
                callsign="OTHER1",
                received_at=second_seen,
            )
        ]
    )
    live = await tracker.snapshot()
    still_archived = await tracker.archived_snapshot()
    assert live[0]["squawk"] == "1200"
    assert live[0]["callsign"] == "OTHER1"
    assert live[0]["started_at"] != first_archive["started_at"]
    assert len(still_archived) == 1
    assert still_archived[0]["squawk"] == "7700"
    assert still_archived[0]["callsign"] == "TEST42"
    assert still_archived[0]["contact_id"] != live[0]["contact_id"]

    await tracker.prune()
    archived = await tracker.archived_snapshot()
    assert len(archived) == 2
    squawks = {item["squawk"] for item in archived}
    callsigns = {item["callsign"] for item in archived}
    assert squawks == {"7700", "1200"}
    assert callsigns == {"TEST42", "OTHER1"}


@pytest.mark.asyncio
async def test_tracker_starts_new_contact_when_identity_unknown(tmp_path) -> None:
    layers = LayerManager(tmp_path)
    layers.refresh()
    tracker = AircraftTracker(55.0, 37.0, layers, 1, 10, 1, max_archive=3)
    first_seen = datetime.now(UTC) - timedelta(minutes=10)
    await tracker.apply(
        [AircraftUpdate(icao="abc123", lat=55.1, lon=37.1, received_at=first_seen)]
    )
    await tracker.prune()
    first_archive = (await tracker.archived_snapshot())[0]
    await tracker.consume_delta()

    second_seen = datetime.now(UTC) - timedelta(seconds=5)
    await tracker.apply(
        [AircraftUpdate(icao="abc123", lat=55.4, lon=37.4, received_at=second_seen)]
    )
    live = await tracker.snapshot()
    still_archived = await tracker.archived_snapshot()
    assert live[0]["squawk"] is None
    assert live[0]["callsign"] is None
    assert live[0]["started_at"] != first_archive["started_at"]
    assert len(still_archived) == 1
    assert still_archived[0]["contact_id"] != live[0]["contact_id"]
    assert still_archived[0]["started_at"] == first_archive["started_at"]


@pytest.mark.asyncio
async def test_tracker_starts_new_contact_when_only_one_identity_field_known(
    tmp_path,
) -> None:
    layers = LayerManager(tmp_path)
    layers.refresh()
    tracker = AircraftTracker(55.0, 37.0, layers, 1, 10, 1, max_archive=3)
    first_seen = datetime.now(UTC) - timedelta(minutes=10)
    await tracker.apply(
        [
            AircraftUpdate(
                icao="abc123",
                lat=55.1,
                lon=37.1,
                squawk="7700",
                received_at=first_seen,
            )
        ]
    )
    await tracker.prune()
    await tracker.consume_delta()

    second_seen = datetime.now(UTC) - timedelta(seconds=5)
    await tracker.apply(
        [
            AircraftUpdate(
                icao="abc123",
                lat=55.4,
                lon=37.4,
                squawk="7700",
                received_at=second_seen,
            )
        ]
    )
    live = await tracker.snapshot()
    still_archived = await tracker.archived_snapshot()
    assert live[0]["squawk"] == "7700"
    assert live[0]["callsign"] is None
    assert len(still_archived) == 1
    assert still_archived[0]["squawk"] == "7700"
    assert still_archived[0]["contact_id"] != live[0]["contact_id"]


