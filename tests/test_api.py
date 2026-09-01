from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_ui_and_api_are_served(tmp_path) -> None:
    settings = Settings(
        layers_dir=tmp_path,
        readsb_json_path=tmp_path / "missing-aircraft.json",
        coverage_path=tmp_path / "coverage-rose.json",
        aircraft_types_path=tmp_path / "aircraft-types.json",
        sessions_dir=tmp_path / "sessions",
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
        assert "Начало контакта" in page
        assert "Потеря контакта" in page
        assert 'id="panel-journal"' in page
        assert 'data-journal-mode="raw"' in page
        assert 'id="journal-list"' in page
        assert 'id="journal-pager"' not in page
        assert 'id="archive-section"' in page
        assert 'id="aircraft-list"' in page
        assert 'id="archive-list"' in page
        assert 'id="panel-types"' in page
        assert 'data-tab="types"' in page
        assert 'id="type-catalog-form"' in page
        assert client.get("/api/aircraft-types").json()["types"] == []
        assert "pane-scroll" in page
        assert "aircraft-card__squawk" in page
        assert "aircraft-card__type" in page
        assert "Время записей — UTC" in page
        assert client.get("/api/aircraft").json()["archived"] == []
        assert client.get("/api/sessions").json() == {"keep_days": 7, "sessions": []}
        assert 'data-tab="sessions"' in page
        assert 'id="panel-sessions"' in page
        assert 'id="toggle-sessions"' in page
        coverage = client.get("/api/coverage").json()
        assert coverage["points"] == []
        assert coverage["samples"] == 0
        assert client.post("/api/coverage/reset").json()["samples"] == 0
        assert 'id="toggle-coverage"' in page
        assert 'id="coverage-stats"' in page
        assert client.get("/app.js").status_code == 200
        assert client.get("/vendor/leaflet/leaflet.css").status_code == 200
