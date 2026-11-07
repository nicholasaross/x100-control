"""The smart plug on the projector's mains (x100_bridge/plug.py): reading its MQTT status.

What the bridge does with it is in test_power_quirks.py. The tests with a broker skip if amqtt isn't
installed (it needs Python 3.10 or later).
"""

import json
import socket
import threading
import time
import uuid

import pytest

from x100.mock import MockServer
from x100.protocol import PowerState
from x100_bridge.core import BridgeCore, Config
from x100_bridge.main import make_plug_watcher
from x100_bridge.plug import PlugStatus, PlugWatcher, parse_broker, parse_switch_status

from .test_bridge import make_inputs

PREFIX = "test-plug"
# As Nick's plug sent it on 7 November 2026, in standby.
STANDBY = (b'{"id":0,"source":"init","tag":null, "output":true, "apower":12.1, "voltage":237.6, "current":0.086, '
           b'"aenergy":{"total":4847.616,"by_minute":[201.401,202.386,203.863],"minute_ts":1794083040},'
           b'"temperature":{"tC":39.9, "tF":103.8}}')


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(predicate, limit=5.0):
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_its_status_is_read_from_what_a_shelly_sends():
    assert parse_switch_status(STANDBY) == PlugStatus(True, 12.1, "init")
    assert parse_switch_status(b'{"id":0,"source":"button","output":false,"apower":0}') == \
        PlugStatus(False, 0.0, "button")
    assert parse_switch_status(b'{"output":true}') == PlugStatus(True, None, "")
    for junk in (b"", b"true", b"[1]", b'{"apower":12}', b'{"output":"on"}', b"\xff"):
        assert parse_switch_status(junk) is None
    assert parse_switch_status(b'{"output":true,"apower":true,"source":5}') == PlugStatus(True, None, "")


def test_the_broker_is_host_and_port():
    assert parse_broker("127.0.0.1:1884") == ("127.0.0.1", 1884)
    assert parse_broker(" broker.local ") == ("broker.local", 1883)
    assert parse_broker("[::1]:1883") == ("::1", 1883)
    for bad in ("", ":1883", "host:port", "host:0", "host:70000"):
        with pytest.raises(ValueError):
            parse_broker(bad)


def test_the_settings(monkeypatch):
    assert not Config().plug_enabled
    monkeypatch.setenv("X100_PLUG_MQTT", "127.0.0.1:1884")
    monkeypatch.setenv("X100_PLUG_PREFIX", " my-plug/ ")
    cfg = Config.from_env()
    assert cfg.plug_enabled and cfg.plug_mqtt == "127.0.0.1:1884" and cfg.plug_prefix == "my-plug"


def test_bad_plug_settings_dont_stop_the_bridge(caplog):
    with MockServer(quirks=True) as srv:
        from .test_power_quirks import driver
        for settings in (dict(plug_mqtt="127.0.0.1:1884"), dict(plug_mqtt="nonsense:port", plug_prefix="p")):
            cfg = Config(**settings)
            core = BridgeCore(cfg, driver(srv), make_inputs(), publish=lambda doc: None)
            assert make_plug_watcher(cfg, core) is None
    assert "X100_PLUG_PREFIX isn't" in caplog.text and "should be host:port" in caplog.text
    assert caplog.text.count("carrying on without the plug") == 2


# ---------------------------------------------------------------------------- with a broker


@pytest.fixture
def broker():
    pytest.importorskip("amqtt")
    from .test_mqtt_integration import BrokerThread

    b = BrokerThread(free_port())
    b.start()
    yield b
    b.stop()


class FakePlug:
    """Plays the plug's part: publishes as a Shelly does, and watches everything on the broker."""

    def __init__(self, port):
        import paho.mqtt.client as mqtt

        self.seen = []
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="plug-" + uuid.uuid4().hex[:6])
        self.client.on_message = lambda client, userdata, msg: self.seen.append(msg.topic)
        self.client.connect("127.0.0.1", port)
        self.client.subscribe("#")
        self.client.loop_start()

    def send(self, topic, payload, retain=False):
        self.client.publish(f"{PREFIX}/{topic}", payload, qos=1, retain=retain).wait_for_publish(timeout=5)

    def close(self):
        self.client.loop_stop()
        self.client.disconnect()


