#!/usr/bin/env python3
import asyncio
import json
import time
from datetime import datetime, timezone

from bleak import BleakClient, BleakScanner
import paho.mqtt.client as mqtt

# ====== CONFIG ======
ADDR = "EC:64:C9:0F:DC:2A"

# GATT chars
AA = "0000aa01-0000-1000-8000-00805f9b34fb"
BB = "0000bb01-0000-1000-8000-00805f9b34fb"
CC = "0000cc01-0000-1000-8000-00805f9b34fb"
DD = "0000dd01-0000-1000-8000-00805f9b34fb"

# MQTT (HA Mosquitto)
MQTT_HOST = "192.168.42.5"
MQTT_PORT = 1883
MQTT_USER = "REDACTED"       # <-- set to None if not needed
MQTT_PASS = "REDACTED_MQTT"         # <-- set to None if not needed

# Topics
TOPIC_STATE = "flametech/hub/dd"
TOPIC_HEARTBEAT = "flametech/hub/last_seen"

# HA discovery base
DISCOVERY_PREFIX = "homeassistant"
DEVICE_ID = "flametech_hub_ec64c90fdc2a"
DEVICE_META = {
    "identifiers": [DEVICE_ID],
    "name": "FlameTech Hub",
    "manufacturer": "FlameTech",
    "model": "Hub",
}

# Polling / reconnect behavior
POLL_SECONDS = 5.0
SCAN_TIMEOUT = 8.0


def log(msg: str):
    print(msg, flush=True)


def iso_utc_now() -> str:
    # HA timestamp sensor expects ISO8601
    return datetime.now(timezone.utc).isoformat()


def mqtt_client() -> mqtt.Client:
    c = mqtt.Client()
    if MQTT_USER:
        c.username_pw_set(MQTT_USER, MQTT_PASS or "")
    c.connect(MQTT_HOST, MQTT_PORT, 60)
    c.loop_start()
    return c


def publish_discovery(m: mqtt.Client):
    # Ambient Temp
    at = {
        "name": "FlameTech Ambient Temp",
        "uniq_id": "flametech_hub_at",
        "stat_t": TOPIC_STATE,
        "val_tpl": "{{ value_json.AT }}",
        "unit_of_meas": "°F",
        "dev_cla": "temperature",
        "dev": DEVICE_META,
    }
    m.publish(f"{DISCOVERY_PREFIX}/sensor/flametech_hub/at/config",
              json.dumps(at), retain=True)

    # ST1..ST5, fw
    for k in ["ST1", "ST2", "ST3", "ST4", "ST5", "fw"]:
        cfg = {
            "name": f"FlameTech {k}",
            "uniq_id": f"flametech_hub_{k.lower()}",
            "stat_t": TOPIC_STATE,
            "val_tpl": f"{{{{ value_json.{k} }}}}",
            "dev": DEVICE_META,
        }
        m.publish(f"{DISCOVERY_PREFIX}/sensor/flametech_hub/{k.lower()}/config",
                  json.dumps(cfg), retain=True)

    # Heartbeat (Last Seen)
    heartbeat_config = {
        "name": "FlameTech Last Seen",
        "uniq_id": "flametech_hub_last_seen",
        "stat_t": TOPIC_HEARTBEAT,
        "dev_cla": "timestamp",
        "icon": "mdi:clock-check",
        "dev": DEVICE_META,
    }
    m.publish(f"{DISCOVERY_PREFIX}/sensor/flametech_hub/last_seen/config",
              json.dumps(heartbeat_config), retain=True)

    log("Published MQTT discovery configs.")


def publish_state(m: mqtt.Client, dd: dict):
    payload = json.dumps(dd)
    rc = m.publish(TOPIC_STATE, payload, retain=True).rc
    log(f"MQTT publish rc={rc} topic={TOPIC_STATE} payload={payload}")


def publish_heartbeat(m: mqtt.Client):
    ts = iso_utc_now()
    rc = m.publish(TOPIC_HEARTBEAT, ts, retain=True).rc
    log(f"MQTT publish rc={rc} topic={TOPIC_HEARTBEAT} payload={ts}")


def parse_dd(raw: bytes) -> dict:
    # DD looks like JSON with a trailing null byte
    s = raw.decode(errors="ignore").strip("\x00").strip()
    if not s:
        return {}
    return json.loads(s)


async def find_device(timeout: float):
    devs = await BleakScanner.discover(timeout=timeout)
    for d in devs:
        if d.address.upper() == ADDR.upper():
            return d
    return None


async def run_bridge():
    m = mqtt_client()
    publish_discovery(m)

    backoff = 2.0
    while True:
        try:
            log("Scanning for hub...")
            dev = await find_device(SCAN_TIMEOUT)
            if not dev:
                log(f"Not found. Sleeping {backoff:.1f}s...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.6, 30.0)
                continue

            log(f"Found: {dev.address} name={dev.name!r}")
            backoff = 2.0

            log("Connecting...")
            async with BleakClient(dev, timeout=20.0) as client:
                log("Connected. Polling DD...")

                while True:
                    # Heartbeat every cycle no matter what
                    publish_heartbeat(m)

                    try:
                        raw = await client.read_gatt_char(DD)
                        dd = parse_dd(raw)
                        if dd:
                            publish_state(m, dd)
                            log(f"{time.strftime('%H:%M:%S')} DD: {dd}")
                        else:
                            log("DD empty/invalid (no publish).")
                    except Exception as e:
                        log(f"DD read failed: {e!r}")

                    await asyncio.sleep(POLL_SECONDS)

        except Exception as e:
            log(f"ERR: {e}")
            log(f"Reconnect sleep {backoff:.1f}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.6, 30.0)


def main():
    log("Starting FlameTech MQTT bridge (persistent connect + poll)...")
    asyncio.run(run_bridge())


if __name__ == "__main__":
    main()
