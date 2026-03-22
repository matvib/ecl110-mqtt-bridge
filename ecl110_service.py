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
INTERVAL     = 60  # seconds

# ---------- MAPS (FW 1.08) ----------
SENSOR_NC_RAW = 1920  # 192.0°C sentinel

TEMPS = {
    "s1_outdoor": ("S1 Outdoor", 11201, "°C", 0.1),
    "s2_room":    ("S2 Room",    11202, "°C", 0.1),
    "s3_flow":    ("S3 Flow",    11203, "°C", 0.1),
    "s4_return":  ("S4 Return",  11204, "°C", 0.1),
}
ROOM_SETPOINT = ("Room Setpoint", 11229, "°C", 0.1)  # FW 1.08
MODE          = ("Mode",          4201,  "text", 1)  # 1..4 -> text
MODE_MAP_FWD  = {1:"AUTO", 2:"COMFORT", 3:"SETBACK", 4:"STANDBY"}

# MQTT topics
AVAIL_T   = f"{NODE_ID}/status"
STATE_T   = lambda key: f"{NODE_ID}/sensor/{key}/state"
MODE_T    = f"{NODE_ID}/sensor/mode/state"
CMD_MODE  = f"{NODE_ID}/cmd/mode"
CMD_ROOM  = f"{NODE_ID}/cmd/room_setpoint"
CMD_REFR  = f"{NODE_ID}/cmd/refresh"

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
def s16(v: int | None) -> int | None:
    """Interpret a 16-bit Modbus register as signed int16."""
    if v is None:
        return None
    return v - 65536 if v >= 32768 else v

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

def publish_discovery(cli):
    dev = {
        "identifiers": [NODE_ID],
        "manufacturer": "Danfoss",
        "model": f"ECL Comfort 110 (App 130, FW {FW_VERSION})",
        "name": FRIENDLY,
    }
    # temps
    for key,(name, _pnu, unit, _scale) in TEMPS.items():
        cfg_t = f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/{key}/config"
        cfg = {
            "name": f"ECL {name}",
            "uniq_id": f"{NODE_ID}_{key}",
            "stat_t": STATE_T(key),
            "avty_t": AVAIL_T,
            "dev": dev,
            "unit_of_meas": unit,
            "dev_cla": "temperature",
            "stat_cla": "measurement",
        }
        cli.publish(cfg_t, json.dumps(cfg), qos=1, retain=True)
    # room setpoint
    key = "room_setpoint"
    name, _pnu, unit, _ = ROOM_SETPOINT
    cfg_t = f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/{key}/config"
    cfg = {
        "name": f"ECL {name}",
        "uniq_id": f"{NODE_ID}_{key}",
        "stat_t": STATE_T(key),
        "avty_t": AVAIL_T,
        "dev": dev,
        "unit_of_meas": unit,
        "dev_cla": "temperature",
        "stat_cla": "measurement",
    }
    cli.publish(cfg_t, json.dumps(cfg), qos=1, retain=True)
    # mode (text)
    cfg_t = f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/mode/config"
    cfg = {
        "name": "ECL Mode",
        "uniq_id": f"{NODE_ID}_mode",
        "stat_t": MODE_T,
        "avty_t": AVAIL_T,
        "dev": dev,
    }
    cli.publish(cfg_t, json.dumps(cfg), qos=1, retain=True)
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
    # 1. Signal we are online
    cli.publish(AVAIL_T, "online", qos=1, retain=True)

    # 2. Collect Temp Values (S1..S4)
    results = []
    for key, (_name, pnu, _unit, scale) in TEMPS.items():
        raw = read_one(mod, pnu)
        sval = s16(raw) if raw is not None else None
        
        if sval is None or (sval == SENSOR_NC_RAW and key.startswith("s")):
            out = "unknown"
        else:
            val = round(sval * scale, 1)
            out = str(val) if -50.0 <= val <= 80.0 else "unknown"
        
        cli.publish(STATE_T(key), out, qos=0, retain=False)
        results.append(out)

    # 3. Handle Room Setpoint
    _name, pnu, _unit, scale = ROOM_SETPOINT
    raw_sp = read_one(mod, pnu)
    sval_sp = s16(raw_sp) if raw_sp is not None else None
    sp_val = "unknown" if sval_sp is None else str(round(sval_sp * scale, 1))
    cli.publish(STATE_T("room_setpoint"), sp_val, qos=0, retain=False)

    # 4. Handle Mode
    _name, pnu, _kind, _ = MODE
    raw_mode = read_one(mod, pnu)
    mode_str = MODE_MAP_FWD.get(raw_mode, f"Err({raw_mode})")
    cli.publish(MODE_T, mode_str, qos=0, retain=False)

    # 5. log output
    temps_joined = "/".join(results)
    log.info(f"Snapshot: {temps_joined} | SP:{sp_val} | {mode_str}")

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
