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
    (28, 28, "состояние / ACAS RA"),
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
    if df in {0, 16} or (df in {17, 18} and decoded.get("adsb_type") == 28):
        _decode_acas(data, decoded)

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
    if type_code == 28 and (data[4] & 0x07) == 2:
        label = "ACAS RA"
    else:
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


NO_DATA = "нет данных"

_RI_LABELS = {
    0: "нет ACAS",
    2: "RA запрещены",
    3: "только вертикальный RA",
    4: "вертикальный и горизонтальный RA",
    7: "вертикальный и горизонтальный RA",
    8: "макс. скорость неизвестна",
    9: "≤75 уз",
    10: "≤150 уз",
    11: "≤300 уз",
    12: "≤600 уз",
    13: "≤1200 уз",
    14: ">1200 уз",
}


def _decode_acas(data: bytes, decoded: dict[str, Any]) -> None:
    df = decoded["df"]
    if df in {0, 16}:
        if len(data) < 4:
            decoded.update(_empty_acas_header())
        else:
            sl = _bit_field(data, 9, 3)
            ri = _bit_field(data, 14, 4)
            decoded["acas_vs"] = "на земле" if _bit_field(data, 6, 1) else "в воздухе"
            decoded["acas_sl"] = "не работает" if sl == 0 else f"уровень {sl}"
            decoded["acas_ri"] = _RI_LABELS.get(ri, NO_DATA)
            if df == 0:
                decoded["acas_cc"] = "есть" if _bit_field(data, 7, 1) else "нет"
            else:
                decoded["acas_cc"] = NO_DATA
        if df == 16 and len(data) >= 11 and data[4] == 0x30:
            _decode_ra_block(data[4:11], decoded, with_threat=False)
        else:
            decoded["acas_ra"] = NO_DATA
            decoded["acas_rac"] = NO_DATA
            decoded["acas_rat"] = NO_DATA
            decoded["acas_threat"] = NO_DATA
        return

    if len(data) >= 11 and (data[4] & 0x07) == 2:
        decoded.update(_empty_acas_header())
        _decode_ra_block(data[4:11], decoded, with_threat=True)
        return
    decoded.update(
        {
            "acas_vs": NO_DATA,
            "acas_sl": NO_DATA,
            "acas_ri": NO_DATA,
            "acas_cc": NO_DATA,
            "acas_ra": NO_DATA,
            "acas_rac": NO_DATA,
            "acas_rat": NO_DATA,
            "acas_threat": NO_DATA,
        }
    )


def _empty_acas_header() -> dict[str, str]:
    return {
        "acas_vs": NO_DATA,
        "acas_sl": NO_DATA,
        "acas_ri": NO_DATA,
        "acas_cc": NO_DATA,
    }


def _decode_ra_block(mb: bytes, decoded: dict[str, Any], with_threat: bool) -> None:
    if len(mb) < 7:
        decoded["acas_ra"] = NO_DATA
        decoded["acas_rac"] = NO_DATA
        decoded["acas_rat"] = NO_DATA
        decoded["acas_threat"] = NO_DATA
        return
    ara0 = _bit_field(mb, 9, 1)
    mte = _bit_field(mb, 28, 1)
    decoded["acas_ra"] = _ara_text(mb, ara0, mte)
    rac_parts = []
    if _bit_field(mb, 23, 1):
        rac_parts.append("не проходить снизу")
    if _bit_field(mb, 24, 1):
        rac_parts.append("не проходить сверху")
    if _bit_field(mb, 25, 1):
        rac_parts.append("не уходить влево")
    if _bit_field(mb, 26, 1):
        rac_parts.append("не уходить вправо")
    decoded["acas_rac"] = ", ".join(rac_parts) if rac_parts else NO_DATA
    decoded["acas_rat"] = "завершён" if _bit_field(mb, 27, 1) else "активен"
    decoded["acas_threat"] = _threat_text(mb) if with_threat else NO_DATA


