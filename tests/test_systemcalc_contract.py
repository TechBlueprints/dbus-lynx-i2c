"""Verification against dbus-systemcalc-py's battery selection logic.

Being a com.victronenergy.battery service has system-level side effects.
These tests transliterate the relevant upstream logic (victronenergy/
dbus-systemcalc-py, fetched 2026-08-04) and run it over the paths our
real service publishes, proving:

  1. Auto-select never prefers us over any real battery monitor
     (dbus_systemcalc.py _autoselect_battery_service).
  2. We are excluded from /Batteries and /AvailableBatteries -- the GX
     "Battery measurements" page, marine MFD app and VRM battery widgets
     (delegates/batterydata.py BatteryTracker.valid, lines 61-63, and the
     `if b.valid` / `tracked.valid and ...` filters at lines 286-300).
  3. DVCC never treats us as a BMS
     (delegates/batteryservice.py Battery.is_bms, lines 12-14).
  4. The one surface we DO appear on, knowingly: the Battery monitor
     dropdown (/AvailableBatteryServices, dbus_systemcalc.py
     _handleservicechange lines 1187-1199, rendered by gui-v2
     PageSettingsBatteries.qml:31) -- kept, because hiding there would
     require /Connected=0 or dropping /ProductName, which breaks the
     device list.
"""

from __future__ import annotations

import types

import pytest

from tests.test_gui_contract import FakeSettingsDevice, FakeVeDbusService


@pytest.fixture
def svc(lynx_service, monkeypatch, tmp_path):
    monkeypatch.setattr(lynx_service, "VeDbusService", FakeVeDbusService)
    monkeypatch.setattr(lynx_service, "SettingsDevice", FakeSettingsDevice)
    (tmp_path / "config.ini").write_text("[DEFAULT]\ndistributors = A, B\n")
    config = lynx_service.load_config(str(tmp_path))
    service = lynx_service.LynxBatteryService(
        None, config, "CH347 HID-I2C (auto)")
    return service._service


# ── transliterated systemcalc logic ────────────────────────────────────────


def autoselect_battery_service(batteries):
    """dbus_systemcalc.py:503-518 _autoselect_battery_service.

    ``batteries`` is {servicename: (instance, seen_info_maxchargevoltage)}.
    Sort key per service: (no BMS paths, not lynxparallel, instance);
    lowest wins.
    """
    entries = [
        (not seen_info,
         not s.startswith("com.victronenergy.battery.lynxparallel"),
         instance,
         s)
        for s, (instance, seen_info) in batteries.items()]
    return sorted(entries, key=lambda x: x[:3])[0][3]


def connected_service_filter(svc):
    """dbus_systemcalc.py:1224-1233 _remove_unconnected_services --
    a service stays in the candidate/dropdown lists iff all three hold."""
    return (svc.paths.get("/Connected") == 1
            and svc.paths.get("/ProductName") is not None
            and svc.paths.get("/Mgmt/Connection") is not None)


def battery_tracker_valid(svc):
    """delegates/batterydata.py:61-63 -- in /Batteries and
    /AvailableBatteries only when /Dc/0/Voltage has a value."""
    return svc.paths.get("/Dc/0/Voltage") is not None


def dvcc_is_bms(svc):
    """delegates/batteryservice.py:12-14 Battery.is_bms."""
    return svc.paths.get("/Info/MaxChargeVoltage") is not None


def readable_service_name(svc):
    """dbus_systemcalc.py:1213-1219 _get_readable_service_name."""
    cn = svc.paths.get("/CustomName")
    if cn is not None and cn.strip():
        return cn
    return "%s on %s" % (svc.paths["/ProductName"],
                         svc.paths["/Mgmt/Connection"])


# ── 1. auto-selection ──────────────────────────────────────────────────────


def our_entry(svc):
    return ("com.victronenergy.battery.lynx_i2c",
            (svc.paths["/DeviceInstance"], dvcc_is_bms(svc)))


def test_autoselect_prefers_any_bms(svc):
    # A managed battery (serialbattery, Lynx Smart BMS, ...) publishes
    # /Info/MaxChargeVoltage and wins on the first sort key.
    batteries = dict([
        our_entry(svc),
        ("com.victronenergy.battery.ttyUSB0", (288, True)),
    ])
    assert autoselect_battery_service(batteries) == \
        "com.victronenergy.battery.ttyUSB0"


def test_autoselect_prefers_plain_shunt_on_instance_tiebreak(svc):
    # A BMV/SmartShunt has no /Info paths, so the tie falls through to
    # lowest-instance-wins. Real monitors sit at ~245-512; our default
    # instance must stay above them. (At the old default of 200 this
    # test fails: we would have hijacked the battery monitor role.)
    for shunt_instance in (245, 288, 512):
        batteries = dict([
            our_entry(svc),
            ("com.victronenergy.battery.ttyO2", (shunt_instance, False)),
        ])
        assert autoselect_battery_service(batteries) == \
            "com.victronenergy.battery.ttyO2", \
            "instance %d shunt lost to us" % shunt_instance


def test_autoselect_when_we_are_alone(svc):
    # Sole battery service -> we do get picked; the dashboard battery
    # tile just shows no data. Documented in the README; pin
    # "No battery monitor" in settings if it bothers you.
    assert autoselect_battery_service(dict([our_entry(svc)])) == \
        "com.victronenergy.battery.lynx_i2c"


# ── 2. battery measurement lists ───────────────────────────────────────────


def test_excluded_from_batteries_and_available_batteries(svc):
    assert not battery_tracker_valid(svc)


def test_stays_excluded_after_updates(lynx_service, monkeypatch, tmp_path):
    monkeypatch.setattr(lynx_service, "VeDbusService", FakeVeDbusService)
    monkeypatch.setattr(lynx_service, "SettingsDevice", FakeSettingsDevice)
    (tmp_path / "config.ini").write_text("[DEFAULT]\ndistributors = A\n")
    service = lynx_service.LynxBatteryService(
        None, lynx_service.load_config(str(tmp_path)), "test")
    service.update("A", 0x10)
    assert not battery_tracker_valid(service._service)


# ── 3. DVCC ────────────────────────────────────────────────────────────────


def test_dvcc_never_sees_us_as_bms(svc):
    assert not dvcc_is_bms(svc)


# ── 4. the dropdown, eyes open ─────────────────────────────────────────────


def test_we_do_appear_in_battery_monitor_dropdown(lynx_service, monkeypatch,
                                                  tmp_path):
    # Unavoidable without breaking the device list; harmless unless a
    # user manually selects us. /Connected (and thus dropdown presence)
    # requires the adapter to have completed at least one transfer.
    monkeypatch.setattr(lynx_service, "VeDbusService", FakeVeDbusService)
    monkeypatch.setattr(lynx_service, "SettingsDevice", FakeSettingsDevice)
    (tmp_path / "config.ini").write_text("[DEFAULT]\ndistributors = A\n")
    service = lynx_service.LynxBatteryService(
        None, lynx_service.load_config(str(tmp_path)), "CH347 HID-I2C (auto)")
    assert not connected_service_filter(service._service)  # before first poll
    service.adapter_found()
    assert connected_service_filter(service._service)
    assert readable_service_name(service._service) == \
        "Lynx Distributor Monitor on CH347 HID-I2C (auto)"


def test_default_instance_is_high(svc):
    assert svc.paths["/DeviceInstance"] == 990
