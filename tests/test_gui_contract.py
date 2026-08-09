"""End-to-end verification against the gui-v2 rendering contract.

These tests run the real LynxBatteryService / FuseMonitor code against
functional fakes (a dict-backed VeDbusService, a scriptable adapter) and
then evaluate the *actual gui-v2 QML logic*, transliterated to Python
with file:line citations, over the paths we publish.  If gui-v2 changes
its bindings, or we break a path or value, the "GX screen" asserted here
goes wrong.

QML sources (local checkout ~/techblueprints/gui-v2):
  pages/settings/devicelist/DeviceListPage.qml:30
      delegate chosen by serviceType -> "battery" services get
      DeviceListDelegate_battery.qml, which pushes PageBattery (line 19).
  pages/settings/devicelist/battery/PageBattery.qml:374-386
      "Fuses" menu entry, preferredVisible when /NrOfDistributors > 0.
  pages/settings/devicelist/battery/PageLynxDistributorList.qml
      per-distributor row: uid /Distributor/<X>/Status (line 74),
      row hidden unless status valid and != 0 (line 38), secondaryText
      logic (lines 39-66), fuse detail page (lines 80-112) shown only
      when status == 1 (line 21, 87).
  pages/settings/devicelist/battery/FuseInfo.qml
      uids /Distributor/<X>/Fuse/<n>/Name (line 20) and /Status
      (line 24); blown means Status == 3 (line 17).
  pages/settings/devicelist/battery/PageBatteryAlarms.qml:114
      uid /Alarms/FuseBlown.
"""

from __future__ import annotations

import types

import pytest

from lynx_distributor import ADDRESSES
from ch347 import CH347Error, I2CNackError


# ── functional fakes ───────────────────────────────────────────────────────


class FakeVeDbusService:
    """Dict-backed stand-in honouring the vedbus API surface we use."""

    def __init__(self, servicename, bus=None, register=None):
        self.servicename = servicename
        self.registered = False
        self.paths = {}
        self.writeable_callbacks = {}

    def add_path(self, path, value, description="", writeable=False,
                 onchangecallback=None, gettextcallback=None):
        assert path not in self.paths, "duplicate add_path %s" % path
        self.paths[path] = value
        if writeable:
            self.writeable_callbacks[path] = onchangecallback

    def register(self):
        self.registered = True

    def __getitem__(self, path):
        return self.paths[path]

    def __setitem__(self, path, value):
        if path not in self.paths:
            raise KeyError(path)
        self.paths[path] = value

    # velib batches updates via `with service as s:` (ServiceContext
    # reads/writes delegate to the parent) -- modelled by returning self.
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeSettingsDevice:
    def __init__(self, bus, supportedSettings, eventCallback, timeout=0):
        self.values = {k: v[1] for k, v in supportedSettings.items()}
        self.eventCallback = eventCallback

    def __getitem__(self, k):
        return self.values[k]

    def __setitem__(self, k, v):
        self.values[k] = v


class FakeAdapter:
    """Scriptable CH347: per-address response byte or exception."""

    def __init__(self):
        self.responses = {}  # addr -> int | Exception
        self.transport = types.SimpleNamespace(path="/dev/hidraw-fake")
        self.speed_hz = 20000
        self.closed = False

    def i2c_read(self, addr, nbytes):
        r = self.responses[addr]
        if isinstance(r, list):
            r = r.pop(0) if len(r) > 1 else r[0]
        if isinstance(r, Exception):
            raise r
        return bytes([r])

    def close(self):
        self.closed = True


# ── fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def env(lynx_service, monkeypatch, tmp_path):
    """Build a FuseMonitor over fakes with the A+B two-distributor config."""
    monkeypatch.setattr(lynx_service, "VeDbusService", FakeVeDbusService)
    monkeypatch.setattr(lynx_service, "SettingsDevice", FakeSettingsDevice)

    (tmp_path / "config.ini").write_text(
        "[DEFAULT]\n"
        "distributors = A, B\n"
        "fuses_per_distributor = 4, 2\n"
        "fuse_names_a = Inverter, Solar\n")
    config = lynx_service.load_config(str(tmp_path))

    adapter = FakeAdapter()
    adapter.responses = {ADDRESSES["A"]: 0x00, ADDRESSES["B"]: 0x00}
    fake_ch347 = types.SimpleNamespace(open=lambda **kw: adapter)
    monkeypatch.setattr(lynx_service, "CH347I2C", fake_ch347)

    monitor = lynx_service.FuseMonitor(bus=None, config=config)
    svc = monitor.service._service
    return types.SimpleNamespace(
        module=lynx_service, monitor=monitor, service=monitor.service,
        svc=svc, adapter=adapter)


# ── gui-v2 QML logic, transliterated ───────────────────────────────────────


def gx_fuses_menu_visible(svc):
    """PageBattery.qml:376 -- preferredVisible: nrOfDistributors.valid
    && nrOfDistributors.value > 0"""
    n = svc.paths.get("/NrOfDistributors")
    return n is not None and n > 0


def gx_distributor_row(svc, letter):
    """PageLynxDistributorList.qml:38-66 -- (visible, secondaryText)."""
    status = svc.paths.get("/Distributor/%s/Status" % letter)
    visible = status is not None and status != 0          # line 38
    if status is None:
        return visible, "--"                              # line 41
    if status == 0:
        return visible, "Not available"                   # line 43
    if status == 2:
        return visible, "No power on busbar"              # line 46
    if status == 3:
        return visible, "Connection lost"                 # line 49
    blown = 0
    for i in range(8):                                    # lines 22-34: 8 FuseInfo
        # FuseInfo.qml:17 -- blown means /Fuse/<n>/Status == 3;
        # uids for fuses 4-7 do not exist on our service -> undefined.
        if svc.paths.get("/Distributor/%s/Fuse/%d/Status" % (letter, i)) == 3:
            blown += 1
    if blown > 1:
        return visible, "%d fuse(s) blown" % blown        # line 61
    if blown == 1:
        return visible, "Fuse blown"                      # line 63
    return visible, "Ok"                                  # line 65


def gx_fuse_detail_page(svc, letter):
    """PageLynxDistributorList.qml:80-112 -- list of (label, secondary)
    rows, or None when the header shows 'No information available'."""
    connected = svc.paths.get("/Distributor/%s/Status" % letter) == 1  # line 21
    if not connected:                                     # lines 85-87
        return None
    rows = []
    for i in range(4):                                    # fuses 0-3 always visible (line 108)
        name = svc.paths.get("/Distributor/%s/Fuse/%d/Name" % (letter, i)) or ""
        label = name or "Fuse %d" % (i + 1)               # line 91
        status = svc.paths.get("/Distributor/%s/Fuse/%d/Status" % (letter, i))
        secondary = {0: "Not available", 1: "Not used",
                     2: "Ok", 3: "Blown"}.get(status, "") # lines 93-105
        rows.append((label, secondary))
    return rows


def gx_battery_alarms_fuse_blown(svc):
    """PageBatteryAlarms.qml:114 -- /Alarms/FuseBlown (0=Ok 1=Warn 2=Alarm)."""
    return svc.paths.get("/Alarms/FuseBlown")


# ── the contract ───────────────────────────────────────────────────────────


def test_service_type_routes_to_battery_pages(env):
    # DeviceListPage.qml:30 picks the delegate from the service type; only
    # DeviceListDelegate_battery pushes PageBattery, the sole reader of
    # /NrOfDistributors in all of gui-v2.
    assert env.svc.servicename.startswith("com.victronenergy.battery.")
    assert env.svc.registered


def test_mandatory_device_paths_exist(env):
    for path in ("/Mgmt/ProcessName", "/Mgmt/ProcessVersion",
                 "/Mgmt/Connection", "/DeviceInstance", "/ProductId",
                 "/ProductName", "/CustomName", "/Connected"):
        assert path in env.svc.paths, path


