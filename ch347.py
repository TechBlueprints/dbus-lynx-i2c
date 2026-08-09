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
CH347 USB-HID I2C driver, pure Python stdlib (no pip on Venus OS).

Speaks to a WCH CH347 adapter (e.g. Waveshare USB TO UART/I2C/SPI/JTAG)
in mode M2 (USB-HID), where the chip enumerates two HID interfaces:
interface 0 is UART1, interface 1 is SPI/I2C/GPIO.  Venus OS ships
usbhid/hidraw stock, so no kernel module is needed.

Wire protocol (derived from WCH's CH347 development manual and the
open-source references i2cy/CH347-HIDAPI and serfreeman1337/go-ch347):

  HID output report:  [0x00 report-id] [len lo] [len hi] [stream ...]
  HID input report:   [len lo] [len hi] [data ...]

where ``len`` counts the stream bytes (command byte through terminator)
and the I2C stream is the CH341-compatible command set:

  0xAA          I2C stream start (packet command)
  0x60 | mode   set speed: 0=20 kHz, 1=100 kHz, 2=400 kHz, 3=750 kHz
  0x74          generate START
  0x75          generate STOP
  0x80 | n      output n bytes (n in bits 5-0), bytes follow
  0xC0 | n      input n+1 bytes ACKed -- a read must be terminated by a
                bare 0xC0 (input one byte, NACK) before STOP
  0x00          end of stream

