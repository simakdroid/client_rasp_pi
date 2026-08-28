from datetime import UTC, datetime

from app.adsb import parse_readsb_aircraft, parse_sbs_line


def test_parse_readsb_aircraft() -> None:
    update = parse_readsb_aircraft(
        {
            "hex": "ABC123",
            "flight": " TEST42 ",
            "lat": 55.7,
            "lon": 37.6,
            "alt_baro": 10000,
            "gs": 420.5,
            "track": 361,
            "seen": 0.5,
        },
        datetime(2026, 1, 1, tzinfo=UTC),
        ttl_s=60,
    )
    assert update is not None
    assert update.icao == "abc123"
    assert update.callsign == "TEST42"
    assert update.track_deg == 1


def test_parse_stale_readsb_aircraft() -> None:
    assert (
        parse_readsb_aircraft(
            {"hex": "abc123", "seen": 61},
            datetime.now(UTC),
            ttl_s=60,
        )
        is None
    )


def test_parse_sbs_position_message() -> None:
    line = (
        "MSG,3,1,1,ABC123,1,2026/08/27,10:00:00.000,"
        "2026/08/27,10:00:00.000,CALL42,12000,350,90,"
        "55.75,37.61,640,7700,0,0,0,0"
    )
    update = parse_sbs_line(line)
    assert update is not None
    assert update.icao == "abc123"
    assert update.lat == 55.75
    assert update.vertical_rate_fpm == 640
