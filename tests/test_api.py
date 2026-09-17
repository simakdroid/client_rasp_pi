from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_ui_and_api_are_served(tmp_path) -> None:
    settings = Settings(
        layers_dir=tmp_path,
        readsb_json_path=tmp_path / "missing-aircraft.json",
        coverage_path=tmp_path / "coverage-rose.json",
        aircraft_types_path=tmp_path / "aircraft-types.json",
        acars_udp_port=55553,
    )
    with TestClient(create_app(settings)) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["adsb"]["status"] == "unavailable"
        assert health.json()["live"] is True
        assert health.json()["ready"] is False
        assert client.get("/api/aircraft").json()["aircraft"] == []
        assert client.get("/api/adsb/messages").json()["events"] == []
        assert client.get("/api/adsb/raw").json()["messages"] == []
        cleared = client.post("/api/adsb/messages/clear").json()
        assert cleared["ok"] is True
        assert cleared["last_id"] == 0
        assert cleared["generation"]
        raw_cleared = client.post("/api/adsb/raw/clear").json()
        assert raw_cleared["ok"] is True
        assert raw_cleared["last_id"] == 0
        assert raw_cleared["generation"]
        paged = client.get("/api/adsb/messages", params={"newest_first": True, "limit": 1})
        assert paged.status_code == 200
        assert paged.json()["has_more"] is False
        assert paged.json()["total"] == 0
        assert paged.json()["next_after_id"] == 0
        assert paged.json()["mode"] == "before"
        latest = client.get("/api/adsb/messages").json()
        assert latest["mode"] == "latest"
        page = client.get("/").text
        assert "Авиационный монитор" in page
        assert 'id="receiver-card"' not in page
        assert 'id="clock"' in page
        assert 'id="panel-journal"' in page
        assert 'data-journal-mode="raw"' in page
        assert 'id="journal-list"' in page
        assert 'id="acars-hint"' in page
        assert 'id="journal-pager"' not in page
        assert 'id="strip-board"' in page
        assert 'id="toggle-surface"' in page
        assert 'id="aircraft-strip-body"' in page
        assert "Список бортов" in page
        assert "Позывной" in page
        assert 'id="strip-time-heading"' in page
        assert 'data-tab="aircraft"' not in page
        assert 'id="panel-aircraft"' not in page
        assert 'id="panel-types"' in page
        assert 'data-tab="types"' in page
        assert 'id="type-catalog-form"' in page
        assert client.get("/api/aircraft-types").json()["types"] == []
        assert 'id="custom-layers"' in page
        assert 'class="custom-layers pane-scroll"' in page
        assert "Время записей — UTC" in page
        assert client.get("/api/aircraft").json()["archived"] == []
        assert client.get("/api/sessions").status_code == 404
        assert 'data-tab="sessions"' not in page
        assert 'id="panel-sessions"' not in page
        assert 'id="toggle-sessions"' not in page
        assert 'data-tab="station"' in page
        assert 'id="panel-station"' in page
        assert 'id="session-upload"' in page
        assert 'id="coverage-bands"' not in page
        assert 'id="acars-quality"' in page
        assert 'id="acars-list"' in page
        assert 'data-tab="acars"' in page
        assert 'data-tab="radio"' not in page
        assert 'id="radio-squelch"' not in page
        assert 'data-journal-mode="geofence"' in page
        assert 'id="gis-diagnostics"' in page
        station = client.get("/api/station").json()
        assert station["live"] is True
        assert "gis" in station
        assert "session" in station
        assert "positions" in station
        assert "radio" in station
        assert station["coverage"]["caption"]
        assert "altitude_bands" in station["coverage"]
        fences = client.get("/api/geofence/events").json()
        assert fences["events"] == []
        assert fences["hysteresis_leave_after"] == 2
        coverage = client.get("/api/coverage").json()
        assert coverage["points"] == []
        layers = client.get("/api/layers").json()
        assert layers["version"] == 0
        assert layers["last_good_version"] == 0
        assert layers["layers"] == []
        assert layers["errors"] == []
        types = client.get("/api/aircraft-types").json()
        assert types["types"] == []
        assert "version" in types
        assert coverage["samples"] == 0
        assert client.post("/api/coverage/reset").json()["samples"] == 0
        assert 'id="toggle-coverage"' not in page
        assert 'id="coverage-stats"' not in page
        assert client.get("/app.js").status_code == 200
        config = client.get("/api/config").json()
        assert config["station"]["lat"]
        assert config["station"]["lon"]
        assert "alt_m" not in config["station"]
        assert "estimate_max_s" not in config
        assert coverage["saved"] is True
        assert coverage.get("save_error") is None
        assert 'id="type-catalog-hint"' in page
        assert client.get("/vendor/leaflet/leaflet.css").status_code == 200
        assert 'id="diagnostics-download"' in page
        script = client.get("/app.js").text
        assert "updateArchiveMarker" not in script
        assert "value >= 5000 ? \"F\" : \"A\"" not in script
        assert "aircraftTypeCode(aircraft)" in script
        assert "latNum" in script
        assert "next_after_id" in script
        assert "/api/acars" in script
        assert "/api/radio/stream" not in script
        assert "/api/radio/stream-status" not in script
        diagnostics = client.get("/api/station/diagnostics").json()
        blob = str(diagnostics)
        assert "admin_token" not in blob
        assert "stream_url" not in blob
        assert diagnostics["settings"]["admin_required"] is False
        assert "python" in diagnostics["app"]
        assert "version" in diagnostics["app"]
        assert diagnostics["gis"]["last_good_version"] == 0
        assert "radio" in diagnostics
        assert "acars_frequencies_mhz" in diagnostics["settings"]
        acars = client.get("/api/acars").json()
        assert acars["messages"] == []
        cleared = client.post("/api/acars/clear").json()
        assert cleared["ok"] is True
        config = client.get("/api/config").json()
        assert config["acars"]["frequencies_mhz"] == [131.525, 131.550, 131.725, 131.825]


