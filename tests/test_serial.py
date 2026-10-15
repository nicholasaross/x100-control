"""The RS-232 path, as on the Pi that does everything: the driver, the bridge and the real bridge process
against the simulated projector on a pseudo-terminal (standing in for the USB serial adapter)."""

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

serial = pytest.importorskip("serial")
if not hasattr(os, "openpty"):
    pytest.skip("needs pseudo-terminals (Linux or macOS)", allow_module_level=True)

from x100.mock import MockSerial  # noqa: E402
from x100.projector import Projector, Unreachable  # noqa: E402
from x100.protocol import PowerState  # noqa: E402
from x100.transport import SerialTransport  # noqa: E402
from x100_bridge.core import BridgeCore, Config  # noqa: E402
from x100_bridge.local_api import LocalApi  # noqa: E402
from x100_listener.client import BridgeClient  # noqa: E402

from .test_bridge import make_inputs  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "serial-test-token-0123456789"


def wait_for(predicate, limit=5.0):
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def clean_env():
    return {k: v for k, v in os.environ.items() if not k.startswith(("X100_", "AWS_IOT", "LOCAL_API"))}


@pytest.fixture(autouse=True)
def flush_that_lets_the_mock_run(monkeypatch):
    """pyserial's flush() calls termios.tcdrain(), which waits until the other end of the line has read
    what was sent. On a pseudo-terminal, the other end is MockSerial's thread. When the test opens the
    port itself, that thread is in the same process. Python 3.9's tcdrain() holds the GIL while it
    waits, so the thread can't run and the test hangs. From 3.10, tcdrain() lets go of the GIL.

    So on 3.9 this waits for the same thing in short sleeps, which do let go. A real adapter drains by
    itself, so the bridge doesn't need this."""
    if sys.version_info >= (3, 10):
        return

    def flush(ser):
        while ser.out_waiting:
            time.sleep(0.001)

    monkeypatch.setattr(serial.Serial, "flush", flush)


@pytest.fixture
def port():
    with MockSerial(warmup=0.5, cooldown=0.3) as m:
        yield m


def test_the_driver_over_rs232(port):
    proj = Projector(SerialTransport(port.path, timeout=1.0, min_gap=0))
    try:
        assert proj.power() == PowerState.OFF
        proj.power_on()
        assert wait_for(lambda: proj.power() == PowerState.ON)
        proj.select_source(0x07)
        proj.set_volume(12)
        proj.set_mute(True)
        assert (port.state.source, port.state.volume, port.state.muted) == (0x07, 12, True)
        assert (proj.source(), proj.volume(), proj.muted()) == (0x07, 12, True)
        proj.power_off()
        assert wait_for(lambda: proj.power() == PowerState.OFF)
    finally:
        proj.transport.close()


def test_line_noise_before_a_reply_is_skipped():
    with MockSerial(junk_prefix=b"\x00\xff\x13\x55") as m:
        proj = Projector(SerialTransport(m.path, timeout=1.0, min_gap=0))
        assert proj.power() == PowerState.OFF
        assert proj.power() == PowerState.OFF  # and the next exchange isn't thrown by leftovers
        proj.transport.close()


def test_a_missing_adapter_is_reported_as_unreachable(tmp_path):
    proj = Projector(SerialTransport(str(tmp_path / "ttyUSB9"), timeout=0.5, min_gap=0))
    with pytest.raises(Unreachable):
        proj.power()


