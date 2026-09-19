#!/usr/bin/env bash
# ecl110-mqtt-bridge installer
#
# Asks a few questions, writes the config, installs a systemd service and
# starts it. Safe to re-run: existing settings are offered as defaults.
#
#   sudo ./setup.sh        interactive
#   sudo ./setup.sh -y     non-interactive: keep the existing config, reinstall
#                          the program and restart (used by deploy.sh)
set -euo pipefail

APP_NAME="ecl110-mqtt-bridge"
APP_DIR="/opt/$APP_NAME"
VENV_DIR="$APP_DIR/venv"
CONF_DIR="/etc/$APP_NAME"
CONF_FILE="$CONF_DIR/config.json"
UNIT_FILE="/etc/systemd/system/$APP_NAME.service"
SVC_USER="ecl110"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOTAL_STEPS=4
ASSUME_YES=false
[[ "${1:-}" == "-y" || "${1:-}" == "--yes" ]] && ASSUME_YES=true

# --- colors: only on a terminal, and never when NO_COLOR is set ---------------
if [[ -t 1 && -z "${NO_COLOR:-}" ]] && command -v tput >/dev/null 2>&1 && [[ "$(tput colors 2>/dev/null || echo 0)" -ge 8 ]]; then
    C_RESET="$(tput sgr0)"; C_BOLD="$(tput bold)"; C_DIM="$(tput dim 2>/dev/null || true)"
    C_RED="$(tput setaf 1)"; C_GREEN="$(tput setaf 2)"; C_YELLOW="$(tput setaf 3)"
    C_BLUE="$(tput setaf 4)"; C_CYAN="$(tput setaf 6)"
else
    C_RESET=""; C_BOLD=""; C_DIM=""; C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_CYAN=""
fi

step() { echo; echo "${C_BOLD}${C_CYAN}==> Step $1/$TOTAL_STEPS: $2${C_RESET}"; }
ok()   { echo "${C_GREEN}  ✔ $*${C_RESET}"; }
warn() { echo "${C_YELLOW}  ! $*${C_RESET}"; }
info() { echo "${C_DIM}    $*${C_RESET}"; }
die()  { echo "${C_RED}${C_BOLD}error:${C_RESET}${C_RED} $*${C_RESET}" >&2; exit 1; }

banner() {
    echo
    echo "${C_BOLD}${C_BLUE}  ┌──────────────────────────────────────────────┐${C_RESET}"
    echo "${C_BOLD}${C_BLUE}  │${C_RESET}${C_BOLD}  ecl110-mqtt-bridge                          ${C_BLUE}│${C_RESET}"
    echo "${C_BOLD}${C_BLUE}  │${C_RESET}  Danfoss ECL Comfort 110 -> Home Assistant   ${C_BOLD}${C_BLUE}│${C_RESET}"
    echo "${C_BOLD}${C_BLUE}  └──────────────────────────────────────────────┘${C_RESET}"
}

ask() {  # ask "prompt" "default" -> answer (default if empty, or always in -y mode)
    local reply
    if $ASSUME_YES; then echo "$2"; return; fi
    if [[ -n "$2" ]]; then
        read -rp "  ${C_BOLD}$1${C_RESET} ${C_DIM}[$2]${C_RESET}: " reply
    else
        read -rp "  ${C_BOLD}$1${C_RESET}: " reply
    fi
    echo "${reply:-$2}"
}
ask_secret() {  # ask_secret "prompt" "current" -> answer (current if empty)
    local reply
    if $ASSUME_YES; then echo "$2"; return; fi
    if [[ -n "$2" ]]; then
        read -rsp "  ${C_BOLD}$1${C_RESET} ${C_DIM}[keep current]${C_RESET}: " reply
    else
        read -rsp "  ${C_BOLD}$1${C_RESET}: " reply
    fi
    echo >&2
    echo "${reply:-$2}"
}
yes_no() {  # yes_no "prompt" "y|n" -> exit 0 for yes
    local reply
    reply="$(ask "$1 (y/n)" "$2")"
    [[ "${reply,,}" == y* ]]
}

