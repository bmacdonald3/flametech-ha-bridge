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
TOPIC_FAN_CMD = "fireplace/fan/set"
TOPIC_FLAME_CMD = "fireplace/flame/set"
TOPIC_SPLIT_CMD = "fireplace/split/set"
TOPIC_EMBER_CMD = "fireplace/ember/set"
TOPIC_OVERHEAD_CMD = "fireplace/overhead/set"

app = Flask(__name__)
creds = None
creds_expire = 0
iot_session = None
ha_mqtt = None
_user_target_temp = None
_user_target_time = 0
_startup_grace = 0
_last_known_target = 72
_last_mode_change = 0

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
# Protocol Encoding/Decoding
# ═══════════════════════════════════════════════════════════════════════════════
#
# Command format (simple):  2:0:1:<control_byte>:<fan_flame_byte>
# Command format (smart):   2:2:1:<target_F>:<control_byte>:<fan_flame_byte>
#
# Control byte (4th field in simple, 5th in smart):
#   bit 7:    always 1 (128)
#   bits 4-6: overhead light level (0=off, 1-5)
#   bit 0:    fireplace on/off (1=on, 0=off)
#
# Fan/flame byte (5th field in simple, 6th in smart):
#   bit 7:    split flow (0=front only, 1=front+back)
#   bits 4-6: fan level (0=off, 1-6)
#   bit 3:    ember light (0=off, 1=on)
#   bits 0-2: flame level (0=off, 1-6)

def decode_control_byte(value):
    """Decode the control byte into on/off and overhead light level."""
    value = int(value)
    on = value & 1
    overhead = (value >> 4) & 7
    return {"on": on, "overhead": overhead}

def encode_control_byte(on, overhead):
    """Encode on/off and overhead light level into control byte."""
    return 128 + (overhead * 16) + (1 if on else 0)

def decode_fan_flame_byte(value):
    """Decode the fan/flame byte into split, fan, ember, flame."""
    value = int(value)
    split = (value >> 7) & 1
    fan = (value >> 4) & 7
    ember = (value >> 3) & 1
    flame = value & 7
    return {"split": split, "fan": fan, "ember": ember, "flame": flame}

def encode_fan_flame_byte(split, fan, ember, flame):
    """Encode split, fan, ember, flame into fan/flame byte."""
    return (split * 128) + (fan * 16) + (ember * 8) + flame

def parse_cmd_string(cmd):
    """Parse a command string into all its components."""
    parts = cmd.split(":") if cmd else []
    if len(parts) == 6:
        # Smart mode: 2:2:1:target_F:control_byte:fan_flame_byte
        mode = "smart"
        target_temp = int(parts[3])
        ctrl = decode_control_byte(int(parts[4]))
        ff = decode_fan_flame_byte(int(parts[5]))
    elif len(parts) == 5:
        # Simple mode: 2:0:1:control_byte:fan_flame_byte
        mode = "simple"
        ctrl = decode_control_byte(int(parts[3]))
        ff = decode_fan_flame_byte(int(parts[4]))
        target_temp = 0
    else:
        return {"mode": "unknown", "is_on": False, "target_temp": 0,
                "overhead": 0, "fan": 0, "flame": 0, "ember": 0, "split": 0}

    return {
        "mode": mode,
        "is_on": bool(ctrl["on"]),
        "target_temp": target_temp,
        "overhead": ctrl["overhead"],
        "fan": ff["fan"],
        "flame": ff["flame"],
        "ember": ff["ember"],
        "split": ff["split"],
    }

def build_cmd(mode, is_on, target_temp, overhead, fan, flame, ember, split):
    """Build a complete command string from all parameters."""
    ctrl_byte = encode_control_byte(is_on, overhead)
    ff_byte = encode_fan_flame_byte(split, fan, ember, flame)
    if mode == "smart":
        return f"2:2:1:{target_temp}:{ctrl_byte}:{ff_byte}"
    else:
        return f"2:0:1:{ctrl_byte}:{ff_byte}"

# ═══════════════════════════════════════════════════════════════════════════════
# Shadow → State Parser
# ═══════════════════════════════════════════════════════════════════════════════

