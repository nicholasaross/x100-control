"""A simulated X100-4K for tests and dry runs.

Speaks the ViewSonic binary protocol on one TCP port and (optionally) PJLink on
another, with realistic-ish behaviour: warm-up and cool-down periods, commands
refused while in standby or warming up, mute only while showing an input.

``quirks=True`` (``--x100-quirks``) behaves like the real X100-4K as traced over
RS-232: its power query says ON in standby and while starting up, in standby it
answers only volume and mute besides, while starting up it answers nothing else
for most of the warm-up, and it doesn't answer "off" when it's already off.

Run it standalone to rehearse the whole stack before touching the projector::

    python -m x100.mock --port 4661 --pjlink-port 4352     # LAN control
    python -m x100.mock --pty                              # RS-232: prints a serial device to use
"""

from __future__ import annotations

import argparse
import logging
import socket
import socketserver
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .protocol import (
    TYPE_ACK,
    TYPE_DISABLED,
    TYPE_READ,
    TYPE_READ_REPLY,
    TYPE_WRITE,
    PowerState,
    ProtocolError,
    build_frame,
    frame_length,
    looks_like_header,
    parse_frame,
)

log = logging.getLogger("x100.mock")

# Mock-only source codes beyond the two ViewSonic documents. Your projector's
# real codes may differ -- that's what `x100ctl learn-inputs` is for.
MOCK_SOURCES: Dict[int, str] = {
    0x03: "HDMI 1",
    0x07: "HDMI 2",
    0x09: "HDMI 3",
    0x0E: "HDMI 4",
    0x00: "Computer In",
    0x1A: "Home",
}
MOCK_PJLINK_INPUTS: Dict[int, str] = {0x03: "31", 0x07: "32", 0x09: "33", 0x0E: "34", 0x00: "11", 0x1A: "51"}


@dataclass
class MockState:
    warmup: float = 3.0
    cooldown: float = 2.0
    power: PowerState = PowerState.OFF
    until: float = 0.0
    source: int = 0x03
    volume: int = 20
    muted: bool = False
    blank: bool = False
    hours: int = 1234
    quirks: bool = False     # behave like the real X100-4K (see quirky_reply)
    drop_power_on: int = 0   # ack this many power-on commands but ignore them
    started: float = 0.0     # when the current warm-up began
    # The real X100-4K, turned off 66 s after "on" (just after its picture appeared), went to standby
    # but then acknowledged "on" without starting, even from its remote, until it was unplugged.
    # Turned off within this many seconds of starting, the simulator hangs the same way.
    hang_if_off_within: float = 0.0
    hung: bool = False       # cleared by unplugging it: MockState.unplug()
    # After a power cut the real one answers only the power query for about 30 s while its main
    # processor boots, then gives odd answers for a while. Asked anything else during that time (as
    # the bridge did, polling), it hung every time: in standby, it then won't start from anything.
    # Left alone, it started fine.
    powered: bool = True             # False: switched off at the wall, it answers nothing at all
    restored_at: float = 0.0         # when it was last switched back on at the wall
    boot_seconds: float = 0.0        # after power returns, only the power query answers this long
    hang_if_asked_while_booting: bool = False  # ...and any other question then hangs it
    drop_replies: int = 0            # say nothing to this many requests (a missed reply)
    sources: Dict[int, str] = field(default_factory=lambda: dict(MOCK_SOURCES))
    keys: List[int] = field(default_factory=list)
    log: List[str] = field(default_factory=list)
    lock: threading.RLock = field(default_factory=threading.RLock)

    def tick(self) -> None:
        now = time.monotonic()
        if self.power == PowerState.WARMING and now >= self.until:
            self.power = PowerState.ON
        elif self.power == PowerState.COOLING and now >= self.until:
            self.power = PowerState.OFF

    # Shared by both protocol front-ends --------------------------------------
    def turn_on(self) -> None:
        self.tick()
        if self.drop_power_on > 0:
            self.drop_power_on -= 1
            return
        if self.hung:
            return
        if self.power == PowerState.OFF:
            self.power = PowerState.WARMING
            self.started = time.monotonic()
            self.until = self.started + self.warmup
            self.muted = False

    def turn_off(self) -> None:
        self.tick()
        if self.power == PowerState.ON:
            if time.monotonic() - self.started < self.hang_if_off_within:
                self.hung = True
            self.power = PowerState.COOLING
            self.until = time.monotonic() + self.cooldown

    def power_cut(self) -> None:
        """Switched off at the wall: silent until power_restored(), and no longer hung."""
        self.powered, self.hung, self.power = False, False, PowerState.OFF

    def power_restored(self) -> None:
        self.powered, self.restored_at = True, time.monotonic()

    def booting(self) -> bool:
        return bool(self.restored_at) and time.monotonic() - self.restored_at < self.boot_seconds

    def unplug(self) -> None:
        """Off at the wall and straight back on: the only cure for the hang."""
        self.power_cut()
        self.power_restored()


