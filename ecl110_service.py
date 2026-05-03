#
# VERSION read only
#
#!/usr/bin/env python3
import json, time, socket, signal, sys, threading, os, logging
from paho.mqtt import client as mqtt
from pymodbus.client import ModbusSerialClient
from pymodbus.pdu import ExceptionResponse
from dotenv import load_dotenv

# ---------- LOGGING ----------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("ecl110")

# ---------- CONFIG ----------
load_dotenv()
PORT         = "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_B003297J-if00-port0"
UNIT         = 5
BAUDRATE     = 19200
TIMEOUT_S    = 2.0
FW_VERSION   = "1.08"

MQTT_HOST    = "localhost"
MQTT_PORT    = 1883
MQTT_USER    = os.getenv("MQTT_USER")
MQTT_PASS    = os.getenv("MQTT_PASS")
DISCOVERY_PREFIX = "homeassistant"
NODE_ID      = "ecl110"
FRIENDLY     = "ECL110"
INTERVAL          = 60     # seconds — fast snapshot 
CONFIG_INTERVAL   = 900    # seconds — slow snapshot

# ---------- MAPS (FW 1.08) ----------
SENSOR_NC_RAW = 1920  # -> 192.0°C <- = disconnected S1..S4

# Temperature sensors. display_name, PNU, unit, scale, (valid_min, valid_max))
# PNU = Modbus register +1 
# valid_min  valid_max = possibly in between  
TEMPS = {
    "s1_outdoor": ("S1 Outdoor", 11201, "°C", 0.1, (-50.0,  60.0)),
    "s2_room":    ("S2 Room",    11202, "°C", 0.1, (  0.0,  50.0)),
    "s3_flow":    ("S3 Flow",    11203, "°C", 0.1, (  0.0, 120.0)),
    "s4_return":  ("S4 Return",  11204, "°C", 0.1, (  0.0, 100.0)),
}

# Room setpoint — set only to non decimal values don't know how display reatcs to 21.5
ROOM_SETPOINT = ("Room Setpoint", 11229, "°C", 0.1, (15.0, 25.0))

# Accumulated when reached = summer cutoff 
ACC_OUTDOOR   = ("Accumulated Outdoor", 11100, "°C", 0.1, (-50.0, 60.0))

# Mode: selection on the panel.
#     0=MANUAL, 1=AUTO, 2=COMFORT, 3=SETBACK, 4=STANDBY
# AUTO = will set mode based on timer schedule. If you have that option
MODE          = ("Mode", 4201) 
MODE_MAP_FWD  = {0: "MANUAL", 1: "AUTO", 2: "COMFORT", 3: "SETBACK", 4: "STANDBY"}

# Operating state: the sun and moon on the display
#     2 = COMFORT (sun, S)
#     3 = ramping toward COMFORT (S) 
#     0 = SETBACK / standby / manual-idle (moon, M)
#     1 = ramping toward SETBACK (M) 
# You need to be on schedule to observer the ramping thats my assumtion
OP_STATE      = ("State", 4211)  # wire 4210

# Pump status 1 / 0
PUMP          = ("Pump", 4002)  # wire 4001

# The config parameters — read on slow interval, almost never change.
CONFIG = {
    # Slope  — the heating curve steepness.
    "slope":    ("Slope",           11175, 0.1, None),
    "cut_out":  ("Heating Cut-out", 11179, 1.0, "°C"),
}

# MQTT topics
AVAIL_T   = f"{NODE_ID}/status"
STATE_T   = lambda key: f"{NODE_ID}/sensor/{key}/state"
BSTATE_T  = lambda key: f"{NODE_ID}/binary_sensor/{key}/state"
MODE_T    = f"{NODE_ID}/sensor/mode/state"
CMD_MODE  = f"{NODE_ID}/cmd/mode"
CMD_ROOM  = f"{NODE_ID}/cmd/room_setpoint"
CMD_REFR  = f"{NODE_ID}/cmd/refresh"

