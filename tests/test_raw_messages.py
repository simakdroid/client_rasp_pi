import pytest

from app.raw_messages import RawMessageLog, normalize_avr_message


def test_normalize_avr_message() -> None:
    assert normalize_avr_message("*8D40621D58C382D690C8AC2863A7;\r\n") == (
        "*8D40621D58C382D690C8AC2863A7;"
    )
    assert normalize_avr_message("*0000;") is None
    assert normalize_avr_message("*NOT-HEX;") is None


@pytest.mark.asyncio
async def test_raw_message_log_is_incremental_and_bounded() -> None:
    message_log = RawMessageLog(max_messages=2)
    await message_log.append("*8D40621D58C382D690C8AC2863A7;")
    first = await message_log.recent()
    await message_log.append("*8D4840D6202CC371C32CE0576098;")
    await message_log.append("*8DA05F219B06B6AF189400CBC33F;")

    recent = await message_log.recent(after_id=first["last_id"])

    assert len(recent["messages"]) == 2
    assert recent["last_id"] == 3
    newest = await message_log.recent(limit=1, newest_first=True)
    assert newest["messages"][0]["raw"] == "*8DA05F219B06B6AF189400CBC33F;"
    assert newest["messages"][0]["icao"] == "a05f21"
    assert newest["messages"][0]["df"] == 17
    assert newest["total"] == 2
    assert newest["has_more"] is True
    older = await message_log.recent(
        before_id=newest["messages"][0]["id"], limit=1, newest_first=True
    )
    assert older["messages"][0]["raw"] == "*8D4840D6202CC371C32CE0576098;"
    assert older["has_more"] is False