def _ara_text(mb: bytes, ara0: int, mte: int) -> str:
    if ara0 == 0 and mte == 0:
        return "RA не сформирован"
    flags: list[str] = []
    if ara0 == 1:
        flags.append("несколько угроз" if mte else "одна угроза")
        flags.append("корректирующий" if _bit_field(mb, 10, 1) else "предупреждающий")
        flags.append("снижение" if _bit_field(mb, 11, 1) else "набор")
        if _bit_field(mb, 12, 1):
            flags.append("увеличенная скорость")
        if _bit_field(mb, 13, 1):
            flags.append("смена направления")
        if _bit_field(mb, 14, 1):
            flags.append("пересечение эшелона")
        flags.append("положительный RA" if _bit_field(mb, 15, 1) else "ограничение вертикальной скорости")
    else:
        flags.append("несколько угроз")
        if _bit_field(mb, 10, 1):
            flags.append("коррекция вверх")
        if _bit_field(mb, 11, 1):
            flags.append("набор")
        if _bit_field(mb, 12, 1):
            flags.append("коррекция вниз")
        if _bit_field(mb, 13, 1):
            flags.append("снижение")
        if _bit_field(mb, 14, 1):
            flags.append("пересечение эшелона")
        if _bit_field(mb, 15, 1):
            flags.append("смена направления")
        if not flags[1:]:
            return NO_DATA
    return ", ".join(flags)


def _threat_text(mb: bytes) -> str:
    tti = _bit_field(mb, 29, 2)
    if tti == 1:
        icao = _bit_field(mb, 31, 24)
        return f"{icao:06x}" if icao else NO_DATA
    if tti != 2:
        return NO_DATA
    altitude = _decode_ac13_field(_bit_field(mb, 31, 13))
    range_n = _bit_field(mb, 44, 7)
    bearing_n = _bit_field(mb, 51, 6)
    if range_n == 0:
        range_text = NO_DATA
    elif range_n == 127:
        range_text = ">12.55 NM"
    else:
        range_text = f"{(range_n - 1) / 10:.2f} NM"
    if bearing_n == 0 or bearing_n > 60:
        bearing_text = NO_DATA
    else:
        bearing_text = f"{6 * (bearing_n - 1)}–{6 * bearing_n}°"
    altitude_text = f"{altitude} ft" if altitude is not None else NO_DATA
    return f"{altitude_text}, {range_text}, {bearing_text}"


def _decode_ac13_field(field: int) -> int | None:
    packed = field.to_bytes(2, "big")
    fake = bytes([0, 0, packed[0], packed[1]])
    return _decode_ac13(fake)


def summary_text(decoded: dict[str, Any]) -> str:
    parts = [str(decoded.get("df_label") or "Mode-S")]
    acas = any(decoded.get(key) is not None for key in (
        "acas_vs", "acas_sl", "acas_ri", "acas_ra",
    ))
    if decoded.get("altitude_ft") is not None:
        parts.append(f"{decoded['altitude_ft']} ft")
    elif acas:
        parts.append(f"высота: {NO_DATA}")
    if decoded.get("squawk"):
        parts.append(f"A{decoded['squawk']}")
    if decoded.get("acas_vs") is not None:
        parts.append(f"вертикальный статус: {decoded['acas_vs']}")
    if decoded.get("acas_sl") is not None:
        parts.append(f"уровень чувствительности: {decoded['acas_sl']}")
    if decoded.get("acas_ri") is not None:
        parts.append(f"сведения ответа: {decoded['acas_ri']}")
    if decoded.get("acas_cc") is not None and decoded.get("df") == 0:
        parts.append(f"перекрёстная связь: {decoded['acas_cc']}")
    if decoded.get("acas_ra") is not None:
        parts.append(f"рекомендация по разрешению: {decoded['acas_ra']}")
    if decoded.get("acas_rac") is not None:
        parts.append(f"дополнение к рекомендации: {decoded['acas_rac']}")
    if decoded.get("acas_rat") is not None:
        parts.append(f"признак завершения рекомендации: {decoded['acas_rat']}")
    if decoded.get("acas_threat") is not None:
        parts.append(f"угроза {decoded['acas_threat']}")
    distance = decoded.get("distance_km")
    if isinstance(distance, int | float):
        parts.append(f"{distance:.1f} км" if distance < 10 else f"{round(distance)} км")
    return " · ".join(parts)
