#
#  VERSION write back 
#
#!/usr/bin/env python3
import json, time, socket, signal, sys, threading, os, logging
from paho.mqtt import client as mqtt
from pymodbus.client import ModbusSerialClient
from pymodbus.pdu import ExceptionResponse
from dotenv import load_dotenv

# ---------- CONFIG ----------
# Precedence: environment variable > config.json > built-in default.
# setup.sh writes config.json; env vars / .env are handy for development.
load_dotenv()
CONFIG_FILE = os.getenv("ECL110_CONFIG", "/etc/ecl110-mqtt-bridge/config.json")

def _load_config_file():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

_cfg = _load_config_file()

def _get(key, env, default, cast=str):
    v = os.getenv(env)
    if v is None:
        v = _cfg.get(key)
    if v is None or v == "":
        return default
    return cast(v)

PORT         = _get("serial_port", "ECL110_PORT", "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_B003297J-if00-port0")
UNIT         = _get("unit_id",     "ECL110_UNIT", 5, int)
BAUDRATE     = _get("baudrate",    "ECL110_BAUD", 19200, int)
TIMEOUT_S    = 2.0
FW_VERSION   = "1.08"

MQTT_HOST    = _get("mqtt_host", "MQTT_HOST", "localhost")
MQTT_PORT    = _get("mqtt_port", "MQTT_PORT", 1883, int)
MQTT_USER    = _get("mqtt_user", "MQTT_USER", None)
MQTT_PASS    = _get("mqtt_pass", "MQTT_PASS", None)
DISCOVERY_PREFIX = _get("discovery_prefix", "DISCOVERY_PREFIX", "homeassistant").strip("/")
NODE_ID      = _get("base_topic",  "ECL110_BASE_TOPIC",  "ecl110").strip("/")   # topic prefix + HA device id
FRIENDLY     = _get("device_name", "ECL110_DEVICE_NAME", "ECL110")
INTERVAL     = _get("interval",    "ECL110_INTERVAL",    60, int)   # seconds — single snapshot loop
LOG_LEVEL    = _get("log_level",   "LOG_LEVEL", "INFO").upper()

# ---------- LOGGING ----------
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("ecl110")

# ---------- MAPS (FW 1.08) ----------
SENSOR_NC_RAW = 1920  # -> 192.0°C <- = disconnected S1..S4

# Temperature sensors. display_name, PNU, unit, scale, (valid_min, valid_max)
# PNU = Modbus register +1
TEMPS = {
    "s1_outdoor": ("S1 Outdoor", 11201, "°C", 0.1, (-50.0,  60.0)),
    "s2_room":    ("S2 Room",    11202, "°C", 0.1, (  0.0,  50.0)),
    "s3_flow":    ("S3 Flow",    11203, "°C", 0.1, (  0.0, 120.0)),
    "s4_return":  ("S4 Return",  11204, "°C", 0.1, (  0.0, 100.0)),
}

# Room setpoint — writable via HA number entity (15..25, integer only)
ROOM_SETPOINT = ("Room Setpoint", 11229, "°C", 0.1, (15.0, 25.0))
ROOM_SETPOINT_STEP = 1  # integer only — panel doesn't accept half-degrees reliably

# Accumulated outdoor — when reached → summer cutoff
ACC_OUTDOOR   = ("Accumulated Outdoor", 11100, "°C", 0.1, (-50.0, 60.0))

# Mode: writable via HA select entity.
#     0=MANUAL, 1=AUTO, 2=COMFORT, 3=SETBACK, 4=STANDBY
MODE          = ("Mode", 4201)
MODE_MAP_FWD  = {0: "MANUAL", 1: "AUTO", 2: "COMFORT", 3: "SETBACK", 4: "STANDBY"}
MODE_MAP_REV  = {v: k for k, v in MODE_MAP_FWD.items()}

# Operating state: sun/moon glyph on the display (4211, wire 4210)
OP_STATE      = ("State", 4211)

# Pump status 1/0 (4002, wire 4001)
PUMP          = ("Pump", 4002)

