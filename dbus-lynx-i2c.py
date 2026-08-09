#!/usr/bin/env python3
# Copyright 2026 Clint Goudie-Nice
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

"""
Venus OS D-Bus service for Victron Lynx Distributor fuse monitoring.

Polls each configured distributor's one-byte fuse status over I2C (via a
CH347 USB-HID adapter on /dev/hidraw*) and publishes a single battery
service mirroring the Lynx Smart BMS distributor schema (Venus wiki
dbus.md, "Lynx Smart BMS" section), so the native GX "Fuses" pages and
alarms render it exactly like the real BMS:

    com.victronenergy.battery.lynx_i2c
        /NrOfDistributors
        /Distributor/<A-D>/Status                0=N/A, 1=Connected,
                                                 2=No bus power, 3=Comms lost
        /Distributor/<A-D>/Alarms/ConnectionLost 0=Ok, 2=Alarm
        /Distributor/<A-D>/Fuse/<0-3>/Name       user-set, 16 bytes max
        /Distributor/<A-D>/Fuse/<0-3>/Status     0=N/A, 1=Not used,
                                                 2=Ok, 3=Blown
        /Distributor/<A-D>/Fuse/<0-3>/Alarms/Blown  0=Ok, 2=Alarm
        /Alarms/FuseBlown                        0=Ok, 2=Alarm (any fuse)

The service publishes no /Dc/0/* or /Soc paths, so systemcalc's battery
auto-selection prefers any real BMS/shunt (they carry /Info/* paths and
lower instances); if auto-select still picks this service, pin the real
monitor in Settings -> System setup -> Battery monitor.
"""

import configparser
import logging
import os
import signal
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(1, os.path.join(BASE_DIR, "ext", "velib_python"))

import dbus  # noqa: E402
from dbus.mainloop.glib import DBusGMainLoop  # noqa: E402
from gi.repository import GLib  # noqa: E402
from vedbus import VeDbusService  # noqa: E402
from settingsdevice import SettingsDevice  # noqa: E402

from ch347 import CH347I2C, CH347Error, I2CNackError, SPEED_LEVELS  # noqa: E402
from lynx_distributor import (  # noqa: E402
    ADDRESSES, MAX_FUSES, FuseStatus, decode, describe)

VERSION = "0.3.0"

# Lynx Smart BMS distributor conventions (Venus wiki dbus.md + gui-v2
# PageLynxDistributorList.qml / FuseInfo.qml):
DIST_NOT_AVAILABLE = 0
DIST_CONNECTED = 1
DIST_NO_BUS_POWER = 2
DIST_COMMS_LOST = 3

FUSE_NOT_AVAILABLE = 0
FUSE_NOT_USED = 1
FUSE_OK = 2
FUSE_BLOWN = 3

ALARM_OK = 0
ALARM_ALARM = 2

FUSE_NAME_MAX_BYTES = 16  # firmware limit on the real BMS; we match it

# Consecutive failed polls before a distributor is marked comms-lost.
FAILS_BEFORE_DISCONNECT = 3

log = logging.getLogger("dbus-lynx-i2c")


class ConfigError(Exception):
    pass


ADAPTER_TYPES = ("ch347", "kernel-i2c", "mock")


class Config:
    def __init__(self, letters, fuse_counts, fuse_names, poll_interval,
                 i2c_speed_hz, hidraw_device, adapter="ch347",
                 i2c_device=None):
        self.letters = letters
        self.fuse_counts = fuse_counts  # {letter: populated fuse positions}
        self.fuse_names = fuse_names    # {letter: [name, ...] padded to MAX_FUSES}
        self.poll_interval = poll_interval
        self.i2c_speed_hz = i2c_speed_hz
        self.hidraw_device = hidraw_device
        self.adapter = adapter          # one of ADAPTER_TYPES
        self.i2c_device = i2c_device    # kernel-i2c only: /dev/i2c-N

    @property
    def mock(self):
        return self.adapter == "mock"


