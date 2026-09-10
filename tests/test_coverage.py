import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pyproj import Geod

from app.config import Settings
from app.coverage import CoverageRose
from app.gis import LayerManager
from app.main import create_app
from app.tracker import AircraftTracker

GEOD = Geod(ellps="WGS84")


def test_coverage_rose_records_farthest_sample_per_azimuth(tmp_path: Path) -> None:
    rose = CoverageRose(55.0, 37.0, tmp_path / "rose.json")
    north, _, north_m = GEOD.inv(37.0, 55.0, 37.0, 56.0)
    east, _, east_m = GEOD.inv(37.0, 55.0, 38.0, 55.0)

    assert rose.observe(north, north_m / 2) is True
    assert rose.observe(north, north_m) is True
    assert rose.observe(north, north_m / 3) is False
    assert rose.observe(east, east_m) is True

    snapshot = rose.snapshot()
    assert snapshot["filled_bins"] == 2
    assert snapshot["samples"] == 3
    assert snapshot["range_updates"] == 3
    assert snapshot["observations"] == 4
    assert snapshot["kind"] == "historical_max_range"
    assert snapshot["max_range_km"] == round(max(north_m, east_m) / 1000, 2)
    assert len(snapshot["points"]) == 361
    station_hits = sum(
        1
        for lat, lon in snapshot["points"][:-1]
        if abs(lat - 55.0) < 1e-6 and abs(lon - 37.0) < 1e-6
    )
    assert station_hits == 358


def test_coverage_rose_persists_and_resets(tmp_path: Path) -> None:
    path = tmp_path / "rose.json"
    rose = CoverageRose(55.0, 37.0, path)
    rose.observe(0, 12_000)
    rose.flush()

    restored = CoverageRose(55.0, 37.0, path)
    snapshot = restored.snapshot()
    assert snapshot["samples"] == 1
    assert snapshot["max_range_km"] == 12.0

    restored.reset()
    restored.flush()
    empty = CoverageRose(55.0, 37.0, path)
    assert empty.snapshot()["samples"] == 0
    assert empty.snapshot()["points"] == []
    assert empty.snapshot()["saved"] is True


def test_corrupt_coverage_is_quarantined(tmp_path: Path) -> None:
    path = tmp_path / "rose.json"
    path.write_text("[]\n", encoding="utf-8")
    rose = CoverageRose(55.0, 37.0, path)
    snapshot = rose.snapshot()
    assert snapshot["points"] == []
    assert snapshot["load_error"]
    assert snapshot["saved"] is True
    assert (tmp_path / "rose.json.bad").is_file()
    assert not path.exists()


def test_nonfinite_coverage_ranges_are_quarantined(tmp_path: Path) -> None:
    path = tmp_path / "rose.json"
    path.write_text(
        json.dumps(
            {
                "station_lat": 55.0,
                "station_lon": 37.0,
                "range_m": [float("inf")] * 360,
                "hits": [0] * 360,
                "samples": 0,
                "revision": 0,
            }
        ),
        encoding="utf-8",
    )
    rose = CoverageRose(55.0, 37.0, path)
    assert rose.snapshot()["points"] == []
    assert rose.snapshot()["load_error"]
    assert (tmp_path / "rose.json.bad").is_file()


def test_corrupt_coverage_does_not_crash_startup(tmp_path: Path) -> None:
    path = tmp_path / "coverage-rose.json"
    path.write_text("[]\n", encoding="utf-8")
    settings = Settings(
        layers_dir=tmp_path,
        readsb_json_path=tmp_path / "missing-aircraft.json",
        coverage_path=path,
        aircraft_types_path=tmp_path / "aircraft-types.json",
    )
    with TestClient(create_app(settings)) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        coverage = client.get("/api/coverage").json()
        assert coverage["points"] == []
        assert coverage["load_error"]


def test_flush_reports_write_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rose = CoverageRose(55.0, 37.0, tmp_path / "rose.json")
    rose.observe(0, 12_000)

    def boom(*_args, **_kwargs) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("app.coverage.atomic_write_json", boom)
    assert rose.flush() is False
    snapshot = rose.snapshot()
    assert snapshot["saved"] is False
    assert "disk full" in snapshot["save_error"]


