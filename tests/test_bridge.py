"""Bridge logic against the simulated projector (real TCP), with a fake shadow."""

import time
import uuid

import pytest

from x100_bridge.core import BridgeCore, Config
from x100.inputs import InputMap, default_inputs
from x100.mock import MockServer
from x100.pjlink import PJLink
from x100.projector import Projector
from x100.transport import TcpTransport

from .fakes import FakeShadow


def make_inputs():
    defs = default_inputs()
    for d in defs:
        if d.alexa == "HDMI 3":
            d.code = 0x09
        if d.alexa == "MEDIA PLAYER":
            d.code = 0x1A
    return InputMap(defs)


def make_core(srv, shadow, one_way=False, pjlink=None, **cfg_kw):
    cfg = Config(poll_interval=5, fast_poll_interval=0.1, heartbeat_interval=60, defer_timeout=10,
                 warmup_seconds=0.4, cooldown_seconds=0.3, volume_step_delay=0, one_way=one_way,
                 pending_check_interval=0.05, **cfg_kw)
    proj = Projector(TcpTransport("127.0.0.1", srv.port, timeout=0.5, min_gap=0), one_way=one_way)
    return BridgeCore(cfg, proj, make_inputs(), publish=shadow.update_reported, pjlink=pjlink)


def cmd(*ops, ts=None, cmd_id=None):
    return {"id": cmd_id or str(uuid.uuid4()), "ts": ts if ts is not None else int(time.time() * 1000),
            "ops": [{"op": o, "value": v} for o, v in ops]}


def send(core, shadow, message, limit=3.0):
    """Submit a command and step the core until its ack lands in the shadow."""
    core.submit(message)
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        core.step(timeout=0.02)
        ack = shadow.reported.get("ack", {})
        if ack.get("id") == message["id"]:
            return ack
    raise AssertionError("no ack")


def run_until(core, predicate, limit=5.0):
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        core.step(timeout=0.02)
        if predicate():
            return True
    return False


@pytest.fixture
def srv():
    with MockServer(warmup=0.4, cooldown=0.3, pjlink_port=0) as s:
        yield s


@pytest.fixture
def shadow():
    return FakeShadow()


def test_startup_publishes_state_and_inputs(srv, shadow):
    core = make_core(srv, shadow)
    core.step()
    r = shadow.reported
    assert r["connected"] is True and r["power"] == "OFF"
    assert [i["name"] for i in r["inputs"]] == ["HDMI 1", "HDMI 2", "HDMI 3", "MEDIA PLAYER"]
    assert r["inputs"][-1]["friendlyNames"] == ["Home", "Smart TV"]
    assert r["bridge"]["online"] is True and r["bridge"]["mode"] == "two-way"


def test_power_on_then_off(srv, shadow):
    core = make_core(srv, shadow)
    ack = send(core, shadow, cmd(("power", "ON")))
    assert ack["ok"] is True
    assert shadow.reported["power"] == "WARMING"
    assert run_until(core, lambda: shadow.reported["power"] == "ON")
    ack = send(core, shadow, cmd(("power", "OFF")))
    assert ack["ok"] and shadow.reported["power"] == "COOLING"
    assert run_until(core, lambda: shadow.reported["power"] == "OFF")


def test_input_while_off_powers_on_then_switches(srv, shadow):
    core = make_core(srv, shadow)
    ack = send(core, shadow, cmd(("input", "HDMI 2")))
    assert ack["ok"] and ack.get("deferred") is True
    assert run_until(core, lambda: shadow.reported.get("input") == "HDMI 2" and shadow.reported["power"] == "ON")
    assert srv.state.source == 0x07


def test_input_by_friendly_name(srv, shadow):
    srv.state.turn_on()
    core = make_core(srv, shadow)
    assert run_until(core, lambda: shadow.reported.get("power") == "ON")
    ack = send(core, shadow, cmd(("input", "home")))
    assert ack["ok"] and shadow.reported["input"] == "MEDIA PLAYER"
    assert srv.state.source == 0x1A


def test_ops_queued_during_warmup_run_in_order(srv, shadow):
    core = make_core(srv, shadow)
    send(core, shadow, cmd(("power", "ON")))
    ack = send(core, shadow, cmd(("input", "HDMI 3"), ("volume", 9), ("mute", True)))
    assert ack["ok"] and ack["deferred"]
    assert run_until(core, lambda: shadow.reported.get("muted") is True)
    assert srv.state.source == 0x09 and srv.state.volume == 9 and srv.state.muted is True
    assert [line for line in srv.state.log if line.startswith("W")][-3:] == ["W 13 01 09", "W 13 2A 09", "W 14 00 01"]


