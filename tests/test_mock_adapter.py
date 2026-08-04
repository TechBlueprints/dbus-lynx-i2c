"""Mock adapter: the no-hardware development backend."""

from __future__ import annotations

import json

import pytest

from ch347 import CH347Error, I2CNackError
from lynx_distributor import ADDRESSES
from mock_adapter import MockAdapter


@pytest.fixture
def state(tmp_path):
    path = tmp_path / "mock-state.json"

    def write(obj):
        path.write_text(json.dumps(obj))
        return str(path)

    write({})
    return type("S", (), {"path": str(path), "write": staticmethod(write)})


def test_missing_file_reads_all_ok(tmp_path):
    a = MockAdapter(str(tmp_path / "nonexistent.json"))
    assert a.i2c_read(ADDRESSES["A"]) == b"\x00"


def test_letter_and_hex_keys(state):
    a = MockAdapter(state.path)
    state.write({"A": "0x10", "0x09": 2})
    assert a.i2c_read(ADDRESSES["A"]) == b"\x10"
    assert a.i2c_read(ADDRESSES["B"]) == b"\x02"
    assert a.i2c_read(ADDRESSES["C"]) == b"\x00"  # unlisted -> OK


def test_int_values(state):
    a = MockAdapter(state.path)
    state.write({"A": 48})
    assert a.i2c_read(ADDRESSES["A"]) == b"\x30"


def test_nack_and_error(state):
    a = MockAdapter(state.path)
    state.write({"A": "nack", "B": "error"})
    with pytest.raises(I2CNackError):
        a.i2c_read(ADDRESSES["A"])
    with pytest.raises(CH347Error):
        a.i2c_read(ADDRESSES["B"])


def test_garbage_file_reads_all_ok(state):
    a = MockAdapter(state.path)
    with open(state.path, "w") as f:
        f.write("not json{{")
    assert a.i2c_read(ADDRESSES["A"]) == b"\x00"


def test_state_reread_every_poll(state):
    a = MockAdapter(state.path)
    assert a.i2c_read(ADDRESSES["A"]) == b"\x00"
    state.write({"A": "0x80"})
    assert a.i2c_read(ADDRESSES["A"]) == b"\x80"


def test_monitor_uses_mock_when_configured(lynx_service, monkeypatch,
                                           tmp_path):
    from tests.test_gui_contract import FakeSettingsDevice, FakeVeDbusService
    monkeypatch.setattr(lynx_service, "VeDbusService", FakeVeDbusService)
    monkeypatch.setattr(lynx_service, "SettingsDevice", FakeSettingsDevice)
    (tmp_path / "config.ini").write_text(
        "[DEFAULT]\ndistributors = A, B\nmock = true\n")
    config = lynx_service.load_config(str(tmp_path))
    assert config.mock is True

    monitor = lynx_service.FuseMonitor(bus=None, config=config)
    monitor._poll()
    svc = monitor.service._service
    assert isinstance(monitor.adapter, MockAdapter)
    assert svc.paths["/Mgmt/Connection"] == "Mock adapter (no hardware)"
    assert svc.paths["/Distributor/A/Status"] == 1
    assert svc.paths["/Distributor/B/Status"] == 1