# Config parameters — slow-changing, but cheap enough to refresh every snapshot
CONFIG = {
    "slope":    ("Slope",           11175, 0.1, None),
    "cut_out":  ("Heating Cut-out", 11179, 1.0, "°C"),
}

# MQTT topics
AVAIL_T   = f"{NODE_ID}/status"
STATE_T   = lambda key: f"{NODE_ID}/sensor/{key}/state"
MODE_T    = f"{NODE_ID}/sensor/mode/state"  # state topic shared with select entity
CMD_MODE  = f"{NODE_ID}/cmd/mode"
CMD_ROOM  = f"{NODE_ID}/cmd/room_setpoint"
CMD_REFR  = f"{NODE_ID}/cmd/refresh"

# Icon mapping
ICONS = {
    "s1_outdoor":     "mdi:thermometer",
    "s2_room":        "mdi:thermometer",
    "s3_flow":        "mdi:thermometer",
    "s4_return":      "mdi:thermometer",
    "acc_outdoor":    "mdi:thermometer-lines",
    "room_setpoint":  "mdi:home-thermometer-outline",
    "mode":           "mdi:cog",
    "slope":          "mdi:chart-bell-curve-cumulative",
    "cut_out":        "mdi:thermostat-cog",
    "state":          "mdi:theme-light-dark",
    "pump":           "mdi:pump",
    "summer_standby": "mdi:white-balance-sunny",
}

# Discovery topics for entities we used to publish but no longer do
# (also includes the now-superseded sensor versions of mode and room_setpoint).
LEGACY_DISCOVERY_TOPICS = [
    f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/actual_mode/config",
    f"{DISCOVERY_PREFIX}/binary_sensor/{NODE_ID}/comfort_active/config",
    f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/displace/config",
    f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/displacement/config",
    f"{DISCOVERY_PREFIX}/binary_sensor/{NODE_ID}/pump/config",
    f"{DISCOVERY_PREFIX}/binary_sensor/{NODE_ID}/summer_standby/config",
    f"{DISCOVERY_PREFIX}/binary_sensor/{NODE_ID}/operating_state/config",
    # superseded by select/number entities:
    f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/mode/config",
    f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/room_setpoint/config",
]

# ---------- Globals ----------
stop_flag = False
bus_lock  = threading.Lock()

def sig_handler(*_):
    global stop_flag
    stop_flag = True
    log.info("Signal received, shutting down…")
signal.signal(signal.SIGINT, sig_handler)
signal.signal(signal.SIGTERM, sig_handler)

# ---------- Helpers ----------
def s16(v):
    """16-bit Modbus register as signed int16."""
    if v is None:
        return None
    return v - 65536 if v >= 32768 else v

def decode_pump_status(raw):
    if raw is None:
        return "unknown"
    return "Running" if raw else "Stopped"

def decode_sensor(raw, scale, valid_range, key):
    if raw is None:
        return "unknown"
    sval = s16(raw)
    if sval == SENSOR_NC_RAW and key.startswith("s"):
        return "unknown"
    val = round(sval * scale, 1)
    lo, hi = valid_range
    if not (lo <= val <= hi):
        log.warning(f"{key}: {val} outside range {lo}..{hi} (raw={raw}); reporting unknown")
        return "unknown"
    return str(val)

def decode_config(raw, scale):
    if raw is None:
        return "unknown"
    sval = s16(raw)
    val = round(sval * scale, 1)
    return str(val)

def decode_state(raw):
    if raw is None:
        return "unknown"
    if raw in (2, 3):
        return "Comfort"
    if raw in (0, 1):
        return "Setback"
    log.warning(f"state: unexpected raw value {raw} on 4211")
    return "unknown"

# ---------- MQTT ----------
def make_mqtt():
    client_id = f"{NODE_ID}-{socket.gethostname()}"
    cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id, clean_session=True)
    if MQTT_USER:
        cli.username_pw_set(MQTT_USER, MQTT_PASS)
    cli.will_set(AVAIL_T, "offline", qos=1, retain=True)
    cli.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    cli.loop_start()
    log.info(f"MQTT connected to {MQTT_HOST}:{MQTT_PORT} as {MQTT_USER or '(no user)'}")
    return cli

