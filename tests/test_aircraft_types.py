from datetime import UTC, datetime

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
    assert reloaded.delete("424B4D") is True
    assert reloaded.lookup("424B4D") is None


def test_catalog_reloads_when_json_file_changes(tmp_path) -> None:
    path = tmp_path / "aircraft-types.json"
    catalog = AircraftTypeCatalog(path)
    assert catalog.list() == []
    path.write_text(
        '{"abc123": {"type_code": "B738", "type_desc": "Boeing 737-800"}}\n',
        encoding="utf-8",
    )
    assert catalog.list() == [
        {"icao": "ABC123", "type_code": "B738", "type_desc": "Boeing 737-800"},
    ]
    path.write_text('{"abc123": {"type_code": "A320"}}\n', encoding="utf-8")
    payload = {"icao": "abc123", "type_code": None}
    catalog.apply(payload)
    assert payload["type_code"] == "A320"


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
        assert client.post("/api/aircraft-types", json={"icao": "zz", "type_code": "B738"}).status_code == 400
        assert client.delete("/api/aircraft-types/4CA1D2").json() == {"ok": True}
        assert client.get("/api/aircraft-types").json()["types"] == []
