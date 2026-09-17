from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _env_value(path: Path, key: str) -> str:
    prefix = f"{key}="
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(prefix):
            value = line[len(prefix) :]
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            return value
    raise AssertionError(f"{key} missing in {path}")


def test_acars_env_matches_backend_and_decoder() -> None:
    backend = (ROOT / "deploy" / "env" / "backend.env.example").read_text(encoding="utf-8")
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    acars = (ROOT / "deploy" / "env" / "acarsdec.env.example").read_text(encoding="utf-8")
    start = (ROOT / "deploy" / "scripts" / "start-acarsdec.sh").read_text(encoding="utf-8")
    assert "AIRMON_ACARS_UDP_PORT=5550" in backend
    assert "AIRMON_ACARS_FREQUENCIES_MHZ=131.525 131.550 131.725 131.825" in backend
    assert "AIRMON_RADIO_CHANNELS_JSON" not in backend
    assert "AIRMON_ACARS_UDP_PORT=5550" in example
    assert "ACARS_UDP_PORT=5550" in acars
    assert "131.525 131.550 131.725 131.825" in acars
    assert "acarsdec -N" in start.replace("\n", " ") or '-N "${host}:${port}"' in start
    env_path = ROOT / "deploy" / "env" / "backend.env.example"
    assert _env_value(env_path, "AIRMON_RADIO_RECEIVER_SERIAL") == "0118"


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

    acars = (ROOT / "deploy" / "systemd" / "acarsdec.service").read_text(encoding="utf-8")
    assert "SupplementaryGroups=rtl-sdr" in acars
    assert "start-acarsdec.sh" in acars
    assert "vhf-available" in acars
    assert "StartLimitIntervalSec=0" in acars
    assert "Conflicts=rtl-airband.service" in acars

    backend = (ROOT / "deploy" / "systemd" / "adsb-vhf-backend.service").read_text(encoding="utf-8")
    assert "BACKEND_HOST" in backend
    assert "StateDirectory=adsb-vhf" in backend
    assert "/var/lib/adsb-vhf" in backend
    assert "/opt/adsb-vhf/data/layers" in backend
    assert "AIRMON_AIRCRAFT_TYPES_PATH=/var/lib/adsb-vhf/aircraft-types.json" in backend
    assert "AIRMON_COVERAGE_PATH=/var/lib/adsb-vhf/coverage-rose.json" in backend
    assert "AIRMON_RADIO_CHANNELS_PATH" not in backend

    install = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    assert "rtl-sdr" in install
    assert "rtl-hotplug.sh" in install
    assert "start-acarsdec.sh" in install
    assert "acarsdec.service" in install
    assert "render-rtl-airband-conf.sh" not in install
    assert "sdr.env" in install
    assert "/var/lib/adsb-vhf" in install
    assert "/opt/air-monitor" not in install
    assert "ensure_backend_writable_file AIRMON_AIRCRAFT_TYPES_PATH" in install
    assert "ensure_backend_writable_file AIRMON_COVERAGE_PATH" in install
    assert "AIRMON_RADIO_CHANNELS_PATH" not in install
    assert "adsb-vhf-radio-channels.path" in install
    assert "disable --now adsb-vhf-radio-channels.path" in install


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
    from importlib.resources import files

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
