"""Kernel /dev/i2c-N backend: the CP2112 fallback path."""

from __future__ import annotations

import types

import pytest

from ch347 import CH347Error, I2CNackError
from kernel_i2c import (
    I2C_SLAVE,
    KernelI2C,
    find_i2c_buses,
    list_i2c_adapters,
)


class FakeKernelI2C(KernelI2C):
    """KernelI2C with the device node replaced by scripted seams."""

    def __init__(self, read_result=b"\x00"):
        self.path = "/dev/i2c-fake"
        self.fd = 42
        self.transport = types.SimpleNamespace(path=self.path)
        self.speed_hz = None
        self.read_result = read_result
        self.ioctls = []
        self.writes = []

    def _ioctl(self, fd, request, arg):
        self.ioctls.append((fd, request, arg))

    def _os_read(self, fd, n):
        if isinstance(self.read_result, Exception):
            raise self.read_result
        return self.read_result[:n]

    def _os_write(self, fd, data):
        if isinstance(self.read_result, Exception):
            raise self.read_result
        self.writes.append(bytes(data))
        return len(data)

    def close(self):
        self.fd = None


def test_read_sets_slave_address_then_reads():
    dev = FakeKernelI2C(b"\x30")
    assert dev.i2c_read(0x08, 1) == b"\x30"
    assert dev.ioctls == [(42, I2C_SLAVE, 0x08)]


@pytest.mark.parametrize("errno", [6, 121])  # ENXIO, EREMOTEIO
def test_nack_errnos_map_to_nack_error(errno):
    dev = FakeKernelI2C(OSError(errno, "nack"))
    with pytest.raises(I2CNackError) as e:
        dev.i2c_read(0x0B, 1)
    assert e.value.addr == 0x0B


def test_other_oserror_bubbles():
    dev = FakeKernelI2C(OSError(19, "no such device"))  # ENODEV: USB gone
    with pytest.raises(OSError):
        dev.i2c_read(0x08, 1)


def test_short_read_raises():
    dev = FakeKernelI2C(b"\x01")
    with pytest.raises(CH347Error):
        dev.i2c_read(0x08, 4)


def test_probe_and_address_validation():
    dev = FakeKernelI2C(b"\x00")
    assert dev.probe(0x08) is True
    dev.read_result = OSError(121, "nack")
    assert dev.probe(0x08) is False
    with pytest.raises(ValueError):
        dev.i2c_read(0x80, 1)


# ── sysfs adapter discovery ────────────────────────────────────────────────


def _mk_adapter(tmp_path, node, name):
    d = tmp_path / node
    d.mkdir()
    (d / "name").write_text(name + "\n")


def test_adapter_discovery(tmp_path):
    _mk_adapter(tmp_path, "i2c-0", "mv64xxx_i2c adapter")
    _mk_adapter(tmp_path, "i2c-4", "DDC")
    _mk_adapter(tmp_path, "i2c-5", "CP2112 SMBus Bridge on hidraw1")
    assert list_i2c_adapters(str(tmp_path)) == [
        ("/dev/i2c-0", "mv64xxx_i2c adapter"),
        ("/dev/i2c-4", "DDC"),
        ("/dev/i2c-5", "CP2112 SMBus Bridge on hidraw1"),
    ]
    assert find_i2c_buses(sysfs=str(tmp_path)) == ["/dev/i2c-5"]


def test_sysfs_candidate_fallback(tmp_path, monkeypatch):
    # The Venus 6.12 kernel has no /sys/class/i2c-adapter (CONFIG_I2C_COMPAT
    # off); discovery must fall through the candidate list.
    import kernel_i2c
    compat = tmp_path / "i2c-adapter"  # never created
    i2cdev = tmp_path / "i2c-dev"
    i2cdev.mkdir()
    _mk_adapter(i2cdev, "i2c-5", "CP2112 SMBus Bridge on hidraw1")
    monkeypatch.setattr(kernel_i2c, "SYSFS_CANDIDATES",
                        (str(i2cdev), str(compat)))
    assert find_i2c_buses() == ["/dev/i2c-5"]
    monkeypatch.setattr(kernel_i2c, "SYSFS_CANDIDATES",
                        (str(tmp_path / "missing-a"), str(tmp_path / "missing-b")))
    assert find_i2c_buses() == []


def test_open_errors_without_cp2112(tmp_path, monkeypatch):
    import kernel_i2c
    monkeypatch.setattr(kernel_i2c, "SYSFS_CANDIDATES", (str(tmp_path),))
    with pytest.raises(CH347Error):
        KernelI2C.open()


# ── config + monitor integration ───────────────────────────────────────────


def test_adapter_config_parsing(lynx_service, tmp_path):
    (tmp_path / "config.ini").write_text(
        "[DEFAULT]\nadapter = kernel-i2c\ni2c_device = /dev/i2c-5\n")
    cfg = lynx_service.load_config(str(tmp_path))
    assert cfg.adapter == "kernel-i2c"
    assert cfg.i2c_device == "/dev/i2c-5"
    assert cfg.mock is False


def test_default_adapter_is_ch347(lynx_service, tmp_path):
    cfg = lynx_service.load_config(str(tmp_path))
    assert cfg.adapter == "ch347"
    assert cfg.i2c_device is None


def test_legacy_mock_flag_still_works(lynx_service, tmp_path):
    (tmp_path / "config.ini").write_text("[DEFAULT]\nmock = true\n")
    cfg = lynx_service.load_config(str(tmp_path))
    assert cfg.adapter == "mock"
    assert cfg.mock is True


def test_invalid_adapter_rejected(lynx_service, tmp_path):
    (tmp_path / "config.ini").write_text("[DEFAULT]\nadapter = ftdi\n")
    with pytest.raises(lynx_service.ConfigError):
        lynx_service.load_config(str(tmp_path))


def test_monitor_uses_kernel_backend(lynx_service, monkeypatch, tmp_path):
    from tests.test_gui_contract import FakeSettingsDevice, FakeVeDbusService
    import kernel_i2c
    monkeypatch.setattr(lynx_service, "VeDbusService", FakeVeDbusService)
    monkeypatch.setattr(lynx_service, "SettingsDevice", FakeSettingsDevice)

    opened = {}

    def fake_open(path=None):
        opened["path"] = path
        return FakeKernelI2C(b"\x00")

    monkeypatch.setattr(kernel_i2c.KernelI2C, "open", staticmethod(fake_open))
    (tmp_path / "config.ini").write_text(
        "[DEFAULT]\ndistributors = A\nadapter = kernel-i2c\n"
        "i2c_device = /dev/i2c-5\n")
    config = lynx_service.load_config(str(tmp_path))
    monitor = lynx_service.FuseMonitor(bus=None, config=config)
    monitor._poll()
    svc = monitor.service._service
    assert opened["path"] == "/dev/i2c-5"
    assert svc.paths["/Mgmt/Connection"] == "Kernel I2C (/dev/i2c-5)"
    assert svc.paths["/Distributor/A/Status"] == 1
    assert svc.paths["/Connected"] == 1
