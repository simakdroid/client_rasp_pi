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
    assert manager.matching_geofences(37, 55, 5000) == {"CTR"}
    assert manager.matching_geofences(37, 55, 6000) == set()
    assert manager.matching_geofences(40, 55, 3000) == set()


def test_exclusive_min_altitude_matches_above_fl(tmp_path) -> None:
    layer = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "name": "TMA",
                    "min_alt_ft": 5000,
                    "min_alt_exclusive": True,
                    "max_alt_ft": 10000,
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[36, 54], [38, 54], [38, 56], [36, 56], [36, 54]]],
                },
            }
        ],
    }
    (tmp_path / "tma.geojson").write_text(json.dumps(layer), encoding="utf-8")
    manager = LayerManager(tmp_path)
    manager.refresh()

    assert manager.matching_geofences(37, 55, 5000) == set()
    assert manager.matching_geofences(37, 55, 5001) == {"TMA"}
    assert manager.matching_geofences(37, 55, 10000) == {"TMA"}
    assert manager.matching_geofences(37, 55, 10001) == set()


def test_control_code_prefers_higher_priority(tmp_path) -> None:
    layer = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "name": "Sector",
                    "code": "С6",
                    "control_priority": 20,
                    "min_alt_ft": 5000,
                    "min_alt_exclusive": True,
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[36, 54], [38, 54], [38, 56], [36, 56], [36, 54]]],
                },
            },
            {
                "type": "Feature",
                "properties": {
                    "name": "TMA",
                    "code": "USRR_APP",
                    "control_priority": 30,
                    "min_alt_ft": 5000,
                    "min_alt_exclusive": True,
                    "max_alt_ft": 26500,
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[36, 54], [38, 54], [38, 56], [36, 56], [36, 54]]],
                },
            },
        ],
    }
    (tmp_path / "overlap.geojson").write_text(json.dumps(layer), encoding="utf-8")
    manager = LayerManager(tmp_path)
    manager.refresh()

    assert manager.matching_control_code(37, 55, 10000) == "USRR_APP"
    assert manager.matching_control_code(37, 55, 27000) == "С6"


def test_kml_polygon_holes_are_excluded(tmp_path) -> None:
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


def test_invalid_layer_does_not_block_others(tmp_path) -> None:
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
    (tmp_path / "broken.geojson").write_text("{not json", encoding="utf-8")
    manager = LayerManager(tmp_path)
    manager.refresh()
    payload = manager.catalog_payload()
    assert payload["version"] >= 1
    assert {layer["id"] for layer in payload["layers"]} == {"ctr"}
    assert any(item["id"] == "broken" for item in payload["errors"])
    assert manager.matching_geofences(37, 55, 1000) == {"CTR"}


def test_failed_refresh_keeps_previous_layer(tmp_path) -> None:
    path = tmp_path / "ctr.geojson"
    path.write_text(
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
    manager = LayerManager(tmp_path)
    manager.refresh()
    version = manager.catalog.version
    path.write_text("{broken", encoding="utf-8")
    manager.refresh()
    assert manager.matching_geofences(37, 55, 1000) == {"CTR"}
    assert manager.get_vector("ctr")["version"] == manager.catalog.version
    assert manager.catalog.version >= version
    errors = manager.catalog_payload()["errors"]
    assert errors
    assert errors[0]["kept_previous"] is True
    assert errors[0]["reason"]


def test_geofence_false_string_is_not_a_zone(tmp_path) -> None:
    (tmp_path / "draw.geojson").write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"name": "Draw", "geofence": "false"},
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
    manager = LayerManager(tmp_path)
    manager.refresh()
    assert manager.matching_geofences(37, 55, 1000) == set()


def test_spatial_index_finds_only_covering_polygon(tmp_path) -> None:
    features = []
    for index in range(40):
        west = 30 + index
        features.append(
            {
                "type": "Feature",
                "properties": {"name": f"Z{index}"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[
                        [west, 50],
                        [west + 0.8, 50],
                        [west + 0.8, 51],
                        [west, 51],
                        [west, 50],
                    ]],
                },
            }
        )
    (tmp_path / "grid.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": features}),
        encoding="utf-8",
    )
    manager = LayerManager(tmp_path)
    manager.refresh()
    names, sector, matched = manager.match_airspace(37.4, 50.4, 1000)
    assert names == {"Z7"}
    assert sector is None
    assert len(matched) == 1
    assert matched[0].name == "Z7"
    assert manager.catalog.geofence_tree is not None
    assert manager.catalog_payload()["geofence_count"] == 40
    payload = manager.catalog_payload()
    assert payload["feature_count"] == 40
    assert payload["load_ms"] >= 0
    assert payload["last_good_version"] == payload["version"]
    assert payload["geofence_policy"]["boundary"] == "inclusive"
    assert "smaller_area" in payload["geofence_policy"]["control_tiebreak"]


def test_all_failed_layers_keep_previous_last_good_version(tmp_path) -> None:
    (tmp_path / "broken.geojson").write_text("{not json", encoding="utf-8")
    manager = LayerManager(tmp_path)
    manager.refresh()
    payload = manager.catalog_payload()
    assert payload["layers"] == []
    assert payload["last_good_version"] == 0
    assert payload["version"] >= 1
    assert payload["errors"]


def _polygon(west: float, south: float, east: float, north: float) -> dict:
    return {
        "type": "Polygon",
        "coordinates": [[
            [west, south],
            [east, south],
            [east, north],
            [west, north],
            [west, south],
        ]],
    }


