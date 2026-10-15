"""The X100-4K's power query says "on" in standby too. These tests run against the simulator in its
quirks mode, which does what the real projector did in traces over RS-232:

* standby: the power query says ON; volume and mute answer; input and "off" get no answer at all;
* starting up (about a minute on the real one): only the power query answers at first, then volume,
  then everything, with a moment that looks like standby again before it settles;
* "off" takes effect at once: it looks like standby straight away. An "on" 5 s later worked;
* but an "off" 66 s after "on", just after its picture appeared, hung it: from then on it
  acknowledged "on" without starting, even from its remote, until it was unplugged
  (MockState.hang_if_off_within);
* and it hung the same way every time it was asked things while booting after a power cut (the
  bridge polling it, or x100ctl trace asking once a second), whether it had been on or in standby;
  left alone, it started fine. It answers the power query throughout (MockState.boot_seconds,
  hang_if_asked_while_booting).
"""

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from x100.mock import MockServer
from x100.projector import Projector
from x100.protocol import PowerState
from x100.transport import TcpTransport
from x100_bridge.core import BridgeCore, Config

from .test_bridge import make_inputs

ROOT = Path(__file__).resolve().parents[1]


def wait_for(predicate, limit=5.0):
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def x100():
    with MockServer(warmup=1.2, cooldown=0.4, quirks=True) as srv:
        yield srv


def driver(srv):
    return Projector(TcpTransport("127.0.0.1", srv.port, timeout=0.5, min_gap=0), probe_timeout=0.15)


def bridge(srv, expect=PowerState.OFF, **overrides):
    settings = dict(poll_interval=30, fast_poll_interval=0.1, heartbeat_interval=600, defer_timeout=10,
                    warmup_seconds=5, cooldown_seconds=0.6, settle_seconds=0.5, start_check_seconds=0.3,
                    volume_step_delay=0, pending_check_interval=0.05)
    settings.update(overrides)
    core = BridgeCore(Config(**settings), driver(srv), make_inputs(), publish=lambda doc: None)
    core.start()
    assert wait_for(lambda: core.state.connected and core.state.power == expect)
    return core


POWER_CUT = dict(outage_probe_seconds=0.3, power_up_quiet_seconds=1.5, poll_interval=0.2)  # scaled down
ON = [{"op": "power", "value": "ON"}]


def switch_off_at_the_wall(srv, core, off_for=0.8, then_quiet=True):
    """Power cut and back, as the bridge sees it: no answers at all, then the power query answers."""
    with srv.state.lock:
        srv.state.power_cut()
    assert wait_for(lambda: core._lost_since is not None, limit=5)
    time.sleep(off_for)
    with srv.state.lock:
        srv.state.power_restored()
    if then_quiet:
        assert wait_for(core._quiet, limit=3)


def mock_power(srv):
    with srv.state.lock:
        srv.state.tick()  # the simulator moves on as it's asked things; nothing else is asking here
        return srv.state.power


def writes(srv, prefix):
    return [entry for entry in srv.state.log if entry.startswith(prefix)]


# ---------------------------------------------------------------------------- the driver


def test_the_reading_tells_standby_start_up_and_on_apart(x100):
    proj = driver(x100)
    standby = proj.power_reading()
    assert (standby.state, standby.reported, standby.quirky) == (PowerState.OFF, PowerState.ON, True)
    assert (standby.volume, standby.muted) == (20, False) and "standby" in standby.describe()
    proj.power_on()
    starting = proj.power_reading()
    assert (starting.state, starting.quirky) == (PowerState.WARMING, True)
    assert wait_for(lambda: proj.power_state() == PowerState.ON)
    assert wait_for(lambda: mock_power(x100) == PowerState.ON)  # past the last start-up hiccup
    on = proj.power_reading()
    assert (on.state, on.quirky) == (PowerState.ON, False) and on.source is not None
    proj.power_off()
    assert proj.power_state() == PowerState.OFF  # standby at once, as the real one does


def test_a_by_the_book_projector_costs_one_query():
    with MockServer(warmup=0.3, cooldown=0.2) as srv:
        proj = driver(srv)
        assert proj.power_reading().state == PowerState.OFF
        assert srv.state.log == ["R 11 00"]