def _reply(value: int, width: int = 1) -> bytes:
    return build_frame(TYPE_READ_REPLY, b"\x00\x00" + value.to_bytes(width, "little"))


ACK = build_frame(TYPE_ACK, b"")
DISABLED = build_frame(TYPE_DISABLED, b"")

POWER_VALUE = {PowerState.OFF: 0, PowerState.ON: 1, PowerState.WARMING: 2, PowerState.COOLING: 3}


SILENCE = b""


def quirky_reply(state: MockState, write: bool, c1: int, c2: int) -> Optional[bytes]:
    """The X100-4K's ways, or None to answer like a by-the-book projector.

    Phases while starting up, as fractions of the warm-up: only the power query answers, then
    volume, then everything (input 0x00 at first), a moment that looks like standby, then on.
    """
    power, is_power = state.power, (c1, c2) in ((0x11, 0x00), (0x11, 0x01))
    if write and (c1, c2) == (0x11, 0x01) and power in (PowerState.OFF, PowerState.COOLING):
        return SILENCE  # already off: it doesn't answer "off"
    if write and is_power:
        return None
    if not write and (c1, c2) == (0x11, 0x00):
        return _reply(1)  # "on", whatever it's doing
    volume_or_mute = not write and (c1, c2) in ((0x14, 0x03), (0x14, 0x00))
    if power in (PowerState.OFF, PowerState.COOLING):
        return None if volume_or_mute else SILENCE  # standby: only volume and mute still answer
    if power == PowerState.WARMING:
        f = (time.monotonic() - state.started) / max(state.warmup, 1e-6)
        if f < 0.6:
            return SILENCE
        if f < 0.75 or 0.85 <= f < 0.9:
            return None if volume_or_mute and (f >= 0.85 or (c1, c2) == (0x14, 0x03)) else SILENCE
        if not write and (c1, c2) == (0x13, 0x01):
            return _reply(0x00 if f < 0.85 else state.source)
        return None if not write else SILENCE
    return None


