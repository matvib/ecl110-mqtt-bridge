# ECL110 MQTT Bridge

A lightweight Python service that bridges a Danfoss ECL Comfort 110 controller (via Modbus RTU) to Home Assistant using MQTT Auto-Discovery.

## Features
* **Home Assistant Auto-Discovery:** Instantly creates a device and sensors in HA without manual YAML configuration.
* **Real-Time Monitoring:** Reads Outdoor, Room, Flow, and Return temperatures, pump status, operating state and heating-curve config.
* **Two-Way Control:** Mode (`select`) and Room Setpoint (`number`, 15–25 °C, whole degrees) are writable from Home Assistant.
* **Robust Reconnection:** Handles Modbus read failures and MQTT drops gracefully.
* **One-Command Install:** `setup.sh` asks for serial port, MQTT broker and base topic, then installs a hardened systemd service.

## Hardware Requirements
* Danfoss ECL Comfort 110 (Tested on FW 1.08)
* USB to RS485 Adapter 
* A machine to run the script 

## Installation

```bash
git clone https://github.com/matvib/ecl110-mqtt-bridge.git
cd ecl110-mqtt-bridge
sudo ./setup.sh
```

The installer asks for:

| Step | Setting | Default |
|---|---|---|
| Serial | USB adapter (picked from `/dev/serial/by-id/`), Modbus unit ID, baud rate | first adapter, `5`, `19200` |
| MQTT | broker host, port, username, password | `localhost:1883`, anonymous |
| Home Assistant | base topic, device name, discovery prefix, poll interval, log level | `ecl110`, `ECL110`, `homeassistant`, `60` s, `INFO` |

It then creates a system user `ecl110` (in `dialout` for serial access), installs the program and a venv to `/opt/ecl110-mqtt-bridge`, writes `/etc/ecl110-mqtt-bridge/config.json`, and starts the `ecl110-mqtt-bridge` systemd service. Re-running `sudo ./setup.sh` offers the current settings as defaults.

The **base topic** prefixes every MQTT topic (`<base>/status`, `<base>/sensor/<key>/state`, `<base>/cmd/{mode,room_setpoint,refresh}`) and doubles as the HA device identifier, so give each controller its own if you run more than one bridge.

Useful commands:

```bash
journalctl -u ecl110-mqtt-bridge -f       # follow the log
sudo nano /etc/ecl110-mqtt-bridge/config.json && sudo systemctl restart ecl110-mqtt-bridge
sudo ./uninstall.sh                        # remove the service (asks before deleting config)
```

For development you can skip the installer: environment variables (or a `.env` file) override `config.json`, e.g. `ECL110_PORT`, `ECL110_UNIT`, `MQTT_HOST`, `MQTT_USER`, `MQTT_PASS`, `ECL110_BASE_TOPIC`, `LOG_LEVEL`. Point `ECL110_CONFIG` at a different config file if needed.

## Updating from GitHub
`deploy.sh` pulls the latest commit and, only if something changed, re-runs `setup.sh -y` (keeps your config, reinstalls, restarts):

```bash
./deploy.sh
```

To poll automatically, add a systemd timer that runs `deploy.sh` every few minutes (`ecl110-deploy.service` + `ecl110-deploy.timer` with `OnUnitActiveSec=5min`). `deploy.sh` needs passwordless sudo for that; `echo "$USER ALL=(root) NOPASSWD: /home/$USER/ecl110-mqtt-bridge/setup.sh -y" | sudo tee /etc/sudoers.d/ecl110-deploy` (adjust the path).

## Current Status
* [x] Read temperatures and mode (Working)
* [x] MQTT Discovery (Working)
* [x] Two-way control (Write Setpoint/Mode) - *Testing*

## Acknowledgments
A massive thank you to [Ingramz/ecl110](https://github.com/Ingramz/ecl110) for documenting the Modbus registers (PNUs) for this controller. Their register map made the read/write logic of this bridge possible!