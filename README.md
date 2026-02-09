# flametech-ha-bridge

FlameTech fireplace BLE → MQTT bridge for Home Assistant.

## What it does
- Connects to FlameTech hub over BLE (Bleak)
- Polls status characteristic (DD01) ~every 5s
- Publishes telemetry + heartbeat to MQTT
- Publishes HA MQTT Discovery configs

## Run
Typically runs as a systemd service on a Raspberry Pi.

## Repo layout
- `src/flametech_mqtt_bridge.py` — bridge script
- `systemd/` — example service unit (recommended)