def test_every_uid_gui_v2_reads_is_published(env):
    uids = ["/NrOfDistributors", "/Alarms/FuseBlown"]
    for letter in ("A", "B"):
        uids.append("/Distributor/%s/Status" % letter)
        for i in range(4):
            uids.append("/Distributor/%s/Fuse/%d/Name" % (letter, i))
            uids.append("/Distributor/%s/Fuse/%d/Status" % (letter, i))
            uids.append("/Distributor/%s/Fuse/%d/Alarms/Blown" % (letter, i))
        uids.append("/Distributor/%s/Alarms/ConnectionLost" % letter)
    for uid in uids:
        assert uid in env.svc.paths, "gui-v2 reads %s but we never publish it" % uid


def test_no_battery_data_paths_leak(env):
    # Publishing /Dc/*, /Soc or /Info/* would make systemcalc treat us as
    # a real battery monitor candidate.
    for path in env.svc.paths:
        assert not path.startswith(("/Dc/", "/Soc", "/Info/")), path


def test_before_first_poll_rows_hidden(lynx_service, monkeypatch, tmp_path):
    monkeypatch.setattr(lynx_service, "VeDbusService", FakeVeDbusService)
    monkeypatch.setattr(lynx_service, "SettingsDevice", FakeSettingsDevice)
    (tmp_path / "config.ini").write_text("[DEFAULT]\ndistributors = A, B\n")
    config = lynx_service.load_config(str(tmp_path))
    service = lynx_service.LynxBatteryService(None, config, "test")
    svc = service._service
    assert gx_fuses_menu_visible(svc)  # menu appears immediately (N=2)
    for letter in ("A", "B"):
        visible, _ = gx_distributor_row(svc, letter)
        assert not visible  # Status 0 = Not available -> row hidden


def test_all_ok_renders_ok(env):
    env.monitor._poll()
    assert gx_fuses_menu_visible(env.svc)
    assert gx_distributor_row(env.svc, "A") == (True, "Ok")
    assert gx_distributor_row(env.svc, "B") == (True, "Ok")
    assert env.svc.paths["/Connected"] == 1
    assert gx_battery_alarms_fuse_blown(env.svc) == 0


def poll_change(env):
    """Changed bytes publish after confirmation on the next poll."""
    env.monitor._poll()
    env.monitor._poll()


def test_fuse_names_and_not_used_rows(env):
    env.monitor._poll()
    # A: 4 populated, custom names for 1-2 (config fuse_names_a)
    assert gx_fuse_detail_page(env.svc, "A") == [
        ("Inverter", "Ok"), ("Solar", "Ok"), ("Fuse 3", "Ok"), ("Fuse 4", "Ok")]
    # B: only 2 populated -> 3/4 show "Not used" even though the wire
    # reads those positions as blown (0xC0 bits float high when empty)
    env.adapter.responses[ADDRESSES["B"]] = 0xC0
    poll_change(env)
    assert gx_fuse_detail_page(env.svc, "B") == [
        ("Fuse 1", "Ok"), ("Fuse 2", "Ok"),
        ("Fuse 3", "Not used"), ("Fuse 4", "Not used")]
    assert gx_distributor_row(env.svc, "B") == (True, "Ok")
    assert gx_battery_alarms_fuse_blown(env.svc) == 0


def test_single_blown_fuse(env):
    env.adapter.responses[ADDRESSES["A"]] = 0x10  # fuse 1
    poll_change(env)
    assert gx_distributor_row(env.svc, "A") == (True, "Fuse blown")
    assert gx_fuse_detail_page(env.svc, "A")[0] == ("Inverter", "Blown")
    assert env.svc.paths["/Distributor/A/Fuse/0/Alarms/Blown"] == 2
    assert gx_battery_alarms_fuse_blown(env.svc) == 2
    # B unaffected
    assert gx_distributor_row(env.svc, "B") == (True, "Ok")


def test_multiple_blown_fuses_counted(env):
    env.adapter.responses[ADDRESSES["A"]] = 0x30  # fuses 1+2
    poll_change(env)
    assert gx_distributor_row(env.svc, "A") == (True, "2 fuse(s) blown")