@pytest.mark.asyncio
async def test_reset_coverage_reports_save_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layers = LayerManager(tmp_path)
    layers.refresh()
    tracker = AircraftTracker(
        55.0, 37.0, layers, 60, 10, 1, coverage_path=tmp_path / "rose.json"
    )

    def boom(*_args, **_kwargs) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("app.tracker.atomic_write_json", boom)
    snapshot = await tracker.reset_coverage()
    assert snapshot["saved"] is False
    assert "disk full" in snapshot["save_error"]


def test_coverage_reset_returns_503_when_unsaved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args, **_kwargs) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("app.tracker.atomic_write_json", boom)
    settings = Settings(
        layers_dir=tmp_path,
        readsb_json_path=tmp_path / "missing-aircraft.json",
        coverage_path=tmp_path / "coverage-rose.json",
        aircraft_types_path=tmp_path / "aircraft-types.json",
    )
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/coverage/reset")
        assert response.status_code == 503
        assert "disk full" in response.json()["detail"]


def test_same_position_is_not_a_new_observation() -> None:
    rose = CoverageRose(55.0, 37.0)
    assert rose.observe(10, 5000, icao="abc123", lat=55.1, lon=37.1) is True
    assert rose.observe(10, 5000, icao="abc123", lat=55.1, lon=37.1) is False
    snapshot = rose.snapshot()
    assert snapshot["observations"] == 1
    assert snapshot["range_updates"] == 1


def test_coverage_records_altitude_bands_and_hourly_buckets() -> None:
    rose = CoverageRose(55.0, 37.0)
    assert rose.observe(10, 8000, icao="abc123", lat=55.2, lon=37.1, altitude_ft=1200) is True
    assert rose.observe(20, 15000, icao="def456", lat=55.3, lon=37.2, altitude_ft=22000) is True
    snapshot = rose.snapshot()
    bands = {item["id"]: item for item in snapshot["altitude_bands"]}
    assert bands["0-5k"]["observations"] == 1
    assert bands["0-5k"]["max_range_km"] == 8.0
    assert bands["15-25k"]["observations"] == 1
    assert bands["15-25k"]["max_range_km"] == 15.0
    assert bands["5-15k"]["observations"] == 0
    assert snapshot["hourly"]
    assert snapshot["hourly"][-1]["observations"] >= 2


def test_loaded_ranges_are_capped_to_configured_max(tmp_path: Path) -> None:
    path = tmp_path / "rose.json"
    path.write_text(
        json.dumps(
            {
                "station_lat": 55.0,
                "station_lon": 37.0,
                "range_m": [800_000] * 360,
                "hits": [1] * 360,
                "samples": 360,
                "revision": 4,
                "altitude_bands": {"0-5k": {"observations": 2, "max_range_m": 800_000}},
                "hourly": "nope",
            }
        ),
        encoding="utf-8",
    )
    rose = CoverageRose(55.0, 37.0, path, max_range_km=100)
    snapshot = rose.snapshot()
    assert snapshot["points"] == []
    assert snapshot["load_error"]
    assert (tmp_path / "rose.json.bad").is_file()

    path.write_text(
        json.dumps(
            {
                "station_lat": 55.0,
                "station_lon": 37.0,
                "range_m": [800_000] * 360,
                "hits": [1] * 360,
                "samples": 360,
                "revision": 4,
                "altitude_bands": {"0-5k": {"observations": 2, "max_range_m": 800_000}},
            }
        ),
        encoding="utf-8",
    )
    restored = CoverageRose(55.0, 37.0, path, max_range_km=100)
    capped = restored.snapshot()
    assert capped["load_error"] is None
    assert capped["max_range_km"] == 100.0
    bands = {item["id"]: item for item in capped["altitude_bands"]}
    assert bands["0-5k"]["max_range_km"] == 100.0


def test_boolean_range_values_are_quarantined(tmp_path: Path) -> None:
    path = tmp_path / "rose.json"
    path.write_text(
        json.dumps(
            {
                "station_lat": 55.0,
                "station_lon": 37.0,
                "range_m": [True] * 360,
                "hits": [0] * 360,
                "samples": 0,
                "revision": 0,
            }
        ),
        encoding="utf-8",
    )
    rose = CoverageRose(55.0, 37.0, path)
    assert rose.snapshot()["load_error"]
    assert (tmp_path / "rose.json.bad").is_file()
