"""Config parsing and constants of the main D-Bus service."""

from __future__ import annotations

import pytest


def write_config(tmp_path, body):
    (tmp_path / "config.ini").write_text("[DEFAULT]\n" + body)
    return str(tmp_path)


def test_defaults_without_config_files(tmp_path, lynx_service):
    cfg = lynx_service.load_config(str(tmp_path))
    assert cfg.letters == ["A"]
    assert cfg.fuse_counts == {"A": 4}
    assert cfg.poll_interval == 5.0
    assert cfg.i2c_speed_hz == 20000
    assert cfg.hidraw_device is None


def test_full_config(tmp_path, lynx_service):
    base = write_config(tmp_path, (
        "distributors = a, B\n"
        "fuses_per_distributor = 2, 4\n"
        "poll_interval = 2.5\n"
        "i2c_speed_hz = 100000\n"
        "hidraw_device = /dev/hidraw3\n"
    ))
    cfg = lynx_service.load_config(base)
    assert cfg.letters == ["A", "B"]
    assert cfg.fuse_counts == {"A": 2, "B": 4}
    assert cfg.poll_interval == 2.5
    assert cfg.i2c_speed_hz == 100000
    assert cfg.hidraw_device == "/dev/hidraw3"


@pytest.mark.parametrize("body", [
    "distributors = E\n",
    "distributors = A, A\n",
    "distributors = \n",
    "distributors = A, B\nfuses_per_distributor = 2\n",
    "fuses_per_distributor = 9\n",
    "fuses_per_distributor = x\n",
    "poll_interval = 0.1\n",
    "poll_interval = fast\n",
    "i2c_speed_hz = 50000\n",
])
def test_invalid_configs_rejected(tmp_path, lynx_service, body):
    base = write_config(tmp_path, body)
    with pytest.raises(lynx_service.ConfigError):
        lynx_service.load_config(base)


def test_digitalinput_conventions(lynx_service):
    # Locked to victronenergy/dbus-digitalinputs + gui-v2 enums.
    assert lynx_service.PRODUCT_ID == 0xA166
    assert lynx_service.TYPE_GENERIC_IO == 10
    assert (lynx_service.STATE_OK, lynx_service.STATE_ALARM) == (8, 9)
    assert (lynx_service.ALARM_OK, lynx_service.ALARM_ALARM) == (0, 2)
