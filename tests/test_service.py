"""Config parsing and Lynx Smart BMS schema mapping of the main service."""

from __future__ import annotations

import pytest

from lynx_distributor import decode


def write_config(tmp_path, body):
    (tmp_path / "config.ini").write_text("[DEFAULT]\n" + body)
    return str(tmp_path)


# ── config ─────────────────────────────────────────────────────────────────


def test_defaults_without_config_files(tmp_path, lynx_service):
    cfg = lynx_service.load_config(str(tmp_path))
    assert cfg.letters == ["A"]
    assert cfg.fuse_counts == {"A": 4}
    assert cfg.fuse_names == {"A": ["", "", "", ""]}
    assert cfg.poll_interval == 5.0
    assert cfg.i2c_speed_hz == 20000
    assert cfg.hidraw_device is None


def test_full_config(tmp_path, lynx_service):
    base = write_config(tmp_path, (
        "distributors = a, B\n"
        "fuses_per_distributor = 2, 4\n"
        "fuse_names_a = Inverter, Solar\n"
        "fuse_names_b = , , DC Panel\n"
        "poll_interval = 2.5\n"
        "i2c_speed_hz = 100000\n"
        "hidraw_device = /dev/hidraw3\n"
    ))
    cfg = lynx_service.load_config(base)
    assert cfg.letters == ["A", "B"]
    assert cfg.fuse_counts == {"A": 2, "B": 4}
    assert cfg.fuse_names == {
        "A": ["Inverter", "Solar", "", ""],
        "B": ["", "", "DC Panel", ""],
    }
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
    "fuse_names_a = 1, 2, 3, 4, 5\n",
    "fuse_names_a = this-name-is-way-over-sixteen-bytes\n",
    "poll_interval = 0.1\n",
    "poll_interval = fast\n",
    "i2c_speed_hz = 50000\n",
])
def test_invalid_configs_rejected(tmp_path, lynx_service, body):
    base = write_config(tmp_path, body)
    with pytest.raises(lynx_service.ConfigError):
        lynx_service.load_config(base)


# ── Lynx Smart BMS schema mapping ──────────────────────────────────────────


def test_bms_schema_constants(lynx_service):
    # Locked to the Venus wiki dbus.md "Lynx Smart BMS" section and
    # gui-v2 PageLynxDistributorList.qml / FuseInfo.qml.
    assert lynx_service.DIST_NOT_AVAILABLE == 0
    assert lynx_service.DIST_CONNECTED == 1
    assert lynx_service.DIST_NO_BUS_POWER == 2
    assert lynx_service.DIST_COMMS_LOST == 3
    assert lynx_service.FUSE_NOT_AVAILABLE == 0
    assert lynx_service.FUSE_NOT_USED == 1
    assert lynx_service.FUSE_OK == 2
    assert lynx_service.FUSE_BLOWN == 3
    assert (lynx_service.ALARM_OK, lynx_service.ALARM_ALARM) == (0, 2)
    assert lynx_service.FUSE_NAME_MAX_BYTES == 16


def test_distributor_status_mapping(lynx_service):
    assert lynx_service.distributor_status_value(decode(0x00)) == 1
    assert lynx_service.distributor_status_value(decode(0x10)) == 1
    # No-supply bit wins regardless of fuse bits
    assert lynx_service.distributor_status_value(decode(0x02)) == 2
    assert lynx_service.distributor_status_value(decode(0x32)) == 2


def test_fuse_status_mapping_all_populated(lynx_service):
    assert lynx_service.fuse_status_values(decode(0x00), 4) == [2, 2, 2, 2]
    assert lynx_service.fuse_status_values(decode(0x10), 4) == [3, 2, 2, 2]
    assert lynx_service.fuse_status_values(decode(0x80), 4) == [2, 2, 2, 3]


def test_fuse_status_mapping_unpopulated_positions(lynx_service):
    # Positions beyond the populated count read "blown" on the wire but
    # must publish as Not used (1), matching the BMS for empty slots.
    values = lynx_service.fuse_status_values(decode(0xD0, num_fuses=2), 2)
    assert values == [3, 2, 1, 1]


def test_zero_fuses_all_not_used(lynx_service):
    values = lynx_service.fuse_status_values(decode(0xF0, num_fuses=0), 0)
    assert values == [1, 1, 1, 1]
