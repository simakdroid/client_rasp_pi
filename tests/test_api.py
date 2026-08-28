from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_ui_and_api_are_served(tmp_path) -> None:
    settings = Settings(
        layers_dir=tmp_path,
        readsb_json_path=tmp_path / "missing-aircraft.json",
        radio_channels_json="[]",
    )
    with TestClient(create_app(settings)) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["adsb"]["status"] == "unavailable"
        assert client.get("/api/aircraft").json()["aircraft"] == []
        page = client.get("/").text
        assert "Авиационный монитор" in page
        assert 'id="receiver-card"' in page
        assert client.get("/app.js").status_code == 200
        assert client.get("/vendor/leaflet/leaflet.css").status_code == 200
