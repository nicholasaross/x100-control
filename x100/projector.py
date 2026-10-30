"""High-level projector client on top of the ViewSonic binary protocol."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

from . import protocol as p
from .protocol import Frame, PowerState, Read, Write
from .transport import (
    DEFAULT_BAUD,
    DEFAULT_TCP_PORT,
    ReplyTimeout,
    SerialTransport,
    TcpTransport,
    Transport,
    TransportError,
)

log = logging.getLogger(__name__)


class ProjectorError(Exception):
    """Base class for projector-side failures."""


class Unreachable(ProjectorError):
    """No connection, or the projector stopped answering."""


class FunctionDisabled(ProjectorError):
    """The projector rejected the command as unavailable right now
    (e.g. mute with no input signal, or anything but power while in standby)."""


class UnexpectedReply(ProjectorError):
    pass


class NotSupported(ProjectorError):
    """The operation needs a reply but the link is configured as one-way."""


@dataclass
class PowerReading:
    """What the projector answered about its power, and what that adds up to."""

    state: PowerState                 # the conclusion
    reported: PowerState              # what the power query itself said
    source: Optional[int] = None      # read on the way, when it answered
    volume: Optional[int] = None
    muted: Optional[bool] = None

    @property
    def quirky(self) -> bool:
        """The power query said "on" but the projector isn't on: the X100-4K in standby or starting up."""
        return self.reported == PowerState.ON and self.state != PowerState.ON

    def describe(self) -> str:
        if not self.quirky:
            return self.state.value
        if self.state == PowerState.OFF:
            return "OFF (standby: its power query says ON, but it doesn't answer about its input)"
        return "WARMING (starting up: it answers only the power query so far)"