def parse_shadow(shadow):
    global _last_known_target
    d = shadow["state"]["desired"]
    r = shadow["state"]["reported"]
    at = float(r["AT"])
    st1 = r.get("ST1", 0)
    cmd = d.get("CMD_LST", {}).get("CMD_steps", [{}])[0].get("C", "")
    parsed = parse_cmd_string(cmd)

    if parsed["mode"] == "smart":
        _last_known_target = parsed["target_temp"]

    # thermostat_active: hub has a target temp and is managing on/off cycling
    thermostat_active = st1 > 0
    # flame_on: the flame is physically burning right now (on-bit in control byte)
    flame_on = parsed["is_on"]
    # is_on: the fireplace system is active (either flame burning or thermostat managing)
    is_on = flame_on or thermostat_active

    return {
        "is_on": is_on,
        "flame_on": flame_on,
        "thermostat_active": thermostat_active,
        "mode": "thermostat" if thermostat_active else ("simple" if flame_on else "off"),
        "AT": round(at, 1),
        "target_temp": parsed["target_temp"],
        "ST1": st1,
        "cid": d.get("CID"),
        "cmd": cmd,
        "fan": parsed["fan"],
        "flame": parsed["flame"],
        "ember": parsed["ember"],
        "split": parsed["split"],
        "overhead": parsed["overhead"],
    }

# ═══════════════════════════════════════════════════════════════════════════════
# Fireplace Commands
# ═══════════════════════════════════════════════════════════════════════════════

def _send_cmd(cmd):
    """Send a command string to the fireplace."""
    cid = next_cid()
    aws_publish({"state": {"desired": {"CID": cid, "CMD_LST": {"CMD_steps": [{"C": cmd, "D": 0.2}]}}}})
    log.info(f"CMD sent: {cmd} CID={cid}")
    return cid

def _get_current_state():
    """Read current state from shadow."""
    shadow = get_shadow()
    d = shadow["state"]["desired"]
    r = shadow["state"]["reported"]
    cmd = d.get("CMD_LST", {}).get("CMD_steps", [{}])[0].get("C", "")
    parsed = parse_cmd_string(cmd)
    parsed["AT"] = float(r["AT"])
    return parsed

def do_on():
    s = _get_current_state()
    cmd = build_cmd("simple", True, 0, s["overhead"], s["fan"], s["flame"], s["ember"], s["split"])
    cid = _send_cmd(cmd)
    poll_and_publish()
    return {"ok": True, "cid": cid, "cmd": cmd}

def do_off():
    s = _get_current_state()
    cmd = build_cmd("simple", False, 0, s["overhead"], s["fan"], s["flame"], s["ember"], s["split"])
    cid = _send_cmd(cmd)
    poll_and_publish()
    return {"ok": True, "cid": cid, "cmd": cmd}

def do_smart(temp):
    try:
        s = _get_current_state()
        ambient = s["AT"]
    except Exception as e:
        log.warning(f"Could not read state for smart decision: {e}")
        ambient = 0
        s = {"overhead": 0, "fan": 0, "flame": 0, "ember": 0, "split": 0}

    if temp > ambient:
        # Step 1: simple ON
        cmd1 = build_cmd("simple", True, 0, s["overhead"], s["fan"], s["flame"], s["ember"], s["split"])
        cid1 = _send_cmd(cmd1)
        log.info(f"SMART step 1 - simple ON: CID={cid1} (target {temp}F > ambient {ambient}F)")
        time.sleep(3)
        # Step 2: smart command
        cmd2 = build_cmd("smart", True, temp, s["overhead"], s["fan"], s["flame"], s["ember"], s["split"])
        cid2 = _send_cmd(cmd2)
        log.info(f"SMART step 2 - target {temp}F: CID={cid2}")
        poll_and_publish()
        return {"ok": True, "cid": cid2, "cmd": cmd2, "target_temp": temp}
    else:
        log.info(f"SMART: target {temp}F <= ambient {ambient}F, sending OFF")
        return do_off()

def do_set_fan(level):
    level = max(0, min(6, int(level)))
    s = _get_current_state()
    cmd = build_cmd(s["mode"], s["is_on"], s["target_temp"], s["overhead"],
                    level, s["flame"], s["ember"], s["split"])
    cid = _send_cmd(cmd)
    log.info(f"FAN set to {level}")
    poll_and_publish()
    return {"ok": True, "cid": cid, "fan": level}

def do_set_flame(level):
    level = max(0, min(6, int(level)))
    s = _get_current_state()
    cmd = build_cmd(s["mode"], s["is_on"], s["target_temp"], s["overhead"],
                    s["fan"], level, s["ember"], s["split"])
    cid = _send_cmd(cmd)
    log.info(f"FLAME set to {level}")
    poll_and_publish()
    return {"ok": True, "cid": cid, "flame": level}