def _write_mbtiles(path, tile_format: str, tile: bytes, tables: bool = True) -> None:
    import sqlite3

    connection = sqlite3.connect(path)
    if tables:
        connection.execute("CREATE TABLE metadata (name text, value text)")
        connection.execute(
            "CREATE TABLE tiles (zoom_level int, tile_column int, tile_row int, tile_data blob)"
        )
        connection.execute("INSERT INTO metadata VALUES ('name', ?)", (path.stem,))
        connection.execute("INSERT INTO metadata VALUES ('format', ?)", (tile_format,))
        connection.execute("INSERT INTO tiles VALUES (0, 0, 0, ?)", (tile,))
    else:
        connection.execute("CREATE TABLE foo (x int)")
    connection.commit()
    connection.close()


def test_geojson_out_of_range_coordinate_is_skipped(tmp_path) -> None:
    (tmp_path / "bad.geojson").write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"name": "Bad"},
                        "geometry": _polygon(200, 55, 201, 56),
                    },
                    {
                        "type": "Feature",
                        "properties": {"name": "CTR"},
                        "geometry": _polygon(36, 54, 38, 56),
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    manager = LayerManager(tmp_path)
    manager.refresh()
    assert manager.matching_geofences(37, 55, 1000) == {"CTR"}
    assert {item.name for item in manager.catalog.geofences} == {"CTR"}


def test_polygon_boundary_is_inside_geofence(tmp_path) -> None:
    (tmp_path / "ctr.geojson").write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"name": "CTR"},
                        "geometry": _polygon(36, 54, 38, 56),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    manager = LayerManager(tmp_path)
    manager.refresh()
    assert manager.matching_geofences(36, 54, 1000) == {"CTR"}


def test_invalid_and_duplicate_layer_ids(tmp_path) -> None:
    layer = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": "CTR"},
                "geometry": _polygon(36, 54, 38, 56),
            }
        ],
    }
    (tmp_path / "ctr.geojson").write_text(json.dumps(layer), encoding="utf-8")
    _write_mbtiles(tmp_path / "ctr.mbtiles", "pbf", b"mvt-bytes")
    (tmp_path / "bad name.geojson").write_text(json.dumps(layer), encoding="utf-8")
    manager = LayerManager(tmp_path)
    manager.refresh()
    payload = manager.catalog_payload()
    assert [item["id"] for item in payload["layers"]] == ["ctr"]
    assert payload["layers"][0]["kind"] == "geojson"
    errors = {item["error"] for item in payload["errors"]}
    assert "duplicate layer id" in errors
    assert "invalid layer id" in errors
    assert manager.matching_geofences(37, 55, 1000) == {"CTR"}


def test_equal_priority_prefers_smaller_polygon(tmp_path) -> None:
    (tmp_path / "overlap.geojson").write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {
                            "name": "Wide",
                            "code": "WIDE",
                            "control_priority": 20,
                        },
                        "geometry": _polygon(36, 54, 38, 56),
                    },
                    {
                        "type": "Feature",
                        "properties": {
                            "name": "Core",
                            "code": "CORE",
                            "control_priority": 20,
                        },
                        "geometry": _polygon(36.4, 54.4, 37.6, 55.6),
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    manager = LayerManager(tmp_path)
    manager.refresh()
    assert manager.matching_control_code(37, 55, 1000) == "CORE"
    names, sector, matched = manager.match_airspace(37, 55, 1000)
    assert names == {"Wide", "Core"}
    assert sector == "CORE"
    assert {item.key for item in matched} == {"overlap:Wide", "overlap:Core"}


def test_equal_priority_and_area_prefers_smaller_key(tmp_path) -> None:
    for layer_id, code in (("alpha", "ZULU"), ("beta", "AAA")):
        (tmp_path / f"{layer_id}.geojson").write_text(
            json.dumps(
                {
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {
                                "name": "Zone",
                                "code": code,
                                "control_priority": 20,
                            },
                            "geometry": _polygon(36, 54, 38, 56),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
    manager = LayerManager(tmp_path)
    manager.refresh()
    assert manager.matching_control_code(37, 55, 1000) == "ZULU"


def test_mbtiles_unknown_format_and_missing_tables_are_errors(tmp_path) -> None:
    _write_mbtiles(tmp_path / "photo.mbtiles", "tiff", b"II*")
    _write_mbtiles(tmp_path / "empty.mbtiles", "pbf", b"x", tables=False)
    _write_mbtiles(tmp_path / "vector.mbtiles", "pbf", b"mvt-bytes")
    manager = LayerManager(tmp_path)
    manager.refresh()
    payload = manager.catalog_payload()
    ids = {layer["id"] for layer in payload["layers"]}
    assert ids == {"vector"}
    errors = {item["id"]: item["error"] for item in payload["errors"]}
    assert "photo" in errors
    assert "tiff" in errors["photo"]
    assert "empty" in errors
    tile, fmt, version = manager.tile_payload("vector", 0, 0, 0)
    assert tile == b"mvt-bytes"
    assert fmt == "pbf"
    assert version == manager.catalog.version


def test_tile_http_metadata_marks_gzip_and_rejects_unknown_format() -> None:
    from app.gis import UnsupportedTileFormatError, tile_http_metadata

    media, headers = tile_http_metadata(b"\x1f\x8bpayload", "pbf")
    assert media == "application/vnd.mapbox-vector-tile"
    assert headers["Content-Encoding"] == "gzip"
    raw_media, raw_headers = tile_http_metadata(b"\x1a\x2b", "pbf")
    assert raw_media == "application/vnd.mapbox-vector-tile"
    assert "Content-Encoding" not in raw_headers
    png_media, png_headers = tile_http_metadata(b"\x89PNG", "png")
    assert png_media == "image/png"
    assert "Content-Encoding" not in png_headers
    try:
        tile_http_metadata(b"abc", "unknown")
    except UnsupportedTileFormatError:
        pass
    else:
        raise AssertionError("expected UnsupportedTileFormatError")