def test_volume_and_mute_when_on(srv, shadow):
    srv.state.turn_on()
    core = make_core(srv, shadow)
    assert run_until(core, lambda: shadow.reported.get("power") == "ON")
    ack = send(core, shadow, cmd(("volume", 30)))
    assert ack["ok"] and shadow.reported["volume"] == 30
    ack = send(core, shadow, cmd(("volume_delta", -4)))
    assert ack["ok"] and shadow.reported["volume"] == 26 == srv.state.volume
    ack = send(core, shadow, cmd(("volume_delta", 99)))
    assert shadow.reported["volume"] == 50
    ack = send(core, shadow, cmd(("mute", True)))
    assert ack["ok"] and shadow.reported["muted"] is True


def test_volume_while_off_is_not_in_operation(srv, shadow):
    core = make_core(srv, shadow)
    ack = send(core, shadow, cmd(("volume", 10)))
    assert ack["ok"] is False and ack["code"] == "NOT_IN_OPERATION"


def test_bad_values(srv, shadow):
    core = make_core(srv, shadow)
    assert send(core, shadow, cmd(("input", "HDMI 4")))["code"] == "INVALID_VALUE"  # not learned yet
    assert send(core, shadow, cmd(("volume", 80)))["code"] == "VALUE_OUT_OF_RANGE"
    assert send(core, shadow, cmd(("power", "MAYBE")))["code"] == "INVALID_VALUE"
    assert send(core, shadow, cmd(("teleport", 1)))["code"] == "INVALID_DIRECTIVE"


def test_power_off_cancels_queued_ops(srv, shadow):
    core = make_core(srv, shadow)
    send(core, shadow, cmd(("power", "ON")))
    send(core, shadow, cmd(("input", "HDMI 3")))
    assert core.pending
    # power OFF while warming is itself deferred, then clears the queue once it runs
    send(core, shadow, cmd(("power", "OFF")))
    assert run_until(core, lambda: shadow.reported["power"] in ("COOLING", "OFF") and not core.pending)


def test_duplicate_and_stale_commands_ignored(srv, shadow):
    core = make_core(srv, shadow)
    message = cmd(("power", "ON"))
    send(core, shadow, message)
    shadow.reported.pop("ack")
    core.submit(message)  # QoS1 redelivery
    core.step(timeout=0.05)
    assert "ack" not in shadow.reported
    stale = cmd(("power", "OFF"), ts=int((time.time() - 600) * 1000))
    core.submit(stale)
    core.step(timeout=0.05)
    assert "ack" not in shadow.reported


def test_projector_unreachable(shadow):
    srv = MockServer().start()
    port = srv.port
    srv.stop()
    cfg = Config(poll_interval=5, fast_poll_interval=0.1)
    proj = Projector(TcpTransport("127.0.0.1", port, timeout=0.3, connect_timeout=0.3, min_gap=0))
    core = BridgeCore(cfg, proj, make_inputs(), publish=shadow.update_reported)
    ack = send(core, shadow, cmd(("power", "ON")))
    assert ack["ok"] is False and ack["code"] == "ENDPOINT_UNREACHABLE"
    assert shadow.reported["connected"] is False


def test_one_way_mode_tracks_state_optimistically():
    with MockServer(warmup=0.2, one_way=True) as srv:
        shadow = FakeShadow()
        core = make_core(srv, shadow, one_way=True)
        ack = send(core, shadow, cmd(("power", "ON")))
        assert ack["ok"] and shadow.reported["power"] == "WARMING"
        assert run_until(core, lambda: shadow.reported["power"] == "ON")
        ack = send(core, shadow, cmd(("input", "HDMI 2"), ("volume", 12), ("volume_delta", 2)))
        assert ack["ok"]
        assert run_until(core, lambda: srv.state.volume == 14, limit=3)
        assert srv.state.source == 0x07
        assert shadow.reported["input"] == "HDMI 2" and shadow.reported["volume"] == 14
        assert shadow.reported["bridge"]["mode"] == "one-way"


def test_one_way_with_pjlink_power_status():
    with MockServer(warmup=0.2, one_way=True, pjlink_port=0) as srv:
        shadow = FakeShadow()
        core = make_core(srv, shadow, one_way=True, pjlink=PJLink("127.0.0.1", srv.pjlink_port))
        core.step()
        assert shadow.reported["power"] == "OFF"
        srv.state.turn_on()  # someone used the remote
        assert run_until(core, lambda: shadow.reported["power"] in ("WARMING", "ON"))