def test_the_bridge_over_rs232_with_the_default_pause(port):
    """What a voice command costs on the bridge. "On" from standby goes straight out, because the last
    reading said standby. "Off" asks first (power, then input), so it pays the 0.1 s pause twice."""
    cfg = Config(poll_interval=30, fast_poll_interval=0.2, heartbeat_interval=600, defer_timeout=10,
                 volume_step_delay=0, pending_check_interval=0.05)
    core = BridgeCore(cfg, Projector(SerialTransport(port.path, timeout=1.0)), make_inputs(),  # min_gap 0.1
                      publish=lambda doc: None)
    core.start()
    try:
        with LocalApi(core, "127.0.0.1:0", TOKEN, timeout=8) as api:
            client = BridgeClient(api.url, TOKEN, timeout=8)
            assert wait_for(lambda: core.state.connected)
            time.sleep(0.2)  # let the start-up poll finish, as it would long before anyone speaks
            result = client.command([{"op": "power", "value": "ON"}])
            assert result.ok and result.state["power"] == "WARMING"
            print(f"\npower on from standby over RS-232: {result.seconds * 1000:.0f} ms")
            assert result.seconds < 0.1
            result = client.command([{"op": "input", "value": "HDMI 2"}])
            assert result.ok and result.deferred
            assert wait_for(lambda: port.state.power == PowerState.ON and port.state.source == 0x07, limit=8)
            result = client.command([{"op": "power", "value": "OFF"}])
            print(f"power off over RS-232 with the default 0.1 s pause: {result.seconds * 1000:.0f} ms")
            assert result.ok and result.state["power"] == "COOLING" and 0.2 <= result.seconds < 0.8
    finally:
        core.stop()
        core.proj.transport.close()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_the_pi_layout_bridge_process_on_rs232(tmp_path, port):
    """bridge.env as install.sh writes it on the Pi that does everything: RS-232, API on loopback."""
    api_port = free_port()
    env_file = tmp_path / "bridge.env"
    env_file.write_text("\n".join([
        f"X100_SERIAL={port.path}",
        f"X100_INPUTS_FILE={tmp_path / 'inputs.json'}",
        f"LOCAL_API_BIND=127.0.0.1:{api_port}",
        f"LOCAL_API_TOKEN={TOKEN}",
        "POLL_INTERVAL=1",
        "X100_POWER_UP_QUIET_SECONDS=0",  # a test machine may have only just started
        "LOG_LEVEL=INFO",
    ]) + "\n")
    check = subprocess.run([sys.executable, "-m", "x100_bridge", "--env-file", str(env_file), "--check"],
                           cwd=ROOT, capture_output=True, text=True, timeout=60, env=clean_env())
    assert check.returncode == 0, check.stdout + check.stderr
    assert f"serial://{port.path}@115200" in check.stdout and "token works" in check.stdout

    proc = subprocess.Popen([sys.executable, "-m", "x100_bridge", "--env-file", str(env_file)], cwd=ROOT,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=clean_env())
    client = BridgeClient(f"http://127.0.0.1:{api_port}", TOKEN, timeout=8)
    try:
        def up():
            try:
                return client.health()["ok"]
            except OSError:
                return False

        assert wait_for(up, limit=20), "the bridge's API never came up"
        result = client.command([{"op": "power", "value": "ON"}, {"op": "input", "value": "HDMI 2"}])
        assert result.ok and result.deferred
        assert wait_for(lambda: port.state.power == PowerState.ON and port.state.source == 0x07, limit=8)
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        out = proc.stdout.read() if proc.stdout else ""
        print(out)
    assert f"projector serial://{port.path}@115200" in out


def test_x100ctl_probe_over_rs232(port):
    out = subprocess.run([sys.executable, "-m", "x100", "--serial", port.path, "probe"], cwd=ROOT,
                         capture_output=True, text=True, timeout=60, env=clean_env())
    assert out.returncode == 0, out.stdout + out.stderr
    assert f"RS-232 {port.path} @ 115200: two-way OK -- power is OFF" in out.stdout
    assert f"Use X100_SERIAL={port.path}" in out.stdout


def test_x100ctl_trace_logs_each_power_change_and_stops_once_settled(port):
    def trace(action):
        out = subprocess.run([sys.executable, "-m", "x100", "--serial", port.path, "trace", action,
                              "--interval", "0.2", "--settle", "1"],
                             cwd=ROOT, capture_output=True, text=True, timeout=60, env=clean_env())
        assert out.returncode == 0, out.stdout + out.stderr
        return out.stdout

    on = trace("on")
    assert on.splitlines()[0].startswith("before    off       power OFF")
    assert "sent power on; the projector acknowledged it" in on and "power WARMING" in on
    assert "settled: unchanged in the new state for 1 s" in on
    assert "  looks like:    warming t+" in on and ", on t+" in on
    assert "  power query:   WARMING t+" in on and ", ON t+" in on
    assert "input:         refused t+" in on and "0x03 HDMI 1 t+" in on
    off = trace("off")
    assert off.splitlines()[0].startswith("before    on        power ON        input 0x03 HDMI 1")
    assert "power COOLING" in off and "  power query:   COOLING t+" in off and ", OFF t+" in off
    assert "  looks like:    cooling t+" in off and ", off t+" in off
    assert port.state.power == PowerState.OFF


def test_x100ctl_trace_without_an_action_just_logs(port):
    out = subprocess.run([sys.executable, "-m", "x100", "--serial", port.path, "trace", "--seconds", "1",
                          "--interval", "0.25"], cwd=ROOT, capture_output=True, text=True, timeout=60,
                         env=clean_env())
    assert out.returncode == 0, out.stdout + out.stderr
    assert "sent power" not in out.stdout and "stopped after 1 s." in out.stdout
    assert out.stdout.count("t+") >= 4 and port.state.power == PowerState.OFF


def test_x100ctl_probe_explains_a_silent_port(tmp_path):
    out = subprocess.run([sys.executable, "-m", "x100", "--serial", str(tmp_path / "ttyUSB9"), "probe"],
                         cwd=ROOT, capture_output=True, text=True, timeout=60, env=clean_env())
    assert "No RS-232 answer" in out.stdout and "straight-through" in out.stdout
