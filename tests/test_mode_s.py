from app.mode_s import crc24, decode_avr


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