Every byte written to the bus echoes one ack byte in the response
(0x01 = ACK), followed by any bytes read from the bus.
"""

import argparse
import os
import re
import select
import struct
import sys
import time

VENDOR_ID = 0x1A86
PRODUCT_ID = 0x55DC
I2C_INTERFACE = 1  # M2 HID interface 1 = SPI/I2C/GPIO (0 = UART1)

# 0x60 | level -- CH347 supports only these four bus clocks.  The Lynx
# community rate is ~50 kHz, which the CH347 cannot produce exactly;
# 20 kHz is the closest rate at or below it.
SPEED_LEVELS = {
    20000: 0,
    100000: 1,
    400000: 2,
    750000: 3,
}

CMD_I2C_STREAM = 0xAA
CMD_I2C_STM_SET = 0x60
CMD_I2C_STM_STA = 0x74
CMD_I2C_STM_STO = 0x75
CMD_I2C_STM_OUT = 0x80
CMD_I2C_STM_IN = 0xC0
CMD_I2C_STM_END = 0x00

MAX_CHUNK = 63  # 6-bit length field on OUT/IN commands
HID_PACKET_SIZE = 512


class CH347Error(Exception):
    """Adapter-level failure (USB gone, malformed response, timeout)."""


class CH347TimeoutError(CH347Error):
    """No HID response within the deadline."""


class I2CNackError(CH347Error):
    """The addressed I2C slave did not acknowledge."""

    def __init__(self, addr: int):
        super().__init__("no ACK from I2C address 0x%02X" % addr)
        self.addr = addr


def find_hidraw_paths(vid: int = VENDOR_ID, pid: int = PRODUCT_ID,
                      interface: int = I2C_INTERFACE,
                      sysfs: str = "/sys/class/hidraw") -> list:
    """Return /dev/hidraw* paths for the given VID:PID and USB interface.

    The kernel exposes one hidraw node per HID interface; the uevent file
    carries HID_ID (bus:vendor:product) and HID_PHYS (…/inputN, where N is
    the USB interface number).
    """
    matches = []
    want_id = "%08X:%08X" % (vid, pid)
    try:
        nodes = sorted(os.listdir(sysfs))
    except FileNotFoundError:
        return []
    for node in nodes:
        uevent_path = os.path.join(sysfs, node, "device", "uevent")
        try:
            with open(uevent_path, "r") as f:
                uevent = dict(
                    line.strip().split("=", 1)
                    for line in f if "=" in line
                )
        except OSError:
            continue
        hid_id = uevent.get("HID_ID", "")
        if not hid_id.upper().endswith(want_id):
            continue
        m = re.search(r"input(\d+)$", uevent.get("HID_PHYS", ""))
        if m is None or int(m.group(1)) != interface:
            continue
        matches.append("/dev/" + node)
    return matches


class HidrawTransport:
    """Raw HID report exchange over a /dev/hidraw* node."""

    def __init__(self, path: str):
        self.path = path
        self.fd = os.open(path, os.O_RDWR)

    def write(self, report: bytes) -> None:
        n = os.write(self.fd, report)
        if n != len(report):
            raise CH347Error("short hidraw write (%d of %d bytes)" % (n, len(report)))

    def read(self, timeout: float) -> bytes:
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return b""
        return os.read(self.fd, HID_PACKET_SIZE)

    def drain(self) -> None:
        """Discard stale input reports (e.g. left over after a timeout)."""
        while True:
            ready, _, _ = select.select([self.fd], [], [], 0)
            if not ready:
                return
            os.read(self.fd, HID_PACKET_SIZE)

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class CH347I2C:
    """I2C master on a CH347 in HID (M2) mode."""

    def __init__(self, transport, timeout: float = 1.0):
        self.transport = transport
        self.timeout = timeout
        self.speed_hz = None

    @classmethod
    def open(cls, path: str = None, speed_hz: int = 20000,
             timeout: float = 1.0) -> "CH347I2C":
        """Open by explicit hidraw path, or auto-detect the adapter."""
        if path is None:
            paths = find_hidraw_paths()
            if not paths:
                raise CH347Error(
                    "no CH347 I2C interface found (VID:PID %04x:%04x); "
                    "is the adapter plugged in and in mode M2?"
                    % (VENDOR_ID, PRODUCT_ID))
            if len(paths) > 1:
                raise CH347Error(
                    "multiple CH347 adapters found (%s); pass an explicit path"
                    % ", ".join(paths))
            path = paths[0]
        dev = cls(HidrawTransport(path), timeout=timeout)
        try:
            dev.set_speed(speed_hz)
        except Exception:
            dev.close()
            raise
        return dev

    def close(self) -> None:
        self.transport.close()

    # ── framing ─────────────────────────────────────────────────────────

    def _xfer(self, stream: bytes, expect: int) -> bytes:
        """Send one I2C command stream, return ``expect`` response bytes.

        ``stream`` is the full packet command stream starting with 0xAA and
        ending with 0x00; the HID report prepends the report id (0x00 --
        stripped by the kernel, the CH347 uses unnumbered reports) and a
        16-bit little-endian stream length.
        """
        self.transport.drain()
        report = b"\x00" + struct.pack("<H", len(stream)) + stream
        self.transport.write(report)
        if expect == 0:
            return b""
        raw = self.transport.read(self.timeout)
        if not raw:
            raise CH347TimeoutError(
                "no response from CH347 within %.1fs" % self.timeout)
        if len(raw) < 2:
            raise CH347Error("runt HID response (%d bytes)" % len(raw))
        length = raw[0] | (raw[1] << 8)
        payload = raw[2:2 + length]
        if len(payload) < expect:
            raise CH347Error(
                "short I2C response: got %d bytes, expected %d"
                % (len(payload), expect))
        return payload

    # ── I2C operations ──────────────────────────────────────────────────

    def set_speed(self, speed_hz: int) -> None:
        """Set the bus clock. Only 20/100/400/750 kHz exist on the CH347."""
        if speed_hz not in SPEED_LEVELS:
            raise ValueError(
                "unsupported I2C speed %d Hz; CH347 supports %s"
                % (speed_hz, sorted(SPEED_LEVELS)))
        stream = bytes([CMD_I2C_STREAM,
                        CMD_I2C_STM_SET | SPEED_LEVELS[speed_hz],
                        CMD_I2C_STM_END])
        self._xfer(stream, expect=0)
        self.speed_hz = speed_hz

    @staticmethod
    def _read_cmds(nbytes: int) -> list:
        # A read must end with a bare 0xC0 (one byte, NACKed) or the next
        # transaction fails; 0xC0|n reads n+1 bytes but we follow the
        # references and only use it for the ACKed prefix.
        if nbytes == 1:
            return [CMD_I2C_STM_IN]
        return [CMD_I2C_STM_IN | (nbytes - 1), CMD_I2C_STM_IN]

    def i2c_read(self, addr: int, nbytes: int = 1) -> bytes:
        """Plain I2C read: START, addr+R, read nbytes, STOP."""
        self._check_addr(addr)
        if not 1 <= nbytes <= MAX_CHUNK:
            raise ValueError("nbytes must be 1..%d" % MAX_CHUNK)
        cmds = [CMD_I2C_STM_STA, CMD_I2C_STM_OUT | 1, (addr << 1) | 1]
        cmds += self._read_cmds(nbytes)
        cmds.append(CMD_I2C_STM_STO)
        payload = self._xfer(self._stream(cmds), expect=1 + nbytes)
        if payload[0] != 0x01:
            raise I2CNackError(addr)
        return bytes(payload[1:1 + nbytes])

    def i2c_read_burst(self, addr: int, data_speed_hz: int = 750000) -> tuple:
        """Read a status byte with the data phase clocked fast.

        The Lynx Distributor aborts its transmission roughly 60 us into
        a data byte (see README "Truncated reads"), so at 20 kHz the
        byte is regularly cut short and the pull-ups fill the rest with
        1s.  The I2C speed opcode is part of the command stream, so the
        clock can change mid-transaction: START and the address stay at
        the (reliable) bus speed, the data byte is clocked at
        ``data_speed_hz`` -- ~11 us at 750 kHz, beating the abort
        window -- and the closing NACK drops back to the bus speed,
        because the slave misses a NACK delivered at high speed and
        keeps driving SDA, which blocks the following transaction.

        Returns ``(status, echo)``.  Every read returns the same status
        byte, so ``echo`` is a second copy clocked at the slow rate: it
        is the byte that now absorbs the truncation, and when it
        survives intact it doubles as an intra-transaction cross-check.
        """
        self._check_addr(addr)
        if data_speed_hz not in SPEED_LEVELS:
            raise ValueError("unsupported data speed %d Hz; CH347 supports %s"
                             % (data_speed_hz, sorted(SPEED_LEVELS)))
        if self.speed_hz is None:
            raise CH347Error("bus speed not set")
        slow = CMD_I2C_STM_SET | SPEED_LEVELS[self.speed_hz]
        fast = CMD_I2C_STM_SET | SPEED_LEVELS[data_speed_hz]
        cmds = [slow,
                CMD_I2C_STM_STA, CMD_I2C_STM_OUT | 1, (addr << 1) | 1,
                fast,
                CMD_I2C_STM_IN | 1,   # status byte, clocked fast, ACKed
                slow,
                CMD_I2C_STM_IN,       # echo copy, slow, closing NACK
                CMD_I2C_STM_STO]
        payload = self._xfer(self._stream(cmds), expect=3)
        if payload[0] != 0x01:
            raise I2CNackError(addr)
        return payload[1], payload[2]

    def i2c_write(self, addr: int, data: bytes = b"") -> None:
        """Plain I2C write: START, addr+W, data, STOP."""
        self._check_addr(addr)
        if len(data) > MAX_CHUNK - 1:
            raise ValueError("write of %d bytes exceeds %d" % (len(data), MAX_CHUNK - 1))
        cmds = [CMD_I2C_STM_STA, CMD_I2C_STM_OUT | (1 + len(data)), addr << 1]
        cmds += list(data)
        cmds.append(CMD_I2C_STM_STO)
        payload = self._xfer(self._stream(cmds), expect=1 + len(data))
        if payload[0] != 0x01:
            raise I2CNackError(addr)
        for i, ack in enumerate(payload[1:1 + len(data)]):
            if ack != 0x01:
                raise CH347Error(
                    "data byte %d NACKed by 0x%02X" % (i, addr))

    def i2c_write_read(self, addr: int, wdata: bytes, nbytes: int) -> bytes:
        """Register-style read: write wdata, repeated START, read nbytes."""
        self._check_addr(addr)
        if not 1 <= nbytes <= MAX_CHUNK:
            raise ValueError("nbytes must be 1..%d" % MAX_CHUNK)
        cmds = [CMD_I2C_STM_STA, CMD_I2C_STM_OUT | (1 + len(wdata)), addr << 1]
        cmds += list(wdata)
        cmds += [CMD_I2C_STM_STA, CMD_I2C_STM_OUT | 1, (addr << 1) | 1]
        cmds += self._read_cmds(nbytes)
        cmds.append(CMD_I2C_STM_STO)
        n_acks = 1 + len(wdata) + 1
        payload = self._xfer(self._stream(cmds), expect=n_acks + nbytes)
        acks = payload[:n_acks]
        if acks[0] != 0x01 or acks[-1] != 0x01:
            raise I2CNackError(addr)
        if any(a != 0x01 for a in acks[1:-1]):
            raise CH347Error("register byte NACKed by 0x%02X" % addr)
        return bytes(payload[n_acks:n_acks + nbytes])

    def probe(self, addr: int) -> bool:
        """True if a slave ACKs its address (zero-length write)."""
        try:
            self.i2c_write(addr)
            return True
        except I2CNackError:
            return False

    def scan(self, first: int = 0x08, last: int = 0x77) -> list:
        return [a for a in range(first, last + 1) if self.probe(a)]

    @staticmethod
    def _stream(cmds: list) -> bytes:
        return bytes([CMD_I2C_STREAM] + cmds + [CMD_I2C_STM_END])

    @staticmethod
    def _check_addr(addr: int) -> None:
        if not 0 <= addr <= 0x7F:
            raise ValueError("I2C address 0x%X out of 7-bit range" % addr)


# ── bring-up CLI ────────────────────────────────────────────────────────


def _cli() -> int:
    p = argparse.ArgumentParser(
        description="CH347 HID-I2C bring-up tool (list/scan/read/watch)")
    p.add_argument("--device", help="hidraw path (default: auto-detect)")
    p.add_argument("--speed", type=int, default=20000,
                   help="bus clock in Hz: 20000, 100000, 400000, 750000 "
                        "(default 20000)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true",
                   help="list detected CH347 I2C hidraw nodes")
    g.add_argument("--scan", action="store_true",
                   help="probe all 7-bit addresses (0x08-0x77)")
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
        paths = find_hidraw_paths()
        if not paths:
            print("no CH347 I2C interface found")
            return 1
        for path in paths:
            print(path)
        return 0

    dev = CH347I2C.open(path=args.device, speed_hz=args.speed)
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
            addr = int(args.read, 0)
            try:
                data = dev.i2c_read(addr, args.length)
            except I2CNackError:
                print("no ACK from 0x%02X (device absent or bus problem)"
                      % addr)
                return 1
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
