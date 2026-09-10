from datetime import UTC, datetime, timedelta

import pytest

from app.gis import LayerManager
from app.models import AircraftUpdate
from app.tracker import AircraftTracker, _page_log


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
                on_ground=False,
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
    empty_older = await tracker.recent_events(
        before_id=journal["events"][0]["id"], limit=1, newest_first=True
    )
    assert empty_older["events"] == []
    assert empty_older["has_more"] is False
    assert journal["generation"]
    cleared = await tracker.clear_events()
    assert cleared["ok"] is True
    assert cleared["last_id"] == journal["last_id"]
    assert cleared["generation"] != journal["generation"]
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


def test_page_log_latest_and_sequential_do_not_skip_ids() -> None:
    records = [{"id": index, "text": str(index)} for index in range(1, 11)]
    latest = _page_log(
        records,
        after_id=0,
        before_id=0,
        limit=3,
        last_id=10,
        items_key="events",
        newest_first=False,
        generation="g1",
    )
    assert [item["id"] for item in latest["events"]] == [8, 9, 10]
    assert latest["next_after_id"] == 10
    assert latest["has_more"] is True
    assert latest["mode"] == "latest"
    sequential = _page_log(
        records,
        after_id=3,
        before_id=0,
        limit=3,
        last_id=10,
        items_key="events",
        newest_first=False,
        generation="g1",
    )
    assert [item["id"] for item in sequential["events"]] == [4, 5, 6]
    assert sequential["next_after_id"] == 6
    assert sequential["has_more"] is True
    assert sequential["mode"] == "since"
    empty = _page_log(
        records,
        after_id=0,
        before_id=1,
        limit=1,
        last_id=10,
        items_key="events",
        newest_first=True,
        generation="g1",
    )
    assert empty["events"] == []
    assert empty["has_more"] is False


@pytest.mark.asyncio
async def test_long_gap_starts_new_contact_even_with_same_identity(tmp_path) -> None:
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
    later = datetime.now(UTC) + timedelta(seconds=10)
    await tracker.apply(
        [
            AircraftUpdate(
                icao="abc123",
                lat=55.4,
                lon=37.4,
                squawk="7700",
                callsign="TEST42",
                received_at=later,
            )
        ]
    )
    live = await tracker.snapshot()
    still_archived = await tracker.archived_snapshot()
    assert live[0]["contact_id"] != first_archive["contact_id"]
    assert live[0]["started_at"] != first_archive["started_at"]
    assert len(still_archived) == 1
    assert still_archived[0]["contact_id"] == first_archive["contact_id"]


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


@pytest.mark.asyncio
async def test_tracker_ignores_stale_position_after_newer_update(tmp_path) -> None:
    layers = LayerManager(tmp_path)
    layers.refresh()
    tracker = AircraftTracker(55.0, 37.0, layers, 60, 10, 1)
    newer = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    older = newer - timedelta(seconds=20)
    await tracker.apply(
        [
            AircraftUpdate(
                icao="abc123",
                lat=55.4,
                lon=37.4,
                altitude_ft=12000,
                speed_kt=400,
                callsign="NEW1",
                on_ground=False,
                received_at=newer,
                position_at=newer,
            )
        ]
    )
    await tracker.apply(
        [
            AircraftUpdate(
                icao="abc123",
                lat=55.1,
                lon=37.1,
                altitude_ft=1000,
                speed_kt=80,
                callsign="OLD1",
                received_at=older,
                position_at=older,
            )
        ]
    )
    live = (await tracker.snapshot())[0]
    assert live["lat"] == 55.4
    assert live["lon"] == 37.4
    assert live["altitude_ft"] == 12000
    assert live["speed_kt"] == 400
    assert live["callsign"] == "NEW1"
    assert live["track"][-1][0] == 55.4


