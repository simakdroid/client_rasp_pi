import json
import re
from importlib.resources import files
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "deploy" / "radio-channels.json"


def _env_value(path: Path, key: str) -> str:
    prefix = f"{key}="
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(prefix):
            value = line[len(prefix) :]
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            return value
    raise AssertionError(f"{key} missing in {path}")


def _channel_catalog() -> list[dict[str, object]]:
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    assert isinstance(catalog, list) and catalog
    return catalog


def test_radio_channel_catalog_matches_env_and_rtl_airband() -> None:
    catalog = _channel_catalog()
    by_id = {item["id"]: item for item in catalog}
    assert set(by_id) == {"tower", "ground", "approach"}

    conf = (ROOT / "deploy" / "rtl-airband" / "rtl_airband.conf.in").read_text(encoding="utf-8")
    freqs = [float(value) for value in re.findall(r"(?m)^\s+freq = ([0-9.]+);", conf)]
    mounts = re.findall(r'mountpoint = "([^"]+)";', conf)
    serial = re.search(r'serial = "([^"]+)";', conf)
    center = float(re.search(r"centerfreq = ([0-9.]+);", conf).group(1))
    sample_rate = float(re.search(r"sample_rate = ([0-9.]+);", conf).group(1))

    assert serial is not None and serial.group(1) == "${VHF_SERIAL}"
    assert freqs == [float(item["frequency_mhz"]) for item in catalog]
    assert mounts == [item["mountpoint"] for item in catalog]

    half_band = sample_rate / 2
    for item in catalog:
        frequency = float(item["frequency_mhz"])
        assert abs(frequency - center) <= half_band
        expected_mount = f"vhf-{int(round(frequency * 1000)):06d}.mp3"
        assert item["mountpoint"] == expected_mount
        assert str(item["stream_url"]).endswith("/" + expected_mount)

    for env_path in (
        ROOT / ".env.example",
        ROOT / "deploy" / "env" / "backend.env.example",
    ):
        channels = json.loads(_env_value(env_path, "AIRMON_RADIO_CHANNELS_JSON"))
        assert [(item["id"], item["frequency_mhz"], item["stream_url"]) for item in channels] == [
            (item["id"], item["frequency_mhz"], item["stream_url"]) for item in catalog
        ]


def test_sdr_env_is_the_serial_source() -> None:
    sdr = (ROOT / "deploy" / "env" / "sdr.env.example").read_text(encoding="utf-8")
    assert "ADSB_PREFERRED_SERIAL=1090" in sdr
    assert "VHF_SERIAL=0118" in sdr
    assert "AIRMON_RADIO_RECEIVER_SERIAL=0118" in sdr
    default = (ROOT / "deploy" / "readsb" / "readsb.default").read_text(encoding="utf-8")
    assert not any(
        line.startswith("ADSB_PREFERRED_SERIAL=") for line in default.splitlines()
    )
    start = (ROOT / "deploy" / "scripts" / "start-readsb.sh").read_text(encoding="utf-8")
    assert "eval " not in start
    assert "source " not in start
    assert "set -f" in start


def test_udev_and_units_use_rtl_sdr_hotplug() -> None:
    assert (ROOT / "deploy" / "scripts" / "rtl-hotplug.sh").is_file()
    assert (ROOT / "deploy" / "systemd" / "adsb-vhf-rtl-hotplug.service").is_file()

    rules = (ROOT / "deploy" / "udev" / "99-adsb-vhf-rtl-sdr.rules").read_text(encoding="utf-8")
    assert 'GROUP="rtl-sdr"' in rules
    assert "adsb-vhf-rtl-hotplug.service" in rules
    assert 'ACTION=="remove"' in rules
    assert "0bda" in rules
    assert "2832" in rules and "2838" in rules

    readsb = (ROOT / "deploy" / "systemd" / "readsb-adsb.service").read_text(encoding="utf-8")
    assert "Group=rtl-sdr" in readsb
    assert "StartLimitIntervalSec=0" in readsb
    assert "sdr.env" in readsb
    assert "ExecCondition=" in readsb

    radio = (ROOT / "deploy" / "systemd" / "rtl-airband.service").read_text(encoding="utf-8")
    assert "SupplementaryGroups=rtl-sdr" in radio
    assert "${VHF_SERIAL}" in radio
    assert "StartLimitIntervalSec=0" in radio

    backend = (ROOT / "deploy" / "systemd" / "adsb-vhf-backend.service").read_text(encoding="utf-8")
    assert "BACKEND_HOST" in backend
    assert "StateDirectory=adsb-vhf" in backend
    assert "/var/lib/adsb-vhf" in backend
    assert "/opt/adsb-vhf/data/layers" in backend
    assert "AIRMON_AIRCRAFT_TYPES_PATH=/var/lib/adsb-vhf/aircraft-types.json" in backend
    assert "AIRMON_COVERAGE_PATH=/var/lib/adsb-vhf/coverage-rose.json" in backend

    install = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    assert "rtl-sdr" in install
    assert "rtl-hotplug.sh" in install
    assert "sdr.env" in install
    assert "/var/lib/adsb-vhf" in install
    assert "/opt/air-monitor" not in install
    assert "ensure_backend_writable_file AIRMON_AIRCRAFT_TYPES_PATH" in install
    assert "ensure_backend_writable_file AIRMON_COVERAGE_PATH" in install


def test_kiosk_fails_if_backend_never_answers() -> None:
    script = (ROOT / "deploy" / "chromium" / "start-kiosk.sh").read_text(encoding="utf-8")
    assert "raise SystemExit(1)" in script
    assert "wait_for_http ||" in script
    assert "--password-store=basic" in script
    assert "--ozone-platform-hint=auto" in script
    assert "WAYLAND_DISPLAY=wayland-0" not in script
    wayfire = (ROOT / "deploy" / "chromium" / "wayfire-autostart.ini").read_text(encoding="utf-8")
    section_lines = [line for line in wayfire.splitlines() if line.strip() == "[autostart]"]
    assert len(section_lines) == 1
    assert "adsb-kiosk.service" in wayfire


def test_wheel_package_includes_frontend_assets() -> None:
    static = files("app") / "static"
    for relative in (
        "index.html",
        "app.js",
        "vendor/leaflet/leaflet.css",
        "vendor/Leaflet.VectorGrid.bundled.js",
    ):
        assert (static / relative).is_file(), relative
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'packages = ["app"]' in pyproject