def test_no_bus_power(env):
    env.adapter.responses[ADDRESSES["A"]] = 0x02
    poll_change(env)
    assert gx_distributor_row(env.svc, "A") == (True, "No power on busbar")
    # Detail page shows the "no information" header (status != 1)
    assert gx_fuse_detail_page(env.svc, "A") is None
    # No-supply is not a fuse alarm
    assert gx_battery_alarms_fuse_blown(env.svc) == 0


def test_no_bus_power_never_raises_spurious_fuse_alarms(env):
    # With the busbar unpowered the fuse bits are garbage; 0xF2 (all fuse
    # bits + no-supply) must not alarm or show blown fuses.
    env.adapter.responses[ADDRESSES["A"]] = 0xF2
    poll_change(env)
    assert gx_distributor_row(env.svc, "A") == (True, "No power on busbar")
    for i in range(4):
        assert env.svc.paths["/Distributor/A/Fuse/%d/Status" % i] == 0
        assert env.svc.paths["/Distributor/A/Fuse/%d/Alarms/Blown" % i] == 0
    assert gx_battery_alarms_fuse_blown(env.svc) == 0
    # Power returns with a genuinely blown fuse -> alarm resumes correctly
    env.adapter.responses[ADDRESSES["A"]] = 0x10
    poll_change(env)
    assert gx_distributor_row(env.svc, "A") == (True, "Fuse blown")
    assert gx_battery_alarms_fuse_blown(env.svc) == 2


def test_comms_lost_after_three_nacks(env):
    env.monitor._poll()
    env.adapter.responses[ADDRESSES["B"]] = I2CNackError(ADDRESSES["B"])
    for _ in range(3):
        env.monitor._poll()
    assert gx_distributor_row(env.svc, "B") == (True, "Connection lost")
    assert gx_fuse_detail_page(env.svc, "B") is None
    assert env.svc.paths["/Distributor/B/Alarms/ConnectionLost"] == 2
    # A keeps polling fine
    assert gx_distributor_row(env.svc, "A") == (True, "Ok")
    # Recovery
    env.adapter.responses[ADDRESSES["B"]] = 0x00
    env.monitor._poll()
    assert gx_distributor_row(env.svc, "B") == (True, "Ok")
    assert env.svc.paths["/Distributor/B/Alarms/ConnectionLost"] == 0


def test_blown_alarm_held_through_comms_loss(env):
    env.adapter.responses[ADDRESSES["A"]] = 0x10
    poll_change(env)
    assert gx_battery_alarms_fuse_blown(env.svc) == 2
    env.adapter.responses[ADDRESSES["A"]] = I2CNackError(ADDRESSES["A"])
    for _ in range(3):
        env.monitor._poll()
    # Row shows the comms problem, but the fuse alarm is not cleared
    assert gx_distributor_row(env.svc, "A") == (True, "Connection lost")
    assert env.svc.paths["/Distributor/A/Fuse/0/Alarms/Blown"] == 2
    assert gx_battery_alarms_fuse_blown(env.svc) == 2
    # Replacing the fuse (read 0x00 again) clears everything
    env.adapter.responses[ADDRESSES["A"]] = 0x00
    env.monitor._poll()
    assert gx_distributor_row(env.svc, "A") == (True, "Ok")
    assert gx_battery_alarms_fuse_blown(env.svc) == 0


def test_adapter_unplug_and_replug(env):
    env.monitor._poll()
    env.adapter.responses[ADDRESSES["A"]] = CH347Error("USB gone")
    env.monitor._poll()  # I/O error -> adapter dropped
    assert env.monitor.adapter is None
    assert env.adapter.closed
    assert env.svc.paths["/Connected"] == 0
    for letter in ("A", "B"):
        assert gx_distributor_row(env.svc, letter) == (True, "Connection lost")
    # Replug: open() succeeds again and readings resume
    env.adapter.responses[ADDRESSES["A"]] = 0x00
    env.adapter.closed = False
    env.monitor._poll()
    assert env.svc.paths["/Connected"] == 1
    assert gx_distributor_row(env.svc, "A") == (True, "Ok")


