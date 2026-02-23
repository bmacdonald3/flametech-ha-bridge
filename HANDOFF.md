# HANDOFF: iFlame Fireplace AWS IoT → Home Assistant Integration
**Date:** 2026-02-23
**Status:** ✅ Fully operational

---

## Summary

Reverse-engineered the iFlame PRO Android app to gain direct cloud control of the fireplace via AWS IoT Core, then built an MQTT bridge to Home Assistant. The fireplace can now be controlled from HA dashboards, automations, and voice assistants.

## Architecture

```
┌─────────────┐    ┌──────────────────┐    ┌───────────────┐    ┌──────────┐    ┌───────────┐
│ iFlame App  │───→│ AWS IoT Core     │───→│ WiFi Hub      │───→│ The ONE  │───→│ Fireplace │
│ HA Bridge   │───→│ (Thing Shadow)   │    │ RFF-10FDC28   │    │ (BLE)    │    │ (RF)      │
└─────────────┘    │ MQTT + Shadow    │    │ 192.168.42.88 │    └──────────┘    └───────────┘
      │            └──────────────────┘    │ FW 13.00      │
      │                                    └───────────────┘
      │ MQTT
      ▼
┌─────────────┐    ┌──────────────────┐
│ HA MQTT     │◀──→│ Home Assistant   │
│ Broker      │    │ 192.168.42.5     │
│ (Mosquitto) │    │ (HAOS)           │
└─────────────┘    └──────────────────┘
```

## What's Running

### Bridge Service (Pi .13 — 192.168.42.13)
- **Service:** `iflame-api.service` (enabled, auto-starts on boot)
- **Script:** `/home/bmacdonald3/flametech-ha-bridge/src/flametech_mqtt_bridge.py`
- **Port:** 5088 (REST API)
- **Repo:** `https://github.com/bmacdonald3/flametech-ha-bridge`
- **Polls** AWS shadow every 30 seconds
- **Publishes** MQTT discovery + state to HA broker

### HA Entities
| Entity | Type | Description |
|--------|------|-------------|
| `switch.iflame_fireplace_fireplace` | Switch | Simple on/off |
| `climate.iflame_fireplace_fireplace_thermostat` | Climate | Smart thermostat (heat/off + temp 60-83°F) |
| `sensor.iflame_fireplace_fireplace_temperature` | Sensor | Ambient temp (°F) from hub |
| `sensor.iflame_fireplace_fireplace_mode` | Sensor | simple / smart |

### Dashboard Card
- `ha_card.yaml` in repo — custom:button-card with orange glow, tap opens thermostat

## AWS Credentials & Config

### Cognito (from APK decompile)
- **User Pool:** `us-east-1_xCzWPPECR`
- **App Client ID:** `REDACTED`
- **App Client Secret:** `REDACTED`
- **Identity Pool:** `REDACTED`
- **IoT Endpoint:** `REDACTED`
- **Account ID:** 057792304509

### Auth Flow
1. Cognito SRP auth → ID token
2. Exchange ID token for Identity Pool credentials (temporary IAM)
3. Attach `WiFi-Hub-Policy` to identity (grants `iot:*`)
4. Use IAM creds for IoT Data plane (shadow read/write, MQTT publish)
5. Credentials expire ~1 hour, auto-refreshed every 50 minutes

### iFlame Account
- **Email:** REDACTED
- **Password:** REDACTED
- **Cognito Username:** `U_benmacdonald3#gmail.com_1771873346.116121`

## Command Protocol

Commands sent to `$aws/things/RFF-10FDC28/shadow/update` via MQTT:

```json
{"state": {"desired": {"CID": "<next_int>", "CMD_LST": {"CMD_steps": [{"C": "<cmd>", "D": 0.2}]}}}}
```

### Command Strings
| Action | Command | Notes |
|--------|---------|-------|
| **Simple ON** | `2:0:1:193:203` | Fixed — sets encoded temp above ambient |
| **Simple OFF** | `2:0:1:192:203` | Fixed — sets encoded temp at ambient |
| **Smart ON at T°F** | `2:2:1:{T}:193:203` | T = target temp in plain °F |

### Field Breakdown
**Simple mode (5 fields):** `channel:mode:?:encoded_temp:max`
- encoded_temp = actual_temp_F + 120 (so 192 = 72°F, 193 = 73°F)

