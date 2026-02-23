# FlameTech HA Bridge

MQTT bridge between iFlame PRO fireplace (via AWS IoT Core) and Home Assistant.

## Architecture

```
iFlame App ──┐
             ├──→ AWS IoT Core (Shadow) ──→ WiFi Hub (RFF-10FDC28) ──→ The ONE (BLE) ──→ Fireplace (RF)
This Bridge ─┘         ↑                         ↓
                       └── Shadow Read ───────────┘
                       
This Bridge ──→ HA MQTT Broker ──→ Home Assistant (switch + climate + sensors)
```

## What It Does

- Authenticates to AWS Cognito using iFlame app credentials
- Sends fireplace commands via MQTT to AWS IoT shadow (same protocol as the app)
- Polls shadow state every 30 seconds for ambient temp and fireplace status
- Publishes MQTT discovery to Home Assistant for auto-detection
- Exposes REST API for direct control

## HA Entities Created

| Entity | Type | Description |
|--------|------|-------------|
| `switch.iflame_fireplace_fireplace` | Switch | Simple on/off |
| `climate.iflame_fireplace_fireplace_thermostat` | Climate | Smart thermostat (heat/off + temp slider) |
| `sensor.iflame_fireplace_fireplace_temperature` | Sensor | Ambient temperature (°F) |
| `sensor.iflame_fireplace_fireplace_mode` | Sensor | Current mode (simple/smart) |

## Command Format

Commands are sent via AWS IoT shadow update to `$aws/things/RFF-10FDC28/shadow/update`:

| Action | Command String | Notes |
|--------|---------------|-------|
| Simple ON | `2:0:1:193:203` | 193 = 73°F (1° above typical ambient) |
| Simple OFF | `2:0:1:192:203` | 192 = 72°F (at/below ambient) |
| Smart Thermostat | `2:2:1:{temp}:193:203` | {temp} = target °F, hub auto on/off |

Temperature encoding in simple mode: encoded = temp_F + 120.

## Installation

```bash
# On the bridge Pi (192.168.42.13)
pip install -r requirements.txt
sudo cp systemd/flametech-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable flametech-bridge
sudo systemctl start flametech-bridge
```

## REST API (port 5088)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/status` | GET | Current state (temp, mode, on/off) |
| `/on` | POST | Simple ON |
| `/off` | POST | Simple OFF |
| `/smart` | POST | Smart mode `{"temp": 73}` |

## Dashboard Card

See `ha_card.yaml` for a custom:button-card config with orange glow effect. Tap opens the thermostat controls.

## Configuration

Edit credentials in `src/flametech_mqtt_bridge.py`:
- AWS: Cognito pool, client ID/secret, identity pool (from APK decompile)
- iFlame: Email/password for iFlame PRO account
- MQTT: HA broker host, port, username, password
- Device: IoT thing name (RFF-10FDC28)