# ---------------------------------------------------------------------------- the bridge's rules


class FakeProjector:
    """For the rules alone: it must never be asked anything except to switch on."""

    def __init__(self):
        self.ons = 0
        self.transport = self

    def describe(self):
        return "fake"

    def power_on(self):
        self.ons += 1

    def power_reading(self):
        raise AssertionError("not expected to read the projector here")


def rules(**overrides):
    settings = dict(warmup_seconds=60, cooldown_seconds=5, start_check_seconds=2)
    settings.update(overrides)
    fake = FakeProjector()
    core = BridgeCore(Config(**settings), fake, make_inputs(), publish=lambda doc: None)
    core._power_query_lies = True
    return core, fake


def test_start_up_hiccups_dont_count_as_standby():
    core, fake = rules()
    core._sent_on = time.monotonic() - 30
    core.state.set_power(PowerState.WARMING)
    assert core._settle_power(PowerState.WARMING) == PowerState.WARMING
    assert core._settle_power(PowerState.OFF) == PowerState.WARMING  # looks like standby mid start-up
    assert core._settle_power(PowerState.ON) == PowerState.ON
    assert core._settle_power(PowerState.OFF) == PowerState.ON  # a gap after it first answered
    core._sent_on = time.monotonic() - 61  # past the start-up window: believe it again
    assert core._settle_power(PowerState.OFF) == PowerState.OFF
    assert fake.ons == 0


def test_after_our_off_it_counts_as_cooling_without_asking():
    core, _ = rules()
    core._sent_off = time.monotonic()
    core.state.set_power(PowerState.COOLING)
    assert core._current_power() == PowerState.COOLING  # FakeProjector would fail if read
    assert core._settle_power(PowerState.OFF) == PowerState.COOLING
    assert core._settle_power(PowerState.ON) == PowerState.ON  # the off didn't take after all
    core._sent_off = time.monotonic() - 6
    assert core._settle_power(PowerState.OFF) == PowerState.OFF


def test_the_bridge_never_sends_an_on_by_itself():
    """It used to send a second "on" if the projector still looked like standby 2 s after the first.
    After a wall power-cycle that second "on" may be what hung the real one, so it no longer does."""
    core, fake = rules()
    for age in (1, 3, 10, 30):
        core._sent_on = time.monotonic() - age
        assert core._settle_power(PowerState.OFF) == PowerState.WARMING
    assert fake.ons == 0


def test_an_off_just_after_start_up_waits_for_it_to_settle():
    core, _ = rules(settle_seconds=15)
    core.state.set_power(PowerState.ON)  # it has only just started answering
    assert core._settling()
    core.state.power_since -= 16
    assert not core._settling()
    core._power_query_lies = False
    core.state.power_since += 16
    assert not core._settling()  # by-the-book projectors needn't wait


def test_power_commands_are_held_for_a_while_after_any_start():
    core, _ = rules(start_hold_seconds=120)
    assert not core._start_held()
    core.state.set_power(PowerState.OFF)
    core._settle_power(PowerState.WARMING)  # out of standby without us: the remote, say
    assert core._start_held()
    core._settle_power(PowerState.OFF)  # looks like standby again within the hold: keep holding
    assert core._start_held()
    core._started_at -= 121
    assert not core._start_held()
    core._settle_power(PowerState.OFF)
    assert core._started_at is None
    # A bridge that starts while the projector is already on can't know when it started: no hold.
    fresh, _ = rules(start_hold_seconds=120)
    fresh._settle_power(PowerState.ON)
    assert not fresh._start_held()
    # ...but one that finds it starting up holds from then.
    starting, _ = rules(start_hold_seconds=120)
    starting._settle_power(PowerState.WARMING)
    assert starting._start_held()
    starting._power_query_lies = False
    assert not starting._start_held()  # by-the-book projectors needn't wait


