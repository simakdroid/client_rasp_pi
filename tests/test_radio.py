from app.radio import RadioMonitor


def test_radio_hidden_until_second_receiver_is_connected(tmp_path) -> None:
    monitor = RadioMonitor(auto_detect=True, sysfs_path=tmp_path)
    _add_rtl_device(tmp_path, "usb1", "1090")

    assert monitor.hardware_available() is False
    assert monitor.role_snapshot("1090")["vhf"] is None

    _add_rtl_device(tmp_path, "usb2", "0118")
    roles = monitor.role_snapshot("1090")
    assert monitor.hardware_available() is True
    assert roles["adsb"] == "1090"
    assert roles["vhf"] == "0118"
    assert roles["vhf_available"] is True


def test_radio_hidden_when_vhf_serial_is_duplicated(tmp_path) -> None:
    monitor = RadioMonitor(auto_detect=True, sysfs_path=tmp_path)
    _add_rtl_device(tmp_path, "usb1", "1090")
    _add_rtl_device(tmp_path, "usb2", "0118")
    _add_rtl_device(tmp_path, "usb3", "0118")

    assert monitor.hardware_available() is False
    assert monitor.role_snapshot("1090")["duplicate_serials"] is True


def test_sdr_roles_follow_connected_serials(tmp_path) -> None:
    _add_rtl_device(tmp_path, "usb1", "1090")
    monitor = RadioMonitor(auto_detect=False, sysfs_path=tmp_path)
    roles = monitor.role_snapshot("1090")
    assert roles["adsb"] == "0"
    assert roles["vhf"] is None
    _add_rtl_device(tmp_path, "usb2", "0118")
    roles = monitor.role_snapshot("1090")
    assert roles["adsb"] == "1090"
    assert roles["vhf"] == "0118"


def _add_rtl_device(root, name: str, serial: str) -> None:
    device = root / name
    device.mkdir()
    (device / "idVendor").write_text("0bda\n", encoding="ascii")
    (device / "idProduct").write_text("2838\n", encoding="ascii")
    (device / "serial").write_text(f"{serial}\n", encoding="ascii")