# Icon mapping 
ICONS = {
    # Measurements — raw probe readings
    "s1_outdoor":     "mdi:thermometer",
    "s2_room":        "mdi:thermometer",
    "s3_flow":        "mdi:thermometer",
    "s4_return":      "mdi:thermometer",
    "acc_outdoor":    "mdi:thermometer-lines",  # filtered/averaged
    # Settings — knobs/configuration
    "room_setpoint":  "mdi:home-thermometer-outline",
    "mode":           "mdi:cog",
    "slope":          "mdi:chart-bell-curve-cumulative",  
    "cut_out":        "mdi:thermostat-cog",
    # Status — live operational state
    "state":          "mdi:theme-light-dark",  # sun/moon, matches panel glyph
    "pump":           "mdi:pump",
    "summer_standby": "mdi:white-balance-sunny",
}

# Discovery topics for entities used to publish but no longer do.
LEGACY_DISCOVERY_TOPICS = [
    f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/actual_mode/config",
    f"{DISCOVERY_PREFIX}/binary_sensor/{NODE_ID}/comfort_active/config",
    f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/displace/config",
    f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/displacement/config",
    f"{DISCOVERY_PREFIX}/binary_sensor/{NODE_ID}/pump/config",
    f"{DISCOVERY_PREFIX}/binary_sensor/{NODE_ID}/summer_standby/config",
    f"{DISCOVERY_PREFIX}/binary_sensor/{NODE_ID}/operating_state/config",
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
    """0/1 to 'Running'/'Stopped'/'unknown'."""
    if raw is None:
        return "unknown"
    return "Running" if raw else "Stopped"

def decode_sensor(raw, scale, valid_range, key):
    if raw is None:
        return "unknown"
    sval = s16(raw)
    # Disconnected-sensor sentinel applies only to physical S* sensors
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
    """For non-temperature numeric values like slope """
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

def _publish_binary_sensor(cli, key, name, dev_cla=None):
    cfg_t = f"{DISCOVERY_PREFIX}/binary_sensor/{NODE_ID}/{key}/config"
    cfg = {
        "name": name,
        "uniq_id": f"{NODE_ID}_{key}",
        "stat_t": BSTATE_T(key),
        "avty_t": AVAIL_T,
        "dev": _device_dict(),
        "pl_on": "ON",
        "pl_off": "OFF",
    }
    if dev_cla:
        cfg["dev_cla"] = dev_cla
    cli.publish(cfg_t, json.dumps(cfg), qos=1, retain=True)

def publish_discovery(cli):
    # --- cleanup: tell HA to forget renamed/removed entities ---
    for t in LEGACY_DISCOVERY_TOPICS:
        cli.publish(t, "", qos=1, retain=True)

    # --- physical temp sensors (S1..S4) ---
    for key, (name, _pnu, unit, _scale, _range) in TEMPS.items():
        _publish_temp_sensor(cli, key, name, unit)

    # --- room setpoint ---
    _publish_temp_sensor(cli, "room_setpoint", ROOM_SETPOINT[0], ROOM_SETPOINT[2])

    # --- accumulated outdoor (filtered) ---
    _publish_temp_sensor(cli, "acc_outdoor", ACC_OUTDOOR[0], ACC_OUTDOOR[2])

    # --- mode (user/schedule selection on the panel) ---
    _publish_text_sensor(cli, "mode", "Mode", MODE_T)

    # --- state (sun=Comfort / moon=Setback, derived from 4211) ---
    _publish_text_sensor(cli, "state", "State", STATE_T("state"))

    # --- pump status (text sensor: Running / Stopped) ---
    _publish_text_sensor(cli, "pump", "Pump Status", STATE_T("pump"))

    # --- summer standby (derived; text sensor: Yes / No) ---
    _publish_text_sensor(cli, "summer_standby", "Summer Standby", STATE_T("summer_standby"))

    # --- curve config (slow refresh, read-only) ---
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

def publish_snapshot(cli, mod):
    """Fast snapshot"""
    cli.publish(AVAIL_T, "online", qos=1, retain=True)

    # 1. S1..S4 physical sensors
    results = []
    for key, (_name, pnu, _unit, scale, valid_range) in TEMPS.items():
        raw = read_one(mod, pnu)
        out = decode_sensor(raw, scale, valid_range, key)
        cli.publish(STATE_T(key), out, qos=0, retain=True)
        results.append(out)

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

    # 6. Pump status
    _name, pnu = PUMP
    raw_pump = read_one(mod, pnu)
    pump_str = decode_pump_status(raw_pump)
    cli.publish(STATE_T("pump"), pump_str, qos=0, retain=True)

    # 7. log line
    temps_joined = "/".join(results)
    log.info(
        f"Snapshot: {temps_joined} | SP:{sp_val} acc:{acc_val} | "
        f"mode:{mode_str} state:{state_str} | pump:{pump_str}"
    )

def publish_config_snapshot(cli, mod):
    """
    Slow snapshot
    """
    config_values = {}
    for key, (_name, pnu, scale, _unit) in CONFIG.items():
        raw = read_one(mod, pnu)
        out = decode_config(raw, scale)
        cli.publish(STATE_T(key), out, qos=0, retain=True)
        config_values[key] = out

    # Derived: summer standby = (acc_outdoor >= cut_out)
    # Re-read acc_outdoor here so the comparison is consistent (within seconds).
    _name, pnu, _unit, scale, valid_range = ACC_OUTDOOR
    raw_acc = read_one(mod, pnu)
    acc_str = decode_sensor(raw_acc, scale, valid_range, "acc_outdoor")
    summer = "unknown"
    try:
        acc_f = float(acc_str)
        cut_f = float(config_values.get("cut_out", "nan"))
        summer = "Yes" if acc_f >= cut_f else "No"
    except (ValueError, TypeError):
        summer = "unknown"
    cli.publish(STATE_T("summer_standby"), summer, qos=0, retain=True)

    log.info(
        f"Config: slope={config_values.get('slope')} "
        f"min={config_values.get('temp_min')} "
        f"max={config_values.get('temp_max')} "
        f"cut={config_values.get('cut_out')} | "
        f"summer_standby={summer}"
    )

def attach_command_handlers(cli, mod):
    def on_message(_cli, _ud, msg):
        payload = msg.payload.decode().strip()
        log.info(f"CMD topic={msg.topic} payload='{payload}' (read-only mode; refreshing)")
        publish_snapshot(cli, mod)  # refresh now
    cli.on_message = on_message
    for t in (CMD_MODE, CMD_ROOM, CMD_REFR):
        cli.subscribe(t)
        log.info(f"Subscribed '{t}'")

# ---------- Main ----------
def main():
    log.info(f"Starting ECL110 service | Port={PORT} Unit={UNIT} FW={FW_VERSION}")
    cli = make_mqtt()
    publish_discovery(cli)

    mod = make_modbus()
    if not mod.connect():
        log.error("Modbus connect failed — continuing; values will be 'unknown'")
    else:
        log.info("Modbus connected")

    attach_command_handlers(cli, mod)

    # Initial snapshots (both fast and slow)
    publish_snapshot(cli, mod)
    publish_config_snapshot(cli, mod)
    last_config_t = time.monotonic()

    try:
        while not stop_flag:
            for _ in range(INTERVAL):
                if stop_flag:
                    break
                time.sleep(1)
            if stop_flag:
                break

            publish_snapshot(cli, mod)

            # Refresh config registers on the slower interval
            if time.monotonic() - last_config_t >= CONFIG_INTERVAL:
                publish_config_snapshot(cli, mod)
                last_config_t = time.monotonic()
    finally:
        try: cli.publish(AVAIL_T, "offline", qos=1, retain=True)
        except Exception: pass
        try: mod.close()
        except Exception: pass
        cli.loop_stop()
        log.info("Stopped")

if __name__ == "__main__":
    main()