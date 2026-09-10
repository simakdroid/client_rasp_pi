import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "deploy" / "scripts" / "rtl-device-mode.sh"


pytestmark = pytest.mark.skipif(os.name == "nt", reason="requires a POSIX shell")


def test_single_blank_serial_uses_index_zero(tmp_path) -> None:
    _add_rtl(tmp_path, "1-1", serial=None)

    assert _run_selector(tmp_path, "count") == "1"
    assert _run_selector(tmp_path, "adsb-device") == "0"


def test_two_receivers_use_preferred_adsb_serial(tmp_path) -> None:
    _add_rtl(tmp_path, "1-1", serial="1090")
    _add_rtl(tmp_path, "1-2", serial="0118")

    assert _run_selector(tmp_path, "count") == "2"
    assert _run_selector(tmp_path, "adsb-device") == "1090"
    assert _run_selector(tmp_path, "vhf-available") == ""


def test_two_receivers_in_reverse_usb_order_keep_roles(tmp_path) -> None:
    _add_rtl(tmp_path, "1-1", serial="0118")
    _add_rtl(tmp_path, "1-2", serial="1090")

    assert _run_selector(tmp_path, "adsb-device") == "1090"
    assert _run_selector(tmp_path, "vhf-available") == ""


def test_duplicate_adsb_serial_is_rejected(tmp_path) -> None:
    _add_rtl(tmp_path, "1-1", serial="1090")
    _add_rtl(tmp_path, "1-2", serial="1090")

    result = _run_selector_result(tmp_path, "adsb-device")
    assert result.returncode == 1
    result = _run_selector_result(tmp_path, "vhf-available")
    assert result.returncode == 1


def test_duplicate_vhf_serial_is_rejected(tmp_path) -> None:
    _add_rtl(tmp_path, "1-1", serial="1090")
    _add_rtl(tmp_path, "1-2", serial="0118")
    _add_rtl(tmp_path, "1-3", serial="0118")

    result = _run_selector_result(tmp_path, "vhf-available")
    assert result.returncode == 1


def _add_rtl(root: Path, name: str, serial: str | None) -> None:
    device = root / name
    device.mkdir()
    (device / "idVendor").write_text("0bda\n", encoding="ascii")
    (device / "idProduct").write_text("2838\n", encoding="ascii")
    if serial is not None:
        (device / "serial").write_text(f"{serial}\n", encoding="ascii")


def _run_selector(sysfs: Path, command: str) -> str:
    result = _run_selector_result(sysfs, command)
    result.check_returncode()
    return result.stdout.strip()


def _run_selector_result(sysfs: Path, command: str) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "RTL_SYSFS_ROOT": str(sysfs),
        "ADSB_PREFERRED_SERIAL": "1090",
        "VHF_SERIAL": "0118",
    }
    return subprocess.run(
        ["/bin/sh", str(SCRIPT), command],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