# --- preflight ---------------------------------------------------------------
banner
[[ $EUID -eq 0 ]] || die "run as root: sudo ./setup.sh"
command -v python3 >/dev/null || die "python3 is required (apt install python3 python3-venv)"
python3 -c "import venv, ensurepip" 2>/dev/null || die "python3-venv is required (apt install python3-venv)"
command -v systemctl >/dev/null || die "systemd is required"
[[ -f "$SRC_DIR/ecl110_service.py" ]]  || die "ecl110_service.py not found next to setup.sh"
[[ -f "$SRC_DIR/requirements.txt" ]]   || die "requirements.txt not found next to setup.sh"
if $ASSUME_YES && [[ ! -f "$CONF_FILE" ]]; then
    die "-y needs an existing config ($CONF_FILE); run sudo ./setup.sh once interactively"
fi

# Existing config values become the defaults on a re-run
current() {
    [[ -f "$CONF_FILE" ]] || return 0
    CONF_FILE="$CONF_FILE" KEY="$1" python3 -c 'import json, os; print(json.load(open(os.environ["CONF_FILE"])).get(os.environ["KEY"], ""))' 2>/dev/null || true
}
DEF_SERIAL="$(current serial_port)"
DEF_UNIT="$(current unit_id)";            DEF_UNIT="${DEF_UNIT:-5}"
DEF_BAUD="$(current baudrate)";           DEF_BAUD="${DEF_BAUD:-19200}"
DEF_MHOST="$(current mqtt_host)";         DEF_MHOST="${DEF_MHOST:-localhost}"
DEF_MPORT="$(current mqtt_port)";         DEF_MPORT="${DEF_MPORT:-1883}"
DEF_MUSER="$(current mqtt_user)"
DEF_MPASS="$(current mqtt_pass)"
DEF_TOPIC="$(current base_topic)";        DEF_TOPIC="${DEF_TOPIC:-ecl110}"
DEF_NAME="$(current device_name)";        DEF_NAME="${DEF_NAME:-ECL110}"
DEF_DISC="$(current discovery_prefix)";   DEF_DISC="${DEF_DISC:-homeassistant}"
DEF_INT="$(current interval)";            DEF_INT="${DEF_INT:-60}"
DEF_LOG="$(current log_level)";           DEF_LOG="${DEF_LOG:-INFO}"
[[ -f "$CONF_FILE" ]] && { echo; info "Existing installation found - current settings are offered as defaults."; }