class Projector:
    """Talks to one projector. Thread-safe (the transport serialises exchanges)."""

    def __init__(self, transport: Transport, one_way: bool = False, probe_timeout: float = 0.6):
        self.transport = transport
        self.one_way = one_way
        self.probe_timeout = probe_timeout  # how long a powered-on projector may take to answer a probe

    # -- low level ------------------------------------------------------------
    def exchange(self, packet: bytes, expect_reply: bool = True, timeout: Optional[float] = None,
                 retry: bool = True) -> Optional[Frame]:
        try:
            return self.transport.transact(packet, expect_reply=expect_reply, timeout=timeout, retry=retry)
        except ReplyTimeout as exc:
            raise Unreachable(f"{self.transport.describe()}: {exc}") from exc
        except TransportError as exc:
            raise Unreachable(str(exc)) from exc

    def write(self, cmd: Write) -> None:
        frame = self.exchange(cmd.encode(), expect_reply=not self.one_way)
        if self.one_way:
            return
        assert frame is not None
        if frame.is_ack:
            return
        if frame.is_disabled:
            raise FunctionDisabled(f"projector refused {cmd}")
        raise UnexpectedReply(f"unexpected reply to {cmd}: {frame.raw.hex(' ')}")

    def read(self, cmd: Read, timeout: Optional[float] = None, retry: bool = True) -> int:
        if self.one_way:
            raise NotSupported("status reads need a two-way link")
        frame = self.exchange(cmd.encode(), timeout=timeout, retry=retry)
        assert frame is not None
        if frame.is_reply:
            return frame.value
        if frame.is_disabled:
            raise FunctionDisabled(f"projector refused {cmd}")
        raise UnexpectedReply(f"unexpected reply to {cmd}: {frame.raw.hex(' ')}")

    def close(self) -> None:
        self.transport.close()

    # -- power ----------------------------------------------------------------
    def power_on(self) -> None:
        self.write(p.POWER_ON)

    def power_off(self) -> None:
        self.write(p.POWER_OFF)

    def power(self) -> PowerState:
        """The power query's own answer. On the X100-4K that's ON in standby too: see power_reading."""
        value = self.read(p.POWER_STATUS)
        state = p.POWER_FROM_VALUE.get(value)
        if state is None:
            log.warning("unknown power status value %d", value)
            return PowerState.UNKNOWN
        return state

    def power_reading(self) -> PowerReading:
        """The power state, allowing for projectors whose power query always says "on".

        The X100-4K answers "on" in standby and while it starts up, as well as when it's on.
        Which other questions it answers tells the three apart. When it's on, it answers about
        its input. In standby it doesn't, but it still answers volume and mute. While it starts
        up (about a minute), it answers only the power query. Projectors that report standby,
        warming up and cooling down themselves cost just the one query.
        """
        reported = self.power()
        if reported != PowerState.ON:
            return PowerReading(reported, reported)
        try:
            return PowerReading(PowerState.ON, reported, source=self.probe(p.SOURCE_STATUS))
        except FunctionDisabled:
            return PowerReading(PowerState.ON, reported)  # it answered: running, just without an input
        except Unreachable:
            pass
        try:
            volume = self.probe(p.VOLUME_STATUS)
            muted = bool(self.probe(p.MUTE_STATUS))
        except (Unreachable, FunctionDisabled):
            return PowerReading(PowerState.WARMING, reported)
        return PowerReading(PowerState.OFF, reported, volume=volume, muted=muted)

    def power_state(self) -> PowerState:
        return self.power_reading().state

    def probe(self, cmd: Read) -> int:
        """A read where silence is an answer: a short wait, no retry."""
        return self.read(cmd, timeout=self.probe_timeout, retry=False)

    def power_probe(self, timeout: Optional[float] = None) -> int:
        """Just the power query, once, with a short wait unless told otherwise. The X100-4K answers it even
        while its main processor boots after a power cut, when any other question can hang it."""
        return self.read(p.POWER_STATUS, timeout=timeout or self.probe_timeout, retry=False)

    def answers_volume(self) -> bool:
        """Whether it answers about its volume at once. The X100-4K does in standby and stops the
        moment it starts up, so straight after "on" this says whether the "on" took."""
        try:
            self.probe(p.VOLUME_STATUS)
            return True
        except (Unreachable, FunctionDisabled):
            return False

    # -- input ----------------------------------------------------------------
    def select_source(self, code: int) -> None:
        self.write(p.source_select(code))

    def source(self) -> int:
        return self.read(p.SOURCE_STATUS)

    # -- audio ----------------------------------------------------------------
    def set_volume(self, level: int) -> None:
        if not 0 <= level <= p.VOLUME_MAX:
            raise ValueError(f"volume must be 0-{p.VOLUME_MAX}")
        self.write(p.volume_set(level))

    def volume(self) -> int:
        return self.read(p.VOLUME_STATUS)

    def volume_up(self) -> None:
        self.write(p.VOLUME_UP)

    def volume_down(self) -> None:
        self.write(p.VOLUME_DOWN)

    def set_mute(self, muted: bool) -> None:
        self.write(p.MUTE_ON if muted else p.MUTE_OFF)

    def muted(self) -> bool:
        return bool(self.read(p.MUTE_STATUS))

    # -- misc -------------------------------------------------------------------
    def key(self, code: int) -> None:
        self.write(p.remote_key(code))

    def set_blank(self, blank: bool) -> None:
        self.write(p.BLANK_ON if blank else p.BLANK_OFF)

    def light_source_hours(self) -> int:
        return self.read(p.LIGHT_SOURCE_HOURS)


def make_transport(host: Optional[str] = None, port: int = DEFAULT_TCP_PORT, serial: Optional[str] = None,
                   baud: int = DEFAULT_BAUD, timeout: float = 2.0, min_gap: float = 0.1) -> Transport:
    """``min_gap`` is the pause kept between commands (X100_MIN_GAP); 0.1 s is a cautious default."""
    if serial:
        return SerialTransport(serial, baudrate=baud, timeout=timeout, min_gap=min_gap)
    if host:
        return TcpTransport(host, port=port, timeout=timeout, min_gap=min_gap)
    raise ValueError("set a projector host (X100_HOST) or serial device (X100_SERIAL)")


def projector_from_env(one_way: Optional[bool] = None) -> Projector:
    """Build a Projector from X100_* environment variables (shared by CLI and bridge)."""
    serial = os.environ.get("X100_SERIAL") or None
    host = os.environ.get("X100_HOST") or None
    transport = make_transport(
        host=host,
        port=int(os.environ.get("X100_PORT", DEFAULT_TCP_PORT)),
        serial=serial,
        baud=int(os.environ.get("X100_BAUD", DEFAULT_BAUD)),
        timeout=float(os.environ.get("X100_TIMEOUT", "2.0")),
        min_gap=float(os.environ.get("X100_MIN_GAP", "0.1")),
    )
    if one_way is None:
        one_way = os.environ.get("X100_ONE_WAY", "false").lower() in ("1", "true", "yes", "on")
    return Projector(transport, one_way=one_way)
