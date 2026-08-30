from __future__ import annotations

from typing import Any

# Mode-S CRC polynomial x^24+x^23+…+x^12+x^10+x^3+1 (ICAO Annex 10).
_GENERATOR = 0b1111111111111010000001001

DF_LABELS = {
    0: "ACAS",
    4: "Высота Mode-S",
    5: "Squawk Mode-S",
    11: "All-call",
    16: "ACAS long",
    17: "ADS-B",
    18: "TIS-B/ADS-R",
    20: "Comm-B высота",
    21: "Comm-B squawk",
}

_TC_LABELS = (
    (1, 4, "позывной"),
    (5, 8, "позиция на земле"),
    (9, 18, "позиция в воздухе"),
    (19, 19, "скорость"),
    (20, 22, "позиция GNSS"),
    (28, 28, "состояние"),
    (29, 29, "целевое состояние"),
    (31, 31, "статус"),
)


def decode_avr(raw: str) -> dict[str, Any]:
    payload = _payload_hex(raw)
    data = bytes.fromhex(payload)
    df = data[0] >> 3
    decoded: dict[str, Any] = {
        "df": df,
        "df_label": DF_LABELS.get(df, f"DF{df}"),
        "icao": None,
        "callsign": None,
        "altitude_ft": None,
        "squawk": None,
        "adsb_type": None,
        "text": DF_LABELS.get(df, f"DF{df}"),
    }
    if df in {11, 17, 18} and len(data) >= 4:
        decoded["icao"] = data[1:4].hex()
    elif df in {0, 4, 5, 16, 20, 21} and len(data) >= 7:
        decoded["icao"] = _icao_from_ap(data)

    if df in {0, 4, 16, 20}:
        decoded["altitude_ft"] = _decode_ac13(data)
    if df in {5, 21}:
        decoded["squawk"] = _decode_id13(data)
    if df in {17, 18} and len(data) == 14:
        _decode_extended_squitter(data, decoded)

    decoded["text"] = summary_text(decoded)
    return decoded


def crc24(data: bytes) -> int:
    bits = [(byte >> shift) & 1 for byte in data for shift in range(7, -1, -1)]
    for index in range(len(bits) - 24):
        if not bits[index]:
            continue
        for offset in range(25):
            bits[index + offset] ^= (_GENERATOR >> (24 - offset)) & 1
    remainder = 0
    for bit in bits[-24:]:
        remainder = (remainder << 1) | bit
    return remainder


def _payload_hex(raw: str) -> str:
    body = raw.strip().upper()[1:-1]
    return body if raw[:1] != "@" else body[12:]


def _bit_field(data: bytes, start: int, length: int) -> int:
    value = int.from_bytes(data, "big")
    shift = len(data) * 8 - start - length + 1
    return (value >> shift) & ((1 << length) - 1)


def _icao_from_ap(data: bytes) -> str | None:
    cleared = data[:-3] + b"\x00\x00\x00"
    icao = crc24(cleared) ^ int.from_bytes(data[-3:], "big")
    if icao == 0 or icao > 0xFFFFFF:
        return None
    return f"{icao:06x}"


def _decode_ac13(data: bytes) -> int | None:
    if len(data) < 4:
        return None
    if data[3] & 0x40:
        return None
    if data[3] & 0x10:
        n_value = (
            ((data[2] & 0x1F) << 6)
            | ((data[3] & 0x80) >> 2)
            | ((data[3] & 0x20) >> 1)
            | (data[3] & 0x0F)
        )
        return 25 * n_value - 1000
    return None


def _decode_id13(data: bytes) -> str | None:
    if len(data) < 4:
        return None
    field = _bit_field(data, 20, 13)
    if field == 0:
        return None
    bits = f"{field:013b}"

    def group(*positions: int) -> int:
        total = 0
        for index, position in enumerate(positions):
            if bits[position - 1] == "1":
                total |= 1 << index
        return total

    a_digit = group(2, 4, 6)
    b_digit = group(8, 10, 12)
    c_digit = group(1, 3, 5)
    d_digit = group(9, 11, 13)
    return f"{a_digit}{b_digit}{c_digit}{d_digit}"


def _decode_extended_squitter(data: bytes, decoded: dict[str, Any]) -> None:
    type_code = data[4] >> 3
    decoded["adsb_type"] = type_code
    if 1 <= type_code <= 4:
        decoded["callsign"] = _decode_callsign(data)
    if 9 <= type_code <= 18 or 20 <= type_code <= 22:
        decoded["altitude_ft"] = _decode_ac12(data)
    label = next(
        (name for start, end, name in _TC_LABELS if start <= type_code <= end),
        f"ТС {type_code}",
    )
    decoded["df_label"] = f"{decoded['df_label']}, {label}"


def _decode_ac12(data: bytes) -> int | None:
    if len(data) < 7:
        return None
    if data[5] & 0x01:
        n_value = ((data[5] >> 1) << 4) | ((data[6] & 0xF0) >> 4)
        return 25 * n_value - 1000
    return None


def _decode_callsign(data: bytes) -> str | None:
    packed = _bit_field(data[4:11], 9, 48)
    chars: list[str] = []
    for shift in range(42, -1, -6):
        value = (packed >> shift) & 0x3F
        if 1 <= value <= 26:
            chars.append(chr(64 + value))
        elif value == 32:
            chars.append(" ")
        elif 48 <= value <= 57:
            chars.append(chr(value))
    callsign = "".join(chars).strip()
    return callsign or None


def summary_text(decoded: dict[str, Any]) -> str:
    parts = [str(decoded.get("df_label") or "Mode-S")]
    if decoded.get("altitude_ft") is not None:
        parts.append(f"{decoded['altitude_ft']} ft")
    if decoded.get("squawk"):
        parts.append(f"A{decoded['squawk']}")
    distance = decoded.get("distance_km")
    if isinstance(distance, int | float):
        parts.append(f"{distance:.1f} км" if distance < 10 else f"{round(distance)} км")
    return " · ".join(parts)