def _device_dict():
    return {
        "identifiers": [NODE_ID],
        "manufacturer": "Danfoss",
        "model": f"ECL Comfort 110 (App 130, FW {FW_VERSION})",
        "name": FRIENDLY,
    }

def _publish_temp_sensor(cli, key, name, unit):
    cfg_t = f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/{key}/config"
    cfg = {
        "name": name,
        "uniq_id": f"{NODE_ID}_{key}",
        "stat_t": STATE_T(key),
        "avty_t": AVAIL_T,
        "dev": _device_dict(),
        "unit_of_meas": unit,
        "dev_cla": "temperature",
        "stat_cla": "measurement",
    }
    if key in ICONS:
        cfg["icon"] = ICONS[key]
    cli.publish(cfg_t, json.dumps(cfg), qos=1, retain=True)

def _publish_text_sensor(cli, key, name, state_topic):
    cfg_t = f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/{key}/config"
    cfg = {
        "name": name,
        "uniq_id": f"{NODE_ID}_{key}",
        "stat_t": state_topic,
        "avty_t": AVAIL_T,
        "dev": _device_dict(),
    }
    if key in ICONS:
        cfg["icon"] = ICONS[key]
    cli.publish(cfg_t, json.dumps(cfg), qos=1, retain=True)

def _publish_numeric_sensor(cli, key, name, unit=None):
    cfg_t = f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/{key}/config"
    cfg = {
        "name": name,
        "uniq_id": f"{NODE_ID}_{key}",
        "stat_t": STATE_T(key),
        "avty_t": AVAIL_T,
        "dev": _device_dict(),
        "stat_cla": "measurement",
    }
    if unit:
        cfg["unit_of_meas"] = unit
        cfg["dev_cla"] = "temperature"
    if key in ICONS:
        cfg["icon"] = ICONS[key]
    cli.publish(cfg_t, json.dumps(cfg), qos=1, retain=True)

def _publish_mode_select(cli):
    cfg_t = f"{DISCOVERY_PREFIX}/select/{NODE_ID}/mode/config"
    cfg = {
        "name": "Mode",
        "uniq_id": f"{NODE_ID}_mode",
        "stat_t": MODE_T,
        "cmd_t": CMD_MODE,
        "avty_t": AVAIL_T,
        "dev": _device_dict(),
        "options": list(MODE_MAP_FWD.values()),
        "icon": ICONS["mode"],
    }
    cli.publish(cfg_t, json.dumps(cfg), qos=1, retain=True)

def _publish_setpoint_number(cli):
    lo, hi = ROOM_SETPOINT[4]
    cfg_t = f"{DISCOVERY_PREFIX}/number/{NODE_ID}/room_setpoint/config"
    cfg = {
        "name": "Room Setpoint",
        "uniq_id": f"{NODE_ID}_room_setpoint",
        "stat_t": STATE_T("room_setpoint"),
        "cmd_t": CMD_ROOM,
        "avty_t": AVAIL_T,
        "dev": _device_dict(),
        "min": lo,
        "max": hi,
        "step": ROOM_SETPOINT_STEP,
        "unit_of_meas": ROOM_SETPOINT[2],
        "dev_cla": "temperature",
        "mode": "box",
        "icon": ICONS["room_setpoint"],
    }
    cli.publish(cfg_t, json.dumps(cfg), qos=1, retain=True)

