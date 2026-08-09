"""CH347 HID-I2C driver: framing, response parsing, sysfs discovery."""

from __future__ import annotations

import struct

import pytest

from ch347 import (
    CH347I2C,
    CH347Error,
    CH347TimeoutError,
    I2CNackError,
    find_hidraw_paths,
)


class FakeTransport:
    """Records HID writes, replays queued responses."""

    def __init__(self, responses=()):
        self.writes = []
        self.responses = list(responses)
        self.path = "/dev/hidraw-fake"
        self.closed = False

    def write(self, report):
        self.writes.append(bytes(report))

    def read(self, timeout):
        return self.responses.pop(0) if self.responses else b""

    def drain(self):
        pass

    def close(self):
        self.closed = True


def frame(payload: bytes) -> bytes:
    """Build a HID input report: 16-bit LE length + payload."""
    return struct.pack("<H", len(payload)) + payload


def make_dev(responses=()):
    t = FakeTransport(responses)
    return CH347I2C(t), t


# ── framing ────────────────────────────────────────────────────────────────


def test_set_speed_frame():
    dev, t = make_dev()
    dev.set_speed(20000)
    assert t.writes == [b"\x00\x03\x00\xaa\x60\x00"]
    assert dev.speed_hz == 20000


def test_set_speed_100k_level():
    dev, t = make_dev()
    dev.set_speed(100000)
    assert t.writes == [b"\x00\x03\x00\xaa\x61\x00"]


def test_set_speed_rejects_unsupported():
    dev, _ = make_dev()
    with pytest.raises(ValueError):
        dev.set_speed(50000)  # CH347 has no 50 kHz


def test_read_single_byte_stream():
    # Lynx distributor A: START, addr 0x08+R, read 1 (NACK), STOP
    dev, t = make_dev([frame(b"\x01\x30")])
    data = dev.i2c_read(0x08, 1)
    assert data == b"\x30"
    stream = b"\xaa\x74\x81\x11\xc0\x75\x00"
    assert t.writes == [b"\x00" + struct.pack("<H", len(stream)) + stream]


def test_read_multi_byte_stream():
    dev, t = make_dev([frame(b"\x01ABCD")])
    data = dev.i2c_read(0x08, 4)
    assert data == b"ABCD"
    # ACKed prefix of 3 (0xC0|3), then final NACKed byte (bare 0xC0)
    stream = b"\xaa\x74\x81\x11\xc3\xc0\x75\x00"
    assert t.writes == [b"\x00" + struct.pack("<H", len(stream)) + stream]


def test_read_nack_raises():
    dev, _ = make_dev([frame(b"\x00\x00")])
    with pytest.raises(I2CNackError) as e:
        dev.i2c_read(0x08, 1)
    assert e.value.addr == 0x08


def test_read_timeout_raises():
    dev, _ = make_dev([])  # no response queued
    with pytest.raises(CH347TimeoutError):
        dev.i2c_read(0x08, 1)


def test_short_response_raises():
    dev, _ = make_dev([frame(b"\x01")])  # ack but no data byte
    with pytest.raises(CH347Error):
        dev.i2c_read(0x08, 1)


def test_write_stream_and_acks():
    dev, t = make_dev([frame(b"\x01\x01\x01")])
    dev.i2c_write(0x09, b"\x01\x02")
    stream = b"\xaa\x74\x83\x12\x01\x02\x75\x00"
    assert t.writes == [b"\x00" + struct.pack("<H", len(stream)) + stream]


def test_write_address_nack():
    dev, _ = make_dev([frame(b"\x00\x00\x00")])
    with pytest.raises(I2CNackError):
        dev.i2c_write(0x09, b"\x01\x02")


def test_write_data_nack():
    dev, _ = make_dev([frame(b"\x01\x01\x00")])
    with pytest.raises(CH347Error):
        dev.i2c_write(0x09, b"\x01\x02")


