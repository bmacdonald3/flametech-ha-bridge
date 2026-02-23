import boto3, json, time, threading
from flask import Flask, jsonify, request
from pycognito import Cognito
from awsiot import mqtt_connection_builder
from awscrt import mqtt as awsmqtt, auth
import paho.mqtt.client as paho_mqtt
import logging

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("iflame")

# ── AWS Config ──
POOL_ID = "us-east-1_xCzWPPECR"
CLIENT_ID = "REDACTED"
CLIENT_SECRET = "REDACTED"
IDENTITY_POOL = "REDACTED"
IOT_EP = "REDACTED"
R = "us-east-1"
THING = "RFF-10FDC28"
EMAIL = "REDACTED"
IFLAME_PW = "REDACTED"

# ── HA MQTT Config ──
HA_MQTT_HOST = "192.168.42.5"
HA_MQTT_PORT = 1883
HA_MQTT_USER = "REDACTED"
HA_MQTT_PASS = "REDACTED_MQTT"
TOPIC_STATE = "fireplace/status"
TOPIC_CMD = "fireplace/set"
TOPIC_AVAIL = "fireplace/available"
TOPIC_CLIMATE_MODE_CMD = "fireplace/climate/mode/set"
TOPIC_CLIMATE_TEMP_CMD = "fireplace/climate/temp/set"
TOPIC_CLIMATE_STATE = "fireplace/climate/state"

app = Flask(__name__)
creds = None
creds_expire = 0
iot_session = None
ha_mqtt = None

# ═══════════════════════════════════════════════════════════════════════════════
# AWS Auth & IoT
# ═══════════════════════════════════════════════════════════════════════════════

def refresh_creds():
    global creds, creds_expire, iot_session
    log.info("Refreshing AWS credentials...")
    u = Cognito(POOL_ID, CLIENT_ID, client_secret=CLIENT_SECRET, username=EMAIL)
    u.authenticate(password=IFLAME_PW)
    ic = boto3.client("cognito-identity", region_name=R)
    lk = f"cognito-idp.{R}.amazonaws.com/{POOL_ID}"
    iid = ic.get_id(IdentityPoolId=IDENTITY_POOL, Logins={lk: u.id_token})["IdentityId"]
    cr = ic.get_credentials_for_identity(IdentityId=iid, Logins={lk: u.id_token})["Credentials"]
    creds = cr
    creds_expire = time.time() + 3000
    iot_session = boto3.Session(
        aws_access_key_id=cr["AccessKeyId"],
        aws_secret_access_key=cr["SecretKey"],
        aws_session_token=cr["SessionToken"],
        region_name=R
    )
    try:
        iot_session.client("iot").attach_policy(policyName="WiFi-Hub-Policy", target=iid)
    except:
        pass
    log.info("AWS credentials refreshed")

def get_shadow():
    if time.time() > creds_expire:
        refresh_creds()
    iot_data = iot_session.client("iot-data", endpoint_url=f"https://{IOT_EP}")
    shadow = iot_data.get_thing_shadow(thingName=THING)
    return json.loads(shadow["payload"].read())

def aws_publish(payload_dict):
    if time.time() > creds_expire:
        refresh_creds()
    cp = auth.AwsCredentialsProvider.new_static(
        access_key_id=creds["AccessKeyId"],
        secret_access_key=creds["SecretKey"],
        session_token=creds["SessionToken"]
    )
    conn = mqtt_connection_builder.websockets_with_default_aws_signing(
        endpoint=IOT_EP, region=R, credentials_provider=cp,
        client_id=f"ha-iflame-{int(time.time())}", clean_session=True,
    )
    conn.connect().result(timeout=10)
    conn.publish(
        topic=f"$aws/things/{THING}/shadow/update",
        payload=json.dumps(payload_dict),
        qos=awsmqtt.QoS.AT_LEAST_ONCE
    )
    time.sleep(2)
    conn.disconnect().result()

def next_cid():
    shadow = get_shadow()
    return str(int(shadow["state"]["desired"]["CID"]) + 1)

# ═══════════════════════════════════════════════════════════════════════════════
# Shadow → State Parser
# ═══════════════════════════════════════════════════════════════════════════════