def test_blank_ofm_url_becomes_none() -> None:
    settings = Settings(ofm_url="  ")
    assert settings.ofm_url is None


def test_vhf_serial_aliases_radio_receiver_serial(monkeypatch) -> None:
    monkeypatch.delenv("AIRMON_RADIO_RECEIVER_SERIAL", raising=False)
    monkeypatch.setenv("VHF_SERIAL", "4242")
    settings = Settings(_env_file=None)
    assert settings.radio_receiver_serial == "4242"


def test_acars_frequencies_parse_from_env(monkeypatch) -> None:
    monkeypatch.delenv("AIRMON_ACARS_FREQUENCIES_MHZ", raising=False)
    settings = Settings(
        _env_file=None,
        acars_frequencies_mhz="131.525,131.825",
    )
    assert settings.acars_frequencies_mhz == [131.525, 131.825]


def test_station_exposes_acars_status(tmp_path) -> None:
    settings = Settings(
        layers_dir=tmp_path,
        readsb_json_path=tmp_path / "missing-aircraft.json",
        coverage_path=tmp_path / "coverage-rose.json",
        aircraft_types_path=tmp_path / "aircraft-types.json",
        radio_auto_detect=False,
        acars_udp_port=55552,
        acars_frequencies_mhz=[131.525, 131.825],
    )
    with TestClient(create_app(settings)) as client:
        station = client.get("/api/station").json()
        assert station["radio"]["enabled"] is True
        assert station["radio"]["count"] == 0
        assert station["radio"]["frequencies_mhz"] == [131.525, 131.825]
        assert "squelch_snr_db" not in station["radio"]
        assert "channels" not in station["radio"]
        assert client.get("/api/radio/stream").status_code == 404



def test_gzip_and_unknown_mbtiles_formats(tmp_path) -> None:
    import gzip

    compressed = gzip.compress(b"mvt-bytes")
    gzip_path = tmp_path / "vector.mbtiles"
    _write_mbtiles(gzip_path, "pbf", compressed)
    unknown_path = tmp_path / "photo.mbtiles"
    _write_mbtiles(unknown_path, "tiff", b"II*")
    settings = Settings(
        layers_dir=tmp_path,
        readsb_json_path=tmp_path / "missing-aircraft.json",
        coverage_path=tmp_path / "coverage-rose.json",
        aircraft_types_path=tmp_path / "aircraft-types.json",
    )
    with TestClient(create_app(settings)) as client:
        catalog = client.get("/api/layers").json()
        version = catalog["version"]
        gzip_response = client.get("/api/tiles/vector/0/0/0")
        assert gzip_response.status_code == 200
        assert gzip_response.content in {compressed, b"mvt-bytes"}
        assert gzip_response.headers.get("etag") == f'"vector-{version}-0-0-0"'
        assert any(item["id"] == "photo" for item in catalog["errors"])
        unknown = client.get("/api/tiles/photo/0/0/0")
        assert unknown.status_code == 404
        cached = client.get(
            "/api/tiles/vector/0/0/0",
            headers={"If-None-Match": f'"vector-{version}-0-0-0"'},
        )
        assert cached.status_code == 304


def _write_mbtiles(path, tile_format: str, tile: bytes) -> None:
    import sqlite3

    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE metadata (name text, value text)")
    connection.execute(
        "CREATE TABLE tiles (zoom_level int, tile_column int, tile_row int, tile_data blob)"
    )
    connection.execute("INSERT INTO metadata VALUES ('name', ?)", (path.stem,))
    connection.execute("INSERT INTO metadata VALUES ('format', ?)", (tile_format,))
    connection.execute(
        "INSERT INTO tiles VALUES (0, 0, 0, ?)",
        (tile,),
    )
    connection.commit()
    connection.close()