def test_a_start_the_bridge_didnt_ask_for_is_logged(caplog):
    """Only the power query answering looks the same whether its remote started it or it's booting after
    a power blip. Saying so in the log shows afterwards what the projector looked like before a hang."""
    with caplog.at_level(logging.INFO, logger="x100.bridge"):
        core, _ = rules()
        core.state.set_power(PowerState.OFF)
        for seen in (PowerState.WARMING, PowerState.WARMING, PowerState.ON):
            core._settle_power(seen)
        on, _ = rules()
        on.state.set_power(PowerState.OFF)
        on._settle_power(PowerState.ON)  # it was already showing a picture by the time the bridge asked
        ours, _ = rules()
        ours.state.set_power(PowerState.WARMING)
        ours._sent_on = ours._started_at = time.monotonic()  # as after the bridge's own "on"
        ours._settle_power(PowerState.WARMING)
    said = [r.getMessage() for r in caplog.records]
    assert len(said) == 2  # once per start, and not for its own
    assert "only the projector's power query answers" in said[0] and "booting after a power blip" in said[0]
    assert said[1].startswith("the projector is on, and the bridge didn't start it")


def test_by_the_book_projectors_are_taken_at_their_word():
    core, _ = rules()
    core._power_query_lies = False
    core._sent_off = time.monotonic()
    assert core._settle_power(PowerState.OFF) == PowerState.OFF


# ---------------------------------------------------------------------------- the bridge, end to end


def test_on_from_standby_goes_straight_out_and_is_ok_once_it_has_started(x100):
    core = bridge(x100)
    try:
        before = len(x100.state.log)
        started = time.monotonic()
        result = core.submit_and_wait([{"op": "power", "value": "ON"}])
        took = time.monotonic() - started
        assert result["ok"] and result["state"]["power"] == "WARMING"
        assert x100.state.log[before] == "W 11 00 00"  # no questions first
        # Then the "ok" waits until it has stopped answering about its volume: it really started.
        assert x100.state.log[before + 1] == "R 14 03 (silent)"
        assert 0.3 <= took < 1.0
        assert wait_for(lambda: core.state.power == PowerState.ON)
        assert x100.state.power in (PowerState.WARMING, PowerState.ON)
    finally:
        core.stop()


def test_an_off_soon_after_start_up_used_to_hang_the_projector(x100):
    """What happened to the real one, in the simulator: with the old 'settled' rule alone, an "off"
    shortly after start-up went out, and from then on it acknowledged "on" without starting."""
    x100.state.hang_if_off_within = 2.5
    core = bridge(x100, start_hold_seconds=0, **POWER_CUT)
    try:
        assert core.submit_and_wait([{"op": "power", "value": "ON"}])["ok"]
        assert wait_for(lambda: core.state.power == PowerState.ON and not core._settling())
        assert core.submit_and_wait([{"op": "power", "value": "OFF"}])["ok"]
        assert x100.state.hung
        time.sleep(0.7)  # past the cool-down
        stuck = core.submit_and_wait([{"op": "power", "value": "ON"}])
        assert not stuck["ok"] and "hasn't started" in stuck["message"]  # said so, rather than chiming "ok"
        switch_off_at_the_wall(x100, core)
        assert wait_for(lambda: not core._quiet(), limit=3)  # it was left alone while it booted
        assert core.submit_and_wait(ON)["ok"]
        assert wait_for(lambda: x100.state.power == PowerState.ON, limit=4)
    finally:
        core.stop()


def test_an_off_soon_after_start_up_is_held_until_its_safe(x100):
    x100.state.hang_if_off_within = 2.5
    core = bridge(x100, start_hold_seconds=3.0)
    try:
        on = core.submit_and_wait([{"op": "power", "value": "ON"}])
        sent_on = time.monotonic()
        assert on["ok"] and wait_for(lambda: core.state.power == PowerState.ON)
        off = core.submit_and_wait([{"op": "power", "value": "OFF"}])
        assert off["ok"] and off["deferred"]
        assert wait_for(lambda: writes(x100, "W 11 01"), limit=6)
        assert time.monotonic() - sent_on >= 3.0 - 0.3  # not before the hold (the "on" ack came 0.3 s in)
        assert not x100.state.hung
        assert wait_for(lambda: core.state.power == PowerState.OFF, limit=3)
        time.sleep(0.7)  # past the cool-down
        assert core.submit_and_wait([{"op": "power", "value": "ON"}])["ok"]  # and it starts again
        assert wait_for(lambda: x100.state.power == PowerState.ON, limit=4)
    finally:
        core.stop()


