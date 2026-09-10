"""Minimal acceptance scenarios required before calling a build stable."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.adsb import ReadsbJsonSource, SbsSource, parse_readsb_aircraft, parse_sbs_line
from app.aircraft_types import AircraftTypeCatalog
from app.broadcast import BroadcastHub
from app.config import RadioChannel, Settings
from app.diagnostics import collect_diagnostics
from app.gis import LayerManager
from app.main import create_app
from app.models import AircraftUpdate
from app.radio import RadioMonitor
from app.runtime import RuntimeStatus, run_supervised
from app.tracker import AircraftTracker

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")


def _layers(tmp_path: Path) -> LayerManager:
    layers = LayerManager(tmp_path)
    layers.refresh()
    return layers


def _tracker(tmp_path: Path, ttl: int = 60, **kwargs) -> AircraftTracker:
    return AircraftTracker(55.0, 37.0, _layers(tmp_path), ttl, 10, 1, **kwargs)


def _ctr_geojson(tmp_path: Path) -> None:
    (tmp_path / "ctr.geojson").write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"name": "CTR"},
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


def _write_mbtiles(path: Path, tile: bytes) -> None:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE metadata (name text, value text)")
    connection.execute(
        "CREATE TABLE tiles (zoom_level int, tile_column int, tile_row int, tile_data blob)"
    )
    connection.execute("INSERT INTO metadata VALUES ('name', 'tiles')")
    connection.execute("INSERT INTO metadata VALUES ('format', 'pbf')")
    connection.execute("INSERT INTO tiles VALUES (0, 0, 0, ?)", (tile,))
    connection.commit()
    connection.close()


def _merge_track_points(base, appends, limit: int = 300):
    track = list(base or [])
    seen = {
        point[3]
        for point in track
        if isinstance(point, (list, tuple)) and len(point) > 3 and point[3]
    }
    for point in appends or []:
        stamp = point[3] if isinstance(point, (list, tuple)) and len(point) > 3 else None
        if stamp and stamp in seen:
            continue
        if stamp:
            seen.add(stamp)
        track.append(point)
    return track[-limit:]


def _settings(tmp_path: Path, **overrides) -> Settings:
    values = dict(
        layers_dir=tmp_path,
        readsb_json_path=tmp_path / "missing-aircraft.json",
        coverage_path=tmp_path / "coverage-rose.json",
        aircraft_types_path=tmp_path / "aircraft-types.json",
    )
    values.update(overrides)
    return Settings(**values)


# --- Данные -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_older_update_does_not_roll_back_newer_fields(tmp_path) -> None:
    tracker = _tracker(tmp_path)
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
                squawk="1200",
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
                squawk="7700",
                received_at=older,
                position_at=older,
            )
        ]
    )
    live = (await tracker.snapshot())[0]
    assert live["lat"] == 55.4
    assert live["altitude_ft"] == 12000
    assert live["speed_kt"] == 400
    assert live["callsign"] == "NEW1"
    assert live["squawk"] == "1200"


@pytest.mark.asyncio
async def test_fresh_seen_with_old_seen_pos_does_not_refresh_position(tmp_path) -> None:
    now = datetime(2026, 9, 10, 12, tzinfo=UTC)
    parsed = parse_readsb_aircraft(
        {"hex": "abc123", "lat": 55.7, "lon": 37.6, "seen": 0.2, "seen_pos": 12},
        now,
        ttl_s=60,
    )
    assert parsed is not None
    assert parsed.received_at == now - timedelta(seconds=0.2)
    assert parsed.position_at == now - timedelta(seconds=12)

    tracker = _tracker(tmp_path)
    fresh_pos = datetime(2026, 9, 10, 12, 0, 5, tzinfo=UTC)
    older_pos = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)
    newer_msg = datetime(2026, 9, 10, 12, 0, 10, tzinfo=UTC)
    await tracker.apply(
        [
            AircraftUpdate(
                icao="abc123",
                lat=55.5,
                lon=37.5,
                received_at=fresh_pos,
                position_at=fresh_pos,
            )
        ]
    )
    await tracker.apply(
        [
            AircraftUpdate(
                icao="abc123",
                lat=55.1,
                lon=37.1,
                callsign="FRESH",
                received_at=newer_msg,
                position_at=older_pos,
            )
        ]
    )
    live = (await tracker.snapshot())[0]
    assert live["lat"] == 55.5
    assert live["lon"] == 37.5
    assert live["callsign"] == "FRESH"
    assert live["position_at"] == fresh_pos.isoformat()


@pytest.mark.asyncio
async def test_frozen_json_does_not_resurrect_contacts(tmp_path) -> None:
    path = tmp_path / "aircraft.json"
    path.write_text(
        json.dumps(
            {
                "now": datetime(2020, 1, 1, tzinfo=UTC).timestamp(),
                "aircraft": [
                    {"hex": "abc123", "lat": 55.1, "lon": 37.1, "seen": 0.1},
                ],
            }
        ),
        encoding="utf-8",
    )
    source = ReadsbJsonSource(path, interval_s=0.02, ttl_s=60)
    batches: list[list] = []

    async def collect() -> None:
        async for batch in source.updates():
            batches.append(batch)
            return

    await asyncio.wait_for(collect(), timeout=2)
    assert batches[0] == []
    assert source.health()["status"] == "stale"

    tracker = _tracker(tmp_path, ttl=1)
    stale_at = datetime.now(UTC) - timedelta(seconds=5)
    frozen_at = datetime(2020, 1, 1, tzinfo=UTC)
    await tracker.apply(
        [
            AircraftUpdate(
                icao="abc123",
                lat=55.2,
                lon=37.2,
                received_at=stale_at,
                position_at=stale_at,
            )
        ]
    )
    await tracker.prune()
    assert await tracker.snapshot() == []
    await tracker.apply(
        [
            AircraftUpdate(
                icao="abc123",
                lat=55.9,
                lon=37.9,
                received_at=frozen_at,
                position_at=frozen_at,
            )
        ]
    )
    await tracker.prune()
    assert await tracker.snapshot() == []


@pytest.mark.asyncio
async def test_bad_sbs_line_does_not_block_following_good_line() -> None:
    assert parse_sbs_line("not-a-message") is None
    good = (
        "MSG,3,1,1,ABC123,1,2026/08/27,10:00:00.000,"
        "2026/08/27,10:00:00.000,CALL42,12000,350,90,"
        "55.75,37.61,640,7700,0,0,0,0"
    )

    async def handler(reader, writer) -> None:
        writer.write(b"not-a-message\n")
        writer.write(good.encode("ascii") + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    source = SbsSource(host, port)
    try:
        received: list[AircraftUpdate] = []

        async def collect() -> None:
            async for batch in source.updates():
                received.extend(batch)
                if received:
                    return

        await asyncio.wait_for(collect(), timeout=3)
        assert received[0].icao == "abc123"
        assert received[0].lat == 55.75
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_two_positions_in_the_same_second_keep_exact_time(tmp_path) -> None:
    first = parse_sbs_line(
        "MSG,3,1,1,ABC123,1,2026/08/27,10:00:00.000,"
        "2026/08/27,10:00:00.000,CALL42,12000,350,90,"
        "55.75,37.61,640,7700,0,0,0,0"
    )
    second = parse_sbs_line(
        "MSG,3,1,1,ABC123,1,2026/08/27,10:00:00.250,"
        "2026/08/27,10:00:00.250,CALL42,12000,350,90,"
        "55.76,37.62,640,7700,0,0,0,0"
    )
    assert first is not None and second is not None
    assert first.received_at.microsecond == 0
    assert second.received_at.microsecond == 250000

    tracker = _tracker(tmp_path)
    await tracker.apply([first, second])
    track = (await tracker.snapshot())[0]["track"]
    stamps = [point[3] for point in track]
    assert first.received_at.isoformat() in stamps
    assert second.received_at.isoformat() in stamps
    assert stamps[-2:] == [first.received_at.isoformat(), second.received_at.isoformat()]


# --- Синхронизация ----------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_during_delta_does_not_duplicate_track_points(tmp_path) -> None:
    tracker = _tracker(tmp_path)
    first = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
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
    aircraft = snapshot["aircraft"][0]
    appends = next(item.get("track_append", []) for item in delta["upsert"])
    merged = _merge_track_points(aircraft["track"], appends)
    stamps = [point[3] for point in merged]
    assert len(stamps) == len(set(stamps))
    assert "if (stamp && seen.has(stamp)) return;" in FRONTEND


@pytest.mark.asyncio
async def test_resync_does_not_roll_back_state(tmp_path) -> None:
    tracker = AircraftTracker(
        55.0, 37.0, _layers(tmp_path), 0, 2, 1, max_archive=1, max_active_aircraft=1
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
        [
            AircraftUpdate(
                icao="aaa111",
                lat=55.3,
                lon=37.3,
                received_at=older,
                position_at=older,
            )
        ]
    )
    await tracker.prune()
    payload = await tracker.consume_delta()
    assert payload is not None
    assert payload["type"] == "resync"
    now = datetime.now(UTC)
    await tracker.apply(
        [
            AircraftUpdate(
                icao="bbb222",
                lat=55.8,
                lon=37.8,
                received_at=now,
                position_at=now,
            )
        ]
    )
    snapshot = await tracker.snapshot_message()
    assert snapshot["type"] == "snapshot"
    assert snapshot["generation"] == payload["generation"]
    assert snapshot["seq"] >= payload["seq"]
    assert snapshot["aircraft"][0]["icao"] == "bbb222"
    assert snapshot["aircraft"][0]["lat"] == 55.8
    assert "seq < state.syncSeq" in FRONTEND


@pytest.mark.asyncio
async def test_new_contact_id_does_not_inherit_old_track(tmp_path) -> None:
    tracker = _tracker(tmp_path, ttl=1, max_archive=3)
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
                position_at=first_seen,
            )
        ]
    )
    await tracker.prune()
    archived = (await tracker.archived_snapshot())[0]
    later = datetime.now(UTC)
    await tracker.apply(
        [
            AircraftUpdate(
                icao="abc123",
                lat=55.4,
                lon=37.4,
                squawk="1200",
                callsign="OTHER1",
                received_at=later,
                position_at=later,
            )
        ]
    )
    live = (await tracker.snapshot())[0]
    assert live["contact_id"] != archived["contact_id"]
    assert [point[0] for point in live["track"]] == [55.4]
    assert "incomingId !== previousId" in FRONTEND


@pytest.mark.asyncio
async def test_slow_client_does_not_grow_memory_without_limit() -> None:
    hub = BroadcastHub(queue_size=1, max_clients=2)
    queue = await hub.subscribe()
    for seq in range(50):
        await hub.publish({"type": "delta", "seq": seq})
    assert queue.qsize() == 1
    assert queue.get_nowait() == {"type": "resync"}
    assert queue.empty()
    await hub.unsubscribe(queue)
    assert hub.client_count() == 0


def test_websocket_cancel_releases_subscription(tmp_path) -> None:
    settings = _settings(tmp_path, websocket_max_clients=1)
    with TestClient(create_app(settings)) as client:
        with client.websocket_connect("/ws/aircraft") as websocket:
            assert websocket.receive_json()["type"] == "snapshot"
        with client.websocket_connect("/ws/aircraft") as websocket:
            assert websocket.receive_json()["type"] == "snapshot"


# --- GIS и сохранение -------------------------------------------------------


def test_corrupt_layer_does_not_stop_others(tmp_path) -> None:
    _ctr_geojson(tmp_path)
    (tmp_path / "broken.geojson").write_text("{not json", encoding="utf-8")
    manager = LayerManager(tmp_path)
    manager.refresh()
    payload = manager.catalog_payload()
    assert {layer["id"] for layer in payload["layers"]} == {"ctr"}
    assert any(item["id"] == "broken" for item in payload["errors"])
    assert manager.matching_geofences(37, 55, 1000) == {"CTR"}


def test_map_and_geofence_use_the_same_geometry_version(tmp_path) -> None:
    _ctr_geojson(tmp_path)
    _write_mbtiles(tmp_path / "tiles.mbtiles", b"pbf-bytes")
    manager = LayerManager(tmp_path)
    manager.refresh()
    catalog = manager.catalog_payload()
    vector = manager.get_vector("ctr")
    tile, fmt, tile_version = manager.tile_payload("tiles", 0, 0, 0)
    assert vector["version"] == catalog["version"]
    assert tile_version == catalog["version"]
    assert fmt == "pbf"
    assert tile == b"pbf-bytes"
    assert manager.matching_geofences(37, 55, 1000) == {"CTR"}
    assert manager.get_vector("ctr")["geojson"]["features"]


def test_kml_holes_keep_their_meaning(tmp_path) -> None:
    (tmp_path / "donut.kml").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Placemark>
    <name>Donut</name>
    <Polygon>
      <outerBoundaryIs><LinearRing>
        <coordinates>36,54 38,54 38,56 36,56 36,54</coordinates>
      </LinearRing></outerBoundaryIs>
      <innerBoundaryIs><LinearRing>
        <coordinates>36.4,54.4 37.6,54.4 37.6,55.6 36.4,55.6 36.4,54.4</coordinates>
      </LinearRing></innerBoundaryIs>
    </Polygon>
  </Placemark>
</kml>
""",
        encoding="utf-8",
    )
    manager = LayerManager(tmp_path)
    manager.refresh()
    assert manager.matching_geofences(36.2, 54.2, 1000) == {"Donut"}
    assert manager.matching_geofences(37, 55, 1000) == set()


