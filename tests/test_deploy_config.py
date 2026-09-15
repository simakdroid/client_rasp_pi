import json
from importlib.resources import files
from pathlib import Path

from app.rtl_airband_conf import load_channels_json, render_conf

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

    freqs = [float(item["frequency_mhz"]) for item in catalog]
    assert freqs == [118.1, 118.5, 119.1]
    for item in catalog:
        frequency = float(item["frequency_mhz"])
        expected_mount = f"vhf-{int(round(frequency * 1000)):06d}.mp3"
        assert item["mountpoint"] == expected_mount
        assert str(item["stream_url"]).endswith("/" + expected_mount)

    example_json = load_channels_json(backend_env=ROOT / "deploy" / "env" / "backend.env.example")
    example = json.loads(example_json)
    assert [(item["id"], float(item["frequency_mhz"])) for item in example] == [
        (item["id"], float(item["frequency_mhz"])) for item in catalog
    ]
    generated = render_conf(channels_json=example_json, icecast_password="x")
    assert 'mode = "scan";' in generated
    assert 'mountpoint = "vhf-scan.mp3";' in generated
    assert f"freqs = ( {', '.join(f'{freq:.3f}' for freq in freqs)} );" in generated

    channels = json.loads(_env_value(ROOT / ".env.example", "AIRMON_RADIO_CHANNELS_JSON"))
    assert [(item["id"], item["frequency_mhz"], item["stream_url"]) for item in channels] == [
        (item["id"], item["frequency_mhz"], item["stream_url"]) for item in catalog
    ]
    backend_env = (ROOT / "deploy" / "env" / "backend.env.example").read_text(encoding="utf-8")
    assert "AIRMON_RADIO_CHANNELS_JSON=" in backend_env
    assert "AIRMON_RADIO_CHANNELS_PATH=" not in backend_env


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
    assert "render-rtl-airband-conf.sh" in radio
    assert "ExecStartPre=+" in radio
    assert "${ICECAST_PORT}" not in radio
    assert "StartLimitIntervalSec=0" in radio
    render = (ROOT / "deploy" / "scripts" / "render-rtl-airband-conf.sh").read_text(
        encoding="utf-8"
    )
    assert "app.rtl_airband_conf" in render
    assert "envsubst" not in render

    backend = (ROOT / "deploy" / "systemd" / "adsb-vhf-backend.service").read_text(encoding="utf-8")
    assert "BACKEND_HOST" in backend
    assert "StateDirectory=adsb-vhf" in backend
    assert "/var/lib/adsb-vhf" in backend
    assert "/opt/adsb-vhf/data/layers" in backend
    assert "AIRMON_AIRCRAFT_TYPES_PATH=/var/lib/adsb-vhf/aircraft-types.json" in backend
    assert "AIRMON_COVERAGE_PATH=/var/lib/adsb-vhf/coverage-rose.json" in backend
    assert "AIRMON_RADIO_CHANNELS_PATH=" not in backend

    install = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    assert "rtl-sdr" in install
    assert "rtl-hotplug.sh" in install
    assert "render-rtl-airband-conf.sh" in install
    assert "sdr.env" in install
    assert "/var/lib/adsb-vhf" in install
    assert "/opt/air-monitor" not in install
    assert "ensure_backend_writable_file AIRMON_AIRCRAFT_TYPES_PATH" in install
    assert "ensure_backend_writable_file AIRMON_COVERAGE_PATH" in install
    assert "AIRMON_RADIO_CHANNELS_PATH=" not in install


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
