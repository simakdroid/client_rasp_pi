import asyncio
import json

import pytest

from app.acars import AcarsLog, ingest_acars_udp, parse_acars_payload


def test_parse_acarsdec_json() -> None:
    parsed = parse_acars_payload(
        json.dumps(
            {
                "timestamp": 1_700_000_000,
                "flight": "AFL123",
                "tail": "RA-89000",
                "label": "H1",
                "text": "ARR TYU",
                "freq": 131.725,
                "error": 0,
                "level": -18.5,
                "mode": "2",
                "block_id": "3",
                "msgno": "M01A",
            }
        )
    )
    assert parsed is not None
    assert parsed["flight"] == "AFL123"
    assert parsed["tail"] == "RA-89000"
    assert parsed["label"] == "H1"
    assert parsed["frequency_mhz"] == 131.725
    assert parsed["summary"].startswith("AFL123")


def test_parse_acars_frequency_in_khz() -> None:
    parsed = parse_acars_payload('{"freq": 131825, "text": "OK"}')
    assert parsed is not None
    assert parsed["frequency_mhz"] == 131.825


def test_parse_acars_ignores_non_json() -> None:
    assert parse_acars_payload("not json") is None
    assert parse_acars_payload("[]") is None
    assert parse_acars_payload("   ") is None


@pytest.mark.asyncio
async def test_acars_log_is_incremental_and_bounded() -> None:
    message_log = AcarsLog(max_messages=2)
    await message_log.append_datagram(b'{"flight":"SBI1","label":"Q0"}')
    first = await message_log.recent()
    await message_log.append_datagram(b'{"flight":"SBI2","label":"Q0"}')
    await message_log.append_datagram(b'{"flight":"SBI3","label":"H1","text":"HI"}')

    recent = await message_log.recent(after_id=first["last_id"])
    assert [item["flight"] for item in recent["messages"]] == ["SBI2", "SBI3"]
    assert recent["last_id"] == 3
    newest = await message_log.recent(limit=1, newest_first=True)
    assert newest["messages"][0]["flight"] == "SBI3"
    assert newest["total"] == 2
    stats = await message_log.stats()
    assert stats["count"] == 2
    assert stats["last_id"] == 3
    cleared = await message_log.clear()
    assert cleared["ok"] is True
    assert (await message_log.recent())["messages"] == []


@pytest.mark.asyncio
async def test_ingest_acars_udp_appends_json() -> None:
    message_log = AcarsLog()
    port = 55551
    task = asyncio.create_task(ingest_acars_udp("127.0.0.1", port, message_log))
    try:
        for _ in range(50):
            await asyncio.sleep(0.01)
            # Wait until the endpoint is listening before sending.
            try:
                transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                    asyncio.DatagramProtocol, remote_addr=("127.0.0.1", port)
                )
            except OSError:
                continue
            transport.sendto(b'{"flight":"UAE12","label":"H1","text":"HELLO"}')
            transport.close()
            break
        else:
            raise AssertionError("ACARS UDP ingest did not bind")
        for _ in range(50):
            if (await message_log.stats())["count"] == 1:
                break
            await asyncio.sleep(0.02)
        recent = await message_log.recent()
        assert recent["messages"][0]["flight"] == "UAE12"
        assert recent["messages"][0]["text"] == "HELLO"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
