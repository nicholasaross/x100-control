"""The bridge's local HTTP API, against the real bridge core and the simulated projector (real TCP)."""

import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import x100
from x100.mock import MockServer
from x100.projector import Projector
from x100.protocol import PowerState
from x100.transport import TcpTransport
from x100_bridge.core import BridgeCore, Config
from x100_bridge.local_api import MAX_BODY, ApiConfigError, LocalApi, check_settings, parse_bind
from x100_bridge.main import check_config
from x100_listener.client import BridgeClient

from .test_bridge import make_inputs

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "0123456789abcdef0123"


def call(api, path, method="GET", body=None, token=TOKEN, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    headers = {"Content-Type": "application/json"} if data is not None else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(api.url + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read()), resp.headers
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}"), exc.headers


def command(api, *ops, token=TOKEN):
    return call(api, "/command", "POST", {"ops": [{"op": o, "value": v} for o, v in ops]}, token=token)


def make_core(srv, **kw):
    cfg = Config(poll_interval=5, fast_poll_interval=0.1, heartbeat_interval=60, defer_timeout=10,
                 volume_step_delay=0, pending_check_interval=0.05, **kw)
    proj = Projector(TcpTransport("127.0.0.1", srv.port, timeout=0.5, min_gap=0))
    return BridgeCore(cfg, proj, make_inputs(), publish=lambda doc: None)


def wait_for(predicate, limit=5.0):
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def srv():
    with MockServer(warmup=0.6, cooldown=0.3) as s:
        yield s


@pytest.fixture
def bridge(srv):
    core = make_core(srv)
    core.start()
    api = LocalApi(core, "127.0.0.1:0", TOKEN, timeout=8).start()
    yield core, api
    api.stop()
    core.stop()


# ---------------------------------------------------------------------------- settings


def test_bind_addresses_parse():
    assert parse_bind("0.0.0.0:8661") == ("0.0.0.0", 8661)
    assert parse_bind(":9000") == ("0.0.0.0", 9000)
    assert parse_bind("127.0.0.1") == ("127.0.0.1", 8661)
    assert parse_bind("[::1]:8662") == ("::1", 8662)
    with pytest.raises(ApiConfigError):
        parse_bind("0.0.0.0:http")
    with pytest.raises(ApiConfigError):
        parse_bind("0.0.0.0:70000")


def test_lan_bind_needs_a_long_enough_token(srv):
    with pytest.raises(ApiConfigError, match="LOCAL_API_TOKEN must be set"):
        check_settings("0.0.0.0:8661", "")
    with pytest.raises(ApiConfigError, match="too short"):
        check_settings("0.0.0.0:8661", "short")
    assert check_settings("127.0.0.1:8661", "") == ("127.0.0.1", 8661)  # this machine only: no token needed
    with pytest.raises(ApiConfigError):
        LocalApi(make_core(srv), "0.0.0.0:0", "")  # refused at start-up, before listening


def test_bridge_check_config_covers_local_mode(monkeypatch):
    monkeypatch.setenv("X100_HOST", "192.0.2.10")
    assert check_config(Config(local_api_bind="0.0.0.0:8661", local_api_token=TOKEN)) == []
    assert any("LOCAL_API_TOKEN must be set" in p
               for p in check_config(Config(local_api_bind="0.0.0.0:8661", local_api_token="")))
    assert any("nothing to do" in p for p in check_config(Config(local_api_bind="off")))


# ---------------------------------------------------------------------------- auth and bad requests


def test_healthz_needs_no_token(bridge):
    _, api = bridge
    status, body, _ = call(api, "/healthz", token="")
    assert status == 200 and body["ok"] is True
    assert (body["version"], body["build"]) == (x100.__version__, x100.BUILD)  # what the listener's --check compares


def test_missing_or_wrong_token_is_refused_and_touches_nothing(bridge, srv):
    _, api = bridge
    status, body, headers = call(api, "/state", token="")
    assert status == 401 and body["code"] == "UNAUTHORIZED"
    assert headers["WWW-Authenticate"].startswith("Bearer")
    assert call(api, "/state", token="wrong-token-wrong-token")[0] == 401
    status, body, _ = command(api, ("power", "ON"), token="wrong-token-wrong-token")
    assert status == 401
    time.sleep(0.2)
    assert not any(e.startswith("W 11") for e in srv.state.log)
    assert srv.state.power == PowerState.OFF