def test_corrupt_coverage_file_does_not_block_startup(tmp_path) -> None:
    path = tmp_path / "coverage-rose.json"
    path.write_text("[]\n", encoding="utf-8")
    with TestClient(create_app(_settings(tmp_path, coverage_path=path))) as client:
        assert client.get("/api/health").status_code == 200
        coverage = client.get("/api/coverage").json()
        assert coverage["points"] == []
        assert coverage["load_error"]
    assert (tmp_path / "coverage-rose.json.bad").is_file()


def test_disk_error_does_not_silently_change_type_catalog(tmp_path, monkeypatch) -> None:
    path = tmp_path / "aircraft-types.json"
    catalog = AircraftTypeCatalog(path)
    catalog.upsert("abc123", "A320")

    def boom(*_args, **_kwargs) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("app.aircraft_types.atomic_write_json", boom)
    with pytest.raises(OSError, match="disk full"):
        catalog.upsert("abc123", "B738")
    assert catalog.lookup("abc123") == {"type_code": "A320"}
    assert AircraftTypeCatalog(path).lookup("abc123") == {"type_code": "A320"}


@pytest.mark.asyncio
async def test_coverage_reset_reports_save_error(tmp_path, monkeypatch) -> None:
    tracker = _tracker(tmp_path, coverage_path=tmp_path / "rose.json")

    def boom(*_args, **_kwargs) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("app.tracker.atomic_write_json", boom)
    snapshot = await tracker.reset_coverage()
    assert snapshot["saved"] is False
    assert "disk full" in snapshot["save_error"]
    with TestClient(create_app(_settings(tmp_path))) as client:
        monkeypatch.setattr("app.tracker.atomic_write_json", boom)
        response = client.post("/api/coverage/reset")
        assert response.status_code == 503