def _parse_fuse_names(sec, letters):
    """Optional per-distributor fuse names: ``fuse_names_a = Inverter, ...``"""
    names = {}
    for letter in letters:
        raw = sec.get("fuse_names_%s" % letter.lower(), "").strip()
        parts = [p.strip() for p in raw.split(",")] if raw else []
        if len(parts) > MAX_FUSES:
            raise ConfigError(
                "fuse_names_%s has %d entries; max %d"
                % (letter.lower(), len(parts), MAX_FUSES))
        padded = []
        for i in range(MAX_FUSES):
            name = parts[i] if i < len(parts) else ""
            if len(name.encode("utf-8")) > FUSE_NAME_MAX_BYTES:
                raise ConfigError(
                    "fuse name %r exceeds %d bytes (the GUI matches the "
                    "BMS firmware limit)" % (name, FUSE_NAME_MAX_BYTES))
            padded.append(name)
        names[letter] = padded
    return names


def load_config(base_dir: str) -> Config:
    """Read config.default.ini overlaid by optional config.ini."""
    cp = configparser.ConfigParser()
    cp.read([os.path.join(base_dir, "config.default.ini"),
             os.path.join(base_dir, "config.ini")])
    sec = cp["DEFAULT"]

    letters = [s.strip().upper()
               for s in sec.get("distributors", "A").split(",") if s.strip()]
    if not letters:
        raise ConfigError("no distributors configured")
    for letter in letters:
        if letter not in ADDRESSES:
            raise ConfigError(
                "unknown distributor %r; valid: %s"
                % (letter, ", ".join(sorted(ADDRESSES))))
    if len(set(letters)) != len(letters):
        raise ConfigError("duplicate distributor letters: %s" % letters)

    raw_counts = sec.get("fuses_per_distributor", "").strip()
    if raw_counts:
        counts = [s.strip() for s in raw_counts.split(",")]
        if len(counts) != len(letters):
            raise ConfigError(
                "fuses_per_distributor has %d entries for %d distributors"
                % (len(counts), len(letters)))
        try:
            counts = [int(c) for c in counts]
        except ValueError:
            raise ConfigError("fuses_per_distributor must be integers")
        for c in counts:
            if not 0 <= c <= MAX_FUSES:
                raise ConfigError("fuse count %d out of range 0..%d"
                                  % (c, MAX_FUSES))
        fuse_counts = dict(zip(letters, counts))
    else:
        fuse_counts = {letter: MAX_FUSES for letter in letters}

    fuse_names = _parse_fuse_names(sec, letters)

    try:
        poll_interval = sec.getfloat("poll_interval", 5.0)
    except ValueError:
        raise ConfigError("poll_interval must be a number")
    if poll_interval < 0.5:
        raise ConfigError("poll_interval must be >= 0.5 seconds")

    try:
        i2c_speed_hz = sec.getint("i2c_speed_hz", 20000)
    except ValueError:
        raise ConfigError("i2c_speed_hz must be an integer")
    if i2c_speed_hz not in SPEED_LEVELS:
        raise ConfigError("i2c_speed_hz must be one of %s"
                          % sorted(SPEED_LEVELS))

    hidraw_device = sec.get("hidraw_device", "").strip() or None
    i2c_device = sec.get("i2c_device", "").strip() or None

    adapter = sec.get("adapter", "ch347").strip().lower()
    if adapter not in ADAPTER_TYPES:
        raise ConfigError("adapter must be one of %s" % (ADAPTER_TYPES,))
    try:
        # Legacy alias for adapter = mock
        if sec.getboolean("mock", False):
            adapter = "mock"
    except ValueError:
        raise ConfigError("mock must be a boolean")

    return Config(letters, fuse_counts, fuse_names, poll_interval,
                  i2c_speed_hz, hidraw_device, adapter=adapter,
                  i2c_device=i2c_device)


def distributor_status_value(status: FuseStatus) -> int:
    """Map a successful read to /Distributor/X/Status."""
    return DIST_NO_BUS_POWER if status.no_supply else DIST_CONNECTED


def fuse_status_values(status: FuseStatus, num_fuses: int) -> list:
    """Map a successful read to the four /Fuse/n/Status values (0-indexed).

    With the busbar unpowered the fuse bits are meaningless -- detection
    measures voltage across each fuse -- so populated positions publish
    Not available instead of trusting the bits (hardware-confirmed by
    twam/dbus-lynx-distributor and pulquero/dbus-i2c; prevents spurious
    blown-fuse alarms when the bus is simply switched off).
    """
    values = []
    for i in range(MAX_FUSES):
        if i >= num_fuses:
            values.append(FUSE_NOT_USED)
        elif status.no_supply:
            values.append(FUSE_NOT_AVAILABLE)
        elif status.fuses[i]:
            values.append(FUSE_BLOWN)
        else:
            values.append(FUSE_OK)
    return values


