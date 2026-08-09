#!/bin/bash
#
# Disable script for dbus-lynx-i2c
# Cleanly stops and removes the service
#

INSTALL_DIR="/data/apps/dbus-lynx-i2c"
SERVICE_NAME="dbus-lynx-i2c"

echo
echo "Disabling $SERVICE_NAME..."

# Bring the service and its logger down cleanly, and stop their supervise
# processes, BEFORE unlinking. Removing the symlink from under a running
# supervisor leaves it flailing at a directory that is disappearing, which
# makes a later re-install race a stale supervisor.
if [ -e "/service/$SERVICE_NAME" ]; then
    svc -dx "/service/$SERVICE_NAME" 2>/dev/null || true
    svc -dx "/service/$SERVICE_NAME/log" 2>/dev/null || true
    sleep 2
fi

# Remove service symlink (it is a symlink; -rf does not follow it)
rm -rf "/service/$SERVICE_NAME" 2>/dev/null || true

# Kill anything that survived
pkill -f "supervise $SERVICE_NAME" 2>/dev/null || true
pkill -f "multilog .* /var/log/$SERVICE_NAME" 2>/dev/null || true
pkill -f "python.*$SERVICE_NAME" 2>/dev/null || true

# Remove enable script from rc.local
sed -i "/.*$SERVICE_NAME.*/d" /data/rc.local 2>/dev/null || true

echo "Service stopped and rc.local cleaned"
echo
echo "Note: To completely remove, also delete: $INSTALL_DIR"
echo