class Heard:
    def __init__(self):
        self.statuses, self.reconnects = [], 0
        self.lock = threading.Lock()

    def status(self, output, watts, source):
        with self.lock:
            self.statuses.append((output, watts, source))

    def reconnected(self):
        with self.lock:
            self.reconnects += 1


def test_it_passes_on_each_reading_and_each_reconnection_and_publishes_nothing(broker):
    plug = FakePlug(broker.port)
    heard = Heard()
    watcher = PlugWatcher(f"127.0.0.1:{broker.port}", PREFIX + "/", heard.status, heard.reconnected)
    try:
        plug.send("online", b"true", retain=True)  # from before the bridge started: not news
        watcher.start()
        assert watcher.connected.wait(5)
        assert wait_for(lambda: watcher.plug_online is True)
        plug.send("status/switch:0", STANDBY)
        plug.send("status/sys", b'{"uptime":12}')  # not what it listens to
        plug.send("status/switch:0", b'{"output":false,"source":"button","apower":0}')
        assert wait_for(lambda: len(heard.statuses) == 2)
        assert heard.statuses == [(True, 12.1, "init"), (False, 0.0, "button")] and heard.reconnects == 0
        plug.send("online", b"false", retain=True)  # the broker's note that it dropped out
        assert wait_for(lambda: watcher.plug_online is False)
        plug.send("online", b"true", retain=True)  # back, perhaps after a restart
        assert wait_for(lambda: heard.reconnects == 1)
        time.sleep(0.3)
        assert set(plug.seen) == {f"{PREFIX}/online", f"{PREFIX}/status/switch:0", f"{PREFIX}/status/sys"}
    finally:
        watcher.stop()
        plug.close()


def test_without_a_broker_the_bridge_carries_on_and_says_so_once(caplog):
    watcher = PlugWatcher(f"127.0.0.1:{free_port()}", PREFIX, lambda *a: None, lambda: None)
    watcher.start()
    try:
        assert wait_for(lambda: "can't read the projector's plug" in caplog.text, limit=5)
        time.sleep(2.5)  # it keeps trying, quietly
        assert caplog.text.count("can't read the projector's plug") == 1
    finally:
        watcher.stop()


def test_a_cut_the_plug_reports_over_mqtt_gets_the_projector_left_alone(broker):
    """End to end: the plug's button, as on 7 November 2026, with the bridge checking only once an hour."""
    from .test_power_quirks import HOURLY, bridge, booting_x100, hourly_checks_under_way

    with MockServer(warmup=1.2, cooldown=0.4, quirks=True) as srv:
        booting_x100(srv)
        core = bridge(srv, **HOURLY)
        plug = FakePlug(broker.port)
        watcher = PlugWatcher(f"127.0.0.1:{broker.port}", PREFIX, core.plug_status, core.plug_reconnected)
        watcher.start()
        try:
            assert watcher.connected.wait(5)
            hourly_checks_under_way(core)
            heard = len(srv.state.log)
            with srv.state.lock:
                srv.state.power_cut()
            plug.send("status/switch:0", json.dumps({"output": False, "apower": 0, "source": "button"}))
            assert wait_for(lambda: core._plug_output is False)
            with srv.state.lock:
                srv.state.power_restored()
            plug.send("status/switch:0", json.dumps({"output": True, "apower": 1.2, "source": "button"}))
            assert wait_for(core._quiet)
            refused = core.submit_and_wait([{"op": "power", "value": "ON"}])
            assert not refused["ok"] and "may still be booting" in refused["message"]
            assert len(srv.state.log) == heard and not srv.state.hung
            assert wait_for(lambda: not core._quiet() and core.state.power == PowerState.OFF, limit=4)
            assert core.submit_and_wait([{"op": "power", "value": "ON"}])["ok"]
        finally:
            watcher.stop()
            plug.close()
            core.stop()
