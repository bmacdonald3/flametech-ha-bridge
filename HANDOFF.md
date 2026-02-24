# HANDOFF: iFlame Fireplace Bridge — 2026-02-24
**Status:** ✅ Fully operational with all controls

---

## Session Summary

Resolved the fan/flame/split encoding inconsistency from last session, discovered ember light and overhead light encoding, corrected the control byte interpretation, added all controls as HA entities, fixed thermostat mode reporting, and built a browser_mod popup card with full fireplace controls.

## What Changed

### 1. Resolved Encoding Inconsistency
**Problem:** Last session's data suggested flame "off" = 8 in F+B mode, creating an apparent inconsistency with front-only mode where flame "off" = 0.

**Root cause:** The "flame off = 8" readings actually had the **ember light** on (bit 3). The flame encoding has no offset — it's clean 0-6 in all modes.

### 2. Complete Protocol Decode

Both command bytes are now fully mapped:

**Control byte (4th field in simple, 5th in smart):**
```
bit 7:    always 1 (128)
bits 4-6: overhead light level (0=off, 1-5)
bit 0:    fireplace on/off (1=on, 0=off)
bits 1-3: unused (always 0)
```
Formula: `128 + (overhead × 16) + on_bit`

**Fan/flame byte (5th field in simple, 6th in smart):**
```
bit 7:    split flow (0=front only, 1=front+back)
bits 4-6: fan level (0=off, 1-6)
bit 3:    ember light (0=off, 1=on)
bits 0-2: flame level (0=off, 1-6)
```
Formula: `(split × 128) + (fan × 16) + (ember × 8) + flame`

**Key insight:** The original ON/OFF commands (193 vs 192) were NOT temperature-encoded. 193 = `128 + 64(overhead 4) + 1(on)`. The `temp + 120` interpretation was a coincidence.

### 3. Calibration Data (all confirmed this session)

| Test | Value | Decode |
|------|-------|--------|
| F+B, fan 0, flame 0 | 128 | 128+0+0+0 ✅ |
| F+B, fan 0, flame 1 | 129 | 128+0+0+1 ✅ |
| F+B, fan 1, flame 0 | 144 | 128+16+0+0 ✅ |
| Front, fan 0, flame 0 | 0 | 0+0+0+0 ✅ |
| Front, fan 0, flame 1 | 1 | 0+0+0+1 ✅ |
| Front, fan 1, flame 0 | 16 | 0+16+0+0 ✅ |
| F+B, fan 6, flame 6 | 230 | 128+96+0+6 ✅ |
| Ember on adds 8 | 42 vs 34 | +8 confirmed ✅ |

Overhead light calibration (control byte):
| Overhead | Value | Decode |
|----------|-------|--------|
| Off, fire off | 128 | 128+0+0 ✅ |
| Off, fire on | 129 | 128+0+1 ✅ |
| 1, fire on | 145 | 128+16+1 ✅ |
| 2, fire on | 161 | 128+32+1 ✅ |
| 4, fire on | 193 | 128+64+1 ✅ |
| 5, fire on | 209 | 128+80+1 ✅ |

### 4. Mode Logic Fix
**Problem:** HA showed fireplace as "off" when the hub's thermostat cycled the flame off (ambient >= target), even though the thermostat was still active.