def test_malformed_requests_get_400_404_405(bridge):
    _, api = bridge
    assert call(api, "/command", "POST", raw=b"not json")[0] == 400
    assert call(api, "/command", "POST", {"ops": []})[0] == 400
    assert call(api, "/command", "POST", {"ops": [1]})[0] == 400
    assert call(api, "/command", "POST", {"ops": [{"op": "power", "value": "ON"}] * 21})[0] == 400
    assert call(api, "/command", "POST", {"op": "power"})[1]["code"] == "BAD_REQUEST"
    assert call(api, "/command")[0] == 405
    assert call(api, "/state", "POST", {})[0] == 405
    assert call(api, "/nowhere")[0] == 404
    assert call(api, "/command", "PUT", {"ops": []})[0] == 405


def test_body_length_is_required_and_capped(bridge):
    _, api = bridge
    conn = http.client.HTTPConnection("127.0.0.1", api.port, timeout=5)
    conn.putrequest("POST", "/command")
    conn.putheader("Authorization", f"Bearer {TOKEN}")
    conn.endheaders()
    resp = conn.getresponse()
    assert resp.status == 411
    conn.close()
    assert call(api, "/command", "POST", raw=b"x" * (MAX_BODY + 1))[0] == 413


# ---------------------------------------------------------------------------- commands


def test_power_on_drives_the_projector(bridge, srv):
    _, api = bridge
    status, body, _ = command(api, ("power", "ON"))
    assert status == 200 and body["ok"] is True and body["deferred"] is False
    assert body["state"]["power"] == "WARMING" and body["state"]["connected"] is True
    assert "W 11 00 00" in srv.state.log
    assert srv.state.power in (PowerState.WARMING, PowerState.ON)
    assert wait_for(lambda: srv.state.power == PowerState.ON)


def test_input_during_warm_up_is_deferred_then_applied(bridge, srv):
    _, api = bridge
    command(api, ("power", "ON"))
    status, body, _ = command(api, ("input", "HDMI 2"))
    assert status == 200 and body["ok"] is True and body["deferred"] is True
    assert wait_for(lambda: srv.state.power == PowerState.ON and srv.state.source == 0x07)


def test_failures_come_back_as_ok_false_with_a_code(bridge, srv):
    core, api = bridge
    assert command(api, ("power", "ON"))[1]["ok"]
    assert wait_for(lambda: srv.state.power == PowerState.ON)
    status, body, _ = command(api, ("input", "HDMI 9"))
    assert status == 200 and body["ok"] is False and body["code"] == "INVALID_VALUE"
    assert command(api, ("volume", 99))[1]["code"] == "VALUE_OUT_OF_RANGE"
    assert command(api, ("dance", 1))[1]["code"] == "INVALID_DIRECTIVE"
    status, body, _ = command(api, ("volume", 12))
    assert body["ok"] is True and body["state"]["volume"] == 12 and srv.state.volume == 12


def test_unreachable_projector_is_reported(bridge, srv):
    _, api = bridge
    srv.stop()
    status, body, _ = command(api, ("power", "ON"))
    assert status == 200 and body["ok"] is False and body["code"] == "ENDPOINT_UNREACHABLE"


def test_state_returns_the_snapshot(bridge, srv):
    core, api = bridge
    assert wait_for(lambda: core.state.connected)
    status, body, _ = call(api, "/state")
    assert status == 200
    assert body["power"] == "OFF" and body["connected"] is True
    assert [i["name"] for i in body["inputs"]] == ["HDMI 1", "HDMI 2", "HDMI 3", "MEDIA PLAYER"]


def test_504_when_the_core_does_not_answer(srv):
    core = make_core(srv)  # never started: nothing takes commands off the queue
    with LocalApi(core, "127.0.0.1:0", TOKEN, timeout=0.3) as api:
        started = time.monotonic()
        status, body, _ = command(api, ("power", "ON"))
    assert status == 504 and body["code"] == "TIMEOUT"
    assert time.monotonic() - started < 3