@pytest.mark.asyncio
async def test_tracker_drops_new_aircraft_over_active_limit(tmp_path) -> None:
    layers = LayerManager(tmp_path)
    layers.refresh()
    tracker = AircraftTracker(55.0, 37.0, layers, 60, 10, 1, max_active_aircraft=1)
    now = datetime.now(UTC)
    await tracker.apply(
        [AircraftUpdate(icao="abc123", lat=55.1, lon=37.1, received_at=now)]
    )
    await tracker.apply(
        [AircraftUpdate(icao="def456", lat=55.2, lon=37.2, received_at=now)]
    )
    snapshot = await tracker.snapshot()
    assert len(snapshot) == 1
    assert snapshot[0]["icao"] == "abc123"


@pytest.mark.asyncio
async def test_mark_type_changed_increments_revision(tmp_path) -> None:
    layers = LayerManager(tmp_path)
    layers.refresh()
    tracker = AircraftTracker(55.0, 37.0, layers, 60, 10, 1)
    await tracker.apply(
        [AircraftUpdate(icao="abc123", lat=55.1, lon=37.1, received_at=datetime.now(UTC))]
    )
    before = (await tracker.snapshot())[0]["revision"]
    await tracker.consume_delta()
    await tracker.mark_type_changed("abc123")
    after = (await tracker.snapshot())[0]["revision"]
    assert after == before + 1
    delta = await tracker.consume_delta()
    assert delta is not None
    assert delta["upsert"][0]["icao"] == "abc123"


@pytest.mark.asyncio
async def test_snapshot_and_delta_share_generation_and_seq(tmp_path) -> None:
    layers = LayerManager(tmp_path)
    layers.refresh()
    tracker = AircraftTracker(55.0, 37.0, layers, 60, 10, 1)
    first = await tracker.snapshot_message()
    assert first["type"] == "snapshot"
    assert first["seq"] == 0
    assert first["generation"]
    await tracker.apply(
        [AircraftUpdate(icao="abc123", lat=55.1, lon=37.1, received_at=datetime.now(UTC))]
    )
    delta = await tracker.consume_delta()
    assert delta is not None
    assert delta["generation"] == first["generation"]
    assert delta["seq"] == 1
    second = await tracker.snapshot_message()
    assert second["seq"] == 1
    assert second["generation"] == first["generation"]
    assert second["aircraft"][0]["icao"] == "abc123"


@pytest.mark.asyncio
async def test_unpublished_delta_overflow_requires_resync(tmp_path) -> None:
    layers = LayerManager(tmp_path)
    layers.refresh()
    tracker = AircraftTracker(
        55.0, 37.0, layers, 0, 2, 1, max_archive=1, max_active_aircraft=1
    )
    older = datetime.now(UTC) - timedelta(seconds=1)
    await tracker.apply(
        [AircraftUpdate(icao="abc123", lat=55.1, lon=37.1, received_at=older, position_at=older)]
    )
    await tracker.prune()
    await tracker.apply(
        [AircraftUpdate(icao="def456", lat=55.2, lon=37.2, received_at=older, position_at=older)]
    )
    await tracker.prune()
    await tracker.apply(
        [AircraftUpdate(icao="aaa111", lat=55.3, lon=37.3, received_at=older, position_at=older)]
    )
    await tracker.prune()
    payload = await tracker.consume_delta()
    assert payload is not None
    assert payload["type"] == "resync"
    assert payload["generation"]
    assert await tracker.consume_delta() is None


@pytest.mark.asyncio
async def test_track_append_can_overlap_snapshot_points(tmp_path) -> None:
    layers = LayerManager(tmp_path)
    layers.refresh()
    tracker = AircraftTracker(55.0, 37.0, layers, 60, 10, 1)
    first = datetime.now(UTC)
    second = first + timedelta(seconds=5)
    await tracker.apply(
        [AircraftUpdate(icao="abc123", lat=55.1, lon=37.1, received_at=first, position_at=first)]
    )
    await tracker.apply(
        [
            AircraftUpdate(
                icao="abc123",
                lat=55.2,
                lon=37.2,
                received_at=second,
                position_at=second,
            )
        ]
    )
    snapshot = await tracker.snapshot_message()
    delta = await tracker.consume_delta()
    assert delta is not None
    snap_times = {point[3] for item in snapshot["aircraft"] for point in item["track"]}
    append_times = {
        point[3]
        for item in delta["upsert"]
        for point in item.get("track_append", [])
    }
    assert snap_times & append_times