def test_an_off_is_held_after_the_remote_starts_it_too(x100):
    x100.state.hang_if_off_within = 2.5
    core = bridge(x100, start_hold_seconds=3.0, poll_interval=0.2)
    try:
        with x100.state.lock:
            x100.state.turn_on()  # the remote's power button
        started = time.monotonic()
        assert wait_for(lambda: core.state.power == PowerState.ON)
        assert core.submit_and_wait([{"op": "power", "value": "OFF"}])["deferred"]
        assert wait_for(lambda: writes(x100, "W 11 01"), limit=6)
        assert time.monotonic() - started >= 3.0 - 0.05 and not x100.state.hung
    finally:
        core.stop()


def test_an_on_straight_after_an_off_waits_until_the_projector_can_take_it(x100):
    x100.state.power = PowerState.ON
    core = bridge(x100, expect=PowerState.ON, poll_interval=0.2)
    try:
        off = core.submit_and_wait([{"op": "power", "value": "OFF"}])
        sent_off = time.monotonic()
        on = core.submit_and_wait([{"op": "power", "value": "ON"}])
        assert off["ok"] and off["state"]["power"] == "COOLING"
        assert on["ok"] and on["deferred"]
        assert wait_for(lambda: writes(x100, "W 11 00"), limit=3)
        waited = time.monotonic() - sent_off
        assert waited >= 0.6, f"sent 'on' only {waited:.2f}s after 'off'"
        assert wait_for(lambda: x100.state.power == PowerState.ON, limit=4)
        assert wait_for(lambda: core.state.power == PowerState.ON)
    finally:
        core.stop()


def test_off_in_standby_does_nothing_and_says_so_quickly(x100):
    core = bridge(x100)
    try:
        started = time.monotonic()
        result = core.submit_and_wait([{"op": "power", "value": "OFF"}])
        assert result["ok"] and not result["deferred"] and result["state"]["power"] == "OFF"
        assert time.monotonic() - started < 1.0
        assert not writes(x100, "W 11 01")  # it wouldn't have answered
    finally:
        core.stop()


def test_an_on_the_projector_ignores_is_reported_and_not_repeated(x100):
    """What the hung X100-4K did: it acknowledged "on" and stayed in standby. The success chime used to
    say otherwise; and a second "on" (the bridge's, or a repeat) is held back, as it can hang it."""
    x100.state.drop_power_on = 1
    core = bridge(x100, start_hold_seconds=1.5, warmup_seconds=1.5)
    try:
        result = core.submit_and_wait([{"op": "power", "value": "ON"}])
        assert not result["ok"] and result["code"] == "NOT_IN_OPERATION"
        assert "hasn't started" in result["message"] and "switch it off at the wall" in result["message"]
        repeat = core.submit_and_wait([{"op": "power", "value": "ON"}])
        assert not repeat["ok"] and "another 'on' this soon can hang it" in repeat["message"]
        assert len(writes(x100, "W 11 00")) == 1  # one "on", nothing more
        time.sleep(1.5)  # past the hold
        assert core.submit_and_wait([{"op": "power", "value": "ON"}])["ok"]  # the projector takes this one
        assert len(writes(x100, "W 11 00")) == 2
        assert wait_for(lambda: x100.state.power == PowerState.ON, limit=4)
    finally:
        core.stop()


def test_an_input_waits_for_start_up_and_a_moment_more(x100):
    core = bridge(x100)
    try:
        result = core.submit_and_wait([{"op": "power", "value": "ON"}, {"op": "input", "value": "HDMI 2"}])
        assert result["ok"] and result["deferred"]
        assert wait_for(lambda: x100.state.source == 0x07, limit=5)
        assert not [e for e in x100.state.log if e.startswith("W 13 01") and "silent" in e]  # never too early
    finally:
        core.stop()