def do_set_split(split_on):
    split = 1 if split_on else 0
    s = _get_current_state()
    cmd = build_cmd(s["mode"], s["is_on"], s["target_temp"], s["overhead"],
                    s["fan"], s["flame"], s["ember"], split)
    cid = _send_cmd(cmd)
    log.info(f"SPLIT set to {'F+B' if split else 'Front'}")
    poll_and_publish()
    return {"ok": True, "cid": cid, "split": split}

def do_set_ember(ember_on):
    ember = 1 if ember_on else 0
    s = _get_current_state()
    cmd = build_cmd(s["mode"], s["is_on"], s["target_temp"], s["overhead"],
                    s["fan"], s["flame"], ember, s["split"])
    cid = _send_cmd(cmd)
    log.info(f"EMBER set to {'ON' if ember else 'OFF'}")
    poll_and_publish()
    return {"ok": True, "cid": cid, "ember": ember}

def do_set_overhead(level):
    level = max(0, min(5, int(level)))
    s = _get_current_state()
    cmd = build_cmd(s["mode"], s["is_on"], s["target_temp"], level,
                    s["fan"], s["flame"], s["ember"], s["split"])
    cid = _send_cmd(cmd)
    log.info(f"OVERHEAD set to {level}")
    poll_and_publish()
    return {"ok": True, "cid": cid, "overhead": level}

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
    global _startup_grace
    log.info(f"HA MQTT connected rc={rc}")
    _startup_grace = time.time() + 5
    client.publish(TOPIC_AVAIL, "online", retain=True)
    client.subscribe(TOPIC_CMD)
    client.subscribe(TOPIC_CLIMATE_MODE_CMD)
    client.subscribe(TOPIC_CLIMATE_TEMP_CMD)
    client.subscribe(TOPIC_FAN_CMD)
    client.subscribe(TOPIC_FLAME_CMD)
    client.subscribe(TOPIC_SPLIT_CMD)
    client.subscribe(TOPIC_EMBER_CMD)
    client.subscribe(TOPIC_OVERHEAD_CMD)
    publish_discovery()

