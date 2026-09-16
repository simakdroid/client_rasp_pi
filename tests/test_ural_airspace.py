import json
from pathlib import Path
from shutil import copy2

from app.gis import LayerManager

LAYERS = Path(__file__).resolve().parents[1] / "data" / "layers"
FILES = (
    "acc-ural.geojson",
    "rpi-yekaterinburg.geojson",
    "udr-yekaterinburg-koltsovo.geojson",
)


def _manager(tmp_path: Path) -> LayerManager:
    for name in FILES:
        copy2(LAYERS / name, tmp_path / name)
    manager = LayerManager(tmp_path)
    manager.refresh()
    return manager


def test_ural_layers_load_and_keep_published_vertices(tmp_path) -> None:
    manager = _manager(tmp_path)
    payload = manager.catalog_payload()
    assert payload["errors"] == []
    assert {item["id"] for item in payload["layers"]} == {
        "acc-ural",
        "rpi-yekaterinburg",
        "udr-yekaterinburg-koltsovo",
    }
    assert payload["geofence_count"] == 16

    u1 = json.loads((LAYERS / "acc-ural.geojson").read_text(encoding="utf-8"))
    assert u1["features"][0]["properties"]["code"] == "U1"
    assert u1["features"][0]["properties"]["min_alt_ft"] == 4921
    # 585211N 0605743E
    assert [60.961944, 58.869722] in u1["features"][0]["geometry"]["coordinates"][0]


def test_ural_altitude_and_control_code(tmp_path) -> None:
    manager = _manager(tmp_path)
    koltsovo = (60.8022, 56.7433)

    low_names, low_code, _ = manager.match_airspace(*koltsovo, 3000)
    assert low_code is None
    assert "РПИ Екатеринбург" in low_names
    assert "Екатеринбург/Кольцово УДР" not in low_names

    app_names, app_code, _ = manager.match_airspace(*koltsovo, 8000)
    assert app_code == "USSS_APP"
    assert "Екатеринбург/Кольцово УДР" in app_names

    high_names, high_code, _ = manager.match_airspace(*koltsovo, 20000)
    assert "Екатеринбург/Кольцово УДР" not in high_names
    assert high_code == "U3"

    assert manager.matching_control_code(47.5, 59.5, 6000) == "U9"
    assert manager.matching_control_code(66.0, 55.8, 6000) == "U4"
    assert manager.matching_control_code(66.0, 55.8, 4000) is None
    assert manager.matching_control_code(58.7, 53.8, 9000) == "U5"
    assert manager.matching_control_code(58.7, 53.8, 6000) is None
    # South of the Russia–Kazakhstan contour must not fall into U4 / FIR.
    assert manager.matching_control_code(66.0, 53.5, 8000) is None
    assert "РПИ Екатеринбург" not in manager.matching_geofences(66.0, 53.5, 8000)


def test_ural_state_border_is_densified() -> None:
    acc = json.loads((LAYERS / "acc-ural.geojson").read_text(encoding="utf-8"))
    fir = json.loads((LAYERS / "rpi-yekaterinburg.geojson").read_text(encoding="utf-8"))
    u4 = next(item for item in acc["features"] if item["properties"]["code"] == "U4")
    u5 = max(
        (item for item in acc["features"] if item["properties"]["code"] == "U5"),
        key=lambda item: len(item["geometry"]["coordinates"][0]),
    )
    assert [69.15, 55.4] in u4["geometry"]["coordinates"][0]
    assert [63.316667, 54.2] in u4["geometry"]["coordinates"][0]
    assert [60.0, 51.983333] in u5["geometry"]["coordinates"][0]
    assert len(u4["geometry"]["coordinates"][0]) > 80
    assert len(u5["geometry"]["coordinates"][0]) > 120
    assert len(fir["features"][0]["geometry"]["coordinates"][0]) > 200