def test_the_ack_goes_out_before_the_state_is_read_back(srv):
    """The listener's answer must not wait for the bridge's full state refresh."""
    core = make_core(srv)
    gate, acked = threading.Event(), threading.Event()
    held = threading.Event()
    real_ack, real_refresh = core._ack, core._refresh_quietly

    def ack(*args, **kwargs):
        real_ack(*args, **kwargs)
        acked.set()

    def refresh():
        if acked.is_set() and not gate.is_set():  # the refresh that follows our command
            held.set()
            gate.wait(5)
        real_refresh()

    core._ack, core._refresh_quietly = ack, refresh
    core.start()
    try:
        with LocalApi(core, "127.0.0.1:0", TOKEN, timeout=4) as api:
            started = time.monotonic()
            status, body, _ = command(api, ("power", "ON"))
            elapsed = time.monotonic() - started
            assert status == 200 and body["ok"] is True
            assert elapsed < 1.5, f"the reply waited {elapsed:.1f} s"
            assert held.wait(2), "the refresh should run after the ack"
            assert not gate.is_set()  # ...and it was still held when the reply came back
            assert srv.state.log[-1] == "W 11 00 00"
            gate.set()
    finally:
        gate.set()
        core.stop()


# ---------------------------------------------------------------------------- the listener's client


def test_listener_client_against_the_api(bridge, srv):
    _, api = bridge
    client = BridgeClient(api.url, TOKEN, timeout=8)
    assert client.health()["ok"] is True
    result = client.command([{"op": "power", "value": "ON"}])
    assert result.ok and not result.deferred and result.state["power"] == "WARMING"
    assert result.seconds < 2
    assert result.describe() == "ok"
    refused = BridgeClient(api.url, "not-the-token-at-all", timeout=8).command([{"op": "power", "value": "OFF"}])
    assert not refused.ok and refused.code == "UNAUTHORIZED"
    failed = client.command([{"op": "input", "value": "HDMI 9"}])
    assert not failed.ok and failed.code == "INVALID_VALUE"


def test_listener_client_when_the_bridge_is_down():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # nothing listens here once the socket closes
    result = BridgeClient(f"http://127.0.0.1:{port}", TOKEN, timeout=1).command([{"op": "power", "value": "ON"}])
    assert not result.ok and result.code == "BRIDGE_UNREACHABLE"


# ---------------------------------------------------------------------------- the real process, local-only


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_bridge_process_local_only(tmp_path, srv):
    """x100-bridge with no AWS settings at all: the local API is the only way in."""
    port = free_port()
    env_file = tmp_path / "bridge.env"
    env_file.write_text("\n".join([
        "X100_HOST=127.0.0.1",
        f"X100_PORT={srv.port}",
        f"X100_INPUTS_FILE={tmp_path / 'inputs.json'}",
        f"LOCAL_API_BIND=127.0.0.1:{port}",
        f"LOCAL_API_TOKEN={TOKEN}",
        "POLL_INTERVAL=1",
        "X100_POWER_UP_QUIET_SECONDS=0",  # a test machine may have only just started
        "LOG_LEVEL=INFO",
    ]) + "\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("X100_", "AWS_IOT", "LOCAL_API"))}
    check = subprocess.run([sys.executable, "-m", "x100_bridge", "--env-file", str(env_file), "--check"],
                           cwd=ROOT, capture_output=True, text=True, timeout=60, env=env)
    assert check.returncode == 0, check.stdout + check.stderr
    assert "token works" in check.stdout

    proc = subprocess.Popen([sys.executable, "-m", "x100_bridge", "--env-file", str(env_file)], cwd=ROOT,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    client = BridgeClient(f"http://127.0.0.1:{port}", TOKEN, timeout=8)
    try:
        def up():
            try:
                return client.health()["ok"]
            except OSError:
                return False

        assert wait_for(up, limit=20), "the bridge's API never came up"
        result = client.command([{"op": "power", "value": "ON"}, {"op": "input", "value": "HDMI 2"}])
        assert result.ok and result.deferred
        assert wait_for(lambda: srv.state.power == PowerState.ON and srv.state.source == 0x07, limit=8)
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        out = proc.stdout.read() if proc.stdout else ""
        print(out)
    assert "x100-bridge starting (local API)" in out and "local API listening" in out
    assert f"running {x100.version_line()}" in out  # so journalctl shows which build is live
