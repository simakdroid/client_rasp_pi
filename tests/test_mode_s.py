from app.mode_s import crc24, decode_avr, NO_DATA


def test_adsb_airborne_position() -> None:
    decoded = decode_avr("*8D40621D58C382D690C8AC2863A7;")
    assert decoded["df"] == 17
    assert decoded["icao"] == "40621d"
    assert decoded["adsb_type"] == 11
    assert decoded["altitude_ft"] == 38000
    assert "позиция в воздухе" in decoded["df_label"]
    assert crc24(bytes.fromhex("8D40621D58C382D690C8AC2863A7")) == 0


def test_adsb_identification_callsign() -> None:
    decoded = decode_avr("*8D4840D6202CC371C32CE0576098;")
    assert decoded["df"] == 17
    assert decoded["icao"] == "4840d6"
    assert decoded["adsb_type"] == 4
    assert decoded["callsign"] == "KLM1023"
    assert crc24(bytes.fromhex("8D4840D6202CC371C32CE0576098")) == 0


def test_adsb_velocity() -> None:
    decoded = decode_avr("*8DA05F219B06B6AF189400CBC33F;")
    assert decoded["df"] == 17
    assert decoded["icao"] == "a05f21"
    assert decoded["adsb_type"] == 19
    assert "скорость" in decoded["df_label"]
    assert crc24(bytes.fromhex("8DA05F219B06B6AF189400CBC33F")) == 0


def test_mode_s_altitude_reply_recovers_icao() -> None:
    n_value = 1040
    head = bytearray(4)
    head[0] = 4 << 3
    head[2] = (n_value >> 6) & 0x1F
    head[3] = 0x10
    if n_value & 32:
        head[3] |= 0x80
    if n_value & 16:
        head[3] |= 0x20
    head[3] |= n_value & 0x0F
    icao = 0xABC123
    parity = (crc24(bytes(head) + b"\x00\x00\x00") ^ icao).to_bytes(3, "big")
    decoded = decode_avr(f"*{bytes(head + parity).hex().upper()};")
    assert decoded["df"] == 4
    assert decoded["icao"] == "abc123"
    assert decoded["altitude_ft"] == 25000


def _altitude_bytes(n_value: int) -> tuple[int, int]:
    low = 0x10
    if n_value & 32:
        low |= 0x80
    if n_value & 16:
        low |= 0x20
    low |= n_value & 0x0F
    return (n_value >> 6) & 0x1F, low


def _finish_mode_s(body: bytearray, icao: int) -> str:
    parity = (crc24(bytes(body) + b"\x00\x00\x00") ^ icao).to_bytes(3, "big")
    return f"*{bytes(body + parity).hex().upper()};"


def test_acas_short_reply_decodes_header() -> None:
    high, low = _altitude_bytes(1040)
    sl, ri, vs, cc = 4, 3, 0, 1
    body = bytearray(4)
    body[0] = (vs << 2) | (cc << 1)
    body[1] = (sl << 5) | ((ri >> 1) & 0x07)
    body[2] = ((ri & 1) << 7) | high
    body[3] = low
    decoded = decode_avr(_finish_mode_s(body, 0xABC123))
    assert decoded["df"] == 0
    assert decoded["icao"] == "abc123"
    assert decoded["altitude_ft"] == 25000
    assert decoded["acas_vs"] == "в воздухе"
    assert decoded["acas_sl"] == "уровень 4"
    assert decoded["acas_ri"] == "только вертикальный RA"
    assert decoded["acas_cc"] == "есть"
    assert decoded["acas_ra"] == NO_DATA
    assert decoded["acas_threat"] == NO_DATA
    assert f"высота: {NO_DATA}" not in decoded["text"]
    assert f"RA {NO_DATA}" in decoded["text"]


def test_acas_long_reply_decodes_ra() -> None:
    high, low = _altitude_bytes(1040)
    sl, ri = 3, 2
    body = bytearray(11)
    body[0] = (16 << 3)
    body[1] = (sl << 5) | ((ri >> 1) & 0x07)
    body[2] = ((ri & 1) << 7) | high
    body[3] = low
    body[4] = 0x30
    # ARA bit9=1, corrective, upward, positive RA
    body[5] = 0b11000010
    decoded = decode_avr(_finish_mode_s(body, 0xABCDEF))
    assert decoded["df"] == 16
    assert decoded["acas_sl"] == "уровень 3"
    assert decoded["acas_ri"] == "RA запрещены"
    assert decoded["acas_cc"] == NO_DATA
    assert "одна угроза" in decoded["acas_ra"]
    assert "набор" in decoded["acas_ra"]
    assert decoded["acas_rat"] == "активен"
    assert decoded["acas_rac"] == NO_DATA
    assert decoded["acas_threat"] == NO_DATA


def test_adsb_acas_ra_broadcast() -> None:
    body = bytearray(11)
    body[0] = (17 << 3) | 5
    body[1:4] = bytes.fromhex("40621d")
    icao = 0xABC123
    body[4] = (28 << 3) | 2
    body[5] = 0b11000010
    body[6] = 0x00
    body[7] = 0x04 | ((icao >> 22) & 0x03)
    body[8] = (icao >> 14) & 0xFF
    body[9] = (icao >> 6) & 0xFF
    body[10] = (icao & 0x3F) << 2
    decoded = decode_avr(f"*{bytes(body).hex().upper()}000000;")
    assert decoded["df"] == 17
    assert decoded["adsb_type"] == 28
    assert "ACAS RA" in decoded["df_label"]
    assert decoded["acas_vs"] == NO_DATA
    assert "корректирующий" in decoded["acas_ra"]
    assert decoded["acas_threat"] == "abc123"
    assert decoded["acas_rat"] == "активен"