def publish_discovery(cli):
    # Tell HA to forget renamed/removed/superseded entities first
    for t in LEGACY_DISCOVERY_TOPICS:
        cli.publish(t, "", qos=1, retain=True)

    # Physical temperature sensors (S1..S4)
    for key, (name, _pnu, unit, _scale, _range) in TEMPS.items():
        _publish_temp_sensor(cli, key, name, unit)

    # Accumulated outdoor (filtered)
    _publish_temp_sensor(cli, "acc_outdoor", ACC_OUTDOOR[0], ACC_OUTDOOR[2])

    # Writable: room setpoint (number) and mode (select)
    _publish_setpoint_number(cli)
    _publish_mode_select(cli)

    # Read-only status
    _publish_text_sensor(cli, "state", "State", STATE_T("state"))
    _publish_text_sensor(cli, "pump", "Pump Status", STATE_T("pump"))
    _publish_text_sensor(cli, "summer_standby", "Summer Standby", STATE_T("summer_standby"))

    # Curve config (read-only)
    for key, (name, _pnu, _scale, unit) in CONFIG.items():
        _publish_numeric_sensor(cli, key, name, unit=unit)

    log.info("MQTT discovery published")

# ---------- Modbus ----------
def make_modbus():
    return ModbusSerialClient(
        port=PORT, baudrate=BAUDRATE, parity="E", stopbits=1, bytesize=8, timeout=TIMEOUT_S
    )

def read_one(mod, pnu):
    try:
        with bus_lock:
            r = mod.read_holding_registers(address=pnu-1, count=1, device_id=UNIT)
        if isinstance(r, ExceptionResponse) or not getattr(r, "registers", []):
            return None
        return r.registers[0]
    except Exception as e:
        log.warning(f"Read PNU {pnu} failed: {e}")
        return None

def write_one(mod, pnu, value):
    try:
        with bus_lock:
            r = mod.write_register(address=pnu-1, value=int(value), device_id=UNIT)
        if isinstance(r, ExceptionResponse):
            log.warning(f"Write PNU {pnu}={value} returned exception: {r}")
            return False
        return True
    except Exception as e:
        log.warning(f"Write PNU {pnu}={value} failed: {e}")
        return False

def publish_snapshot(cli, mod):
    """Single snapshot: reads everything, publishes everything, derives summer_standby."""
    cli.publish(AVAIL_T, "online", qos=1, retain=True)

    # 1. S1..S4 physical sensors
    temp_results = []
    for key, (_name, pnu, _unit, scale, valid_range) in TEMPS.items():
        raw = read_one(mod, pnu)
        out = decode_sensor(raw, scale, valid_range, key)
        cli.publish(STATE_T(key), out, qos=0, retain=True)
        temp_results.append(out)

    # 2. Room setpoint
    _name, pnu, _unit, scale, valid_range = ROOM_SETPOINT
    raw_sp = read_one(mod, pnu)
    sp_val = decode_sensor(raw_sp, scale, valid_range, "room_setpoint")
    cli.publish(STATE_T("room_setpoint"), sp_val, qos=0, retain=True)

    # 3. Accumulated outdoor
    _name, pnu, _unit, scale, valid_range = ACC_OUTDOOR
    raw_acc = read_one(mod, pnu)
    acc_val = decode_sensor(raw_acc, scale, valid_range, "acc_outdoor")
    cli.publish(STATE_T("acc_outdoor"), acc_val, qos=0, retain=True)

    # 4. Mode (user/schedule selection)
    _name, pnu = MODE
    raw_mode = read_one(mod, pnu)
    mode_str = MODE_MAP_FWD.get(raw_mode, f"Err({raw_mode})")
    cli.publish(MODE_T, mode_str, qos=0, retain=True)

    # 5. State (sun/moon glyph from 4211) — Comfort / Setback
    _name, pnu = OP_STATE
    raw_op = read_one(mod, pnu)
    state_str = decode_state(raw_op)
    cli.publish(STATE_T("state"), state_str, qos=0, retain=True)

    # 6. Pump
    _name, pnu = PUMP
    raw_pump = read_one(mod, pnu)
    pump_str = decode_pump_status(raw_pump)
    cli.publish(STATE_T("pump"), pump_str, qos=0, retain=True)

    # 7. Config (slope, cut_out)
    config_values = {}
    for key, (_name, pnu, scale, _unit) in CONFIG.items():
        raw = read_one(mod, pnu)
        out = decode_config(raw, scale)
        cli.publish(STATE_T(key), out, qos=0, retain=True)
        config_values[key] = out

    # 8. Derived: summer standby = (acc_outdoor >= cut_out)
    summer = "unknown"
    try:
        acc_f = float(acc_val)
        cut_f = float(config_values.get("cut_out", "nan"))
        summer = "Yes" if acc_f >= cut_f else "No"
    except (ValueError, TypeError):
        summer = "unknown"
    cli.publish(STATE_T("summer_standby"), summer, qos=0, retain=True)

    # 9. Single log line
    temps_joined = "/".join(temp_results)
    log.info(
        f"Snapshot: {temps_joined} | SP:{sp_val} acc:{acc_val} | "
        f"mode:{mode_str} state:{state_str} | pump:{pump_str} | "
        f"slope={config_values.get('slope')} cut={config_values.get('cut_out')} | "
        f"summer={summer}"
    )

