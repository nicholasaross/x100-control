"""The log: what the bridge and the listener write, so that what happened can be told afterwards.

- the serial traffic, in words, whenever the projector isn't simply on, and any bytes it sends unasked;
- what the projector's draw says it did (plugwatch.py), from the plug's readings on 9-17 November 2026;
- one line as each standby period ends, and the daily line's totals;
- the reply the listener played;
- the plug recorder (``x100-bridge --record-plug``).
"""

from __future__ import annotations

import logging
import threading
import time

import pytest

from x100 import protocol as p
from x100.mock import MockServer
from x100.projector import Projector
from x100.protocol import PowerState
from x100.transport import Transport, describe_bytes
from x100_bridge.plug import format_message, record
from x100_bridge.plugwatch import PlugWatch
from x100_listener.listener import Event
from x100_listener.main import describe, reply_said

from .test_power_quirks import HOURLY, OFF, ON, bridge, driver, mock_power, plug_reading_as_usual, wait_for


@pytest.fixture
def x100():
    with MockServer(warmup=1.2, cooldown=0.4, quirks=True) as srv:
        yield srv


def said(caplog, start=""):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith(start)]


def appears(caplog, start, limit=3.0):
    return wait_for(lambda: said(caplog, start), limit=limit)


# ---------------------------------------------------------------------------- the serial line, in words


def test_each_command_has_a_name_and_each_answer_a_meaning():
    assert p.command_name(p.POWER_STATUS) == "power?"
    assert p.command_name(p.POWER_ON) == "'on'" and p.command_name(p.POWER_OFF) == "'off'"
    assert p.command_name(p.source_select(0x03)) == "input 0x03"
    assert p.command_name(p.volume_set(20)) == "volume 20"
    assert p.command_name(p.remote_key(0x92)) == "key 0x92"
    assert p.command_name(p.Read(0x99, 0x01)) == "read 99 01?"
    assert p.answer_text(p.POWER_STATUS, 1) == "on" and p.answer_text(p.POWER_STATUS, 7) == "7"
    assert p.answer_text(p.MUTE_STATUS, 0) == "off" and p.answer_text(p.SOURCE_STATUS, 3) == "0x03"
    assert p.answer_text(p.VOLUME_STATUS, 9) == "9"


def test_the_driver_tells_of_every_exchange_and_what_came_back(x100):
    heard = []
    proj = driver(x100)
    proj.on_exchange = lambda what, answer, seconds: heard.append((what, answer, seconds))
    with x100.state.lock:
        x100.state.power = PowerState.OFF
    reading = proj.power_reading()
    assert reading.state == PowerState.OFF
    assert [(w, a) for w, a, _ in heard] == [("power?", "on"), ("input?", "no reply"), ("volume?", "20"),
                                            ("mute?", "off")]
    assert heard[1][2] >= 0.1  # the wait for the answer that didn't come
    heard.clear()
    proj.power_on()
    assert [(w, a) for w, a, _ in heard] == [("'on'", "ok")]


def test_a_listener_that_fails_doesnt_get_in_a_commands_way(x100, caplog):
    proj = driver(x100)

    def broken(*args):
        raise RuntimeError("bug")

    proj.on_exchange = broken
    proj.power_on()  # still sent, and answered
    assert "couldn't note an exchange" in caplog.text


class _Wire(Transport):
    """A link that has bytes waiting before each command, then answers it."""

    def __init__(self, waiting: bytes, reply: bytes):
        super().__init__(timeout=0.2, min_gap=0)
        self.waiting, self.reply, self.opened = waiting, reply, False

    def _open(self):
        self.opened = True

    def _close(self):
        self.opened = False

    def _is_open(self):
        return self.opened

    def _send(self, data):
        self.buffer = self.reply

    def _recv(self, n, deadline):
        out, self.buffer = self.buffer[:n], self.buffer[n:]
        return out

    def _drain(self):
        out, self.waiting = self.waiting, b""
        return out

    def describe(self):
        return "wire"


def test_bytes_the_projector_sends_unasked_are_logged_with_what_they_say(caplog):
    caplog.set_level(logging.INFO)
    late = p.build_frame(p.TYPE_READ_REPLY, bytes([0x00, 0x00, 0x09]))  # a late answer: 9
    ack = p.build_frame(p.TYPE_ACK, b"")
    proj = Projector(_Wire(late, ack))
    proj.power_on()
    line = next(m for m in said(caplog) if "nobody was waiting for" in m)
    assert "value 9" in line and late.hex(" ") in line
    assert describe_bytes(b"\x01\x02") == "01 02"