class LynxBatteryService:
    """Single battery service mirroring the Lynx Smart BMS fuse schema."""

    def __init__(self, bus, config: Config, connection: str):
        self.config = config
        self.fail_counts = {letter: 0 for letter in config.letters}
        self._last_raw = {letter: None for letter in config.letters}
        self._pending = {}  # letter -> unconfirmed changed/corrupt byte

        default_name = "Lynx Distributor Monitor"
        settings_base = "/Settings/Devices/lynx_i2c"
        self._settings = SettingsDevice(
            bus,
            {
                # systemcalc's battery auto-select breaks ties (services
                # without /Info/MaxChargeVoltage, e.g. us vs. a SmartShunt)
                # by LOWEST device instance, and real monitors sit at
                # ~245-512.  Default far above so we always lose the tie.
                "instance": ["%s/ClassAndVrmInstance" % settings_base,
                             "battery:990", 0, 0],
                "customname": ["%s/CustomName" % settings_base, "", 0, 0],
            },
            eventCallback=self._setting_changed,
            timeout=120,
        )
        instance = int(self._settings["instance"].split(":")[1])

        svc = VeDbusService("com.victronenergy.battery.lynx_i2c", bus,
                            register=False)
        svc.add_path("/Mgmt/ProcessName", __file__)
        svc.add_path("/Mgmt/ProcessVersion", VERSION)
        svc.add_path("/Mgmt/Connection", connection)
        svc.add_path("/DeviceInstance", instance)
        svc.add_path("/ProductId", 0xFFFF)
        svc.add_path("/ProductName",
                     self._settings["customname"] or default_name)
        svc.add_path("/CustomName", self._settings["customname"],
                     writeable=True, onchangecallback=self._customname_written)
        svc.add_path("/FirmwareVersion", VERSION)
        svc.add_path("/HardwareVersion", None)
        # 0 until the adapter proves itself with a completed transfer.
        svc.add_path("/Connected", 0)

        svc.add_path("/NrOfDistributors", len(config.letters))
        svc.add_path("/Alarms/FuseBlown", ALARM_OK)
        for letter in config.letters:
            base = "/Distributor/%s" % letter
            svc.add_path("%s/Status" % base, DIST_NOT_AVAILABLE,
                         gettextcallback=self._status_text)
            svc.add_path("%s/Alarms/ConnectionLost" % base, ALARM_OK)
            # Diagnostic: corrupt frames discarded by the validity guard
            # (bus noise trend can be watched here instead of the logs).
            svc.add_path("%s/CorruptReads" % base, 0)
            for i in range(MAX_FUSES):
                svc.add_path("%s/Fuse/%d/Name" % (base, i),
                             config.fuse_names[letter][i],
                             writeable=True,
                             onchangecallback=self._fuse_name_written)
                svc.add_path("%s/Fuse/%d/Status" % (base, i),
                             FUSE_NOT_AVAILABLE)
                svc.add_path("%s/Fuse/%d/Alarms/Blown" % (base, i), ALARM_OK)
        svc.register()
        self._service = svc
        self._default_name = default_name
        log.info("registered com.victronenergy.battery.lynx_i2c "
                 "(instance %d, distributors %s)",
                 instance, ", ".join(config.letters))

    @staticmethod
    def _status_text(path, value):
        return {DIST_NOT_AVAILABLE: "Not available",
                DIST_CONNECTED: "Connected",
                DIST_NO_BUS_POWER: "No bus power",
                DIST_COMMS_LOST: "Communications lost"}.get(value, str(value))

    # ── settings plumbing ───────────────────────────────────────────────

    def _customname_written(self, path, value):
        self._settings["customname"] = value
        self._service["/ProductName"] = value or self._default_name
        return True

    def _setting_changed(self, setting, oldvalue, newvalue):
        if setting == "customname":
            self._service["/CustomName"] = newvalue
            self._service["/ProductName"] = newvalue or self._default_name

    def _fuse_name_written(self, path, value):
        # GUI-side renames are accepted but only live until restart; use
        # config.ini fuse_names_<letter> for persistent names.
        if len(str(value).encode("utf-8")) > FUSE_NAME_MAX_BYTES:
            return False
        return True

    # ── poll results ────────────────────────────────────────────────────

    def update(self, letter: str, raw: int) -> None:
        """Publish a successfully-read status byte for one distributor.

        Two guards against corrupted bus transactions (observed in the
        field: a one-poll 0x3F glitch on an otherwise healthy bus):
        a byte with impossible bits is discarded as a failed poll, and a
        *changed* byte must repeat on the next poll before it publishes,
        so a single glitch can never flap state or raise a false alarm.
        A real fuse event still publishes within two poll intervals.
        """
        num_fuses = self.config.fuse_counts[letter]
        status = decode(raw, num_fuses)
        if status.unknown_bits:
            if raw != self._pending.get(letter):
                log.warning("distributor %s: corrupt read 0x%02X (impossible "
                            "bits 0x%02X), discarding",
                            letter, raw, status.unknown_bits)
            self._pending[letter] = raw
            path = "/Distributor/%s/CorruptReads" % letter
            self._service[path] = self._service[path] + 1
            self.comm_failure(letter)
            return
        self.fail_counts[letter] = 0
        last = self._last_raw[letter]
        if last is not None and raw != last and self._pending.get(letter) != raw:
            self._pending[letter] = raw  # changed: await confirmation
            return
        self._pending.pop(letter, None)
        if raw != self._last_raw[letter]:
            log.info("distributor %s: %s", letter, describe(status))
        self._last_raw[letter] = raw
        base = "/Distributor/%s" % letter
        with self._service as s:
            s["%s/Status" % base] = distributor_status_value(status)
            s["%s/Alarms/ConnectionLost" % base] = ALARM_OK
            for i, value in enumerate(fuse_status_values(status, num_fuses)):
                s["%s/Fuse/%d/Status" % (base, i)] = value
                s["%s/Fuse/%d/Alarms/Blown" % (base, i)] = (
                    ALARM_ALARM if value == FUSE_BLOWN else ALARM_OK)
            self._publish_fuse_blown(s)

    def comm_failure(self, letter: str) -> None:
        """One failed poll; mark comms-lost after a few in a row."""
        self.fail_counts[letter] += 1
        if self.fail_counts[letter] == FAILS_BEFORE_DISCONNECT:
            log.warning("distributor %s: no response after %d polls, "
                        "marking communications lost",
                        letter, self.fail_counts[letter])
            self._mark_comms_lost(letter)

    def adapter_lost(self) -> None:
        with self._service as s:
            s["/Connected"] = 0
        for letter in self.config.letters:
            self._mark_comms_lost(letter)

    def adapter_found(self) -> None:
        self._service["/Connected"] = 1

    def _mark_comms_lost(self, letter: str) -> None:
        self._last_raw[letter] = None
        self._pending.pop(letter, None)
        base = "/Distributor/%s" % letter
        with self._service as s:
            s["%s/Status" % base] = DIST_COMMS_LOST
            s["%s/Alarms/ConnectionLost" % base] = ALARM_ALARM
            for i in range(MAX_FUSES):
                s["%s/Fuse/%d/Status" % (base, i)] = FUSE_NOT_AVAILABLE
            # Fuse /Alarms/Blown values are deliberately held: a dead bus
            # must not silently clear an active fuse alarm.

    def _publish_fuse_blown(self, s) -> None:
        blown = any(
            s["/Distributor/%s/Fuse/%d/Alarms/Blown" % (letter, i)]
            == ALARM_ALARM
            for letter in self.config.letters
            for i in range(MAX_FUSES))
        s["/Alarms/FuseBlown"] = ALARM_ALARM if blown else ALARM_OK