# ---------- Command handling ----------
def handle_mode_cmd(cli, mod, payload):
    sel = payload.strip().upper()
    if sel not in MODE_MAP_REV:
        log.warning(f"Mode '{payload}' invalid; allowed: {list(MODE_MAP_REV.keys())}")
        return False
    raw = MODE_MAP_REV[sel]
    if write_one(mod, MODE[1], raw):
        log.info(f"Mode → {sel} (raw={raw})")
        return True
    return False

def handle_setpoint_cmd(cli, mod, payload):
    try:
        val = float(payload.strip())
    except ValueError:
        log.warning(f"Setpoint '{payload}' is not a number")
        return False
    lo, hi = ROOM_SETPOINT[4]
    if not (lo <= val <= hi):
        log.warning(f"Setpoint {val} outside allowed range {lo}..{hi}")
        return False
    # Force integer degrees — the panel doesn't accept 21.5 reliably,
    # so we round here even if HA/MQTT sent a fractional value.
    val_int = int(round(val))
    if val_int != val:
        log.info(f"Setpoint {val} rounded to {val_int} (integer-only)")
    raw = val_int * 10
    if write_one(mod, ROOM_SETPOINT[1], raw):
        log.info(f"Setpoint → {val_int}°C (raw={raw})")
        return True
    return False

def attach_command_handlers(cli, mod):
    def on_message(_cli, _ud, msg):
        payload = msg.payload.decode().strip()
        log.info(f"CMD topic={msg.topic} payload='{payload}'")

        changed = False
        if msg.topic == CMD_MODE:
            changed = handle_mode_cmd(cli, mod, payload)
        elif msg.topic == CMD_ROOM:
            changed = handle_setpoint_cmd(cli, mod, payload)
        elif msg.topic == CMD_REFR:
            changed = True  # force a snapshot
        else:
            log.warning(f"Unhandled command topic: {msg.topic}")

        if changed:
            publish_snapshot(cli, mod)

    cli.on_message = on_message
    for t in (CMD_MODE, CMD_ROOM, CMD_REFR):
        cli.subscribe(t)
        log.info(f"Subscribed '{t}'")

# ---------- Main ----------
def main():
    log.info(f"Starting ECL110 service | Port={PORT} Unit={UNIT} FW={FW_VERSION} | "
             f"config={CONFIG_FILE if _cfg else '(defaults/env)'} base_topic={NODE_ID}")
    cli = make_mqtt()
    publish_discovery(cli)

    mod = make_modbus()
    if not mod.connect():
        log.error("Modbus connect failed — continuing; values will be 'unknown'")
    else:
        log.info("Modbus connected")

    attach_command_handlers(cli, mod)
    publish_snapshot(cli, mod)

    try:
        while not stop_flag:
            for _ in range(INTERVAL):
                if stop_flag:
                    break
                time.sleep(1)
            if stop_flag:
                break
            publish_snapshot(cli, mod)
    finally:
        try: cli.publish(AVAIL_T, "offline", qos=1, retain=True)
        except Exception: pass
        try: mod.close()
        except Exception: pass
        cli.loop_stop()
        log.info("Stopped")

if __name__ == "__main__":
    main()