def handle_frame(state: MockState, data: bytes) -> bytes:
    """Return the projector's reply to one request frame (empty: it says nothing)."""
    frame = parse_frame(data)
    with state.lock:
        if not state.powered:
            return SILENCE  # switched off at the wall
        if state.drop_replies > 0:
            state.drop_replies -= 1
            return SILENCE
        if state.booting() and len(frame.payload) in (4, 5) and frame.payload[0] == 0x34:
            write = frame.type == TYPE_WRITE
            c1, c2 = (frame.payload[1], frame.payload[2]) if write else (frame.payload[3], frame.payload[4])
            if not write and (c1, c2) == (0x11, 0x00):
                state.log.append("R 11 00 (booting)")
                return _reply(1)  # its standby processor answers this one
            state.log.append(("W" if write else "R") + f" {c1:02X} {c2:02X} (booting)")
            if state.hang_if_asked_while_booting:
                state.hung = True
            return SILENCE
        state.tick()
        on = state.power == PowerState.ON
        if state.quirks and len(frame.payload) in (4, 5) and frame.payload[0] == 0x34:
            write = frame.type == TYPE_WRITE
            c1, c2 = (frame.payload[1], frame.payload[2]) if write else (frame.payload[3], frame.payload[4])
            special = quirky_reply(state, write, c1, c2)
            if special is not None:
                state.log.append(("W" if write else "R") + f" {c1:02X} {c2:02X}" + (" (silent)" if not special else ""))
                return special
            if not write:
                on = True  # a read it answers at all, it answers as if on (volume in standby, say)
        if frame.type == TYPE_WRITE and len(frame.payload) == 4 and frame.payload[0] == 0x34:
            c1, c2, d = frame.payload[1], frame.payload[2], frame.payload[3]
            state.log.append(f"W {c1:02X} {c2:02X} {d:02X}")
            if (c1, c2) == (0x11, 0x00):
                state.turn_on()
                return ACK
            if (c1, c2) == (0x11, 0x01):
                state.turn_off()
                return ACK
            if not on:
                return DISABLED
            if (c1, c2) == (0x13, 0x01):
                if d not in state.sources:
                    return DISABLED
                state.source = d
                state.muted = False
                return ACK
            if (c1, c2) == (0x14, 0x00):
                state.muted = bool(d)
                return ACK
            if (c1, c2) == (0x14, 0x01):
                state.volume = min(50, state.volume + 1)
                return ACK
            if (c1, c2) == (0x14, 0x02):
                state.volume = max(0, state.volume - 1)
                return ACK
            if (c1, c2) == (0x13, 0x2A):
                if d > 50:
                    return DISABLED
                state.volume = d
                return ACK
            if (c1, c2) == (0x12, 0x09):
                state.blank = bool(d)
                return ACK
            if (c1, c2) == (0x02, 0x04):
                state.keys.append(d)
                return ACK
            if (c1, c2) == (0x13, 0x00):
                return ACK
            return DISABLED
        if frame.type == TYPE_READ and len(frame.payload) == 5 and frame.payload[0] == 0x34:
            c1, c2 = frame.payload[3], frame.payload[4]
            state.log.append(f"R {c1:02X} {c2:02X}")
            if (c1, c2) == (0x11, 0x00):
                return _reply(POWER_VALUE[state.power])
            if (c1, c2) == (0x15, 0x01):
                return _reply(state.hours, width=4)
            if not on:
                return DISABLED
            if (c1, c2) == (0x13, 0x01):
                return _reply(state.source)
            if (c1, c2) == (0x14, 0x00):
                return _reply(int(state.muted))
            if (c1, c2) == (0x14, 0x03):
                return _reply(state.volume)
            if (c1, c2) == (0x12, 0x09):
                return _reply(int(state.blank))
            return DISABLED
        return DISABLED


class _ViewSonicHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server: "MockServer" = self.server.mock  # type: ignore[attr-defined]
        sock: socket.socket = self.request
        with server.connections_lock:
            server.connections.add(sock)
        try:
            self._serve(server, sock)
        finally:
            with server.connections_lock:
                server.connections.discard(sock)

    def _serve(self, server: "MockServer", sock: socket.socket) -> None:
        buf = b""
        served = 0
        while not server.stopping.is_set():
            try:
                sock.settimeout(0.5)
                chunk = sock.recv(256)
            except socket.timeout:
                continue
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while len(buf) >= 5:
                if not looks_like_header(buf[:5]):
                    buf = buf[1:]
                    continue
                total = frame_length(buf[:5])
                if len(buf) < total:
                    break
                data, buf = buf[:total], buf[total:]
                try:
                    reply = handle_frame(server.state, data)
                except ProtocolError as exc:
                    log.info("ignoring bad frame: %s", exc)
                    continue
                if server.one_way or not reply:
                    continue
                if server.reply_delay:
                    time.sleep(server.reply_delay)
                try:
                    sock.sendall(server.junk_prefix + reply)
                except OSError:
                    return
                served += 1
                if server.close_after and served >= server.close_after:
                    return  # projector drops the session (tests reconnects)


