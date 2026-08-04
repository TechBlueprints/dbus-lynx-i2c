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
Mock CH347 adapter for exercising the full service without hardware.

Enabled with ``mock = true`` in config.ini.  Each poll re-reads a JSON
state file next to the service (``mock-state.json``), so scenarios can
be driven live from a shell while watching the GX UI:

    # all OK (also the default when the file is missing)
    echo '{"A": "0x00", "B": "0x00"}' > mock-state.json

    # distributor A: fuse 1 blown;  B: no power on busbar
    echo '{"A": "0x10", "B": "0x02"}' > mock-state.json

    # distributor B stops responding (I2C NACK -> comms lost after 3 polls)
    echo '{"A": "0x00", "B": "nack"}' > mock-state.json

    # whole adapter drops off USB (service re-opens it next poll)
    echo '{"A": "error"}' > mock-state.json

Values are status bytes (int or hex string), or the strings ``nack`` /
``error``.  Keys are distributor letters (preferred) or 7-bit addresses
("0x08").  Missing keys read as 0x00.
"""

import json
import os
import types

from ch347 import CH347Error, I2CNackError
from lynx_distributor import ADDRESSES

STATE_FILENAME = "mock-state.json"


class MockAdapter:
    """Drop-in for CH347I2C, scripted by a JSON state file."""

    def __init__(self, state_path: str):
        self.state_path = state_path
        self.transport = types.SimpleNamespace(path="mock:%s" % state_path)
        self.speed_hz = 0

    def _state(self) -> dict:
        try:
            with open(self.state_path, "r") as f:
                state = json.load(f)
        except (OSError, ValueError):
            return {}
        return state if isinstance(state, dict) else {}

    def i2c_read(self, addr: int, nbytes: int = 1) -> bytes:
        state = self._state()
        value = None
        for letter, a in ADDRESSES.items():
            if a == addr and letter in state:
                value = state[letter]
                break
        if value is None:
            for key in ("0x%02x" % addr, "0x%02X" % addr, str(addr)):
                if key in state:
                    value = state[key]
                    break
        if value is None:
            value = 0
        if isinstance(value, str):
            v = value.strip().lower()
            if v == "nack":
                raise I2CNackError(addr)
            if v == "error":
                raise CH347Error("mock adapter error (scripted)")
            value = int(v, 0)
        return bytes([value & 0xFF] * nbytes)

    def close(self) -> None:
        pass