def test_in_standby_hourly_power_checks_and_readings_get_a_line_but_frequent_checks_dont(x100, caplog,
                                                                                          monkeypatch):
    caplog.set_level(logging.INFO, logger="x100.bridge")
    core = bridge(x100, power_check_seconds=0.2, power_check_wait=0.15, x100_quirks=True)
    try:
        assert appears(caplog, "serial (reading): power? on")  # the first reading, as the bridge starts
        time.sleep(0.8)  # several checks, every 0.2 s: routine
        assert not said(caplog, "serial (power check)")
        monkeypatch.setattr("x100_bridge.core.SERIAL_CHECKS_LOGGED_FROM", 0.1)  # as if an hour apart
        assert appears(caplog, "serial (power check): power? on (")
    finally:
        core.stop()


def test_the_bridges_on_and_what_it_asked_first_come_before_what_it_made_of_them(x100, caplog):
    caplog.set_level(logging.INFO, logger="x100.bridge")
    core = bridge(x100, x100_quirks=True, power_check_seconds=3600)
    try:
        assert core.submit_and_wait(ON)["ok"]
        lines = said(caplog)
        sent = next(i for i, m in enumerate(lines) if m.startswith("serial (command power=ON):") and "'on' ok" in m)
        change = lines.index("projector: OFF -> WARMING (the bridge sent 'on')")
        assert sent < change
    finally:
        core.stop()


def test_while_it_is_on_and_nothing_happens_the_polls_add_no_lines(x100, caplog):
    caplog.set_level(logging.INFO, logger="x100.bridge")
    core = bridge(x100, x100_quirks=True, poll_interval=0.2, fast_poll_window=0.3, settle_seconds=0.2,
                  start_hold_seconds=0.3, warmup_seconds=0.5, power_check_seconds=0)
    try:
        assert core.submit_and_wait(ON)["ok"]
        assert wait_for(lambda: core.state.power == PowerState.ON and core._routine(), limit=5)
        time.sleep(0.3)
        before = len(said(caplog, "serial (reading)"))
        time.sleep(1.2)  # several polls, every 0.2 s
        assert len(said(caplog, "serial (reading)")) == before
        assert core.submit_and_wait(OFF)["ok"]  # and a command still gets its line
        assert appears(caplog, "serial (command power=OFF):")
    finally:
        core.stop()


# ---------------------------------------------------------------------------- standby periods, and hangs


def test_a_hang_shows_what_was_asked_then_ends_its_standby_period(x100, caplog):
    caplog.set_level(logging.INFO, logger="x100.bridge")
    core = bridge(x100, x100_quirks=True, power_check_seconds=3600)
    try:
        with x100.state.lock:
            x100.state.hung = True
        assert not core.submit_and_wait(ON)["ok"]
        lines = said(caplog)
        warned = next(i for i, m in enumerate(lines) if m.startswith("the projector acknowledged power on"))
        assert lines[warned - 1].startswith("serial (command power=ON): volume? ")  # still answering: no start
        period = next(m for m in lines if m.startswith("standby period:"))
        assert "(it was already in standby when the bridge started)" in period
        assert "0 full readings, 1 commands" in period  # it had read it just before: "on" went straight out
        assert period.endswith("ended by the bridge's 'on': it was acknowledged, but the projector didn't start")
        assert core._today["hangs"] == 1
    finally:
        core.stop()


def test_after_a_hang_it_is_the_same_standby_not_a_fresh_one(x100, caplog):
    """On 17 November the second "on" in a hang said "standby for 0.0 h, since 13:21 (its remote, or the
    projector itself, switched it off)": the reading after the first "on" had taken it for a fresh standby."""
    caplog.set_level(logging.INFO, logger="x100.bridge")
    core = bridge(x100, x100_quirks=True, power_check_seconds=3600, start_hold_seconds=0.3, warmup_seconds=1.5,
                  settle_seconds=0.2)
    try:
        assert core.submit_and_wait(ON)["ok"]
        assert wait_for(lambda: mock_power(x100) == PowerState.ON and core.state.power == PowerState.ON, limit=5)
        time.sleep(0.5)  # past its start hold
        assert core.submit_and_wait(OFF)["ok"]
        assert wait_for(lambda: mock_power(x100) == PowerState.OFF, limit=3)
        time.sleep(0.8)  # its cool-down
        with x100.state.lock:
            x100.state.hung = True
        assert not core.submit_and_wait(ON)["ok"]
        assert wait_for(lambda: core.state.power == PowerState.OFF, limit=5)  # a reading: still standby
        assert not core.submit_and_wait(ON)["ok"]
        hangs = said(caplog, "the projector acknowledged power on")
        assert len(hangs) == 2 and all("(the bridge switched it off)" in h for h in hangs)
        periods = [m for m in said(caplog, "standby period:") if "(the bridge switched it off" in m]
        assert len(periods) == 1
        assert periods[0].endswith("ended by the bridge's 'on': it was acknowledged, but the projector didn't start")
    finally:
        core.stop()