def parse_shadow(shadow):
    d = shadow["state"]["desired"]
    r = shadow["state"]["reported"]
    at = float(r["AT"])
    st1 = r.get("ST1", 0)
    cmd = d.get("CMD_LST", {}).get("CMD_steps", [{}])[0].get("C", "")
    parts = cmd.split(":") if cmd else []

    if len(parts) == 6:
        mode = "smart"
        set_temp = int(parts[3])
        is_on = set_temp > at
    elif len(parts) == 5:
        mode = "simple"
        set_temp = int(parts[3]) - 120
        is_on = set_temp > at
    else:
        mode = "unknown"
        set_temp = 0
        is_on = False

    return {
        "is_on": is_on,
        "mode": mode,
        "AT": round(at, 1),
        "set_temp": set_temp,
        "ST1": st1,
        "ST2": r.get("ST2", 0),
        "ST3": r.get("ST3", 0),
        "ST4": r.get("ST4", 0),
        "ST5": r.get("ST5", 0),
        "cid": d.get("CID"),
        "cmd": cmd,
    }

# ═══════════════════════════════════════════════════════════════════════════════
# Fireplace Commands
# ═══════════════════════════════════════════════════════════════════════════════

def do_on():
    cid = next_cid()
    cmd = "2:0:1:193:203"
    aws_publish({"state": {"desired": {"CID": cid, "CMD_LST": {"CMD_steps": [{"C": cmd, "D": 0.2}]}}}})
    log.info(f"ON: CID={cid}")
    poll_and_publish()
    return {"ok": True, "cid": cid, "cmd": cmd}

def do_off():
    cid = next_cid()
    cmd = "2:0:1:192:203"
    aws_publish({"state": {"desired": {"CID": cid, "CMD_LST": {"CMD_steps": [{"C": cmd, "D": 0.2}]}}}})
    log.info(f"OFF: CID={cid}")
    poll_and_publish()
    return {"ok": True, "cid": cid, "cmd": cmd}

def do_smart(temp):
    cid = next_cid()
    cmd = f"2:2:1:{temp}:193:203"
    aws_publish({"state": {"desired": {"CID": cid, "CMD_LST": {"CMD_steps": [{"C": cmd, "D": 0.2}]}}}})
    log.info(f"SMART {temp}F: CID={cid}")
    poll_and_publish()
    return {"ok": True, "cid": cid, "cmd": cmd, "target_temp": temp}

# ═══════════════════════════════════════════════════════════════════════════════
# HA MQTT Bridge
# ═══════════════════════════════════════════════════════════════════════════════

def setup_ha_mqtt():
    global ha_mqtt
    ha_mqtt = paho_mqtt.Client(paho_mqtt.CallbackAPIVersion.VERSION2, client_id="iflame-bridge")
    ha_mqtt.username_pw_set(HA_MQTT_USER, HA_MQTT_PASS)
    ha_mqtt.will_set(TOPIC_AVAIL, "offline", retain=True)
    ha_mqtt.on_connect = on_ha_connect
    ha_mqtt.on_message = on_ha_message
    ha_mqtt.connect(HA_MQTT_HOST, HA_MQTT_PORT)
    ha_mqtt.loop_start()

def on_ha_connect(client, userdata, flags, rc, properties=None):
    log.info(f"HA MQTT connected rc={rc}")
    client.publish(TOPIC_AVAIL, "online", retain=True)
    client.subscribe(TOPIC_CMD)
    client.subscribe(TOPIC_CLIMATE_MODE_CMD)
    client.subscribe(TOPIC_CLIMATE_TEMP_CMD)
    publish_discovery()

def on_ha_message(client, userdata, msg):
    payload = msg.payload.decode()
    topic = msg.topic
    log.info(f"HA command on {topic}: {payload}")
    try:
        if topic == TOPIC_CMD:
            # Simple switch
            if payload == "ON":
                do_on()
            elif payload == "OFF":
                do_off()
        elif topic == TOPIC_CLIMATE_MODE_CMD:
            # Climate mode: off, heat
            if payload == "off":
                do_off()
            elif payload == "heat":
                do_on()
                log.info("Heat mode: sent simple ON")
        elif topic == TOPIC_CLIMATE_TEMP_CMD:
            # Climate temp set - always do ON first, then SMART
            _temp = int(float(payload))
            log.info(f"Temp command received: {_temp}F")
            shadow = get_shadow()
            at = float(shadow["state"]["reported"]["AT"])
            if _temp <= at:
                _temp = int(at) + 2
                log.info(f"Temp adjusted to {_temp}F (was below ambient {at}F)")
            do_on()
            do_smart(_temp)
    except Exception as e:
        log.error(f"Command failed: {e}")