def on_ha_message(client, userdata, msg):
    global _last_known_target, _user_target_temp, _user_target_time, _last_mode_change
    payload = msg.payload.decode()
    topic = msg.topic

    if time.time() < _startup_grace:
        log.info(f"Ignoring retained msg on {topic}: {payload} (startup grace)")
        return

    log.info(f"HA command on {topic}: {payload}")
    try:
        if topic == TOPIC_CMD:
            if payload == "ON":
                do_on()
            elif payload == "OFF":
                do_off()

        elif topic == TOPIC_CLIMATE_MODE_CMD:
            if payload == "off":
                do_off()
                _last_mode_change = time.time()
            elif payload == "heat":
                target = _last_known_target if _last_known_target and _last_known_target > 60 else 72
                try:
                    shadow = get_shadow()
                    ambient = float(shadow["state"]["reported"]["AT"])
                    if target <= ambient:
                        target = int(ambient) + 2
                        log.info(f"Heat mode: target {_last_known_target}F <= ambient {ambient}F, bumped to {target}F")
                except Exception as e:
                    log.warning(f"Could not check ambient for heat mode: {e}")
                    if target <= 72:
                        target = 74
                log.info(f"Heat mode: sending SMART at {target}F")
                do_smart(target)
                _last_mode_change = time.time()

        elif topic == TOPIC_CLIMATE_TEMP_CMD:
            _temp = int(float(payload))
            if (time.time() - _last_mode_change) < 5:
                log.info(f"Ignoring stale temp {_temp}F ({time.time() - _last_mode_change:.1f}s after mode change)")
                return
            _user_target_temp = _temp
            _user_target_time = time.time()
            _last_known_target = _temp
            log.info(f"Temp command received: {_temp}F")
            do_smart(_temp)

        elif topic == TOPIC_FAN_CMD:
            do_set_fan(int(float(payload)))

        elif topic == TOPIC_FLAME_CMD:
            do_set_flame(int(float(payload)))

        elif topic == TOPIC_SPLIT_CMD:
            if payload in ("ON", "on", "1", "true"):
                do_set_split(True)
            else:
                do_set_split(False)

        elif topic == TOPIC_EMBER_CMD:
            if payload in ("ON", "on", "1", "true"):
                do_set_ember(True)
            else:
                do_set_ember(False)

        elif topic == TOPIC_OVERHEAD_CMD:
            do_set_overhead(int(float(payload)))

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

    # Fan level (number 0-6)
    ha_mqtt.publish("homeassistant/number/iflame_fan/config", json.dumps({
        "name": "Fireplace Fan",
        "unique_id": "iflame_fan_level",
        "command_topic": TOPIC_FAN_CMD,
        "state_topic": TOPIC_STATE,
        "value_template": "{{ value_json.fan }}",
        "min": 0,
        "max": 6,
        "step": 1,
        "mode": "slider",
        "icon": "mdi:fan",
        "availability_topic": TOPIC_AVAIL,
        "device": dev,
    }), retain=True)

    # Flame level (number 0-6)
    ha_mqtt.publish("homeassistant/number/iflame_flame/config", json.dumps({
        "name": "Fireplace Flame",
        "unique_id": "iflame_flame_level",
        "command_topic": TOPIC_FLAME_CMD,
        "state_topic": TOPIC_STATE,
        "value_template": "{{ value_json.flame }}",
        "min": 0,
        "max": 6,
        "step": 1,
        "mode": "slider",
        "icon": "mdi:fire",
        "availability_topic": TOPIC_AVAIL,
        "device": dev,
    }), retain=True)

    # Split flow (switch)
    ha_mqtt.publish("homeassistant/switch/iflame_split/config", json.dumps({
        "name": "Fireplace Split Flow",
        "unique_id": "iflame_split_flow",
        "command_topic": TOPIC_SPLIT_CMD,
        "state_topic": TOPIC_STATE,
        "value_template": "{{ 'ON' if value_json.split == 1 else 'OFF' }}",
        "payload_on": "ON",
        "payload_off": "OFF",
        "icon": "mdi:arrow-split-vertical",
        "availability_topic": TOPIC_AVAIL,
        "device": dev,
    }), retain=True)

    # Ember light (switch)
    ha_mqtt.publish("homeassistant/switch/iflame_ember/config", json.dumps({
        "name": "Fireplace Ember Light",
        "unique_id": "iflame_ember_light",
        "command_topic": TOPIC_EMBER_CMD,
        "state_topic": TOPIC_STATE,
        "value_template": "{{ 'ON' if value_json.ember == 1 else 'OFF' }}",
        "payload_on": "ON",
        "payload_off": "OFF",
        "icon": "mdi:fire-circle",
        "availability_topic": TOPIC_AVAIL,
        "device": dev,
    }), retain=True)

    # Overhead lights (number 0-5)
    ha_mqtt.publish("homeassistant/number/iflame_overhead/config", json.dumps({
        "name": "Fireplace Overhead Lights",
        "unique_id": "iflame_overhead_lights",
        "command_topic": TOPIC_OVERHEAD_CMD,
        "state_topic": TOPIC_STATE,
        "value_template": "{{ value_json.overhead }}",
        "min": 0,
        "max": 5,
        "step": 1,
        "mode": "slider",
        "icon": "mdi:ceiling-light",
        "availability_topic": TOPIC_AVAIL,
        "device": dev,
    }), retain=True)

    log.info("MQTT discovery published (all controls)")

def publish_state(state):
    """Publish to both switch state and climate state topics."""
    if not ha_mqtt:
        return
    ha_mqtt.publish(TOPIC_STATE, json.dumps(state), retain=True)

    # Climate state
    st1 = state.get("ST1", 0)
    target = st1 if st1 > 0 else state.get("target_temp", 0)
    if target == 0:
        target = _last_known_target
    user_set_recently = _user_target_temp is not None and (time.time() - _user_target_time) < 30
    if user_set_recently:
        if st1 == _user_target_temp:
            pass
        else:
            target = _user_target_temp
    climate = {
        "mode": "heat" if state.get("thermostat_active") or state.get("flame_on") else "off",
        "target_temp": target,
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

@app.route("/fan", methods=["POST"])
def set_fan():
    try:
        level = int(request.json.get("level", 0))
        return jsonify(do_set_fan(level))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/flame", methods=["POST"])
def set_flame():
    try:
        level = int(request.json.get("level", 0))
        return jsonify(do_set_flame(level))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/split", methods=["POST"])
def set_split():
    try:
        on = request.json.get("on", False)
        return jsonify(do_set_split(on))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/ember", methods=["POST"])
def set_ember():
    try:
        on = request.json.get("on", False)
        return jsonify(do_set_ember(on))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/overhead", methods=["POST"])
def set_overhead():
    try:
        level = int(request.json.get("level", 0))
        return jsonify(do_set_overhead(level))
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
