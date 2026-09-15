"""Build rtl_airband.conf from AIRMON_RADIO_CHANNELS_JSON in backend.env."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from pydantic import ValidationError

from .config import SCAN_MOUNTPOINT, RadioChannel

STATS_PATH = "/run/rtl-airband/stats.prom"
DEFAULT_BACKEND_ENV = Path("/etc/adsb-vhf/backend.env")
DEFAULT_OUTPUT = Path("/run/rtl-airband/rtl_airband.conf")
MAX_CHANNELS = 32


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
    channels_path: Path | None = None,
) -> str:
    text = (channels_json or "").strip()
    if text:
        return text
    file_path = channels_path
    if file_path is None and backend_env is None:
        file_path = Path(
            os.environ.get("AIRMON_RADIO_CHANNELS_PATH", "/var/lib/adsb-vhf/radio-channels.json")
        )
    if file_path is not None and file_path.is_file():
        return file_path.read_text(encoding="utf-8")
    env_path = backend_env or Path(os.environ.get("AIRMON_BACKEND_ENV", str(DEFAULT_BACKEND_ENV)))
    parsed = parse_env_file(env_path)
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


def libconfig_string(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError("libconfig strings cannot contain newlines")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _strip(value: str | None, default: str) -> str:
    text = default if value is None else value
    return text.replace("\r", "").strip()


def _freq_list(channels: list[RadioChannel]) -> str:
    return ", ".join(f"{channel.frequency_mhz:.3f}" for channel in channels)


def _label_list(channels: list[RadioChannel]) -> str:
    labels = []
    for channel in channels:
        name = channel.name.strip() or f"{channel.frequency_mhz:.3f} MHz"
        labels.append(libconfig_string(name))
    return ", ".join(labels)


def render_conf(
    *,
    channels_json: str,
    vhf_serial: str = "0118",
    icecast_host: str = "127.0.0.1",
    icecast_port: str = "8000",
    icecast_user: str = "source",
    icecast_password: str = "",
) -> str:
    channels = parse_channels(channels_json)
    serial = _strip(vhf_serial, "0118") or "0118"
    host = _strip(icecast_host, "127.0.0.1") or "127.0.0.1"
    port = _strip(icecast_port, "8000") or "8000"
    user = _strip(icecast_user, "source") or "source"
    password = _strip(icecast_password, "")
    if not port.isdigit():
        raise ValueError(f"ICECAST_PORT must be an integer, got: [{port}]")
    names = " + ".join(channel.name.strip() or channel.id for channel in channels)
    return (
        "# Generated from AIRMON_RADIO_CHANNELS_JSON in /etc/adsb-vhf/backend.env.\n"
        "# Scan mode retunes the dongle, so frequencies need not fit in 2.56 MHz.\n"
        f'stats_filepath = "{STATS_PATH}";\n'
        "\n"
        "devices:\n"
        "(\n"
        "  {\n"
        '    type = "rtlsdr";\n'
        f"    serial = {libconfig_string(serial)};\n"
        "    gain = 28;\n"
        "    correction = 0;\n"
        '    mode = "scan";\n'
        "    channels:\n"
        "    (\n"
        "      {\n"
        f"        freqs = ( {_freq_list(channels)} );\n"
        f"        labels = ( {_label_list(channels)} );\n"
        '        modulation = "am";\n'
        "        squelch_snr_threshold = 8.0;\n"
        "        outputs:\n"
        "        (\n"
        "          {\n"
        '            type = "icecast";\n'
        f"            server = {libconfig_string(host)};\n"
        f"            port = {port};\n"
        f"            mountpoint = {libconfig_string(SCAN_MOUNTPOINT)};\n"
        f"            name = {libconfig_string(names or 'VHF AM scanner')};\n"
        '            genre = "Aviation";\n'
        f"            username = {libconfig_string(user)};\n"
        f"            password = {libconfig_string(password)};\n"
        "            send_scan_freq_tags = true;\n"
        "          }\n"
        "        );\n"
        "      }\n"
        "    );\n"
        "  }\n"
        ");\n"
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