def test_a_start_the_bridge_didnt_send_ends_the_standby_period(x100, caplog):
    caplog.set_level(logging.INFO, logger="x100.bridge")
    core = bridge(x100, x100_quirks=True, poll_interval=0.2, power_check_seconds=0)
    try:
        with x100.state.lock:
            x100.state.turn_on()  # its remote
        assert appears(caplog, "standby period:", limit=5)
        period = said(caplog, "standby period:")[0]
        assert period.endswith("ended by a start the bridge didn't send (its remote, or its own button)")
    finally:
        core.stop()


# ---------------------------------------------------------------------------- the plug's view (plugwatch.py)


def watch():
    return PlugWatch(3.0, 25.0, 50.0, stamp=lambda t: f"t{t:g}")


def feed(w, readings, base=0.0):
    events = []
    for at, watts in readings:
        events += w.tick(base + at)
        events += w.reading(base + at, True, watts)
    return events


def test_the_bridges_on_that_started_it_17_november_morning():
    w = watch()
    feed(w, [(0, 12.1)])
    w.sent("on", 1000.0)
    events = feed(w, [(2, 6.8), (3, 13.1), (4, 14.7), (5, 19.8), (6, 30.3), (13, 42.0), (15, 92.4), (16, 147.3)],
                  base=1000.0)
    assert [e.kind for e in events] == ["start"]
    assert events[0].by_bridge
    assert events[0].text == ("the projector came on: a dip to 6.8 W at t1002, then 92 W 13 s later; "
                              "2 s after the bridge's 'on'")
    assert w.tick(1100.0) == []  # nothing more to tell about that "on"


def test_an_on_ignored_then_two_presses_of_its_remote_16_november():
    """18:31:18 the bridge's "on", ignored (a hang). 18:31:41 its remote: a dip, then a boot-like draw, but no
    start. 18:32:52 its remote again: it started."""
    w = watch()
    feed(w, [(0, 12.0)])
    w.sent("on", 0.0)
    events = feed(w, [(10, 12.0), (23, 6.7), (24, 12.1), (27, 13.8), (35, 16.2), (36, 17.3), (51, 16.8),
                      (70, 15.9), (73, 17.5), (87, 15.5), (94, 6.8), (95, 15.8), (97, 20.0), (98, 30.6),
                      (107, 42.0), (108, 92.8), (109, 147.7)])
    assert [(e.kind, e.by_bridge) for e in events] == [("no_reaction", True), ("press", False), ("start", False)]
    assert "didn't react to it at all" in events[0].text
    assert events[1].text.startswith("something reached the projector at t23 (a dip to 6.7 W), but it didn't start")
    assert events[1].text.endswith("not by the bridge: its remote, or its own button")
    assert events[2].text.startswith("the projector came on: a dip to 6.8 W at t94")


def test_offs_by_the_bridge_and_by_its_remote():
    w = watch()
    feed(w, [(0, 151.0)])
    w.sent("off", 10.0)
    events = feed(w, [(12, 21.3), (13, 15.6), (17, 11.7)])  # 17 November, 10:53:56
    assert [(e.kind, e.by_bridge) for e in events] == [("off", True)]
    assert events[0].text == "the projector went off: from 151 W to 21 W at t12; 2 s after the bridge's 'off'"
    w = watch()
    events = feed(w, [(0, 151.7), (16, 92.9), (17, 16.3), (18, 13.8)])  # 16 November, 19:26, its remote
    assert [(e.kind, e.by_bridge) for e in events] == [("off", False)]
    assert events[0].text == ("the projector went off: from 152 W to 16 W at t17; not by the bridge: its remote, "
                              "or the projector itself")


