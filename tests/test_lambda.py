"""Lambda tests.

* Unit tests drive the handler against a hand-built shadow.
* End-to-end tests run the real bridge (thread) against the simulated projector
  over TCP, with the Lambda talking to it through a fake IoT data plane, so a
  directive goes all the way: Alexa JSON -> Lambda -> "IoT" -> bridge -> TCP ->
  projector -> ack -> shadow -> Lambda -> Alexa JSON.
* Every response that Amazon's published Smart Home message schema covers is
  validated against it (set ALEXA_SCHEMA=/path/to/alexa_smart_home_message_schema.json,
  or drop the file into tests/data/; see tests/README.md).
"""

import importlib.util
import json
import os
import time
import uuid
from pathlib import Path

import pytest

from x100_bridge.core import BridgeCore, Config
from x100.inputs import InputMap, default_inputs
from x100.mock import MockServer
from x100.projector import Projector
from x100.transport import TcpTransport

from .fakes import FakeIotData, FakeShadow

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------- fixtures


def load_lambda(**env):
    defaults = {"THING_NAME": "x100-projector", "IOT_DATA_ENDPOINT": "example-ats.iot.eu-west-1.amazonaws.com",
                "ACK_TIMEOUT": "3", "STALE_AFTER": "180", "FRIENDLY_NAME": "Projector"}
    defaults.update(env)
    old = {k: os.environ.get(k) for k in defaults}
    os.environ.update(defaults)
    try:
        spec = importlib.util.spec_from_file_location(f"lambda_function_{uuid.uuid4().hex}",
                                                      ROOT / "alexa" / "lambda" / "lambda_function.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _schema_path():
    for candidate in (os.environ.get("ALEXA_SCHEMA"), ROOT / "tests" / "data" / "alexa_smart_home_message_schema.json"):
        if candidate and Path(candidate).exists():
            return Path(candidate)
    return None


@pytest.fixture(scope="session")
def validate():
    path = _schema_path()
    if path is None:
        def skip(_response):
            pytest.skip("Alexa schema not found (see tests/README.md)")
        return skip
    import jsonschema

    schema = json.loads(path.read_text())
    validator = jsonschema.Draft4Validator(schema)

    def check(response):
        errors = sorted(validator.iter_errors(response), key=lambda e: e.path)
        assert not errors, "\n".join(f"{list(e.path)}: {e.message[:200]}" for e in errors[:5])
    return check


def directive(namespace, name, payload=None, endpoint_id="x100-projector", correlation=True):
    d = {"directive": {
        "header": {"namespace": namespace, "name": name, "payloadVersion": "3", "messageId": str(uuid.uuid4())},
        "payload": payload if payload is not None else {}}}
    if correlation:
        d["directive"]["header"]["correlationToken"] = "corr-" + uuid.uuid4().hex
    if endpoint_id is not None:
        d["directive"]["endpoint"] = {"scope": {"type": "BearerToken", "token": "tok"},
                                      "endpointId": endpoint_id, "cookie": {}}
    return d


def discover():
    return directive("Alexa.Discovery", "Discover", {"scope": {"type": "BearerToken", "token": "tok"}},
                     endpoint_id=None, correlation=False)


def props(resp):
    return {(p["namespace"], p["name"]): p["value"] for p in resp["context"]["properties"]}


class Clock:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t


# ---------------------------------------------------------------------------- unit tests


@pytest.fixture
def unit():
    clock = Clock()
    shadow = FakeShadow(clock=clock)
    lf = load_lambda()
    lf._iot = FakeIotData(shadow)
    return lf, shadow, clock


def healthy(shadow, **reported):
    base = {"connected": True, "power": "ON", "input": "HDMI 2", "volume": 22, "muted": False,
            "inputs": [{"name": "HDMI 1"}, {"name": "HDMI 2"},
                       {"name": "MEDIA PLAYER", "friendlyNames": ["Home", "Smart TV"]}],
            "bridge": {"heartbeat": int(time.time()), "online": True}}
    base.update(reported)
    shadow.update_reported(base)


def test_discovery_without_bridge_still_lists_projector(unit, validate):
    lf, _, _ = unit
    resp = lf.lambda_handler(discover(), None)
    validate(resp)
    (ep,) = resp["event"]["payload"]["endpoints"]
    assert ep["friendlyName"] == "Projector" and ep["displayCategories"] == ["TV"]
    ic = next(c for c in ep["capabilities"] if c["interface"] == "Alexa.InputController")
    assert ic["inputs"] == [{"name": "HDMI 1"}, {"name": "HDMI 2"}]


def test_discovery_uses_inputs_and_scenes_from_bridge(unit, validate):
    lf, shadow, _ = unit
    healthy(shadow, scenes=[{"name": "Projector Focus", "key": 0x92}])
    resp = lf.lambda_handler(discover(), None)
    validate(resp)
    eps = resp["event"]["payload"]["endpoints"]
    ic = next(c for c in eps[0]["capabilities"] if c["interface"] == "Alexa.InputController")
    assert [i["name"] for i in ic["inputs"]] == ["HDMI 1", "HDMI 2", "MEDIA PLAYER"]
    assert ic["inputs"][2]["friendlyNames"] == ["Home", "Smart TV"]
    assert eps[1]["friendlyName"] == "Projector Focus" and eps[1]["displayCategories"] == ["SCENE_TRIGGER"]


def test_discovery_drops_invalid_input_names(unit):
    lf, shadow, _ = unit
    healthy(shadow, inputs=[{"name": "HDMI 1"}, {"name": "Laserdisc"}])
    ep = lf.lambda_handler(discover(), None)["event"]["payload"]["endpoints"][0]
    ic = next(c for c in ep["capabilities"] if c["interface"] == "Alexa.InputController")
    assert ic["inputs"] == [{"name": "HDMI 1"}]


def test_report_state(unit, validate):
    lf, shadow, _ = unit
    healthy(shadow)
    resp = lf.lambda_handler(directive("Alexa", "ReportState"), None)
    validate(resp)
    assert resp["event"]["header"]["name"] == "StateReport"
    p = props(resp)
    assert p[("Alexa.PowerController", "powerState")] == "ON"
    assert p[("Alexa.InputController", "input")] == "HDMI 2"
    assert p[("Alexa.Speaker", "volume")] == 22
    assert p[("Alexa.EndpointHealth", "connectivity")] == {"value": "OK"}


def test_warming_reports_on_and_unreachable_projector_reports_unreachable(unit, validate):
    lf, shadow, _ = unit
    healthy(shadow, power="WARMING", connected=False)
    resp = lf.lambda_handler(directive("Alexa", "ReportState"), None)
    validate(resp)
    p = props(resp)
    assert p[("Alexa.PowerController", "powerState")] == "ON"
    assert p[("Alexa.EndpointHealth", "connectivity")] == {"value": "UNREACHABLE"}


def test_stale_bridge_is_bridge_unreachable(unit, validate):
    lf, shadow, clock = unit
    healthy(shadow)
    clock.t += 1000  # no heartbeat for ~17 minutes
    resp = lf.lambda_handler(directive("Alexa", "ReportState"), None)
    validate(resp)
    assert resp["event"]["payload"]["type"] == "BRIDGE_UNREACHABLE"
    resp = lf.lambda_handler(directive("Alexa.PowerController", "TurnOn"), None)
    validate(resp)
    assert resp["event"]["payload"]["type"] == "BRIDGE_UNREACHABLE"
    assert lf._iot.published == []  # didn't bother sending


def test_bridge_said_goodbye(unit):
    lf, shadow, _ = unit
    healthy(shadow)
    shadow.update_reported({"bridge": {"heartbeat": int(time.time()), "online": False}})
    resp = lf.lambda_handler(directive("Alexa.PowerController", "TurnOn"), None)
    assert resp["event"]["payload"]["type"] == "BRIDGE_UNREACHABLE"


def test_no_ack_times_out_as_unreachable(validate):
    shadow = FakeShadow()
    lf = load_lambda(ACK_TIMEOUT="0.5")
    lf._iot = FakeIotData(shadow)
    healthy(shadow)
    t0 = time.monotonic()
    resp = lf.lambda_handler(directive("Alexa.PowerController", "TurnOff"), None)
    validate(resp)
    assert resp["event"]["payload"]["type"] == "ENDPOINT_UNREACHABLE"
    assert time.monotonic() - t0 < 1.5
    (msg,) = lf._iot.published
    assert msg["topic"] == "x100/x100-projector/cmd" and msg["qos"] == 1
    assert msg["payload"]["ops"] == [{"op": "power", "value": "OFF"}]


def test_unknown_endpoint_and_directive(unit, validate):
    lf, shadow, _ = unit
    healthy(shadow)
    resp = lf.lambda_handler(directive("Alexa.PowerController", "TurnOn", endpoint_id="toaster"), None)
    validate(resp)
    assert resp["event"]["payload"]["type"] == "NO_SUCH_ENDPOINT"
    resp = lf.lambda_handler(directive("Alexa.ThermostatController", "SetTargetTemperature"), None)
    validate(resp)
    assert resp["event"]["payload"]["type"] == "INVALID_DIRECTIVE"
    resp = lf.lambda_handler({"directive": {"header": {"payloadVersion": "2"}}}, None)
    assert resp["event"]["payload"]["type"] == "INVALID_DIRECTIVE"


def test_unknown_input_rejected_before_sending(unit, validate):
    lf, shadow, _ = unit
    healthy(shadow)
    resp = lf.lambda_handler(directive("Alexa.InputController", "SelectInput", {"input": "HDMI 4"}), None)
    validate(resp)
    assert resp["event"]["payload"]["type"] == "INVALID_VALUE"
    assert lf._iot.published == []


def test_accept_grant(unit):
    lf, _, _ = unit
    d = {"directive": {"header": {"namespace": "Alexa.Authorization", "name": "AcceptGrant",
                                  "payloadVersion": "3", "messageId": "m"},
                       "payload": {"grant": {"type": "OAuth2.AuthorizationCode", "code": "c"},
                                   "grantee": {"type": "BearerToken", "token": "t"}}}}
    resp = lf.lambda_handler(d, None)
    assert resp["event"]["header"]["name"] == "AcceptGrant.Response"


def test_allowed_user_id(monkeypatch):
    shadow = FakeShadow()
    healthy(shadow)
    lf = load_lambda(ALLOWED_USER_ID="amzn1.account.ME")
    lf._iot = FakeIotData(shadow)
    monkeypatch.setattr(lf, "lwa_user_id", lambda token: "amzn1.account.ME" if token == "mine" else "amzn1.account.X")
    ok = directive("Alexa", "ReportState")
    ok["directive"]["endpoint"]["scope"]["token"] = "mine"
    assert lf.lambda_handler(ok, None)["event"]["header"]["name"] == "StateReport"
    bad = directive("Alexa", "ReportState")
    bad["directive"]["endpoint"]["scope"]["token"] = "someone-else"
    assert lf.lambda_handler(bad, None)["event"]["payload"]["type"] == "INVALID_AUTHORIZATION_CREDENTIAL"


# ---------------------------------------------------------------------------- end to end


@pytest.fixture
def e2e():
    """Alexa JSON -> Lambda -> fake IoT -> real bridge thread -> TCP -> simulated projector."""
    srv = MockServer(warmup=0.6, cooldown=0.4).start()
    shadow = FakeShadow()
    defs = default_inputs()
    for d in defs:
        if d.alexa == "MEDIA PLAYER":
            d.code = 0x1A
    cfg = Config(poll_interval=5, fast_poll_interval=0.1, heartbeat_interval=30, volume_step_delay=0,
                 pending_check_interval=0.05, scenes={"Projector Focus": 0x92})
    proj = Projector(TcpTransport("127.0.0.1", srv.port, timeout=0.5, min_gap=0))
    core = BridgeCore(cfg, proj, InputMap(defs), publish=shadow.update_reported)
    core.start()
    lf = load_lambda(ACK_TIMEOUT="4")
    lf._iot = FakeIotData(shadow, on_publish=lambda topic, payload: core.submit(payload))
    deadline = time.monotonic() + 3
    while "bridge" not in shadow.reported and time.monotonic() < deadline:
        time.sleep(0.02)
    yield lf, srv, shadow
    core.stop()
    proj.close()
    srv.stop()


def wait(predicate, limit=5.0):
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_e2e_voice_session(e2e, validate):
    lf, srv, shadow = e2e
    say = lambda ns, name, payload=None: lf.lambda_handler(directive(ns, name, payload), None)  # noqa: E731

    # "Alexa, discover devices"
    resp = lf.lambda_handler(discover(), None)
    validate(resp)
    eps = resp["event"]["payload"]["endpoints"]
    assert [e["friendlyName"] for e in eps] == ["Projector", "Projector Focus"]

    # "Alexa, switch the projector to Home" while it's off: powers on, then switches
    resp = say("Alexa.InputController", "SelectInput", {"input": "Home"})
    validate(resp)
    assert resp["event"]["header"]["name"] == "Response"
    assert props(resp)[("Alexa.PowerController", "powerState")] == "ON"
    assert props(resp)[("Alexa.InputController", "input")] == "MEDIA PLAYER"  # what was asked, not the stale one
    assert wait(lambda: srv.state.source == 0x1A and srv.state.power.value == "ON")

    # "Alexa, set the projector volume to 30"
    resp = say("Alexa.Speaker", "SetVolume", {"volume": 30})
    validate(resp)
    assert props(resp)[("Alexa.Speaker", "volume")] == 30 == srv.state.volume

    # "Alexa, turn the projector up" (default step) and "...down by 10"
    resp = say("Alexa.Speaker", "AdjustVolume", {"volume": 10, "volumeDefault": True})
    validate(resp)
    assert props(resp)[("Alexa.Speaker", "volume")] == 33
    resp = say("Alexa.Speaker", "AdjustVolume", {"volume": -10, "volumeDefault": False})
    assert props(resp)[("Alexa.Speaker", "volume")] == 23 == srv.state.volume

    # "Alexa, set the projector volume to 80" -> clamped to the projector's max
    resp = say("Alexa.Speaker", "SetVolume", {"volume": 80})
    assert props(resp)[("Alexa.Speaker", "volume")] == 50

    # "Alexa, mute the projector"
    resp = say("Alexa.Speaker", "SetMute", {"mute": True})
    validate(resp)
    assert props(resp)[("Alexa.Speaker", "muted")] is True and srv.state.muted is True

    # "Alexa, change the projector to HDMI 2"
    resp = say("Alexa.InputController", "SelectInput", {"input": "HDMI 2"})
    validate(resp)
    assert props(resp)[("Alexa.InputController", "input")] == "HDMI 2" and srv.state.source == 0x07

    # Alexa app opens the device card
    resp = say("Alexa", "ReportState")
    validate(resp)
    p = props(resp)
    assert p[("Alexa.InputController", "input")] == "HDMI 2"
    assert p[("Alexa.EndpointHealth", "connectivity")] == {"value": "OK"}

    # "Alexa, turn on Projector Focus" (scene -> remote key)
    focus = directive("Alexa.SceneController", "Activate", endpoint_id=eps[1]["endpointId"])
    resp = lf.lambda_handler(focus, None)
    validate(resp)
    assert resp["event"]["header"]["name"] == "ActivationStarted"
    assert srv.state.keys[-1] == 0x92

    # "Alexa, turn off the projector"
    resp = say("Alexa.PowerController", "TurnOff")
    validate(resp)
    assert props(resp)[("Alexa.PowerController", "powerState")] == "OFF"
    assert wait(lambda: srv.state.power.value == "OFF")

    # "Alexa, mute the projector" while it's off -> honest error
    resp = say("Alexa.Speaker", "SetMute", {"mute": True})
    validate(resp)
    assert resp["event"]["payload"]["type"] == "NOT_IN_OPERATION"


def test_e2e_latency(e2e):
    lf, srv, shadow = e2e
    t0 = time.monotonic()
    resp = lf.lambda_handler(directive("Alexa.PowerController", "TurnOn"), None)
    elapsed = time.monotonic() - t0
    assert resp["event"]["header"]["name"] == "Response"
    assert elapsed < 2.0, f"took {elapsed:.2f}s"


def test_e2e_projector_unplugged(e2e, validate):
    lf, srv, shadow = e2e
    srv.stop()  # projector loses power / cable pulled
    resp = lf.lambda_handler(directive("Alexa.PowerController", "TurnOn"), None)
    validate(resp)
    assert resp["event"]["payload"]["type"] == "ENDPOINT_UNREACHABLE"
    assert wait(lambda: shadow.reported["connected"] is False)
    resp = lf.lambda_handler(directive("Alexa", "ReportState"), None)
    assert props(resp)[("Alexa.EndpointHealth", "connectivity")] == {"value": "UNREACHABLE"}