**Smart mode (6 fields):** `channel:mode:?:target_F:encoded_low:max`
- target_F = temperature in plain Fahrenheit (no encoding)

### Shadow State
```json
{
  "reported": {
    "AT": "72.28",        // Ambient temp (°F)
    "ST1": 73,            // Thermostat target (when in smart mode, else 0)
    "ST2": 0, "ST3": 0, "ST4": 0, "ST5": 0,
    "LastCID": "150",     // Last processed command ID
    "deviceInfo": {
      "DeviceID": "RFF-10FDC28",
      "FW": "13.00",
      "IP": "192.168.42.88",
      "SSID": "archer3400"
    }
  }
}
```

### Key Behaviors
- Hub heartbeat: updates reported state every ~30 seconds
- CID must increment for hub to process a new command
- Hub ACKs by setting `LastCID` = command's CID
- `is_on` determined by: set_temp > ambient_temp
- MQTT publish to shadow topic is required (HTTP shadow API alone doesn't work)

## MQTT Config
- **Broker:** 192.168.42.5:1883
- **User:** REDACTED
- **Password:** REDACTED_MQTT
- **Topics:**
  - `fireplace/status` — JSON state (retained)
  - `fireplace/set` — Switch commands (ON/OFF)
  - `fireplace/available` — Online/offline (retained, LWT)
  - `fireplace/climate/mode/set` — Climate mode (off/heat)
  - `fireplace/climate/temp/set` — Climate target temp
  - `fireplace/climate/state` — Climate state JSON (retained)
  - `homeassistant/switch/iflame_fireplace/config` — Discovery
  - `homeassistant/climate/iflame_thermostat/config` — Discovery
  - `homeassistant/sensor/iflame_*/config` — Discovery

## REST API (port 5088 on .13)
| Endpoint | Method | Body | Description |
|----------|--------|------|-------------|
| `/status` | GET | — | Current state JSON |
| `/on` | POST | — | Simple ON |
| `/off` | POST | — | Simple OFF |
| `/smart` | POST | `{"temp": 73}` | Smart thermostat mode |

## Reverse Engineering Notes

### How We Got Here
1. **Pi-hole DNS logging** revealed MQTT endpoint `REDACTED`
2. **APK decompile** (androguard) of iFlame PRO extracted all AWS config from `amplifyconfiguration.json`
3. **Cognito auth** via pycognito with SRP + client secret
4. **IoT thing discovery** — found 247,781 things in the account; identified `RFF-10FDC28` as our hub
5. **Policy attachment** — `WiFi-Hub-Policy` (allows `iot:*`) needed for shadow access
6. **Shadow analysis** — decoded command format by MQTT sniffing during app on/off/thermostat
7. **Key insight:** HTTP shadow update (boto3 `update_thing_shadow`) doesn't trigger the hub; must publish via MQTT to `$aws/things/{thing}/shadow/update`

### APK Resources
- Config: `res/raw/amplifyconfiguration.json`
- Key classes: `RFFRemoteControlCloudRepository`, `CloudModeAPI`, `MQTTTopicProvider`
- Device types: RFF, RFH, SOLX, FireBug, WeatherSmart, SmartHub, EchoFire, SkySmart, Proflame

### Other Discovered AWS Resources
- API Gateway endpoints: `gxlvgoouw8`, `327ayp4dud`, `xu76n2yfv6`, `9ufawa8ra3`, `uut73kytc9`
- S3: `girardiot-userfiles-mobilehub-1077482955`
- AppEngine: `https://iflame-16d29.ue.r.appspot.com/`
- OAuth: `t2fi.auth.us-east-1.amazoncognito.com`

## Maintenance

### Restart Service
```bash
sudo systemctl restart iflame-api
```

### View Logs
```bash
sudo journalctl -u iflame-api -f
```

### Update Code
```bash
cd ~/flametech-ha-bridge
git pull
sudo systemctl restart iflame-api
```

### If Credentials Fail
The iFlame account password or Cognito config may change with app updates. Re-extract from APK if needed.

## Files on Pi .13
- `/home/bmacdonald3/flametech-ha-bridge/` — Git repo
- `/home/bmacdonald3/iflame_api.py` — Original working copy (can be removed)
- `/etc/systemd/system/iflame-api.service` — Systemd service
- `/tmp/mqtt_sniff*.py` — MQTT capture scripts (temp)
