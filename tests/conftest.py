"""Shared test fixtures and mock infrastructure.

The production code imports Venus OS libraries (dbus, gi, vedbus,
settingsdevice) that are not available in a normal development/CI
environment.  We mock them at the sys.modules level before any
production code is imported, then load the dash-named main script via
importlib under the module name ``lynx_service``.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
from unittest.mock import MagicMock

import pytest

# ── Mock dbus and gi ────────────────────────────────────────────────────────

_dbus = types.ModuleType("dbus")
_dbus.bus = types.ModuleType("dbus.bus")
_dbus.bus.BusConnection = MagicMock()
_dbus.SystemBus = MagicMock
_dbus.SessionBus = MagicMock
_dbus.service = types.ModuleType("dbus.service")
_dbus.service.BusName = MagicMock()
_dbus.exceptions = types.ModuleType("dbus.exceptions")
_dbus.exceptions.DBusException = type("DBusException", (Exception,), {})
_dbus_mainloop_glib = types.ModuleType("dbus.mainloop.glib")
_dbus_mainloop_glib.DBusGMainLoop = MagicMock()

sys.modules.setdefault("dbus", _dbus)
sys.modules.setdefault("dbus.bus", _dbus.bus)
sys.modules.setdefault("dbus.service", _dbus.service)
sys.modules.setdefault("dbus.exceptions", _dbus.exceptions)
sys.modules.setdefault("dbus.mainloop", types.ModuleType("dbus.mainloop"))
sys.modules.setdefault("dbus.mainloop.glib", _dbus_mainloop_glib)

_gi = types.ModuleType("gi")
_gi.repository = types.ModuleType("gi.repository")
_gi.repository.GLib = MagicMock()
sys.modules.setdefault("gi", _gi)
sys.modules.setdefault("gi.repository", _gi.repository)

# ── Mock velib_python (vedbus, settingsdevice) ──────────────────────────────

sys.modules.setdefault("vedbus", types.ModuleType("vedbus"))
sys.modules["vedbus"].VeDbusService = MagicMock()

sys.modules.setdefault("settingsdevice", types.ModuleType("settingsdevice"))
sys.modules["settingsdevice"].SettingsDevice = MagicMock()

# ── Load the dash-named main script as module "lynx_service" ────────────────

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="session")
def lynx_service():
    spec = importlib.util.spec_from_file_location(
        "lynx_service", os.path.join(_BASE, "dbus-lynx-i2c.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules["lynx_service"] = module
    spec.loader.exec_module(module)
    return module