# --- 1) Serial / Modbus ------------------------------------------------------
step 1 "Serial port and Modbus"
mapfile -t CANDIDATES < <(ls -1 /dev/serial/by-id/* 2>/dev/null || true)
if (( ${#CANDIDATES[@]} > 0 )); then
    info "USB serial adapters found (stable by-id paths, survive reboots):"
    for i in "${!CANDIDATES[@]}"; do
        mark=""; [[ "${CANDIDATES[$i]}" == "$DEF_SERIAL" ]] && mark="  (current)"
        echo "    $((i+1))) ${CANDIDATES[$i]}$mark"
    done
    # default: current setting if still present, else the only/first candidate
    pick_default=""
    for i in "${!CANDIDATES[@]}"; do [[ "${CANDIDATES[$i]}" == "$DEF_SERIAL" ]] && pick_default="$((i+1))"; done
    [[ -z "$pick_default" && -z "$DEF_SERIAL" ]] && pick_default="1"
    [[ -z "$pick_default" ]] && pick_default="$DEF_SERIAL"
    while true; do
        reply="$(ask "Serial port (number or full path)" "$pick_default")"
        if [[ "$reply" =~ ^[0-9]+$ ]] && (( reply >= 1 && reply <= ${#CANDIDATES[@]} )); then
            SERIAL="${CANDIDATES[$((reply-1))]}"; break
        elif [[ "$reply" == /dev/* ]]; then
            SERIAL="$reply"; break
        fi
        warn "Enter a number from the list or a /dev/... path."
    done
else
    warn "No /dev/serial/by-id devices found - is the USB-RS485 adapter plugged in?"
    while true; do
        SERIAL="$(ask "Serial port path" "${DEF_SERIAL:-/dev/ttyUSB0}")"
        [[ "$SERIAL" == /dev/* ]] && break
        warn "Enter a /dev/... path."
    done
fi
[[ -e "$SERIAL" ]] || warn "$SERIAL does not exist right now - the service will retry when it appears."
ok "Serial port $SERIAL"

while true; do
    UNIT_ID="$(ask "Modbus unit ID (ECL110 default is 5)" "$DEF_UNIT")"
    [[ "$UNIT_ID" =~ ^[0-9]+$ ]] && (( UNIT_ID >= 1 && UNIT_ID <= 247 )) && break
    warn "Enter a number between 1 and 247."
done
while true; do
    BAUD="$(ask "Baud rate" "$DEF_BAUD")"
    [[ "$BAUD" =~ ^[0-9]+$ ]] && break
    warn "Enter a number, e.g. 19200."
done
ok "Unit $UNIT_ID @ $BAUD 8E1"

# --- 2) MQTT -----------------------------------------------------------------
step 2 "MQTT broker"
info "Usually the Mosquitto add-on in Home Assistant, or a local broker."
MQTT_HOST="$(ask "Broker host or IP" "$DEF_MHOST")"
while true; do
    MQTT_PORT="$(ask "Broker port" "$DEF_MPORT")"
    [[ "$MQTT_PORT" =~ ^[0-9]+$ ]] && (( MQTT_PORT >= 1 && MQTT_PORT <= 65535 )) && break
    warn "Enter a number between 1 and 65535."
done
MQTT_USER="$(ask "MQTT username (empty for anonymous)" "$DEF_MUSER")"
MQTT_PASS=""
[[ -n "$MQTT_USER" ]] && MQTT_PASS="$(ask_secret "MQTT password" "$DEF_MPASS")"
if timeout 3 bash -c "exec 3<>/dev/tcp/$MQTT_HOST/$MQTT_PORT" 2>/dev/null; then
    ok "Broker reachable at $MQTT_HOST:$MQTT_PORT"
else
    warn "Could not open a TCP connection to $MQTT_HOST:$MQTT_PORT - continuing anyway."
fi

# --- 3) Home Assistant -------------------------------------------------------
step 3 "Home Assistant"
info "The base topic prefixes every MQTT topic (<base>/status, <base>/sensor/...,"
info "<base>/cmd/...) and is also the HA device identifier. Use a different one per"
info "controller if you run several bridges."
while true; do
    BASE_TOPIC="$(ask "Base topic" "$DEF_TOPIC")"
    BASE_TOPIC="${BASE_TOPIC#/}"; BASE_TOPIC="${BASE_TOPIC%/}"
    [[ "$BASE_TOPIC" =~ ^[A-Za-z0-9_-]+$ ]] && break
    warn "Letters, digits, '_' and '-' only (no slashes)."
done
DEVICE_NAME="$(ask "Device name shown in HA" "$DEF_NAME")"
DISC_PREFIX="$(ask "HA discovery prefix" "$DEF_DISC")"
while true; do
    INTERVAL="$(ask "Poll interval in seconds" "$DEF_INT")"
    [[ "$INTERVAL" =~ ^[0-9]+$ ]] && (( INTERVAL >= 5 )) && break
    warn "Enter a number of seconds (minimum 5)."
done
while true; do
    LOG_LEVEL="$(ask "Log level (DEBUG/INFO/WARNING)" "$DEF_LOG")"
    LOG_LEVEL="${LOG_LEVEL^^}"
    [[ "$LOG_LEVEL" =~ ^(DEBUG|INFO|WARNING|ERROR)$ ]] && break
    warn "Enter DEBUG, INFO, WARNING or ERROR."
done
ok "Base topic '$BASE_TOPIC', device '$DEVICE_NAME', every ${INTERVAL}s"

# --- 4) Install --------------------------------------------------------------
step 4 "Install and start"

if ! id -u "$SVC_USER" >/dev/null 2>&1; then
    nologin="$(command -v nologin || echo /bin/false)"
    useradd --system --no-create-home --shell "$nologin" "$SVC_USER"
    ok "Created system user '$SVC_USER'"
fi
# Serial devices are owned by dialout on Debian/Raspberry Pi OS
if getent group dialout >/dev/null; then
    usermod -aG dialout "$SVC_USER"
fi

install -d -m 755 "$APP_DIR"
install -m 755 "$SRC_DIR/ecl110_service.py" "$APP_DIR/ecl110_service.py"
install -m 644 "$SRC_DIR/requirements.txt"  "$APP_DIR/requirements.txt"
ok "Program installed to $APP_DIR"

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    python3 -m venv "$VENV_DIR"
    ok "Virtualenv created"
fi
info "Installing Python dependencies (paho-mqtt, pymodbus, python-dotenv)..."
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
ok "Dependencies installed"

install -d -m 750 -o root -g "$SVC_USER" "$CONF_DIR"
# Write the config with python so every value is JSON-escaped correctly
SERIAL="$SERIAL" UNIT_ID="$UNIT_ID" BAUD="$BAUD" \
MQTT_HOST="$MQTT_HOST" MQTT_PORT="$MQTT_PORT" MQTT_USER="$MQTT_USER" MQTT_PASS="$MQTT_PASS" \
BASE_TOPIC="$BASE_TOPIC" DEVICE_NAME="$DEVICE_NAME" DISC_PREFIX="$DISC_PREFIX" \
INTERVAL="$INTERVAL" LOG_LEVEL="$LOG_LEVEL" CONF_FILE="$CONF_FILE" python3 - <<'PY'
import json, os
env = os.environ
cfg = {
    "serial_port": env["SERIAL"],
    "unit_id": int(env["UNIT_ID"]),
    "baudrate": int(env["BAUD"]),
    "mqtt_host": env["MQTT_HOST"],
    "mqtt_port": int(env["MQTT_PORT"]),
    "mqtt_user": env["MQTT_USER"],
    "mqtt_pass": env["MQTT_PASS"],
    "base_topic": env["BASE_TOPIC"],
    "device_name": env["DEVICE_NAME"],
    "discovery_prefix": env["DISC_PREFIX"],
    "interval": int(env["INTERVAL"]),
    "log_level": env["LOG_LEVEL"],
}
path = env["CONF_FILE"]
old = json.load(open(path)) if os.path.exists(path) else {}
cfg = {**old, **cfg}                  # keep keys edited by hand
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
PY
chown root:"$SVC_USER" "$CONF_FILE"
chmod 640 "$CONF_FILE"
ok "Config written to $CONF_FILE"

cat > "$UNIT_FILE" <<UNIT
[Unit]
Description=ecl110-mqtt-bridge - Danfoss ECL Comfort 110 Modbus to MQTT
After=network-online.target mosquitto.service
Wants=network-online.target

[Service]
Type=simple
User=$SVC_USER
Group=$SVC_USER
SupplementaryGroups=dialout
Environment=ECL110_CONFIG=$CONF_FILE
Environment=PYTHONUNBUFFERED=1
WorkingDirectory=$APP_DIR
ExecStart=$VENV_DIR/bin/python $APP_DIR/ecl110_service.py
Restart=always
RestartSec=10
KillSignal=SIGTERM
TimeoutStopSec=15
SyslogIdentifier=ecl110
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT
ok "systemd unit written"

systemctl daemon-reload
systemctl enable "$APP_NAME" >/dev/null
systemctl restart "$APP_NAME"   # restart, not start: a re-run must pick up the new config
sleep 2
if systemctl is-active --quiet "$APP_NAME"; then
    ok "Service is running"
else
    die "service failed to start - check: journalctl -u $APP_NAME -e"
fi

# --- summary -----------------------------------------------------------------
echo
echo "${C_BOLD}${C_GREEN}  ┌──────────────────────────────────────────────┐${C_RESET}"
echo "${C_BOLD}${C_GREEN}  │  Installed and running                       │${C_RESET}"
echo "${C_BOLD}${C_GREEN}  └──────────────────────────────────────────────┘${C_RESET}"
echo
echo "  Serial:        $SERIAL  (unit $UNIT_ID, $BAUD 8E1)"
echo "  MQTT:          $MQTT_HOST:$MQTT_PORT${MQTT_USER:+ as $MQTT_USER}"
echo "  Topics:        $BASE_TOPIC/status, $BASE_TOPIC/sensor/<key>/state, $BASE_TOPIC/cmd/{mode,room_setpoint,refresh}"
echo "  HA device:     '$DEVICE_NAME' via $DISC_PREFIX/ discovery"
echo "  Logs:          journalctl -u $APP_NAME -f"
echo "  Config:        $CONF_FILE  (after editing: systemctl restart $APP_NAME)"
echo "  Re-run setup:  sudo ./setup.sh      Remove: sudo ./uninstall.sh"
echo
if ! $ASSUME_YES && yes_no "Show the log for the first snapshot now (up to 30s)?" "y"; then
    echo
    timeout 30 journalctl -u "$APP_NAME" -f -n 20 -o cat 2>/dev/null | sed 's/^/    /' || true
    echo
    info "Look for 'Snapshot:' lines with real temperatures. 'unknown' everywhere means"
    info "the serial port or unit ID is wrong; check: journalctl -u $APP_NAME -e"
fi
echo