def test_the_remote_is_noticed_on_and_off(x100):
    core = bridge(x100, poll_interval=0.2)
    try:
        seen = []

        def record():
            while not done.is_set():
                if not seen or seen[-1] != core.state.power:
                    seen.append(core.state.power)
                time.sleep(0.01)

        done = threading.Event()
        recorder = threading.Thread(target=record, daemon=True)
        recorder.start()
        with x100.state.lock:
            x100.state.turn_on()  # the remote's power button
        assert wait_for(lambda: core.state.power == PowerState.ON and x100.state.power == PowerState.ON)
        with x100.state.lock:
            x100.state.turn_off()
        assert wait_for(lambda: core.state.power == PowerState.OFF)
        done.set()
        recorder.join()
        assert PowerState.WARMING in seen and PowerState.ON in seen
    finally:
        core.stop()


# ---------------------------------------------------------------------------- x100ctl


def run_x100ctl(*args):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("X100_", "AWS_IOT", "LOCAL_API"))}
    return subprocess.run([sys.executable, "-m", "x100", *args], cwd=ROOT, capture_output=True, text=True,
                          timeout=60, env=env)


def test_x100ctl_status_and_trace_on_an_x100():
    pytest.importorskip("serial")
    if not hasattr(os, "openpty"):
        pytest.skip("needs pseudo-terminals")
    from x100.mock import MockSerial

    with MockSerial(warmup=3.0, cooldown=0.3, quirks=True) as m:
        status = run_x100ctl("--serial", m.path, "--timeout", "0.5", "status")
        assert status.returncode == 0, status.stdout + status.stderr
        assert "power:   OFF (standby: its power query says ON" in status.stdout
        assert "light source hours: only while it's on" in status.stdout
        on = run_x100ctl("--serial", m.path, "--timeout", "0.5", "trace", "on", "--interval", "0.2", "--settle", "1")
        assert on.returncode == 0, on.stdout + on.stderr
        assert on.stdout.splitlines()[0].startswith("before    standby   power ON")
        assert "  looks like:    starting t+" in on.stdout and "settled" in on.stdout
        off = run_x100ctl("--serial", m.path, "--timeout", "0.5", "trace", "off", "--interval", "0.2", "--settle", "1")
        assert off.returncode == 0, off.stdout + off.stderr
        assert "  looks like:    standby t+" in off.stdout and "settled" in off.stdout
        assert "  power query:   ON t+" in off.stdout


def test_x100ctl_listen_sends_nothing_and_trace_can_ask_only_about_power():
    """The two safe ways to watch it power up: listen says nothing at all; trace --power-only asks only
    the question it answers while booting."""
    pytest.importorskip("serial")
    if not hasattr(os, "openpty"):
        pytest.skip("needs pseudo-terminals")
    from x100.mock import MockSerial

    with MockSerial(warmup=3.0, cooldown=0.3, quirks=True) as m:
        listen = run_x100ctl("--serial", m.path, "--timeout", "0.5", "listen", "--seconds", "1")
        assert listen.returncode == 0, listen.stdout + listen.stderr
        assert "Nothing is sent to the projector" in listen.stdout
        assert "0 bytes received: nothing at all" in listen.stdout and m.state.log == []
        trace = run_x100ctl("--serial", m.path, "--timeout", "0.5", "trace", "--power-only", "--seconds", "1.5",
                            "--interval", "0.5")
        assert trace.returncode == 0, trace.stdout + trace.stderr
        assert "answers   power ON        input not asked" in trace.stdout
        assert set(m.state.log) == {"R 11 00"}


# ---------------------------------------------------------------------------- changes of mind


def test_the_latest_power_command_wins(x100):
    """Off while it's still settling after start-up is queued; an on after it must cancel it."""
    core = bridge(x100, settle_seconds=1.5)
    try:
        assert core.submit_and_wait([{"op": "power", "value": "ON"}])["ok"]
        assert wait_for(lambda: core.state.power == PowerState.ON)
        off = core.submit_and_wait([{"op": "power", "value": "OFF"}])
        assert off["ok"] and off["deferred"]  # it has only just come on
        assert core.submit_and_wait([{"op": "power", "value": "ON"}])["ok"]
        time.sleep(2.0)  # past the settle time: a queued off would have run by now
        assert not writes(x100, "W 11 01") and core.state.power == PowerState.ON
    finally:
        core.stop()


