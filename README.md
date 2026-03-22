# ECL110 MQTT Bridge

A lightweight Python service that bridges a Danfoss ECL Comfort 110 controller (via Modbus RTU) to Home Assistant using MQTT Auto-Discovery.

## Features
* **Home Assistant Auto-Discovery:** Instantly creates a device and sensors in HA without manual YAML configuration.
* **Real-Time Monitoring:** Reads Outdoor, Room, Flow, and Return temperatures.
* **Robust Reconnection:** Handles Modbus read failures and MQTT drops gracefully.
* **Systemd Ready:** Designed to run continuously as a background service on a Raspberry Pi or Linux machine.

## Hardware Requirements
* Danfoss ECL Comfort 110 (Tested on FW 1.08)
* USB to RS485 Adapter 
* A machine to run the script 

## Installation

1. Clone this repository:
   ```bash
   git clone [https://github.com/matvib/ecl110-mqtt-bridge.git](https://github.com/matvib/ecl110-mqtt-bridge.git)
   cd ecl110-mqtt-bridge
   ```

2. Create a virtual environment and install dependencies:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

3. Create a `.env` file in the root directory with your MQTT credentials:
   ```env
   MQTT_USER=your_username
   MQTT_PASS=your_password
   ```

4. Update the `CONFIG` section in `ecl110_service.py` to match your hardware's USB port and Modbus unit ID.

## Running as a Service
An example `ecl110.service` file is included (or can be created). Update the paths to match your installation directory, then copy it to `/etc/systemd/system/` and run:

```bash
sudo systemctl daemon-reload
sudo systemctl enable ecl110
sudo systemctl start ecl110
```

## Current Status
* [x] Read temperatures and mode (Working)
* [x] MQTT Discovery (Working)
* [ ] Two-way control (Write Setpoint/Mode) - *In Progress*

## Acknowledgments
A massive thank you to [Ingramz/ecl110](https://github.com/Ingramz/ecl110) for documenting the Modbus registers (PNUs) for this controller. Their register map made the read/write logic of this bridge possible!