class _PJLinkHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        server: "MockServer" = self.server.mock  # type: ignore[attr-defined]
        state = server.state
        import hashlib

        seed = "498e4a67"
        if server.pjlink_password:
            self.wfile.write(f"PJLINK 1 {seed}\r".encode())
            expected = hashlib.md5((seed + server.pjlink_password).encode()).hexdigest()
        else:
            self.wfile.write(b"PJLINK 0\r")
            expected = ""
        first = True
        buf = b""
        while True:
            chunk = self.request.recv(256)
            if not chunk:
                return
            buf += chunk
            while b"\r" in buf:
                line_b, buf = buf.split(b"\r", 1)
                line = line_b.decode("ascii", "replace")
                if first and expected:
                    if not line.startswith(expected):
                        self.wfile.write(b"PJLINK ERRA\r")
                        return
                    line = line[len(expected):]
                first = False
                self.wfile.write((self._answer(state, line) + "\r").encode())

    @staticmethod
    def _answer(state: MockState, line: str) -> str:
        if not line.startswith("%1") or len(line) < 7:
            return "%1XXXX=ERR1"
        body, arg = line[2:6], line[7:]
        with state.lock:
            state.tick()
            if body == "POWR":
                if arg == "?":
                    return "%1POWR=" + {PowerState.OFF: "0", PowerState.ON: "1",
                                        PowerState.COOLING: "2", PowerState.WARMING: "3"}[state.power]
                if arg == "1":
                    state.turn_on()
                    return "%1POWR=OK"
                if arg == "0":
                    state.turn_off()
                    return "%1POWR=OK"
                return "%1POWR=ERR2"
            if body == "INPT" and arg == "?":
                if state.power != PowerState.ON:
                    return "%1INPT=ERR3"
                return "%1INPT=" + MOCK_PJLINK_INPUTS.get(state.source, "31")
            if body == "AVMT" and arg == "?":
                return "%1AVMT=" + ("21" if state.muted else "30")
            static = {"NAME": "X100-4K MOCK", "INF1": "ViewSonic", "INF2": "X100-4K", "CLSS": "1",
                      "INST": " ".join(sorted(set(MOCK_PJLINK_INPUTS.values())))}
            if body in static and arg == "?":
                return f"%1{body}={static[body]}"
        return f"%1{body}=ERR1"


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class MockServer:
    """Start with .start(); ports are available as .port / .pjlink_port."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0, pjlink_port: Optional[int] = None,
                 warmup: float = 3.0, cooldown: float = 2.0, one_way: bool = False,
                 pjlink_password: Optional[str] = None, reply_delay: float = 0.0,
                 close_after: int = 0, junk_prefix: bytes = b"", quirks: bool = False):
        self.state = MockState(warmup=warmup, cooldown=cooldown, quirks=quirks)
        self.close_after = close_after
        self.junk_prefix = junk_prefix
        self.one_way = one_way
        self.reply_delay = reply_delay
        self.pjlink_password = pjlink_password
        self.stopping = threading.Event()
        self.connections: set = set()  # open sessions, cut at once by stop(): the power went off
        self.connections_lock = threading.Lock()
        self._vs = _Server((host, port), _ViewSonicHandler)
        self._vs.mock = self  # type: ignore[attr-defined]
        self._pj = None
        if pjlink_port is not None:
            self._pj = _Server((host, pjlink_port), _PJLinkHandler)
            self._pj.mock = self  # type: ignore[attr-defined]
        self._threads: List[threading.Thread] = []

    @property
    def port(self) -> int:
        return self._vs.server_address[1]

    @property
    def pjlink_port(self) -> Optional[int]:
        return self._pj.server_address[1] if self._pj else None

    def start(self) -> "MockServer":
        for srv in filter(None, (self._vs, self._pj)):
            t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
            t.start()
            self._threads.append(t)
        return self

    def stop(self) -> None:
        self.stopping.set()
        with self.connections_lock:
            for sock in list(self.connections):
                try:
                    sock.shutdown(socket.SHUT_RDWR)  # like a pulled plug: no last reply on the way out
                except OSError:
                    pass
        for srv in filter(None, (self._vs, self._pj)):
            srv.shutdown()
            srv.server_close()

    def __enter__(self) -> "MockServer":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


class MockSerial:
    """The same simulated projector on a pseudo-terminal, standing in for its RS-232 port.

    ``path`` is a serial device (``/dev/pts/N``) to open exactly like ``/dev/ttyUSB0``:
    ``SerialTransport(mock.path)`` or ``X100_SERIAL=<path>``. Linux and macOS only.
    """

    def __init__(self, warmup: float = 3.0, cooldown: float = 2.0, reply_delay: float = 0.0,
                 junk_prefix: bytes = b"", quirks: bool = False):
        import os
        import pty
        import tty

        self.state = MockState(warmup=warmup, cooldown=cooldown, quirks=quirks)
        self.reply_delay = reply_delay
        self.junk_prefix = junk_prefix
        self.stopping = threading.Event()
        self._master, self._slave = pty.openpty()
        tty.setraw(self._slave)  # no echo or line editing: replies must not come back as requests
        self.path = os.ttyname(self._slave)
        self._thread: Optional[threading.Thread] = None

    def start(self) -> "MockSerial":
        self._thread = threading.Thread(target=self._serve, name="mock-serial", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        import os

        self.stopping.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        for fd in (self._master, self._slave):
            try:
                os.close(fd)
            except OSError:
                pass

    def __enter__(self) -> "MockSerial":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    def _serve(self) -> None:
        import os
        import select

        buf = b""
        while not self.stopping.is_set():
            try:
                ready, _, _ = select.select([self._master], [], [], 0.1)
                if not ready:
                    continue
                chunk = os.read(self._master, 256)
            except OSError:
                return  # closed by stop()
            buf += chunk
            while len(buf) >= 5:
                if not looks_like_header(buf[:5]):
                    buf = buf[1:]
                    continue
                total = frame_length(buf[:5])
                if len(buf) < total:
                    break
                data, buf = buf[:total], buf[total:]
                try:
                    reply = handle_frame(self.state, data)
                except ProtocolError as exc:
                    log.info("ignoring bad frame: %s", exc)
                    continue
                if not reply:
                    continue
                if self.reply_delay:
                    time.sleep(self.reply_delay)
                try:
                    os.write(self._master, self.junk_prefix + reply)
                except OSError:
                    return


def main(argv: Optional[List[str]] = None) -> None:
    ap = argparse.ArgumentParser(description="Simulated ViewSonic X100-4K for dry runs")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=4661)
    ap.add_argument("--pjlink-port", type=int, default=None)
    ap.add_argument("--pjlink-password", default=None)
    ap.add_argument("--warmup", type=float, default=8.0)
    ap.add_argument("--cooldown", type=float, default=4.0)
    ap.add_argument("--one-way", action="store_true", help="never reply (simulate a write-only LAN port)")
    ap.add_argument("--pty", action="store_true",
                    help="simulate the RS-232 port instead: prints a serial device for X100_SERIAL")
    ap.add_argument("--x100-quirks", action="store_true",
                    help="behave like the real X100-4K: its power query says ON in standby too")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    srv: "MockServer | MockSerial"
    if args.pty:
        srv = MockSerial(warmup=args.warmup, cooldown=args.cooldown, quirks=args.x100_quirks).start()
        log.info("mock X100-4K on serial device %s  (X100_SERIAL=%s)", srv.path, srv.path)
    else:
        srv = MockServer(host=args.host, port=args.port, pjlink_port=args.pjlink_port,
                         warmup=args.warmup, cooldown=args.cooldown, one_way=args.one_way,
                         pjlink_password=args.pjlink_password, quirks=args.x100_quirks).start()
        log.info("mock X100-4K on %s:%d%s", args.host, srv.port,
                 f", PJLink on {srv.pjlink_port}" if srv.pjlink_port else "")
    last = None
    try:
        while True:
            time.sleep(0.5)
            with srv.state.lock:
                srv.state.tick()
                snap = (srv.state.power.value, MOCK_SOURCES.get(srv.state.source), srv.state.volume, srv.state.muted)
            if snap != last:
                log.info("power=%s input=%s volume=%s muted=%s", *snap)
                last = snap
    except KeyboardInterrupt:
        pass
    finally:
        srv.stop()


if __name__ == "__main__":
    main()