def test_off_cancels_an_on_still_waiting_for_the_last_off(x100):
    x100.state.power = PowerState.ON
    core = bridge(x100, expect=PowerState.ON)
    try:
        assert core.submit_and_wait([{"op": "power", "value": "OFF"}])["ok"]
        assert core.submit_and_wait([{"op": "power", "value": "ON"}])["deferred"]
        assert core.submit_and_wait([{"op": "power", "value": "OFF"}])["ok"]
        time.sleep(1.0)  # past the cool-down: a queued on would have gone out
        assert not writes(x100, "W 11 00") and core.state.power in (PowerState.COOLING, PowerState.OFF)
    finally:
        core.stop()


# ---------------------------------------------------------------------------- after a power cut


def booting_x100(srv):
    """As the real one does after a power cut: only the power query answers while it boots, and any
    other question then hangs it (twice traced, with x100ctl asking once a second)."""
    srv.state.boot_seconds, srv.state.hang_if_asked_while_booting = 1.2, True


def test_after_a_power_cut_it_asks_only_about_power_then_leaves_it_alone(x100):
    booting_x100(x100)
    core = bridge(x100, **POWER_CUT)
    try:
        switch_off_at_the_wall(x100, core)
        heard = len(x100.state.log)
        time.sleep(1.2)  # well into the quiet period
        assert len(x100.state.log) == heard  # not a word
        assert wait_for(lambda: not core._quiet())
        assert [e for e in x100.state.log if "(booting)" in e] == ["R 11 00 (booting)"]  # the probe alone
        assert not x100.state.hung
        assert core.submit_and_wait(ON)["ok"]
        assert wait_for(lambda: x100.state.power == PowerState.ON, limit=4)
    finally:
        core.stop()


def test_without_the_quiet_the_questions_hang_it_as_they_did_the_real_one(x100):
    booting_x100(x100)
    core = bridge(x100, **dict(POWER_CUT, power_up_quiet_seconds=0))
    try:
        switch_off_at_the_wall(x100, core, then_quiet=False)
        assert wait_for(lambda: x100.state.hung, limit=3)
        time.sleep(1.3)  # its boot is over: from standby, an "on" would normally start it
        result = core.submit_and_wait(ON)
        assert not result["ok"] and "hasn't started" in result["message"]
    finally:
        core.stop()


def test_a_blip_between_standby_polls_hangs_it(x100, caplog):
    """Perhaps what hung the real one on 2026-10-14: the power went off and came back between two standby
    polls, so the bridge never heard silence. The next poll took the boot for a normal start-up, and its
    question about the input hung it."""
    caplog.set_level(logging.INFO, logger="x100.bridge")
    booting_x100(x100)
    x100.state.boot_seconds = 2.0  # the bridge loop can poll up to 0.25 s late: keep the poll inside the boot
    core = bridge(x100, poll_interval=2.0, fast_poll_window=0)
    try:
        with x100.state.lock:
            x100.state.power_cut()  # just after a standby poll
        time.sleep(1.0)
        with x100.state.lock:
            x100.state.power_restored()  # and back before the next one
        assert wait_for(lambda: "R 13 01 (booting)" in x100.state.log, limit=3)
        assert x100.state.hung and core._lost_since is None and not core._quiet()  # it never saw the cut
        assert wait_for(lambda: any("booting after a power blip" in r.getMessage() for r in caplog.records))
        assert wait_for(lambda: not x100.state.booting() and core.state.power == PowerState.OFF, limit=4)
        result = core.submit_and_wait(ON)
        assert not result["ok"] and "looks like it's in standby" in result["message"]
    finally:
        core.stop()


