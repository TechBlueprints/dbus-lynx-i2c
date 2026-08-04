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
CH347 USB-HID adapter on /dev/hidraw*) and publishes one digital-input
style D-Bus service per distributor:

    com.victronenergy.digitalinput.lynx_distributor_a
        /State        8 = OK, 9 = Alarm            (GX device list)
        /Alarm        0 = ok, 2 = alarm            (GX/VRM notifications)
        /InputState   0/1 mirror of the alarm condition
        /Distributor/StatusByte, /Distributor/NoBusSupply
        /Fuses/<n>/Blown  per populated fuse position

The digitalinput service class is provisional (README "Software plan"
step 3): it renders in the GX device list with an ok/alarm state and its
/Alarm path follows the dbus-digitalinputs convention (0/2), pending
verification of what systemcalc/VRM actually display.
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
from lynx_distributor import ADDRESSES, MAX_FUSES, decode, describe  # noqa: E402

VERSION = "0.1.0"

# dbus-digitalinputs conventions (see victronenergy/dbus-digitalinputs):
PRODUCT_ID = 0xA166           # digital input
TYPE_GENERIC_IO = 10          # /Type "Generic I/O"
STATE_OK = 8                  # /State, "ok/alarm" translation pair
STATE_ALARM = 9
ALARM_OK = 0                  # /Alarm
ALARM_ALARM = 2

# Consecutive failed polls before a distributor is marked disconnected.
FAILS_BEFORE_DISCONNECT = 3
# Seconds between attempts to (re)open the CH347 adapter.
ADAPTER_RETRY_SECONDS = 10

log = logging.getLogger("dbus-lynx-i2c")


class ConfigError(Exception):
    pass


class Config:
    def __init__(self, letters, fuse_counts, poll_interval,
                 i2c_speed_hz, hidraw_device):
        self.letters = letters
        self.fuse_counts = fuse_counts  # {letter: populated fuse positions}
        self.poll_interval = poll_interval
        self.i2c_speed_hz = i2c_speed_hz
        self.hidraw_device = hidraw_device


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

    return Config(letters, fuse_counts, poll_interval,
                  i2c_speed_hz, hidraw_device)


class DistributorService:
    """One com.victronenergy.digitalinput.* service per Lynx Distributor."""

    def __init__(self, bus, letter: str, num_fuses: int, connection: str):
        self.letter = letter
        self.address = ADDRESSES[letter]
        self.num_fuses = num_fuses
        self.fail_count = 0
        self._alarm_condition = False
        self._last_raw = None

        key = "lynx_distributor_%s" % letter.lower()
        default_name = "Lynx Distributor %s" % letter
        settings_base = "/Settings/Devices/%s" % key
        default_instance = 100 + ord(letter) - ord("A")

        self._settings = SettingsDevice(
            bus,
            {
                "instance": ["%s/ClassAndVrmInstance" % settings_base,
                             "digitalinput:%d" % default_instance, 0, 0],
                "customname": ["%s/CustomName" % settings_base, "", 0, 0],
                "alarmsetting": ["%s/AlarmSetting" % settings_base, 1, 0, 1],
            },
            eventCallback=self._setting_changed,
            timeout=120,
        )
        instance = int(self._settings["instance"].split(":")[1])

        svc = VeDbusService(
            "com.victronenergy.digitalinput.%s" % key, bus, register=False)
        svc.add_path("/Mgmt/ProcessName", __file__)
        svc.add_path("/Mgmt/ProcessVersion", VERSION)
        svc.add_path("/Mgmt/Connection", connection)
        svc.add_path("/DeviceInstance", instance)
        svc.add_path("/ProductId", PRODUCT_ID)
        svc.add_path("/ProductName",
                     self._settings["customname"] or default_name)
        svc.add_path("/CustomName", self._settings["customname"],
                     writeable=True, onchangecallback=self._customname_written)
        svc.add_path("/FirmwareVersion", None)
        svc.add_path("/HardwareVersion", None)
        svc.add_path("/Connected", 0)

        svc.add_path("/Type", TYPE_GENERIC_IO)
        svc.add_path("/State", STATE_OK,
                     gettextcallback=lambda p, v:
                         "alarm" if v == STATE_ALARM else "ok")
        svc.add_path("/InputState", 0)
        svc.add_path("/Alarm", ALARM_OK)
        svc.add_path("/Count", 0)
        svc.add_path("/Settings/AlarmSetting", self._settings["alarmsetting"],
                     writeable=True, onchangecallback=self._alarmsetting_written)

        svc.add_path("/Distributor/Letter", letter)
        svc.add_path("/Distributor/Address", self.address)
        svc.add_path("/Distributor/NumberOfFuses", num_fuses)
        svc.add_path("/Distributor/StatusByte", None)
        svc.add_path("/Distributor/NoBusSupply", None)
        for i in range(1, MAX_FUSES + 1):
            svc.add_path("/Fuses/%d/Blown" % i, None)
        svc.register()
        self._service = svc
        self._default_name = default_name
        log.info("registered com.victronenergy.digitalinput.%s "
                 "(instance %d, addr 0x%02X, %d fuses)",
                 key, instance, self.address, num_fuses)

    # ── settings plumbing ───────────────────────────────────────────────

    def _customname_written(self, path, value):
        self._settings["customname"] = value
        self._service["/ProductName"] = value or self._default_name
        return True

    def _alarmsetting_written(self, path, value):
        if value not in (0, 1):
            return False
        self._settings["alarmsetting"] = value
        self._publish_alarm()
        return True

    def _setting_changed(self, setting, oldvalue, newvalue):
        # A setting changed behind our back (e.g. via com.victronenergy.settings)
        if setting == "customname":
            self._service["/CustomName"] = newvalue
            self._service["/ProductName"] = newvalue or self._default_name
        elif setting == "alarmsetting":
            self._service["/Settings/AlarmSetting"] = newvalue
            self._publish_alarm()

    def _publish_alarm(self):
        active = self._alarm_condition and bool(self._settings["alarmsetting"])
        self._service["/Alarm"] = ALARM_ALARM if active else ALARM_OK

    # ── poll results ────────────────────────────────────────────────────

    def update(self, raw: int) -> None:
        """Publish a successfully-read status byte."""
        self.fail_count = 0
        status = decode(raw, self.num_fuses)
        if raw != self._last_raw:
            log.info("distributor %s: %s", self.letter, describe(status))
            self._last_raw = raw
        was_active = self._alarm_condition
        self._alarm_condition = status.alarm_active
        with self._service as s:
            s["/Connected"] = 1
            s["/Distributor/StatusByte"] = raw
            s["/Distributor/NoBusSupply"] = int(status.no_supply)
            for i in range(1, MAX_FUSES + 1):
                s["/Fuses/%d/Blown" % i] = (
                    int(status.fuses[i - 1]) if i <= self.num_fuses else None)
            s["/InputState"] = int(status.alarm_active)
            s["/State"] = STATE_ALARM if status.alarm_active else STATE_OK
            if status.alarm_active and not was_active:
                s["/Count"] = self._service["/Count"] + 1
            s["/Alarm"] = (ALARM_ALARM
                           if status.alarm_active
                           and bool(self._settings["alarmsetting"])
                           else ALARM_OK)

    def comm_failure(self) -> None:
        """One failed poll; mark disconnected after a few in a row."""
        self.fail_count += 1
        if self.fail_count == FAILS_BEFORE_DISCONNECT:
            log.warning("distributor %s: no response after %d polls, "
                        "marking disconnected", self.letter, self.fail_count)
            self.mark_disconnected()

    def mark_disconnected(self) -> None:
        self._last_raw = None
        with self._service as s:
            s["/Connected"] = 0
            s["/Distributor/StatusByte"] = None
            s["/Distributor/NoBusSupply"] = None
            for i in range(1, MAX_FUSES + 1):
                s["/Fuses/%d/Blown" % i] = None
        # /State, /Alarm and the alarm condition are left as-is: a dead bus
        # must not silently clear an active fuse alarm.


