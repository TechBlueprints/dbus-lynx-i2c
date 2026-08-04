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
Kernel /dev/i2c-N backend, pure Python stdlib (fcntl.ioctl -- no smbus2,
which Venus OS does not ship).

This is the CP2112 fallback path: a Silabs CP2112 (e.g. the CP2112EK
eval kit) is driven by the in-kernel hid-cp2112 driver, which Venus OS
ships stock, and appears as a native I2C bus -- on a Cerbo GX the
internal buses are i2c-0..3 (SoC) and i2c-4 (HDMI DDC), so the CP2112
lands on i2c-5.  It also works with any other adapter that yields a
kernel I2C bus.  The bus clock is set by the kernel driver (CP2112
default: 100 kHz -- community-proven for the Lynx bus), not from here.

Auto-detection matches the adapter name in /sys/class/i2c-adapter
against "cp2112"; pass an explicit device path for anything else.
"""

import argparse
import fcntl
import os
import sys
import time
import types

from ch347 import CH347Error, I2CNackError

# From linux/i2c-dev.h
I2C_SLAVE = 0x0703

# Kernel I2C fault codes for "no ACK": ENXIO (address phase, per the
# kernel's fault-code doc) and EREMOTEIO (legacy, still used by drivers).
NACK_ERRNOS = (6, 121)

# The Venus 6.12 kernel builds without CONFIG_I2C_COMPAT, so the classic
# /sys/class/i2c-adapter does not exist there; /sys/class/i2c-dev does
# (verified on a Cerbo GX) and both share the i2c-N/name layout.
SYSFS_CANDIDATES = ("/sys/class/i2c-dev", "/sys/class/i2c-adapter")
MAX_READ = 8192


def list_i2c_adapters(sysfs: str = None) -> list:
    """All kernel I2C buses as (device path, adapter name) tuples."""
    if sysfs is None:
        sysfs = next((c for c in SYSFS_CANDIDATES if os.path.isdir(c)), None)
        if sysfs is None:
            return []
    adapters = []
    try:
        nodes = os.listdir(sysfs)
    except FileNotFoundError:
        return []
    for node in sorted(nodes, key=lambda n: int(n.split("-")[-1])
                       if n.split("-")[-1].isdigit() else 0):
        try:
            with open(os.path.join(sysfs, node, "name"), "r") as f:
                name = f.read().strip()
        except OSError:
            continue
        adapters.append(("/dev/" + node, name))
    return adapters


def find_i2c_buses(name_pattern: str = "cp2112",
                   sysfs: str = None) -> list:
    """Device paths of buses whose adapter name matches the pattern."""
    return [path for path, name in list_i2c_adapters(sysfs)
            if name_pattern.lower() in name.lower()]


class KernelI2C:
    """I2C master over a kernel /dev/i2c-N bus (i2c-dev ioctl interface)."""

    # Seams so tests can run without a real device node.
    _ioctl = staticmethod(fcntl.ioctl)
    _os_read = staticmethod(os.read)
    _os_write = staticmethod(os.write)

    def __init__(self, path: str):
        self.path = path
        self.fd = os.open(path, os.O_RDWR)
        self.transport = types.SimpleNamespace(path=path)
        self.speed_hz = None  # fixed by the kernel bus driver

    @classmethod
    def open(cls, path: str = None) -> "KernelI2C":
        """Open by explicit path, or auto-detect a CP2112 bus."""
        if path is None:
            paths = find_i2c_buses()
            if not paths:
                names = ", ".join("%s (%s)" % a for a in list_i2c_adapters()) \
                    or "none"
                raise CH347Error(
                    "no CP2112 kernel I2C bus found; set i2c_device "
                    "explicitly. Available buses: %s" % names)
            if len(paths) > 1:
                raise CH347Error(
                    "multiple CP2112 buses found (%s); set i2c_device "
                    "explicitly" % ", ".join(paths))
            path = paths[0]
        return cls(path)

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def _set_addr(self, addr: int) -> None:
        if not 0 <= addr <= 0x7F:
            raise ValueError("I2C address 0x%X out of 7-bit range" % addr)
        self._ioctl(self.fd, I2C_SLAVE, addr)

    def i2c_read(self, addr: int, nbytes: int = 1) -> bytes:
        """Plain I2C read: START, addr+R, read nbytes, STOP."""
        if not 1 <= nbytes <= MAX_READ:
            raise ValueError("nbytes must be 1..%d" % MAX_READ)
        self._set_addr(addr)
        try:
            data = self._os_read(self.fd, nbytes)
        except OSError as e:
            if e.errno in NACK_ERRNOS:
                raise I2CNackError(addr)
            raise
        if len(data) < nbytes:
            raise CH347Error(
                "short read from 0x%02X: got %d of %d bytes"
                % (addr, len(data), nbytes))
        return data

    def i2c_write(self, addr: int, data: bytes = b"") -> None:
        """Plain I2C write: START, addr+W, data, STOP."""
        self._set_addr(addr)
        try:
            self._os_write(self.fd, bytes(data))
        except OSError as e:
            if e.errno in NACK_ERRNOS:
                raise I2CNackError(addr)
            raise

    def probe(self, addr: int) -> bool:
        """True if a slave answers a 1-byte read (Lynx-safe probing)."""
        try:
            self.i2c_read(addr, 1)
            return True
        except I2CNackError:
            return False

    def scan(self, first: int = 0x08, last: int = 0x77) -> list:
        return [a for a in range(first, last + 1) if self.probe(a)]


# ── bring-up CLI ────────────────────────────────────────────────────────


def _cli() -> int:
    p = argparse.ArgumentParser(
        description="Kernel I2C (CP2112 fallback) bring-up tool")
    p.add_argument("--device", help="/dev/i2c-N (default: auto-detect CP2112)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true",
                   help="list kernel I2C buses and their adapter names")
    g.add_argument("--scan", action="store_true",
                   help="read-probe all 7-bit addresses (0x08-0x77)")
    g.add_argument("--read", metavar="ADDR",
                   help="read bytes from ADDR (e.g. 0x08)")
    g.add_argument("--watch", metavar="ADDR",
                   help="poll ADDR, print every change with a timestamp")
    p.add_argument("--length", type=int, default=1,
                   help="bytes per read (default 1)")
    p.add_argument("--interval", type=float, default=1.0,
                   help="watch poll interval in seconds (default 1.0)")
    args = p.parse_args()

    if args.list:
        adapters = list_i2c_adapters()
        if not adapters:
            print("no kernel I2C buses found")
            return 1
        for path, name in adapters:
            print("%s  %s" % (path, name))
        return 0

    dev = KernelI2C.open(path=args.device)
    try:
        if args.scan:
            found = dev.scan()
            if not found:
                print("no I2C slaves found")
                return 1
            for a in found:
                print("0x%02X" % a)
            return 0

        if args.read:
            data = dev.i2c_read(int(args.read, 0), args.length)
            print(" ".join("%02X" % b for b in data))
            return 0

        if args.watch:
            addr = int(args.watch, 0)
            last = None
            print("watching 0x%02X every %.1fs (Ctrl-C to stop)"
                  % (addr, args.interval))
            while True:
                try:
                    data = dev.i2c_read(addr, args.length)
                    text = " ".join("%02X (0b%s)" % (b, format(b, "08b"))
                                    for b in data)
                except I2CNackError:
                    data, text = None, "NACK (no response)"
                if data != last:
                    print("%s  %s" % (time.strftime("%H:%M:%S"), text))
                    last = data
                time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    finally:
        dev.close()
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
