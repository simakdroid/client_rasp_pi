import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.aircraft_types import AircraftTypeCatalog
from app.config import Settings
from app.gis import LayerManager
from app.main import create_app
from app.models import AircraftUpdate
from app.tracker import AircraftTracker


def test_catalog_roundtrip_and_fallback(tmp_path) -> None:
    path = tmp_path / "aircraft-types.json"
    catalog = AircraftTypeCatalog(path)
    entry = catalog.upsert("424B4D", "A320", "Airbus A320")
    assert entry == {"icao": "424B4D", "type_code": "A320", "type_desc": "Airbus A320"}
    reloaded = AircraftTypeCatalog(path)
    assert reloaded.lookup("424b4d") == {"type_code": "A320", "type_desc": "Airbus A320"}
    payload = {"icao": "424b4d", "type_code": None, "type_desc": None}
    reloaded.apply(payload)
    assert payload["type_code"] == "A320"
    known = {"icao": "424b4d", "type_code": "B738", "type_desc": "Boeing 737-800"}
    reloaded.apply(known)
    assert known["type_code"] == "B738"
    with pytest.raises(ValueError, match="24-bit hex"):
        catalog.upsert("~abc123", "A320")


def test_catalog_reloads_when_json_file_changes(tmp_path) -> None:
    path = tmp_path / "aircraft-types.json"
    catalog = AircraftTypeCatalog(path)
    assert catalog.list() == []
    path.write_text(
        '{"abc123": {"type_code": "B738", "type_desc": "Boeing 737-800"}}\n',
        encoding="utf-8",
    )
    assert catalog.list() == []
    assert catalog.refresh() is True
    assert catalog.list() == [
        {"icao": "ABC123", "type_code": "B738", "type_desc": "Boeing 737-800"},
    ]
    path.write_text('{"abc123": {"type_code": "A320"}}\n', encoding="utf-8")
    payload = {"icao": "abc123", "type_code": None}
    catalog.apply(payload)
    assert payload["type_code"] == "B738"
    payload = {"icao": "abc123", "type_code": None}
    catalog.refresh()
    catalog.apply(payload)
    assert payload["type_code"] == "A320"


def test_failed_load_does_not_stick_fingerprint(tmp_path) -> None:
    path = tmp_path / "aircraft-types.json"
    path.write_text("{not json", encoding="utf-8")
    catalog = AircraftTypeCatalog(path)
    assert catalog.list() == []
    path.write_text('{"abc123": {"type_code": "A320"}}\n', encoding="utf-8")
    assert catalog.refresh() is True
    assert catalog.lookup("abc123") == {"type_code": "A320"}


def test_upsert_keeps_concurrent_external_entries(tmp_path) -> None:
    path = tmp_path / "aircraft-types.json"
    catalog = AircraftTypeCatalog(path)
    catalog.upsert("abc123", "A320")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["def456"] = {"type_code": "B738"}
    path.write_text(json.dumps(payload), encoding="utf-8")
    catalog.upsert("aaa111", "C172")
    listed = {item["icao"]: item["type_code"] for item in catalog.list()}
    assert listed == {"ABC123": "A320", "DEF456": "B738", "AAA111": "C172"}


@pytest.mark.asyncio
async def test_tracker_uses_catalog_only_when_type_missing(tmp_path) -> None:
    layers = LayerManager(tmp_path)
    layers.refresh()
    catalog = AircraftTypeCatalog(tmp_path / "types.json")
    catalog.upsert("abc123", "A321")
    tracker = AircraftTracker(55.0, 37.0, layers, 60, 10, 1, type_catalog=catalog)
    await tracker.apply(
        [AircraftUpdate(icao="abc123", lat=55.1, lon=37.1, received_at=datetime.now(UTC))]
    )
    snapshot = await tracker.snapshot()
    assert snapshot[0]["type_code"] == "A321"


