import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.adsb import ReadsbJsonSource, parse_readsb_aircraft, parse_sbs_line
from app.config import Settings
from app.models import AircraftUpdate


def test_parse_readsb_aircraft() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    update = parse_readsb_aircraft(
        {
            "hex": "ABC123",
            "flight": " TEST42 ",
            "lat": 55.7,
            "lon": 37.6,
            "alt_baro": 10000,
            "gs": 420.5,
            "track": 361,
            "t": "b738",
            "desc": "Boeing 737-800",
            "category": "A3",
            "seen": 0.5,
        },
        now,
        ttl_s=60,
    )
    assert update is not None
    assert update.icao == "abc123"
    assert update.callsign == "TEST42"
    assert update.track_deg == 1
    assert update.type_code == "B738"
    assert update.type_desc == "Boeing 737-800"
    assert update.category == "A3"
    assert update.on_ground is False
    assert update.altitude_ft == 10000
    assert update.altitude_reference == "baro"
    assert update.received_at == now - timedelta(seconds=0.5)
    assert update.position_at == now - timedelta(seconds=0.5)


def test_parse_readsb_uses_seen_pos_for_position_time() -> None:
    now = datetime(2026, 9, 10, 12, tzinfo=UTC)
    update = parse_readsb_aircraft(
        {
            "hex": "abc123",
            "lat": 55.7,
            "lon": 37.6,
            "seen": 0.2,
            "seen_pos": 12,
        },
        now,
        ttl_s=60,
    )
    assert update is not None
    assert update.received_at == now - timedelta(seconds=0.2)
    assert update.position_at == now - timedelta(seconds=12)


def test_parse_readsb_keeps_heading_out_of_ground_track() -> None:
    update = parse_readsb_aircraft(
        {
            "hex": "abc123",
            "true_heading": 90,
            "geom_rate": -512,
            "alt_geom": 18000,
            "seen": 0.1,
        },
        datetime(2026, 1, 1, tzinfo=UTC),
        ttl_s=60,
    )
    assert update is not None
    assert update.track_deg is None
    assert update.true_heading_deg == 90
    assert update.altitude_ft == 18000
    assert update.altitude_reference == "geom"
    assert update.vertical_rate_fpm == -512
    assert update.vertical_rate_source == "geom"


def test_parse_readsb_prefers_baro_altitude_and_rate() -> None:
    update = parse_readsb_aircraft(
        {
            "hex": "abc123",
            "alt_baro": 12000,
            "alt_geom": 12100,
            "baro_rate": 640,
            "geom_rate": 700,
            "track": 10,
            "true_heading": 12,
            "seen": 0.1,
        },
        datetime(2026, 1, 1, tzinfo=UTC),
        ttl_s=60,
    )
    assert update is not None
    assert update.altitude_ft == 12000
    assert update.altitude_reference == "baro"
    assert update.vertical_rate_fpm == 640
    assert update.vertical_rate_source == "baro"
    assert update.track_deg == 10
    assert update.true_heading_deg == 12


def test_parse_readsb_drops_stale_position() -> None:
    update = parse_readsb_aircraft(
        {
            "hex": "abc123",
            "lat": 55.7,
            "lon": 37.6,
            "seen": 1,
            "seen_pos": 90,
        },
        datetime.now(UTC),
        ttl_s=60,
    )
    assert update is not None
    assert update.lat is None
    assert update.lon is None
    assert update.position_at is None


def test_parse_readsb_unknown_ground_state_stays_unknown() -> None:
    update = parse_readsb_aircraft(
        {"hex": "abc123", "seen": 0.5},
        datetime(2026, 1, 1, tzinfo=UTC),
        ttl_s=60,
    )
    assert update is not None
    assert update.on_ground is None
    assert update.altitude_ft is None


def test_parse_readsb_ignores_mode_s_emitter_type() -> None:
    update = parse_readsb_aircraft(
        {
            "hex": "1558c4",
            "flight": "SDM6223",
            "type": "mode_s",
            "alt_baro": 1475,
            "squawk": "2144",
            "seen": 0.5,
        },
        datetime(2026, 1, 1, tzinfo=UTC),
        ttl_s=60,
    )
    assert update is not None
    assert update.type_code is None
    assert update.callsign == "SDM6223"