class FuseMonitor:
    """Owns the CH347 adapter and the per-distributor poll loop."""

    def __init__(self, bus, config: Config):
        self.config = config
        self.adapter = None
        self._adapter_error_logged = False
        connection = "CH347 HID-I2C (%s)" % (config.hidraw_device or "auto")
        self.services = [
            DistributorService(bus, letter, config.fuse_counts[letter],
                               connection)
            for letter in config.letters
        ]

    def start(self) -> None:
        self._poll()
        GLib.timeout_add(int(self.config.poll_interval * 1000), self._poll)

    def stop(self) -> None:
        self._drop_adapter(log_it=False)

    # ── adapter lifecycle ───────────────────────────────────────────────

    def _ensure_adapter(self) -> bool:
        if self.adapter is not None:
            return True
        try:
            self.adapter = CH347I2C.open(
                path=self.config.hidraw_device,
                speed_hz=self.config.i2c_speed_hz)
        except (CH347Error, OSError) as e:
            if not self._adapter_error_logged:
                log.warning("CH347 adapter unavailable: %s (retrying every "
                            "poll)", e)
                self._adapter_error_logged = True
            return False
        self._adapter_error_logged = False
        log.info("CH347 adapter opened on %s at %d Hz",
                 self.adapter.transport.path, self.config.i2c_speed_hz)
        return True

    def _drop_adapter(self, log_it: bool = True) -> None:
        if self.adapter is None:
            return
        try:
            self.adapter.close()
        except OSError:
            pass
        self.adapter = None
        if log_it:
            log.warning("CH347 adapter lost; will retry")
        for svc in self.services:
            svc.mark_disconnected()

    # ── poll loop ───────────────────────────────────────────────────────

    def _poll(self) -> bool:
        if not self._ensure_adapter():
            for svc in self.services:
                svc.comm_failure()
            return True
        for svc in self.services:
            try:
                raw = self.adapter.i2c_read(svc.address, 1)[0]
            except I2CNackError:
                svc.comm_failure()
            except (CH347Error, OSError) as e:
                log.warning("adapter I/O failed: %s", e)
                self._drop_adapter()
                return True
            else:
                svc.update(raw)
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