def test_aircraft_types_api(tmp_path) -> None:
    settings = Settings(
        layers_dir=tmp_path,
        readsb_json_path=tmp_path / "missing-aircraft.json",
        coverage_path=tmp_path / "coverage-rose.json",
        aircraft_types_path=tmp_path / "aircraft-types.json",
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/aircraft-types").json()["types"] == []
        created = client.post(
            "/api/aircraft-types",
            json={"icao": "4ca1d2", "type_code": "B738", "type_desc": "Boeing 737-800"},
        )
        assert created.status_code == 200
        assert created.json()["icao"] == "4CA1D2"
        listed = client.get("/api/aircraft-types").json()["types"]
        assert listed == [
            {"icao": "4CA1D2", "type_code": "B738", "type_desc": "Boeing 737-800"},
        ]
        invalid = client.post("/api/aircraft-types", json={"icao": "zz", "type_code": "B738"})
        assert invalid.status_code == 422
        assert client.delete("/api/aircraft-types/4CA1D2").json() == {"ok": True}
        assert client.get("/api/aircraft-types").json()["types"] == []


def test_catalog_keeps_memory_if_write_fails(tmp_path, monkeypatch) -> None:
    path = tmp_path / "aircraft-types.json"
    catalog = AircraftTypeCatalog(path)
    catalog.upsert("abc123", "A320")

    def boom(*_args, **_kwargs) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("app.aircraft_types.atomic_write_json", boom)
    with pytest.raises(OSError, match="disk full"):
        catalog.upsert("abc123", "B738")
    assert catalog.lookup("abc123") == {"type_code": "A320"}
    reloaded = AircraftTypeCatalog(path)
    assert reloaded.lookup("abc123") == {"type_code": "A320"}


def test_aircraft_types_api_returns_503_when_unsaved(tmp_path, monkeypatch) -> None:
    settings = Settings(
        layers_dir=tmp_path,
        readsb_json_path=tmp_path / "missing-aircraft.json",
        coverage_path=tmp_path / "coverage-rose.json",
        aircraft_types_path=tmp_path / "aircraft-types.json",
    )
    with TestClient(create_app(settings)) as client:
        created = client.post(
            "/api/aircraft-types",
            json={"icao": "4ca1d2", "type_code": "B738"},
        )
        assert created.status_code == 200

        def boom(*_args, **_kwargs) -> None:
            raise OSError("disk full")

        monkeypatch.setattr("app.aircraft_types.atomic_write_json", boom)
        failed = client.post(
            "/api/aircraft-types",
            json={"icao": "4ca1d2", "type_code": "A320"},
        )
        assert failed.status_code == 503
        listed = client.get("/api/aircraft-types").json()["types"]
        assert listed == [{"icao": "4CA1D2", "type_code": "B738"}]


@pytest.mark.asyncio
async def test_list_does_not_hide_external_catalog_change_from_tracker(tmp_path) -> None:
    path = tmp_path / "types.json"
    catalog = AircraftTypeCatalog(path)
    layers = LayerManager(tmp_path)
    layers.refresh()
    tracker = AircraftTracker(55.0, 37.0, layers, 60, 10, 1, type_catalog=catalog)
    await tracker.apply(
        [AircraftUpdate(icao="abc123", lat=55.1, lon=37.1, received_at=datetime.now(UTC))]
    )
    await tracker.refresh_manual_types()
    await tracker.consume_delta()
    path.write_text('{"abc123": {"type_code": "A321"}}\n', encoding="utf-8")
    assert catalog.list() == []
    await tracker.refresh_manual_types()
    snapshot = await tracker.snapshot()
    assert snapshot[0]["type_code"] == "A321"
    delta = await tracker.consume_delta()
    assert delta is not None
    assert delta["upsert"][0]["type_code"] == "A321"


@pytest.mark.asyncio
async def test_type_catalog_change_updates_archive(tmp_path) -> None:
    catalog = AircraftTypeCatalog(tmp_path / "types.json")
    layers = LayerManager(tmp_path)
    layers.refresh()
    tracker = AircraftTracker(
        55.0, 37.0, layers, 1, 10, 1, type_catalog=catalog, max_archive=2
    )
    first_seen = datetime.now(UTC) - timedelta(minutes=10)
    await tracker.apply(
        [AircraftUpdate(icao="abc123", lat=55.1, lon=37.1, received_at=first_seen)]
    )
    await tracker.prune()
    await tracker.consume_delta()
    entry = await tracker.persist_type_upsert("abc123", "A320")
    assert entry["type_code"] == "A320"
    await tracker.mark_type_changed("abc123")
    delta = await tracker.consume_delta()
    assert delta is not None
    assert delta["archive"][0]["icao"] == "abc123"
    assert delta["archive"][0]["type_code"] == "A320"
    archived = await tracker.archived_snapshot()
    assert archived[0]["type_code"] == "A320"