**Fix:** Three-mode state based on `ST1` (hub's reported thermostat target):
- `ST1 > 0` → mode = `thermostat` (hub managing on/off cycling, may or may not be burning)
- `ST1 = 0` + on-bit → mode = `simple` (flame on, no thermostat)
- `ST1 = 0` + no on-bit → mode = `off`

New state fields:
- `is_on`: system active (flame burning OR thermostat active)
- `flame_on`: flame is physically burning right now
- `thermostat_active`: hub has a target temp and is managing cycling
- `mode`: "thermostat" | "simple" | "off"

Climate entity now shows "heat" when thermostat is active regardless of whether flame is currently burning.

### 5. Smart Mode Investigation
Tested the iFlame app's "auto control" (smart) mode. Result: the shadow command format is identical to our thermostat mode (`2:2:1:target:ctrl:ff`), `MODE` stays at 1, and the hub does NOT dynamically change fan/flame in the desired state. True dynamic fan/flame adjustment may only exist on the physical remote via direct RF/BLE to "The ONE" module. From the cloud/shadow perspective, "smart" and "thermostat" are indistinguishable.

### 6. New HA Entities
All entities under device "iFlame Fireplace":

| Entity ID | Type | Range | Description |
|-----------|------|-------|-------------|
| `switch.iflame_fireplace_fireplace` | switch | ON/OFF | Power on/off |
| `climate.iflame_fireplace_fireplace_thermostat` | climate | 60-83°F | Thermostat mode |
| `sensor.iflame_fireplace_fireplace_temperature` | sensor | °F | Ambient temp |
| `sensor.iflame_fireplace_fireplace_mode` | sensor | text | thermostat/simple/off |
| `number.iflame_fireplace_fireplace_fan` | number | 0-6 | Fan level |
| `number.iflame_fireplace_fireplace_flame` | number | 0-6 | Flame level |
| `number.iflame_fireplace_fireplace_overhead_lights` | number | 0-5 | Overhead lights |
| `switch.iflame_fireplace_fireplace_ember_light` | switch | ON/OFF | Ember light |
| `switch.iflame_fireplace_fireplace_split_flow` | switch | ON/OFF | Front+back flow |

### 7. HA Dashboard Card
Built a custom:button-card with browser_mod popup:
- **Compact tile**: 2×4 grid, 115px, shows mode + temps/levels, amber glow animation
- **Popup**: Full controls — power, thermostat, flame ±, fan ±, ember toggle, overhead ±, split toggle
- Card YAML: `~/flametech-ha-bridge/ha_card.yaml`

### 8. REST API Additions
| Endpoint | Method | Body | Description |
|----------|--------|------|-------------|
| `/fan` | POST | `{"level": 0-6}` | Set fan level |
| `/flame` | POST | `{"level": 0-6}` | Set flame level |
| `/split` | POST | `{"on": true/false}` | Set split flow |
| `/ember` | POST | `{"on": true/false}` | Set ember light |
| `/overhead` | POST | `{"level": 0-5}` | Set overhead lights |

## Files Changed
- `src/flametech_mqtt_bridge.py` — Complete rewrite of encoding/decoding, all controls added
- `src/flametech_mqtt_bridge.py.bak2` — Backup from start of session
- `src/flametech_mqtt_bridge.py.bak3` — Backup from mid-session
- `ha_card.yaml` — Updated dashboard card with popup controls
- `HANDOFF.md` — Updated

## Files on Pi .13
- `/home/bmacdonald3/flametech-ha-bridge/` — Git repo
- `/home/bmacdonald3/HANDOFF-2026-02-24.md` — This file
- `/etc/systemd/system/iflame-api.service` — Systemd service (unchanged)

## Quick Reference
```bash
# Restart service
sudo systemctl restart iflame-api

# View logs
sudo journalctl -u iflame-api -f

# Check current state
curl -s http://localhost:5088/status | python3 -m json.tool

# Test controls
curl -s -X POST http://localhost:5088/fan -H "Content-Type: application/json" -d '{"level": 3}'
curl -s -X POST http://localhost:5088/flame -H "Content-Type: application/json" -d '{"level": 4}'
curl -s -X POST http://localhost:5088/ember -H "Content-Type: application/json" -d '{"on": true}'
curl -s -X POST http://localhost:5088/overhead -H "Content-Type: application/json" -d '{"level": 3}'
curl -s -X POST http://localhost:5088/split -H "Content-Type: application/json" -d '{"on": true}'
```