def test_commands_get_an_answer_and_the_projector_hears_nothing(x100):
    core = bridge(x100, **dict(POWER_CUT, power_up_quiet_seconds=2.0))
    try:
        with x100.state.lock:
            x100.state.power_cut()
        assert wait_for(lambda: core._lost_since is not None, limit=5)
        off = core.submit_and_wait(ON)
        assert not off["ok"] and off["code"] == "ENDPOINT_UNREACHABLE"
        assert "switched off at the wall" in off["message"]
        with x100.state.lock:
            x100.state.power_restored()
        assert wait_for(core._quiet, limit=3)
        heard = len(x100.state.log)
        booting = core.submit_and_wait(ON)
        assert not booting["ok"] and booting["code"] == "NOT_IN_OPERATION"
        assert "may still be booting" in booting["message"] and "remote" in booting["message"]
        time.sleep(0.5)
        assert len(x100.state.log) == heard  # not even the "on"
    finally:
        core.stop()


def test_even_a_moments_silence_gets_it_left_alone(x100):
    """A missed reply looks the same as a power blip it's booting after, so it gets the same care."""
    booting_x100(x100)
    core = bridge(x100, **POWER_CUT)
    try:
        with x100.state.lock:
            x100.state.drop_replies = 2  # one reading goes unanswered (it's tried twice)
        assert wait_for(core._quiet, limit=4)
        assert [e for e in x100.state.log if "(booting)" in e] == []
        assert wait_for(lambda: not core._quiet() and core.state.connected, limit=4)
        assert core.submit_and_wait(ON)["ok"]
    finally:
        core.stop()


def test_a_bridge_started_while_the_projector_is_unplugged_waits_for_its_boot(x100):
    booting_x100(x100)
    with x100.state.lock:
        x100.state.power_cut()
    core = BridgeCore(Config(fast_poll_interval=0.1, heartbeat_interval=600, defer_timeout=10, warmup_seconds=5,
                             cooldown_seconds=0.6, settle_seconds=0.5, start_check_seconds=0.3,
                             pending_check_interval=0.05, **POWER_CUT),
                      driver(x100), make_inputs(), publish=lambda doc: None)
    core.start()
    try:
        assert wait_for(lambda: core._lost_since is not None, limit=5)
        with x100.state.lock:
            x100.state.power_restored()
        assert wait_for(core._quiet, limit=3)
        assert wait_for(lambda: not core._quiet(), limit=3)
        assert not x100.state.hung and core.submit_and_wait(ON)["ok"]
    finally:
        core.stop()


def test_a_bridge_starting_with_this_computer_leaves_the_projector_alone_at_first(x100):
    """A mains cut takes the Pi and the projector together: when the Pi is back, the projector may
    still be booting, so the bridge says nothing until the quiet time after the Pi started is up."""
    booting_x100(x100)
    with x100.state.lock:
        x100.state.power_restored()  # the mains has just come back
    core = BridgeCore(Config(poll_interval=0.2, fast_poll_interval=0.1, heartbeat_interval=600, defer_timeout=10,
                             warmup_seconds=5, cooldown_seconds=0.6, settle_seconds=0.5, start_check_seconds=0.3,
                             pending_check_interval=0.05, power_up_quiet_seconds=1.5),
                      driver(x100), make_inputs(), publish=lambda doc: None, uptime=lambda: 0.2)
    core.start()
    try:
        time.sleep(0.6)
        assert x100.state.log == []  # not a word while it boots
        refused = core.submit_and_wait(ON)
        assert not refused["ok"] and "may still be booting" in refused["message"]
        assert wait_for(lambda: core.state.connected and core.state.power == PowerState.OFF, limit=4)
        assert not x100.state.hung
    finally:
        core.stop()


def test_a_bridge_on_a_computer_thats_been_up_a_while_starts_straight_away(x100):
    core = BridgeCore(Config(poll_interval=0.2, fast_poll_interval=0.1, heartbeat_interval=600,
                             power_up_quiet_seconds=1.5),
                      driver(x100), make_inputs(), publish=lambda doc: None, uptime=lambda: 4000.0)
    core.start()
    try:
        assert wait_for(lambda: core.state.connected, limit=2) and not core._quiet()
    finally:
        core.stop()