def test_write_read_register_style():
    dev, t = make_dev([frame(b"\x01\x01\x01\x5a")])
    data = dev.i2c_write_read(0x50, b"\x10", 1)
    assert data == b"\x5a"
    stream = b"\xaa\x74\x82\xa0\x10\x74\x81\xa1\xc0\x75\x00"
    assert t.writes == [b"\x00" + struct.pack("<H", len(stream)) + stream]


def test_probe_ack_and_nack():
    dev, _ = make_dev([frame(b"\x01"), frame(b"\x00")])
    assert dev.probe(0x08) is True
    assert dev.probe(0x0B) is False


def test_address_range_validation():
    dev, _ = make_dev()
    with pytest.raises(ValueError):
        dev.i2c_read(0x80, 1)
    with pytest.raises(ValueError):
        dev.i2c_read(0x08, 0)
    with pytest.raises(ValueError):
        dev.i2c_read(0x08, 64)


# ── sysfs discovery ────────────────────────────────────────────────────────


def _mk_hidraw(tmp_path, name, hid_id, phys):
    d = tmp_path / name / "device"
    d.mkdir(parents=True)
    (d / "uevent").write_text(
        "DRIVER=hid-generic\nHID_ID=%s\nHID_PHYS=%s\nHID_NAME=x\n"
        % (hid_id, phys))


def test_find_hidraw_selects_interface_1(tmp_path):
    _mk_hidraw(tmp_path, "hidraw0", "0003:00001A86:000055DC",
               "usb-xhci-hcd.0-1/input0")  # UART interface
    _mk_hidraw(tmp_path, "hidraw1", "0003:00001A86:000055DC",
               "usb-xhci-hcd.0-1/input1")  # SPI/I2C/GPIO interface
    _mk_hidraw(tmp_path, "hidraw2", "0003:0000046D:0000C31C",
               "usb-xhci-hcd.0-2/input1")  # some other device
    assert find_hidraw_paths(sysfs=str(tmp_path)) == ["/dev/hidraw1"]


def test_find_hidraw_empty_when_absent(tmp_path):
    assert find_hidraw_paths(sysfs=str(tmp_path)) == []
    assert find_hidraw_paths(sysfs=str(tmp_path / "missing")) == []


def test_open_errors_when_no_adapter(tmp_path, monkeypatch):
    import ch347
    monkeypatch.setattr(ch347, "find_hidraw_paths", lambda *a, **k: [])
    with pytest.raises(CH347Error):
        CH347I2C.open()


# ── burst read (fast data phase) ───────────────────────────────────────────


def test_burst_read_stream_and_speed_changes():
    dev, t = make_dev([frame(b"\x01\x30\x30")])
    dev.speed_hz = 20000
    status, echo = dev.i2c_read_burst(0x08, 750000)
    assert (status, echo) == (0x30, 0x30)
    # slow(0x60) addr, fast(0x63) status+ACK, slow(0x60) echo+NACK, stop
    stream = b"\xaa\x60\x74\x81\x11\x63\xc1\x60\xc0\x75\x00"
    assert t.writes == [b"\x00" + struct.pack("<H", len(stream)) + stream]


def test_burst_read_400k_level():
    dev, t = make_dev([frame(b"\x01\x00\x00")])
    dev.speed_hz = 20000
    dev.i2c_read_burst(0x08, 400000)
    assert b"\x62\xc1" in t.writes[0]   # level 2 == 400 kHz


def test_burst_read_nack():
    dev, _ = make_dev([frame(b"\x00\xff\xff")])
    dev.speed_hz = 20000
    with pytest.raises(I2CNackError):
        dev.i2c_read_burst(0x08)


def test_burst_read_rejects_bad_speed():
    dev, _ = make_dev()
    dev.speed_hz = 20000
    with pytest.raises(ValueError):
        dev.i2c_read_burst(0x08, 50000)
