#!/usr/bin/env bash
# Removes the ecl110-mqtt-bridge service. Asks before deleting the config.
set -euo pipefail

APP_NAME="ecl110-mqtt-bridge"
APP_DIR="/opt/$APP_NAME"
CONF_DIR="/etc/$APP_NAME"
UNIT_FILE="/etc/systemd/system/$APP_NAME.service"
SVC_USER="ecl110"

[[ $EUID -eq 0 ]] || { echo "run as root: sudo ./uninstall.sh" >&2; exit 1; }

read -rp "Remove $APP_NAME service and program? (y/n) [n]: " reply
[[ "${reply,,}" == y* ]] || exit 0

systemctl disable --now "$APP_NAME" >/dev/null 2>&1 || true
rm -f "$UNIT_FILE"
systemctl daemon-reload
rm -rf "$APP_DIR"
echo "Service and program removed."
echo "Note: the device stays in Home Assistant until you delete it there (MQTT"
echo "integration), and the retained discovery topics stay on the broker until cleared."

read -rp "Also delete config ($CONF_DIR) and the '$SVC_USER' user? (y/n) [n]: " reply
if [[ "${reply,,}" == y* ]]; then
    rm -rf "$CONF_DIR"
    userdel "$SVC_USER" >/dev/null 2>&1 || true
    echo "Config and service user removed."
else
    echo "Config kept."
fi
