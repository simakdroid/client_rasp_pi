from pathlib import Path

from pyproj import Geod

from app.coverage import CoverageRose

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