def test_a_step_in_its_standby_draw_isnt_a_press_15_november():
    """A minute after its remote switched it off (11:17:41), its standby draw stepped down, from 11.3 W to
    about 10.5 W, through one reading of 9.8 W. It then wavered around 10.4 W for minutes. Nothing started."""
    w = watch()
    events = feed(w, [(0, 151.0), (2, 88.6), (3, 16.3), (4, 13.6), (7, 11.8), (22, 11.3), (42, 9.8), (44, 10.6),
                      (50, 10.6), (60, 10.8), (70, 10.5), (82, 10.3), (108, 10.5), (110, 10.5), (112, 10.4),
                      (142, 10.4), (146, 10.3), (186, 10.4), (192, 10.3), (240, 10.5), (246, 10.7), (248, 10.3)])
    events += w.tick(400.0)
    assert [(e.kind, e.by_bridge) for e in events] == [("off", False), ("step", False)]
    assert events[1].text == ("its standby draw dipped to 9.8 W at t42, then 10.8 W: something changed inside it, "
                              "but nothing started it")


def test_a_step_back_up_isnt_a_press_either_11_november():
    w = watch()
    events = feed(w, [(0, 13.6), (33, 9.5), (33.5, 11.2), (38, 12.3), (55, 12.6), (60, 12.4), (89, 12.8),
                      (109, 13.4)])
    assert [e.kind for e in events] == ["step"]


def test_an_off_it_ignored():
    w = watch()
    feed(w, [(0, 150.0)])
    w.sent("off", 5.0)
    events = feed(w, [(15, 150.4), (16, 150.2)])
    assert [e.kind for e in events] == ["no_off"]


def test_a_boot_after_the_plugs_power_came_back_9_november():
    w = watch()
    feed(w, [(0, 12.0)])
    w.reading(1.0, False, 0.0)  # the plug's button
    readings = [(121.4, 0.0), (121.5, 0.1), (122.4, 12.0), (123.4, 11.1), (126.4, 14.0), (127.4, 12.9),
                (131.5, 14.4), (134.4, 15.8), (135.4, 17.2), (136.4, 15.8), (137.4, 14.5), (138.4, 15.6),
                (139.4, 16.7), (153.6, 17.9), (154.4, 19.6), (156.4, 17.2), (157.4, 14.0), (159.4, 12.4),
                (167.9, 14.1), (168.4, 12.9), (169.4, 14.0), (172.2, 13.3), (174.4, 12.7), (232.3, 12.1)]
    events = feed(w, readings)
    assert [e.kind for e in events] == ["boot"]
    assert events[0].text == ("its power came back at t122.4, and it booted: its draw peaked at 19.6 W at +32 s, "
                              "and was back under 14.5 W from +35 s")


def test_unplugged_plugged_in_and_started_by_its_remote_during_the_boot_17_november():
    w = watch()
    feed(w, [(0, 13.3)])
    events = feed(w, [(4, 0.0), (5, 4.1), (6, 11.6), (10, 12.4), (14, 16.4), (37, 15.3), (55, 13.6),
                      (57, 0.0)])  # unplugged for a second, then again before its boot was over
    assert [e.kind for e in events] == ["boot"]
    assert events[0].text.endswith("and then lost its power again at +52 s")
    events = feed(w, [(66, 1.9), (67, 9.4), (68, 11.5), (70, 14.1), (80, 17.3), (102, 18.9), (104, 14.6),
                      (107, 13.2), (109, 12.8), (113, 10.3), (114, 12.9), (115, 17.9), (116, 26.4), (117, 32.3),
                      (125, 41.3), (127, 51.2), (128, 148.5)])
    assert [(e.kind, e.by_bridge) for e in events] == [("boot", None), ("start", False)]
    assert events[0].text.endswith("peaked at 18.9 W at +35 s, when something started it at +46 s")
    assert events[1].text.startswith("the projector came on: a dip to 10.3 W at t113, then 51 W 14 s later")


def test_without_recent_readings_it_says_it_cant_tell():
    w = watch()
    feed(w, [(0, 12.0)])
    w.sent("on", 400.0)
    events = w.tick(411.0)
    assert [e.kind for e in events] == ["no_readings"]