class FuseMonitor:
    """Owns the CH347 adapter and the poll loop."""

    def __init__(self, bus, config: Config):
        self.config = config
        self.adapter = None
        # Logging is transition-based: a wedged adapter (opens fine, every
        # read fails) must not emit a warning burst on each poll cycle.
        self._outage_logged = False
        self._mock_logged = False
        self._healthy = False  # a transfer succeeded on the current adapter
        if config.mock:
            connection = "Mock adapter (no hardware)"
        elif config.adapter == "kernel-i2c":
            connection = "Kernel I2C (%s)" % (config.i2c_device
                                              or "auto CP2112")
        else:
            connection = "CH347 HID-I2C (%s)" % (config.hidraw_device or "auto")
        self.service = LynxBatteryService(bus, config, connection)

    def start(self) -> None:
        self._poll()
        GLib.timeout_add(int(self.config.poll_interval * 1000), self._poll)

    def stop(self) -> None:
        self._drop_adapter(log_it=False)

    # ── adapter lifecycle ───────────────────────────────────────────────

    def _ensure_adapter(self) -> bool:
        if self.adapter is not None:
            return True
        if self.config.mock:
            from mock_adapter import MockAdapter, STATE_FILENAME
            self.adapter = MockAdapter(os.path.join(BASE_DIR, STATE_FILENAME))
            if not self._mock_logged:
                log.warning("MOCK MODE: simulating distributors from %s",
                            self.adapter.state_path)
                self._mock_logged = True
            return True
        try:
            if self.config.adapter == "kernel-i2c":
                from kernel_i2c import KernelI2C
                self.adapter = KernelI2C.open(path=self.config.i2c_device)
            else:
                self.adapter = CH347I2C.open(
                    path=self.config.hidraw_device,
                    speed_hz=self.config.i2c_speed_hz)
        except (CH347Error, OSError) as e:
            if not self._outage_logged:
                log.warning("CH347 adapter unavailable: %s (will keep "
                            "retrying)", e)
                self._outage_logged = True
            return False
        return True

    def _mark_healthy(self) -> None:
        """A transfer completed (data or NACK): the adapter itself works."""
        if not self._healthy:
            self._healthy = True
            self._outage_logged = False
            speed = ("%d Hz" % self.adapter.speed_hz
                     if self.adapter.speed_hz else "bus-defined rate")
            log.info("I2C adapter OK on %s (%s)",
                     self.adapter.transport.path, speed)
            self.service.adapter_found()

    def _drop_adapter(self, log_it: bool = True) -> None:
        if self.adapter is None:
            return
        try:
            self.adapter.close()
        except OSError:
            pass
        self.adapter = None
        if log_it and self._healthy:
            log.warning("CH347 adapter lost; will retry")
        self._healthy = False
        self.service.adapter_lost()

    # ── poll loop ───────────────────────────────────────────────────────

    def _poll(self) -> bool:
        if not self._ensure_adapter():
            for letter in self.config.letters:
                self.service.comm_failure(letter)
            return True
        for letter in self.config.letters:
            try:
                raw = self.adapter.i2c_read(ADDRESSES[letter], 1)[0]
            except I2CNackError:
                # The USB round-trip worked; only the distributor is silent.
                self._mark_healthy()
                self.service.comm_failure(letter)
            except (CH347Error, OSError) as e:
                if self._healthy or not self._outage_logged:
                    log.warning("adapter I/O failed: %s (will keep "
                                "retrying)", e)
                    self._outage_logged = True
                self._drop_adapter()
                return True
            else:
                self._mark_healthy()
                self.service.update(letter, raw)
        return True


def dbusconnection():
    return (dbus.SessionBus()
            if "DBUS_SESSION_BUS_ADDRESS" in os.environ
            else dbus.SystemBus())


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(message)s")
    log.info("dbus-lynx-i2c v%s starting", VERSION)

    try:
        config = load_config(BASE_DIR)
    except ConfigError as e:
        log.error("configuration error: %s", e)
        sys.exit(1)
    log.info("distributors: %s; poll every %.1fs; I2C %d Hz",
             ", ".join("%s(0x%02X, %d fuses)"
                       % (l, ADDRESSES[l], config.fuse_counts[l])
                       for l in config.letters),
             config.poll_interval, config.i2c_speed_hz)

    DBusGMainLoop(set_as_default=True)
    monitor = FuseMonitor(dbusconnection(), config)

    mainloop = GLib.MainLoop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig,
                             lambda *a: (mainloop.quit(), False)[1])
    monitor.start()
    try:
        mainloop.run()
    finally:
        monitor.stop()
    log.info("dbus-lynx-i2c stopped")


if __name__ == "__main__":
    main()
