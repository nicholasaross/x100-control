"""Alexa Smart Home add-on backend for the ViewSonic X100-4K bridge.

    Echo -> Alexa -> this Lambda -> AWS IoT Core (x100/<thing>/cmd) -> bridge on your LAN -> projector

The bridge acknowledges every command in the thing's device shadow; this
function waits for that ack (up to ACK_TIMEOUT seconds) so Alexa reports real
failures instead of a cheerful "OK" when the projector is unplugged.

Uses only the standard library and boto3 (included in the Lambda runtime).

Environment
    THING_NAME          IoT thing / shadow name            (x100-projector)
    IOT_DATA_ENDPOINT   xxxx-ats.iot.<region>.amazonaws.com (required)
    FRIENDLY_NAME       name Alexa uses                    (Projector)
    ACK_TIMEOUT         seconds to wait for the bridge     (6.0)
    STALE_AFTER         heartbeat age that means offline   (180)
    VOLUME_STEP         "turn it up" step on the 0-50 scale (3)
    ALLOWED_USER_ID     optional: only this Amazon user id (amzn1.account...) may use the skill
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger()
log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

THING_NAME = os.environ.get("THING_NAME", "x100-projector")
IOT_DATA_ENDPOINT = os.environ.get("IOT_DATA_ENDPOINT", "")
ENDPOINT_ID = os.environ.get("ENDPOINT_ID", "x100-projector")
FRIENDLY_NAME = os.environ.get("FRIENDLY_NAME", "Projector")
ACK_TIMEOUT = float(os.environ.get("ACK_TIMEOUT", "6.0"))
STALE_AFTER = float(os.environ.get("STALE_AFTER", "180"))
VOLUME_STEP = int(os.environ.get("VOLUME_STEP", "3"))
MAX_VOLUME = int(os.environ.get("MAX_VOLUME", "50"))
ALLOWED_USER_ID = os.environ.get("ALLOWED_USER_ID", "").strip()

CMD_TOPIC = f"x100/{THING_NAME}/cmd"
SCENE_PREFIX = "x100-scene-"
DEFAULT_INPUTS = [{"name": "HDMI 1"}, {"name": "HDMI 2"}]
VALID_INPUTS = set(
    [f"AUX {i}" for i in range(1, 8)] + [f"HDMI {i}" for i in range(1, 11)] + [f"INPUT {i}" for i in range(1, 11)]
    + [f"LINE {i}" for i in range(1, 8)]
    + ["BLURAY", "CABLE", "CD", "COAX 1", "COAX 2", "COMPOSITE 1", "DVD", "GAME", "HD RADIO", "HDMI ARC", "IPOD",
       "MEDIA PLAYER", "OPTICAL 1", "OPTICAL 2", "PHONO", "PLAYSTATION", "PLAYSTATION 3", "PLAYSTATION 4",
       "SATELLITE", "SMARTCAST", "TUNER", "TV", "USB DAC", "VIDEO 1", "VIDEO 2", "VIDEO 3", "XBOX"]
)
ERROR_TYPES = {"ENDPOINT_UNREACHABLE", "BRIDGE_UNREACHABLE", "NOT_IN_OPERATION", "VALUE_OUT_OF_RANGE",
               "INVALID_VALUE", "INVALID_DIRECTIVE", "INTERNAL_ERROR", "NO_SUCH_ENDPOINT", "ENDPOINT_BUSY",
               "INVALID_AUTHORIZATION_CREDENTIAL"}

_iot = None
_user_cache: Dict[str, tuple] = {}


def iot():
    """boto3 iot-data client (tests replace this)."""
    global _iot
    if _iot is None:
        import boto3  # available in the Lambda runtime

        if not IOT_DATA_ENDPOINT:
            raise RuntimeError("IOT_DATA_ENDPOINT is not set")
        _iot = boto3.client("iot-data", endpoint_url=f"https://{IOT_DATA_ENDPOINT}")
    return _iot


class AlexaError(Exception):
    def __init__(self, error_type: str, message: str, extra: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.type = error_type if error_type in ERROR_TYPES else "INTERNAL_ERROR"
        self.message = message[:500] or self.type
        self.extra = extra or {}


# ---------------------------------------------------------------------------
# Shadow access
# ---------------------------------------------------------------------------


class Shadow:
    def __init__(self, doc: Dict[str, Any]):
        self.reported: Dict[str, Any] = doc.get("state", {}).get("reported", {}) or {}
        self.meta: Dict[str, Any] = doc.get("metadata", {}).get("reported", {}) or {}
        self.now = float(doc.get("timestamp") or time.time())

    def ts(self, *path: str) -> Optional[float]:
        node: Any = self.meta
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return None
            node = node[key]
        if isinstance(node, dict) and "timestamp" in node:
            return float(node["timestamp"])
        if isinstance(node, list) and node and isinstance(node[0], dict) and "timestamp" in node[0]:
            return float(node[0]["timestamp"])
        return None

    def bridge_online(self) -> bool:
        bridge = self.reported.get("bridge") or {}
        heartbeat = self.ts("bridge", "heartbeat")
        if bridge.get("online") is False or heartbeat is None:
            return False
        return (self.now - heartbeat) <= STALE_AFTER

    def inputs(self) -> List[Dict[str, Any]]:
        raw = self.reported.get("inputs")
        if not isinstance(raw, list):
            return list(DEFAULT_INPUTS)
        out = []
        for item in raw:
            if isinstance(item, dict) and item.get("name") in VALID_INPUTS:
                entry: Dict[str, Any] = {"name": item["name"]}
                names = [n for n in item.get("friendlyNames", []) if isinstance(n, str) and n.strip()]
                if names:
                    entry["friendlyNames"] = names
                out.append(entry)
        return out

    def scenes(self) -> List[Dict[str, Any]]:
        raw = self.reported.get("scenes")
        if not isinstance(raw, list):
            return []
        return [s for s in raw if isinstance(s, dict) and isinstance(s.get("name"), str)
                and isinstance(s.get("key"), int)]


def read_shadow() -> Optional[Shadow]:
    client = iot()
    try:
        resp = client.get_thing_shadow(thingName=THING_NAME)
    except client.exceptions.ResourceNotFoundException:
        return None
    return Shadow(json.loads(resp["payload"].read()))


def send_command(ops: List[Dict[str, Any]]) -> Shadow:
    """Publish ops to the bridge and wait for its ack in the shadow.

    If the bridge queued the ops (projector still warming up), the returned
    shadow shows the values that were requested, which is what Alexa should
    tell the user and display.
    """
    shadow = read_shadow()
    if shadow is None or not shadow.bridge_online():
        raise AlexaError("BRIDGE_UNREACHABLE", "The projector bridge is offline.")
    cmd_id = str(uuid.uuid4())
    message = {"id": cmd_id, "ts": int(time.time() * 1000), "ops": ops}
    iot().publish(topic=CMD_TOPIC, qos=1, payload=json.dumps(message).encode())
    log.info("sent %s %s", cmd_id, ops)
    deadline = time.monotonic() + ACK_TIMEOUT
    delay = 0.1
    while time.monotonic() < deadline:
        time.sleep(min(delay, max(0.0, deadline - time.monotonic())))
        delay = min(delay * 1.5, 0.4)
        shadow = read_shadow()
        ack = (shadow.reported.get("ack") or {}) if shadow else {}
        if ack.get("id") == cmd_id:
            if ack.get("ok"):
                assert shadow is not None
                if ack.get("deferred"):
                    apply_expected(shadow, ops)
                return shadow
            raise AlexaError(str(ack.get("code") or "INTERNAL_ERROR"), str(ack.get("message") or ""))
    raise AlexaError("ENDPOINT_UNREACHABLE", "The projector bridge didn't confirm the command in time.")


def apply_expected(shadow: Shadow, ops: List[Dict[str, Any]]) -> None:
    r = shadow.reported
    for op in ops:
        kind, value = op.get("op"), op.get("value")
        if kind == "input":
            r["input"] = value
            if r.get("power") in ("OFF", "COOLING", None):
                r["power"] = "WARMING"
        elif kind == "volume":
            r["volume"] = value
        elif kind == "mute":
            r["muted"] = value
        elif kind == "power":
            r["power"] = "WARMING" if value == "ON" else "COOLING"


# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------


def iso(ts: Optional[float]) -> str:
    dt = datetime.fromtimestamp(ts if ts is not None else time.time(), tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def prop(namespace: str, name: str, value: Any, ts: Optional[float], now: float) -> Dict[str, Any]:
    age_ms = int(max(0.0, now - ts) * 1000) if ts is not None else 60000
    return {"namespace": namespace, "name": name, "value": value, "timeOfSample": iso(ts),
            "uncertaintyInMilliseconds": min(age_ms, 3_600_000)}


def properties(shadow: Shadow) -> List[Dict[str, Any]]:
    r, now = shadow.reported, shadow.now
    props: List[Dict[str, Any]] = []
    power = r.get("power")
    if power in ("ON", "WARMING", "OFF", "COOLING"):
        props.append(prop("Alexa.PowerController", "powerState", "ON" if power in ("ON", "WARMING") else "OFF",
                          shadow.ts("power"), now))
    if isinstance(r.get("input"), str) and r["input"] in VALID_INPUTS:
        props.append(prop("Alexa.InputController", "input", r["input"], shadow.ts("input"), now))
    if isinstance(r.get("volume"), int) and not isinstance(r.get("volume"), bool):
        props.append(prop("Alexa.Speaker", "volume", max(0, min(100, r["volume"])), shadow.ts("volume"), now))
    if isinstance(r.get("muted"), bool):
        props.append(prop("Alexa.Speaker", "muted", r["muted"], shadow.ts("muted"), now))
    reachable = bool(r.get("connected")) and shadow.bridge_online()
    props.append(prop("Alexa.EndpointHealth", "connectivity", {"value": "OK" if reachable else "UNREACHABLE"},
                      shadow.ts("connected"), now))
    return props


def header(namespace: str, name: str, directive_header: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    h = {"namespace": namespace, "name": name, "payloadVersion": "3", "messageId": str(uuid.uuid4())}
    token = (directive_header or {}).get("correlationToken")
    if token:
        h["correlationToken"] = token
    return h


def response_endpoint(directive: Dict[str, Any]) -> Dict[str, Any]:
    ep = directive.get("endpoint", {})
    out: Dict[str, Any] = {"endpointId": ep.get("endpointId", ENDPOINT_ID)}
    if isinstance(ep.get("scope"), dict):
        out["scope"] = ep["scope"]
    return out


def error_response(directive: Dict[str, Any], err: AlexaError) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"type": err.type, "message": err.message}
    payload.update(err.extra)
    event: Dict[str, Any] = {"header": header("Alexa", "ErrorResponse", directive.get("header")), "payload": payload}
    if "endpoint" in directive:
        event["endpoint"] = response_endpoint(directive)
    return {"event": event}


def control_response(directive: Dict[str, Any], shadow: Shadow) -> Dict[str, Any]:
    return {
        "event": {"header": header("Alexa", "Response", directive.get("header")),
                  "endpoint": response_endpoint(directive), "payload": {}},
        "context": {"properties": properties(shadow)},
    }


def projector_endpoint(shadow: Optional[Shadow]) -> Dict[str, Any]:
    def iface(name: str, props: Optional[List[str]] = None) -> Dict[str, Any]:
        cap: Dict[str, Any] = {"type": "AlexaInterface", "interface": name, "version": "3"}
        if props:
            cap["properties"] = {"supported": [{"name": p} for p in props],
                                 "proactivelyReported": False, "retrievable": True}
        return cap

    inputs = shadow.inputs() if shadow else list(DEFAULT_INPUTS)
    capabilities = [iface("Alexa"), iface("Alexa.PowerController", ["powerState"])]
    if inputs:
        ic = iface("Alexa.InputController", ["input"])
        ic["inputs"] = inputs
        capabilities.append(ic)
    capabilities += [iface("Alexa.Speaker", ["volume", "muted"]),
                     iface("Alexa.EndpointHealth", ["connectivity"])]
    return {
        "endpointId": ENDPOINT_ID,
        "manufacturerName": "ViewSonic",
        "description": "ViewSonic X100-4K projector via x100-bridge",
        "friendlyName": FRIENDLY_NAME,
        "displayCategories": ["TV"],
        "additionalAttributes": {"manufacturer": "ViewSonic", "model": "X100-4K", "customIdentifier": THING_NAME},
        "cookie": {},
        "capabilities": capabilities,
    }


def scene_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "scene"
    return SCENE_PREFIX + slug + "-" + hashlib.sha1(name.encode()).hexdigest()[:6]


def scene_endpoint(scene: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "endpointId": scene_id(scene["name"]),
        "manufacturerName": "ViewSonic",
        "description": f"Presses remote key 0x{scene['key']:02X} on the X100-4K",
        "friendlyName": scene["name"],
        "displayCategories": ["SCENE_TRIGGER"],
        "cookie": {"key": str(scene["key"])},
        "capabilities": [
            {"type": "AlexaInterface", "interface": "Alexa", "version": "3"},
            {"type": "AlexaInterface", "interface": "Alexa.SceneController", "version": "3",
             "supportsDeactivation": False},
        ],
    }


# ---------------------------------------------------------------------------
# Directive handlers
# ---------------------------------------------------------------------------


def handle_discovery(directive: Dict[str, Any]) -> Dict[str, Any]:
    try:
        shadow = read_shadow()
    except Exception as exc:  # discovery should still succeed if AWS hiccups
        log.warning("shadow read failed during discovery: %s", exc)
        shadow = None
    endpoints = [projector_endpoint(shadow)]
    if shadow:
        endpoints += [scene_endpoint(s) for s in shadow.scenes()]
    return {"event": {"header": header("Alexa.Discovery", "Discover.Response"),
                      "payload": {"endpoints": endpoints}}}


def handle_report_state(directive: Dict[str, Any]) -> Dict[str, Any]:
    shadow = read_shadow()
    if shadow is None or not shadow.bridge_online():
        raise AlexaError("BRIDGE_UNREACHABLE", "The projector bridge is offline.")
    return {
        "event": {"header": header("Alexa", "StateReport", directive.get("header")),
                  "endpoint": response_endpoint(directive), "payload": {}},
        "context": {"properties": properties(shadow)},
    }


def resolve_input(requested: str, shadow: Optional[Shadow]) -> str:
    want = " ".join(str(requested).upper().split())
    for item in (shadow.inputs() if shadow else DEFAULT_INPUTS):
        if want == item["name"] or want in (" ".join(n.upper().split()) for n in item.get("friendlyNames", [])):
            return item["name"]
    raise AlexaError("INVALID_VALUE", f"The projector doesn't have an input called {requested}.")


def handle_control(directive: Dict[str, Any]) -> Dict[str, Any]:
    ns, name = directive["header"]["namespace"], directive["header"]["name"]
    payload = directive.get("payload") or {}
    if ns == "Alexa.PowerController" and name in ("TurnOn", "TurnOff"):
        ops = [{"op": "power", "value": "ON" if name == "TurnOn" else "OFF"}]
    elif ns == "Alexa.InputController" and name == "SelectInput":
        ops = [{"op": "input", "value": resolve_input(payload.get("input", ""), read_shadow())}]
    elif ns == "Alexa.Speaker" and name == "SetVolume":
        ops = [{"op": "volume", "value": max(0, min(MAX_VOLUME, int(payload.get("volume", 0))))}]
    elif ns == "Alexa.Speaker" and name == "AdjustVolume":
        delta = int(payload.get("volume", 0))
        if payload.get("volumeDefault"):
            delta = VOLUME_STEP if delta >= 0 else -VOLUME_STEP
        ops = [{"op": "volume_delta", "value": delta}]
    elif ns == "Alexa.Speaker" and name == "SetMute":
        ops = [{"op": "mute", "value": bool(payload.get("mute"))}]
    else:
        raise AlexaError("INVALID_DIRECTIVE", f"{ns}.{name} isn't supported.")
    return control_response(directive, send_command(ops))


def handle_scene(directive: Dict[str, Any]) -> Dict[str, Any]:
    if directive["header"]["name"] != "Activate":
        raise AlexaError("INVALID_DIRECTIVE", "Scenes can only be activated.")
    endpoint_id = directive["endpoint"]["endpointId"]
    shadow = read_shadow()
    scene = next((s for s in (shadow.scenes() if shadow else []) if scene_id(s["name"]) == endpoint_id), None)
    if scene is None:
        raise AlexaError("NO_SUCH_ENDPOINT", "That scene no longer exists; ask Alexa to discover devices.")
    send_command([{"op": "key", "value": scene["key"]}])
    return {
        "context": {},
        "event": {"header": header("Alexa.SceneController", "ActivationStarted", directive.get("header")),
                  "endpoint": response_endpoint(directive),
                  "payload": {"cause": {"type": "VOICE_INTERACTION"}, "timestamp": iso(time.time())}},
    }


# ---------------------------------------------------------------------------
# Optional caller check
# ---------------------------------------------------------------------------


def bearer_token(directive: Dict[str, Any]) -> Optional[str]:
    for scope in ((directive.get("endpoint") or {}).get("scope"), (directive.get("payload") or {}).get("scope"),
                  (directive.get("payload") or {}).get("grantee")):
        if isinstance(scope, dict) and scope.get("token"):
            return scope["token"]
    return None


def lwa_user_id(token: str) -> str:
    key = hashlib.sha256(token.encode()).hexdigest()
    cached = _user_cache.get(key)
    if cached and cached[1] > time.time():
        return cached[0]
    req = urllib.request.Request("https://api.amazon.com/user/profile",
                                 headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=3) as resp:
        user_id = json.loads(resp.read()).get("user_id", "")
    _user_cache[key] = (user_id, time.time() + 3600)
    return user_id


def check_caller(directive: Dict[str, Any]) -> None:
    if not ALLOWED_USER_ID:
        return
    token = bearer_token(directive)
    if not token:
        raise AlexaError("INVALID_AUTHORIZATION_CREDENTIAL", "Missing access token.")
    try:
        user_id = lwa_user_id(token)
    except Exception as exc:
        raise AlexaError("INVALID_AUTHORIZATION_CREDENTIAL", f"Couldn't verify the caller: {exc}") from exc
    if user_id != ALLOWED_USER_ID:
        log.warning("rejected Amazon user %s (ALLOWED_USER_ID is %s)", user_id, ALLOWED_USER_ID)
        raise AlexaError("INVALID_AUTHORIZATION_CREDENTIAL", "This account isn't allowed to use this skill.")


# ---------------------------------------------------------------------------


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    directive = event.get("directive") or {}
    hdr = directive.get("header") or {}
    ns, name = hdr.get("namespace"), hdr.get("name")
    log.info("directive %s.%s endpoint=%s", ns, name, (directive.get("endpoint") or {}).get("endpointId"))
    started = time.monotonic()
    try:
        if not directive or hdr.get("payloadVersion") != "3":
            raise AlexaError("INVALID_DIRECTIVE", "Only Smart Home API v3 directives are supported.")
        check_caller(directive)
        if ns == "Alexa.Authorization" and name == "AcceptGrant":
            # We don't send proactive events, so there's no grant to store.
            result = {"event": {"header": header("Alexa.Authorization", "AcceptGrant.Response"), "payload": {}}}
        elif ns == "Alexa.Discovery" and name == "Discover":
            result = handle_discovery(directive)
        else:
            endpoint_id = (directive.get("endpoint") or {}).get("endpointId")
            if endpoint_id and endpoint_id.startswith(SCENE_PREFIX) and ns == "Alexa.SceneController":
                result = handle_scene(directive)
            elif endpoint_id != ENDPOINT_ID:
                raise AlexaError("NO_SUCH_ENDPOINT", f"Unknown endpoint {endpoint_id}.")
            elif ns == "Alexa" and name == "ReportState":
                result = handle_report_state(directive)
            else:
                result = handle_control(directive)
    except AlexaError as err:
        log.warning("%s.%s -> %s: %s", ns, name, err.type, err.message)
        result = error_response(directive, err)
    except Exception as exc:  # never leave Alexa without an answer
        log.exception("unhandled error")
        result = error_response(directive, AlexaError("INTERNAL_ERROR", str(exc)))
    log.info("%s.%s answered %s in %.0f ms", ns, name, result["event"]["header"]["name"],
             (time.monotonic() - started) * 1000)
    return result