# --- Эксплуатация -----------------------------------------------------------


def test_backend_restart_restores_ui_and_journal_generation(tmp_path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        page = client.get("/").text
        assert "Авиационный монитор" in page
        first = client.get("/api/adsb/messages").json()
        assert first["generation"]
        assert first["mode"] == "latest"
    with TestClient(create_app(settings)) as client:
        assert "Авиационный монитор" in client.get("/").text
        second = client.get("/api/adsb/messages").json()
        assert second["generation"]
        assert second["generation"] != first["generation"]
        assert second["events"] == []
        assert second["next_after_id"] == 0


@pytest.mark.asyncio
async def test_background_task_failure_changes_readiness() -> None:
    runtime = RuntimeStatus()
    runtime.tasks["ws-broadcast"] = "running"
    runtime.tasks["maintenance"] = "running"

    async def boom() -> None:
        raise RuntimeError("ingest died")

    await run_supervised("adsb-ingest", boom, runtime, fatal=False)
    snapshot = runtime.snapshot(adsb={"status": "online"}, source_mode="json")
    assert snapshot["tasks"]["adsb-ingest"] == "failed"
    assert snapshot["ready"] is False
    assert snapshot["live"] is True
    assert snapshot["status"] == "degraded"


def test_sdr_roles_follow_serials_not_plug_order(tmp_path) -> None:
    for name, serial in (("1-2", "1090"), ("1-1", "0118")):
        device = tmp_path / name
        device.mkdir()
        (device / "idVendor").write_text("0bda\n", encoding="ascii")
        (device / "idProduct").write_text("2838\n", encoding="ascii")
        (device / "serial").write_text(f"{serial}\n", encoding="ascii")
    channel = RadioChannel(
        id="tower",
        name="Tower",
        frequency_mhz=118.1,
        stream_url="http://127.0.0.1:8000/tower",
    )
    monitor = RadioMonitor([channel], auto_detect=True, sysfs_path=tmp_path)
    assert sorted(monitor.connected_serials()) == ["0118", "1090"]
    assert monitor.hardware_available() is True


def test_duplicate_serials_are_detected(tmp_path) -> None:
    for name in ("1-1", "1-2"):
        device = tmp_path / name
        device.mkdir()
        (device / "idVendor").write_text("0bda\n", encoding="ascii")
        (device / "idProduct").write_text("2838\n", encoding="ascii")
        (device / "serial").write_text("0118\n", encoding="ascii")
    extra = tmp_path / "1-3"
    extra.mkdir()
    (extra / "idVendor").write_text("0bda\n", encoding="ascii")
    (extra / "idProduct").write_text("2838\n", encoding="ascii")
    (extra / "serial").write_text("1090\n", encoding="ascii")
    monitor = RadioMonitor(
        [
            RadioChannel(
                id="tower",
                name="Tower",
                frequency_mhz=118.1,
                stream_url="http://127.0.0.1:8000/tower",
            )
        ],
        auto_detect=True,
        sysfs_path=tmp_path,
    )
    assert monitor.hardware_available() is False
    assert monitor.connected_serials().count("0118") == 2


@pytest.mark.asyncio
async def test_backend_reads_metrics_but_not_icecast_password(tmp_path) -> None:
    (tmp_path / "rtl_airband.conf").write_text(
        'password = "SUPERSECRET";\n', encoding="utf-8"
    )
    stats = tmp_path / "stats.prom"
    stats.write_text(
        'channel_activity_counter{freq="118.100",label="Tower"}\t3\n'
        'channel_activity_counter{freq="118.100",label="Tower"}\t4\n',
        encoding="utf-8",
    )
    channel = RadioChannel(
        id="tower",
        name="Tower",
        frequency_mhz=118.1,
        stream_url="http://user:pass@127.0.0.1:8000/vhf.mp3",
    )
    monitor = RadioMonitor([channel], stats_path=stats, auto_detect=False)
    first = await monitor.status()
    stats.write_text(
        'channel_activity_counter{freq="118.100",label="Tower"}\t5\n',
        encoding="utf-8",
    )
    second = await monitor.status()
    blob = json.dumps(second)
    assert "SUPERSECRET" not in blob
    assert "user:pass" in blob  # channel config still has URL; diagnostics must not
    assert second[0]["active"] is True
    payload = collect_diagnostics(
        settings=Settings(
            radio_channels_json=json.dumps(
                [
                    {
                        "id": "tower",
                        "name": "Вышка",
                        "frequency_mhz": 118.1,
                        "stream_url": "http://user:pass@127.0.0.1:8000/vhf.mp3",
                    }
                ]
            )
        ),
        health={"status": "ok", "live": True, "ready": True, "source_mode": "json"},
        gis={"version": 1, "last_good_version": 1, "load_ms": 1, "errors": []},
        coverage={"saved": True, "load_error": None, "save_error": None},
        host={},
    )
    dumped = json.dumps(payload)
    assert "SUPERSECRET" not in dumped
    assert "user:pass" not in dumped
    assert "stream_url" not in dumped
    unit = (ROOT / "deploy" / "systemd" / "rtl-airband.service").read_text(encoding="utf-8")
    backend = (ROOT / "deploy" / "systemd" / "adsb-vhf-backend.service").read_text(
        encoding="utf-8"
    )
    assert "chmod 0600" in unit
    assert "stats.prom" in unit
    assert "rtl_airband.conf" not in backend
    assert first[0]["id"] == "tower"


def test_chromium_before_backend_recovers_interface() -> None:
    kiosk = (ROOT / "deploy" / "chromium" / "start-kiosk.sh").read_text(encoding="utf-8")
    assert "raise SystemExit(1)" in kiosk
    assert "wait_for_http ||" in kiosk
    assert "scheduleReconnect" in FRONTEND
    assert "connectAircraftSocket" in FRONTEND
    assert "Повторное подключение" in FRONTEND
    assert "Restart=always" in (ROOT / "deploy" / "systemd" / "adsb-kiosk.service").read_text(
        encoding="utf-8"
    )
