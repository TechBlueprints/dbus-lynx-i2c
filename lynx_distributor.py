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
Victron Lynx Distributor I2C status-byte decoding.

A distributor is a single I2C slave (address set by its 2-way DIP switch)
that answers a one-byte read with its fuse status:

    0x10  fuse 1 blown/missing        0x02  busbar has no supply
    0x20  fuse 2 blown/missing
    0x40  fuse 3 blown/missing        0x00  all fuses present and intact
    0x80  fuse 4 blown/missing

The bit order comes from community reverse-engineering; three
independent hardware-validated implementations agree on this map
(twam/dbus-lynx-distributor, Otherbright's Pico W write-up,
NightHawk32/Lynx-Distributor-Gateway).  Still confirm on our own
hardware -- pull each fuse in turn and watch the byte:

    python3 lynx_distributor.py --watch A

Manual caveat: with batteries on multiple circuits, a blown battery-side
fuse may not read as blown until the battery is under charge/discharge
(not enough voltage across the fuse to trigger detection).  Unpopulated
fuse positions read as blown/missing on the wire, which is why decoding
takes ``num_fuses``.
"""

import argparse
import sys
import time
from dataclasses import dataclass

# DIP-switch address map, labelled left-to-right per Victron convention.
ADDRESSES = {
    "A": 0x08,  # both switches off (factory default)
    "B": 0x09,  # sw1 on
    "C": 0x0A,  # sw2 on
    "D": 0x0B,  # both on
}

MAX_FUSES = 4
FUSE_BITS = (0x10, 0x20, 0x40, 0x80)  # fuse 1..4
BIT_NO_SUPPLY = 0x02
KNOWN_BITS = 0xF2


@dataclass(frozen=True)
class FuseStatus:
    """Decoded distributor status byte."""

    raw: int
    fuses: tuple  # bool per populated fuse position, True = blown/missing
    no_supply: bool
    unknown_bits: int  # set bits outside the documented encoding

    @property
    def alarm_active(self) -> bool:
        return any(self.fuses) or self.no_supply

    @property
    def blown_fuses(self) -> tuple:
        """1-based positions of blown/missing fuses."""
        return tuple(i + 1 for i, blown in enumerate(self.fuses) if blown)


def decode(raw: int, num_fuses: int = MAX_FUSES) -> FuseStatus:
    """Decode a status byte, considering only the populated fuse positions."""
    if not 0 <= raw <= 0xFF:
        raise ValueError("status byte 0x%X out of range" % raw)
    if not 0 <= num_fuses <= MAX_FUSES:
        raise ValueError("num_fuses must be 0..%d" % MAX_FUSES)
    fuses = tuple(bool(raw & FUSE_BITS[i]) for i in range(num_fuses))
    return FuseStatus(
        raw=raw,
        fuses=fuses,
        no_supply=bool(raw & BIT_NO_SUPPLY),
        unknown_bits=raw & ~KNOWN_BITS & 0xFF,
    )


def describe(status: FuseStatus) -> str:
    """One-line human-readable summary, used for logs and the CLI."""
    parts = []
    if status.blown_fuses:
        parts.append("fuse %s blown/missing"
                     % ",".join(str(i) for i in status.blown_fuses))
    if status.no_supply:
        parts.append("busbar has no supply")
    if status.unknown_bits:
        parts.append("unknown bits 0x%02X" % status.unknown_bits)
    if not parts:
        parts.append("all fuses OK")
    return "0x%02X: %s" % (status.raw, "; ".join(parts))


# ── bring-up CLI ────────────────────────────────────────────────────────


def _cli() -> int:
    p = argparse.ArgumentParser(
        description="Decode or live-watch Lynx Distributor fuse status")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--decode", metavar="BYTE",
                   help="decode a status byte (e.g. 0x30)")
    g.add_argument("--watch", metavar="DIST", choices=sorted(ADDRESSES),
                   help="poll distributor A-D via CH347, log every change "
                        "(the fuse-pull verification workflow)")
    p.add_argument("--fuses", type=int, default=MAX_FUSES,
                   help="populated fuse positions (default 4)")
    p.add_argument("--interval", type=float, default=1.0,
                   help="watch poll interval in seconds (default 1.0)")
    p.add_argument("--device", help="hidraw path (default: auto-detect)")
    p.add_argument("--speed", type=int, default=20000,
                   help="I2C clock in Hz (default 20000)")
    args = p.parse_args()

    if args.decode:
        print(describe(decode(int(args.decode, 0), args.fuses)))
        return 0

    # Imported here so --decode works without the adapter attached.
    from ch347 import CH347I2C, I2CNackError

    addr = ADDRESSES[args.watch]
    dev = CH347I2C.open(path=args.device, speed_hz=args.speed)
    print("watching distributor %s (0x%02X) every %.1fs -- pull fuses one "
          "at a time to verify the bit order (Ctrl-C to stop)"
          % (args.watch, addr, args.interval))
    last = None
    try:
        while True:
            try:
                raw = dev.i2c_read(addr, 1)[0]
                text = describe(decode(raw, args.fuses))
            except I2CNackError:
                raw, text = None, "NACK (distributor not responding)"
            if raw != last:
                print("%s  %s" % (time.strftime("%H:%M:%S"), text))
                last = raw
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    finally:
        dev.close()


if __name__ == "__main__":
    sys.exit(_cli())