@pytest.mark.asyncio
async def test_geofence_enter_is_immediate_and_leave_uses_hysteresis(tmp_path) -> None:
    import json

    (tmp_path / "ctr.geojson").write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"name": "CTR", "min_alt_ft": 0, "max_alt_ft": 5000},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [[[36, 54], [38, 54], [38, 56], [36, 56], [36, 54]]],
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    layers = LayerManager(tmp_path)
    layers.refresh()
    tracker = AircraftTracker(55.0, 37.0, layers, 60, 10, 1)
    now = datetime.now(UTC)
    await tracker.apply(
        [
            AircraftUpdate(
                icao="abc123",
                lat=55.1,
                lon=37.1,
                altitude_ft=2000,
                altitude_reference="baro",
                on_ground=False,
                received_at=now,
                position_at=now,
            )
        ]
    )
    live = (await tracker.snapshot())[0]
    assert live["geofences"] == ["CTR"]
    assert live["geofence_ids"] == ["ctr:CTR"]
    events = (await tracker.recent_events())["events"]
    enters = [item for item in events if item["kind"] == "geofence_enter"]
    assert len(enters) == 1
    assert enters[0]["zone"] == "CTR"
    assert enters[0]["layer_id"] == "ctr"
    assert enters[0]["catalog_version"] == layers.catalog.version
    assert enters[0]["hysteresis_leave_after"] == 2
    assert enters[0]["geofence_key"] == "ctr:CTR"

    await tracker.apply(
        [
            AircraftUpdate(
                icao="abc123",
                lat=55.12,
                lon=37.12,
                altitude_ft=2100,
                altitude_reference="baro",
                on_ground=False,
                received_at=now + timedelta(seconds=5),
                position_at=now + timedelta(seconds=5),
            )
        ]
    )
    still_inside_events = (await tracker.recent_events())["events"]
    assert len([item for item in still_inside_events if item["kind"] == "geofence_enter"]) == 1
    geofence_only = await tracker.recent_events(kinds=("geofence_enter", "geofence_leave"))
    assert {item["kind"] for item in geofence_only["events"]} == {"geofence_enter"}

    await tracker.apply(
        [
            AircraftUpdate(
                icao="abc123",
                lat=60.0,
                lon=40.0,
                altitude_ft=2000,
                received_at=now + timedelta(seconds=10),
                position_at=now + timedelta(seconds=10),
            )
        ]
    )
    still_inside = (await tracker.snapshot())[0]
    assert still_inside["geofences"] == ["CTR"]
    assert not any(
        item["kind"] == "geofence_leave" for item in (await tracker.recent_events())["events"]
    )

    await tracker.apply(
        [
            AircraftUpdate(
                icao="abc123",
                lat=60.1,
                lon=40.1,
                altitude_ft=2000,
                received_at=now + timedelta(seconds=20),
                position_at=now + timedelta(seconds=20),
            )
        ]
    )
    left = (await tracker.snapshot())[0]
    assert left["geofences"] == []
    assert left["geofence_ids"] == []
    leaves = [
        item
        for item in (await tracker.recent_events())["events"]
        if item["kind"] == "geofence_leave"
    ]
    assert len(leaves) == 1
    assert leaves[0]["zone"] == "CTR"
    assert leaves[0]["hysteresis_misses"] == 2
    assert leaves[0]["hysteresis_leave_after"] == 2
    assert "Выход из зоны CTR" in leaves[0]["text"]
    assert "гистерезис 2/2" in leaves[0]["text"]


