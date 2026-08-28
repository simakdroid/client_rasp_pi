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
        assert client.get("/api/adsb/messages").json()["events"] == []
        assert client.get("/api/adsb/raw").json()["messages"] == []
        paged = client.get("/api/adsb/messages", params={"newest_first": True, "limit": 1})
        assert paged.status_code == 200
        assert paged.json()["has_more"] is False
        assert paged.json()["total"] == 0
        page = client.get("/").text
        assert "Авиационный монитор" in page
        assert 'id="receiver-card"' not in page
        assert 'id="clock"' in page
        assert "aircraft-card__flight" in page
        assert 'id="panel-journal"' in page
        assert 'data-journal-mode="raw"' in page
        assert 'id="journal-pager"' in page
        assert 'id="archive-section"' in page
        assert "aircraft-card__squawk" in page
        assert "Время записей — UTC" in page
        assert client.get("/api/aircraft").json()["archived"] == []
        assert client.get("/app.js").status_code == 200
        assert client.get("/vendor/leaflet/leaflet.css").status_code == 200