def test_the_bridge_logs_the_plugs_view_and_puts_it_in_the_standby_period(x100, caplog):
    caplog.set_level(logging.INFO, logger="x100.bridge")
    core = bridge(x100, **dict(HOURLY, summary_seconds=0.5))
    try:
        plug_reading_as_usual(core)
        core.plug_status(True, 151.0, "timer", 4)  # its remote started it (not seen: a jump to on)
        core.plug_status(True, 92.9, "timer", 4)
        core.plug_status(True, 16.3, "timer", 4)  # and switched it off
        assert appears(caplog, "plug: the projector went off")
        assert appears(caplog, "in the last 0 h: ")
        assert wait_for(lambda: any("the plug saw 1 starts (0 by the bridge), 1 offs (0 by the bridge)" in m
                                    for m in said(caplog, "in the last")), limit=2)
        assert core.submit_and_wait(ON)["ok"]
        period = said(caplog, "standby period:")[-1]
        assert "(its remote, or the projector itself, switched it off; its draw fell at " in period
        assert period.endswith("ended by the bridge's 'on': it started")
    finally:
        core.stop()


# ---------------------------------------------------------------------------- the listener's reply


def test_the_listener_says_what_it_played():
    assert reply_said("not_started", "spoken") == ('said "It didn\'t start. It might need switching off at the '
                                                   'wall."')
    assert reply_said("not_started", "the fail chime") == "played the fail chime (for not_started)"
    assert reply_said("ok", "the ok chime") == "played the ok chime"
    assert reply_said("ok", "nothing: chimes are off") == "played nothing: chimes are off"
    assert reply_said("ok", "") == "reply: ok"
    event = Event("result", 0.0, {"ok": False, "outcome": "NOT_IN_OPERATION: hung", "bridge_ms": 3124,
                                  "reply": "not_started", "sound": "spoken"})
    assert describe(event).endswith("to the bridge's answer; said \"It didn't start. It might need switching off "
                                    "at the wall.\"")


# ---------------------------------------------------------------------------- the plug recorder


TIMER = (b'{"id":0,"source":"timer","tag":null, "output":true, "apower":12.0, "voltage":236.2, "current":0.086, '
         b'"aenergy":{"total":8829.626,"by_minute":[199.923,199.431,199.431],"minute_ts":1794673200},'
         b'"counts":{"on_time":1207140,"on_time_rst_ts":0,"switch_on":4,"switch_on_rst_ts":0,"on_above_thr":120779,'
         b'"on_above_thr_rst_ts":0},"temperature":{"tC":40.0, "tF":104.0}}')


def test_each_plug_message_is_one_line():
    assert format_message("status/switch:0", TIMER) == ("switch:0 output=on watts=12.0 amps=0.086 volts=236.2 "
                                                        "source=timer switch_on=4 celsius=40.0")
    assert format_message("online", b"true", retained=True) == "online true (retained)"
    assert format_message("status/sys", b'{"uptime":12}') == 'status/sys {"uptime":12}'
    assert format_message("status/switch:0", b"junk") == "status/switch:0 junk"


@pytest.fixture
def plug_broker():
    pytest.importorskip("amqtt")
    from .test_mqtt_integration import BrokerThread
    from .test_plug import free_port

    b = BrokerThread(free_port())
    b.start()
    yield b
    b.stop()


def test_the_recorder_writes_a_line_for_each_message_and_publishes_nothing(plug_broker):
    from .test_plug import PREFIX, FakePlug

    broker = plug_broker

    lines, stop = [], threading.Event()
    plug = FakePlug(broker.port)
    thread = threading.Thread(target=record, args=(f"127.0.0.1:{broker.port}", PREFIX, lines.append, stop))
    thread.start()
    try:
        assert wait_for(lambda: any(m.startswith("recorder: recording") for m in lines), limit=5)
        plug.send("status/switch:0", TIMER)
        plug.send("online", b"false")
        assert wait_for(lambda: "online false" in lines, limit=5)
        assert any(m.startswith("switch:0 output=on watts=12.0") for m in lines)
        assert set(plug.seen) == {f"{PREFIX}/status/switch:0", f"{PREFIX}/online"}  # nothing of its own
    finally:
        stop.set()
        thread.join(timeout=5)
        plug.close()


def test_with_no_plug_set_the_recorder_does_nothing(monkeypatch, capsys):
    from x100_bridge.main import main

    monkeypatch.delenv("X100_PLUG_MQTT", raising=False)
    assert main(["--record-plug"]) == 0
    assert "no plug to record" in capsys.readouterr().out

