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
Venus OS service for Victron Lynx Distributor fuse monitoring over I2C.

Reads each distributor's fuse-status byte through a Waveshare CH347 USB
adapter (M2/HID mode, /dev/hidraw*) and publishes fuse states and alarm
paths to D-Bus so blown fuses surface on the GX display and VRM.

See README.md for the hardware design. Planned structure:

  ch347.py           CH347 HID-I2C driver (stdlib only -- no pip on Venus)
  lynx_distributor.py  Status-byte decode (bit order verified empirically)
  dbus-lynx-i2c.py   Poller + D-Bus service via ext/velib_python

NOT YET IMPLEMENTED -- this placeholder keeps the daemontools service
from crash-looping if the repo is installed before the driver lands.
"""

import logging
import os
import sys
import time

# Add ext folders to sys.path
_ext_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ext")
sys.path.insert(1, os.path.join(_ext_dir, "velib_python"))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("dbus-lynx-i2c")


def main() -> None:
    log.warning("dbus-lynx-i2c: driver not yet implemented -- idling.")
    log.warning("See https://github.com/TechBlueprints/dbus-lynx-i2c for status.")
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
