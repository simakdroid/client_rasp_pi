import json

from app.gis import LayerManager


def test_geojson_geofence_with_altitude(tmp_path) -> None:
    layer = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "name": "CTR",
                    "min_alt_ft": 0,
                    "max_alt_ft": 5000,
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[36, 54], [38, 54], [38, 56], [36, 56], [36, 54]]],
                },
            }
        ],
    }
    (tmp_path / "ctr.geojson").write_text(json.dumps(layer), encoding="utf-8")
    manager = LayerManager(tmp_path)
    manager.refresh()

    assert manager.matching_geofences(37, 55, 3000) == {"CTR"}
    assert manager.matching_geofences(37, 55, 6000) == set()
    assert manager.matching_geofences(40, 55, 3000) == set()