def test_parse_stale_readsb_aircraft() -> None:
    assert (
        parse_readsb_aircraft(
            {"hex": "abc123", "seen": 61},
            datetime.now(UTC),
            ttl_s=60,
        )
        is None
    )


def test_parse_readsb_rejects_non_icao_tilde_address() -> None:
    assert (
        parse_readsb_aircraft(
            {"hex": "~abc123", "seen": 0.1},
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
    assert update.received_at.microsecond == 0


def test_parse_sbs_keeps_fractional_seconds() -> None:
    line = (
        "MSG,3,1,1,ABC123,1,2026/08/27,10:00:00.250,"
        "2026/08/27,10:00:00.250,CALL42,12000,350,90,"
        "55.75,37.61,640,7700,0,0,0,0"
    )
    update = parse_sbs_line(line)
    assert update is not None
    assert update.received_at.microsecond == 250000


def test_parse_sbs_interprets_timestamp_in_named_timezone() -> None:
    line = (
        "MSG,3,1,1,ABC123,1,2026/08/27,10:00:00.000,"
        "2026/08/27,10:00:00.000,CALL42,12000,350,90,"
        "55.75,37.61,640,7700,0,0,0,0"
    )
    update = parse_sbs_line(line, timezone="Europe/Moscow")
    assert update is not None
    assert update.received_at == datetime(2026, 8, 27, 7, 0, tzinfo=UTC)
    assert update.altitude_reference == "baro"
    assert update.vertical_rate_source == "baro"


def test_unknown_sbs_timezone_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(sbs_timezone="Not/AZone")


def test_parse_sbs_bad_line_does_not_raise() -> None:
    assert parse_sbs_line("not-a-message") is None
    assert parse_sbs_line("MSG,3,1,1,ZZZZZZ," + "," * 30) is None
    huge = "MSG,3,1,1,ABC123,1,2026/08/27,10:00:00," + ("x" * 8000)
    assert parse_sbs_line(huge) is None


def test_aircraft_update_rejects_invalid_identity_and_coords() -> None:
    with pytest.raises(ValidationError):
        AircraftUpdate(icao="nota12")
    with pytest.raises(ValidationError):
        AircraftUpdate(icao="~abc123")
    update = AircraftUpdate(icao="abc123", lat=55.0)
    assert update.lat is None
    assert update.lon is None


@pytest.mark.asyncio
async def test_readsb_source_skips_unchanged_and_isolates_bad_records(tmp_path) -> None:
    path = tmp_path / "aircraft.json"
    now = datetime.now(UTC).timestamp()
    path.write_text(
        json.dumps(
            {
                "now": now,
                "aircraft": [
                    {"hex": "abc123", "lat": 55.1, "lon": 37.1, "seen": 0.1, "seen_pos": 0.1},
                    "broken",
                    {"hex": "nothex"},
                    {"hex": "def456", "lat": 55.2, "lon": 37.2, "seen": 0.1, "seen_pos": 0.1},
                ],
            }
        ),
        encoding="utf-8",
    )
    source = ReadsbJsonSource(path, interval_s=0.02, ttl_s=60)
    batches: list[list] = []

    async def collect() -> None:
        async for batch in source.updates():
            batches.append(batch)
            if len(batches) >= 3:
                return

    await asyncio.wait_for(collect(), timeout=2)
    icaos = {item.icao for item in batches[0]}
    assert icaos == {"abc123", "def456"}
    assert batches[1] == []
    assert batches[2] == []


@pytest.mark.asyncio
async def test_readsb_source_does_not_apply_stale_dump(tmp_path) -> None:
    path = tmp_path / "aircraft.json"
    path.write_text(
        json.dumps(
            {
                "now": datetime(2020, 1, 1, tzinfo=UTC).timestamp(),
                "aircraft": [
                    {"hex": "abc123", "lat": 55.1, "lon": 37.1, "seen": 0.1},
                ],
            }
        ),
        encoding="utf-8",
    )
    source = ReadsbJsonSource(path, interval_s=0.02, ttl_s=60)
    batches: list[list] = []

    async def collect() -> None:
        async for batch in source.updates():
            batches.append(batch)
            if batches:
                return

    await asyncio.wait_for(collect(), timeout=2)
    assert batches[0] == []
    assert source.health()["status"] == "stale"