def test_corrupt_byte_discarded(env):
    # Field-observed glitch: one poll returned 0x3F (impossible bits 0x0D)
    # on an otherwise healthy bus. Must not flap state or alarm.
    env.monitor._poll()
    assert gx_distributor_row(env.svc, "A") == (True, "Ok")
    env.adapter.responses[ADDRESSES["A"]] = 0x3F
    env.monitor._poll()
    assert gx_distributor_row(env.svc, "A") == (True, "Ok")
    assert gx_battery_alarms_fuse_blown(env.svc) == 0
    env.adapter.responses[ADDRESSES["A"]] = 0x00
    env.monitor._poll()
    assert gx_distributor_row(env.svc, "A") == (True, "Ok")


def test_persistent_corrupt_reads_become_comms_lost(env):
    env.monitor._poll()
    env.adapter.responses[ADDRESSES["A"]] = 0x3F
    for _ in range(3):
        env.monitor._poll()
    assert gx_distributor_row(env.svc, "A") == (True, "Connection lost")


def test_single_glitch_byte_never_alarms(env):
    # A one-poll 0x30 ("2 fuses blown", plausible bits) must be debounced.
    env.monitor._poll()
    env.adapter.responses[ADDRESSES["A"]] = 0x30
    env.monitor._poll()
    assert gx_distributor_row(env.svc, "A") == (True, "Ok")
    assert gx_battery_alarms_fuse_blown(env.svc) == 0
    env.adapter.responses[ADDRESSES["A"]] = 0x00
    env.monitor._poll()
    env.monitor._poll()
    assert gx_distributor_row(env.svc, "A") == (True, "Ok")
    assert gx_battery_alarms_fuse_blown(env.svc) == 0


def test_real_change_publishes_after_confirmation(env):
    env.monitor._poll()
    env.adapter.responses[ADDRESSES["A"]] = 0x10
    env.monitor._poll()  # first sighting: pending
    assert gx_distributor_row(env.svc, "A") == (True, "Ok")
    env.monitor._poll()  # confirmed
    assert gx_distributor_row(env.svc, "A") == (True, "Fuse blown")
    assert gx_battery_alarms_fuse_blown(env.svc) == 2


def test_corrupt_reads_counted_on_dbus(env):
    # A persistently-corrupt byte burns the whole per-poll attempt budget.
    per_poll = env.module.FuseMonitor.READ_ATTEMPTS
    env.monitor._poll()
    assert env.svc.paths["/Distributor/A/CorruptReads"] == 0
    env.adapter.responses[ADDRESSES["A"]] = 0x3F
    env.monitor._poll()
    env.monitor._poll()
    assert env.svc.paths["/Distributor/A/CorruptReads"] == 2 * per_poll
    assert env.svc.paths["/Distributor/B/CorruptReads"] == 0
    env.adapter.responses[ADDRESSES["A"]] = 0x00
    env.monitor._poll()
    assert env.svc.paths["/Distributor/A/CorruptReads"] == 2 * per_poll


def test_intra_poll_disagreement_filtered(env):
    # A corrupted-but-plausible byte (0x10, "fuse blown") in one
    # transaction is out-voted within the same poll by two agreeing
    # clean reads: no state change, no alarm, one counted discard.
    env.monitor._poll()
    env.adapter.responses[ADDRESSES["A"]] = [0x10, 0x00, 0x00]
    env.monitor._poll()
    assert gx_distributor_row(env.svc, "A") == (True, "Ok")
    assert gx_battery_alarms_fuse_blown(env.svc) == 0
    assert env.svc.paths["/Distributor/A/CorruptReads"] == 1


def test_noisy_poll_gives_up_without_publishing(env):
    # Valid bytes that never agree exhaust the attempt budget: treated
    # as a failed poll, state untouched.
    env.monitor._poll()
    env.adapter.responses[ADDRESSES["A"]] = [0x10, 0x00, 0x10, 0x00, 0x00]
    env.monitor._poll()
    assert gx_distributor_row(env.svc, "A") == (True, "Ok")
    assert gx_battery_alarms_fuse_blown(env.svc) == 0