def publish_discovery():
    dev = {
        "identifiers": ["iflame_rff_10fdc28"],
        "name": "iFlame Fireplace",
        "manufacturer": "iFlame / Girard",
        "model": "RFF-10FDC28",
        "sw_version": "13.00"
    }

    # Switch (simple on/off)
    ha_mqtt.publish("homeassistant/switch/iflame_fireplace/config", json.dumps({
        "name": "Fireplace",
        "unique_id": "iflame_fireplace_switch",
        "command_topic": TOPIC_CMD,
        "state_topic": TOPIC_STATE,
        "value_template": "{{ 'ON' if value_json.is_on else 'OFF' }}",
        "payload_on": "ON",
        "payload_off": "OFF",
        "availability_topic": TOPIC_AVAIL,
        "icon": "mdi:fireplace",
        "device": dev,
    }), retain=True)

    # Climate (smart thermostat)
    ha_mqtt.publish("homeassistant/climate/iflame_thermostat/config", json.dumps({
        "name": "Fireplace Thermostat",
        "unique_id": "iflame_fireplace_thermostat",
        "modes": ["off", "heat"],
        "mode_command_topic": TOPIC_CLIMATE_MODE_CMD,
        "mode_state_topic": TOPIC_CLIMATE_STATE,
        "mode_state_template": "{{ value_json.mode }}",
        "temperature_command_topic": TOPIC_CLIMATE_TEMP_CMD,
        "temperature_state_topic": TOPIC_CLIMATE_STATE,
        "temperature_state_template": "{{ value_json.target_temp }}",
        "current_temperature_topic": TOPIC_CLIMATE_STATE,
        "current_temperature_template": "{{ value_json.current_temp }}",
        "min_temp": 60,
        "max_temp": 83,
        "temp_step": 1,
        "temperature_unit": "F",
        "availability_topic": TOPIC_AVAIL,
        "icon": "mdi:fireplace",
        "device": dev,
    }), retain=True)

    # Ambient temp sensor
    ha_mqtt.publish("homeassistant/sensor/iflame_ambient_temp/config", json.dumps({
        "name": "Fireplace Temperature",
        "unique_id": "iflame_ambient_temp",
        "state_topic": TOPIC_STATE,
        "value_template": "{{ value_json.AT }}",
        "unit_of_measurement": "\u00b0F",
        "device_class": "temperature",
        "state_class": "measurement",
        "availability_topic": TOPIC_AVAIL,
        "device": dev,
    }), retain=True)

    # Mode sensor
    ha_mqtt.publish("homeassistant/sensor/iflame_mode/config", json.dumps({
        "name": "Fireplace Mode",
        "unique_id": "iflame_mode",
        "state_topic": TOPIC_STATE,
        "value_template": "{{ value_json.mode }}",
        "availability_topic": TOPIC_AVAIL,
        "icon": "mdi:fire",
        "device": dev,
    }), retain=True)

    log.info("MQTT discovery published")

def publish_state(state):
    """Publish to both switch state and climate state topics."""
    if not ha_mqtt:
        return
    ha_mqtt.publish(TOPIC_STATE, json.dumps(state), retain=True)

    # Climate state
    climate = {
        "mode": "heat" if state["mode"] == "smart" else ("heat" if state["is_on"] else "off"),
        "target_temp": state["set_temp"],
        "current_temp": state["AT"],
    }
    ha_mqtt.publish(TOPIC_CLIMATE_STATE, json.dumps(climate), retain=True)

def poll_and_publish():
    try:
        shadow = get_shadow()
        state = parse_shadow(shadow)
        publish_state(state)
    except Exception as e:
        log.error(f"Poll failed: {e}")

def poll_loop():
    while True:
        try:
            poll_and_publish()
        except Exception as e:
            log.error(f"Poll loop error: {e}")
        time.sleep(30)

# ═══════════════════════════════════════════════════════════════════════════════
# Flask REST API
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/status")
def status():
    try:
        shadow = get_shadow()
        return jsonify(parse_shadow(shadow))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/on", methods=["POST"])
def turn_on():
    try:
        return jsonify(do_on())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/off", methods=["POST"])
def turn_off():
    try:
        return jsonify(do_off())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/smart", methods=["POST"])
def smart_mode():
    try:
        temp = int(request.json.get("temp", 73))
        return jsonify(do_smart(temp))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    refresh_creds()
    setup_ha_mqtt()
    t = threading.Thread(target=poll_loop, daemon=True)
    t.start()
    log.info("iFlame API + MQTT bridge starting on port 5088")
    app.run(host="0.0.0.0", port=5088)
