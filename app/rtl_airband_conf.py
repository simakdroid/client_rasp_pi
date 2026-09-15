"""Build rtl_airband.conf from AIRMON_RADIO_CHANNELS_JSON in backend.env."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from pydantic import ValidationError

from .config import RadioChannel, vhf_mountpoint

SAMPLE_RATE_MHZ = 2.56
STATS_PATH = "/run/rtl-airband/stats.prom"
DEFAULT_BACKEND_ENV = Path("/etc/adsb-vhf/backend.env")
DEFAULT_OUTPUT = Path("/run/rtl-airband/rtl_airband.conf")
MAX_CHANNELS = 8


def vhf_mountpoint_from_channel(channel: RadioChannel) -> str:
    return (channel.mountpoint or "").strip() or vhf_mountpoint(channel.frequency_mhz)


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip().lstrip("\ufeff")
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip().strip("\r")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def load_channels_json(
    *,
    channels_json: str | None = None,
    backend_env: Path | None = None,
) -> str:
    text = (channels_json or "").strip()
    if text:
        return text
    path = backend_env or Path(os.environ.get("AIRMON_BACKEND_ENV", str(DEFAULT_BACKEND_ENV)))
    parsed = parse_env_file(path)
    return parsed.get("AIRMON_RADIO_CHANNELS_JSON", "[]")


def parse_channels(channels_json: str) -> list[RadioChannel]:
    try:
        payload = json.loads(channels_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"AIRMON_RADIO_CHANNELS_JSON is not valid JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise ValueError("AIRMON_RADIO_CHANNELS_JSON must contain a JSON array")
    if not payload:
        raise ValueError("AIRMON_RADIO_CHANNELS_JSON must list at least one channel")
    if len(payload) > MAX_CHANNELS:
        raise ValueError(f"AIRMON_RADIO_CHANNELS_JSON supports at most {MAX_CHANNELS} channels")
    channels = [RadioChannel.model_validate(item) for item in payload]
    ids = [channel.id for channel in channels]
    if len(set(ids)) != len(ids):
        raise ValueError("channel id values must be unique")
    freqs = [channel.frequency_mhz for channel in channels]
    if len(set(freqs)) != len(freqs):
        raise ValueError("channel frequencies must be unique")
    return channels


def tuner_center_mhz(frequencies: list[float], sample_rate: float = SAMPLE_RATE_MHZ) -> float:
    lowest = min(frequencies)
    highest = max(frequencies)
    center = round((lowest + highest) / 2, 3)
    half = sample_rate / 2
    for frequency in frequencies:
        if abs(frequency - center) > half:
            raise ValueError(
                f"{frequency} MHz does not fit in a {sample_rate:g} MHz RTL-SDR window "
                f"centered at {center} MHz; keep all VHF channels within {sample_rate:g} MHz"
            )
    return center


def libconfig_string(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError("libconfig strings cannot contain newlines")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _strip(value: str | None, default: str) -> str:
    text = default if value is None else value
    return text.replace("\r", "").strip()


def render_conf(
    *,
    channels_json: str,
    vhf_serial: str = "0118",
    icecast_host: str = "127.0.0.1",
    icecast_port: str = "8000",
    icecast_user: str = "source",
    icecast_password: str = "",
    sample_rate: float = SAMPLE_RATE_MHZ,
) -> str:
    channels = parse_channels(channels_json)
    serial = _strip(vhf_serial, "0118") or "0118"
    host = _strip(icecast_host, "127.0.0.1") or "127.0.0.1"
    port = _strip(icecast_port, "8000") or "8000"
    user = _strip(icecast_user, "source") or "source"
    password = _strip(icecast_password, "")
    if not port.isdigit():
        raise ValueError(f"ICECAST_PORT must be an integer, got: [{port}]")
    center = tuner_center_mhz([channel.frequency_mhz for channel in channels], sample_rate)
    blocks = [
        _channel_block(channel, host, port, user, password) for channel in channels
    ]
    joined = ",\n".join(blocks)
    return (
        "# Generated from AIRMON_RADIO_CHANNELS_JSON in /etc/adsb-vhf/backend.env.\n"
        f'stats_filepath = "{STATS_PATH}";\n'
        "\n"
        "devices:\n"
        "(\n"
        "  {\n"
        '    type = "rtlsdr";\n'
        f"    serial = {libconfig_string(serial)};\n"
        "    gain = 28;\n"
        "    correction = 0;\n"
        '    mode = "multichannel";\n'
        f"    sample_rate = {sample_rate:g};\n"
        f"    centerfreq = {center:.3f};\n"
        "\n"
        "    channels:\n"
        "    (\n"
        f"{joined}\n"
        "    );\n"
        "  }\n"
        ");\n"
    )


def _channel_block(
    channel: RadioChannel,
    host: str,
    port: str,
    user: str,
    password: str,
) -> str:
    mount = vhf_mountpoint_from_channel(channel)
    name = channel.name.strip() or f"VHF AM {channel.frequency_mhz:.3f} MHz"
    return (
        "      {\n"
        f"        freq = {channel.frequency_mhz:.3f};\n"
        '        modulation = "am";\n'
        "        squelch_snr_threshold = 8.0;\n"
        "        outputs:\n"
        "        (\n"
        "          {\n"
        '            type = "icecast";\n'
        f"            server = {libconfig_string(host)};\n"
        f"            port = {port};\n"
        f"            mountpoint = {libconfig_string(mount)};\n"
        f"            name = {libconfig_string(name)};\n"
        '            genre = "Aviation";\n'
        f"            username = {libconfig_string(user)};\n"
        f"            password = {libconfig_string(password)};\n"
        "          }\n"
        "        );\n"
        "      }"
    )


def write_output(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    try:
        import grp
        import pwd

        uid = pwd.getpwnam("rtl-airband").pw_uid
        gid = grp.getgrnam("rtl-airband").gr_gid
        os.chown(path, uid, gid)
    except (ImportError, KeyError, OSError, PermissionError):
        pass


def main(argv: list[str] | None = None) -> int:
    del argv
    try:
        text = render_conf(
            channels_json=load_channels_json(),
            vhf_serial=os.environ.get("VHF_SERIAL", "0118"),
            icecast_host=os.environ.get("ICECAST_HOST", "127.0.0.1"),
            icecast_port=os.environ.get("ICECAST_PORT", "8000"),
            icecast_user=os.environ.get("ICECAST_USER", "source"),
            icecast_password=os.environ.get("ICECAST_PASSWORD", ""),
        )
        output = Path(os.environ.get("RTL_AIRBAND_OUTPUT", str(DEFAULT_OUTPUT)))
        write_output(output, text)
    except (OSError, ValueError, ValidationError) as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
