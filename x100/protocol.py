"""ViewSonic projector RS-232 / LAN control protocol.

This implements the binary protocol in ViewSonic's "Projector RS-232/LAN Control
Protocol" specification, which ViewSonic describes as applying to all of its
projectors (with per-model variations). The same packets travel over:

* RS-232 (DB-9, 115200 8N1, usually via a null-modem cable), and
* TCP port 4661 on the projector's LAN-control ("CONTROL") Ethernet port.

Every frame has the same shape::

    [type] 0x14 0x00 [len_lo] [len_hi] [payload: len bytes] [checksum]

    checksum = sum(every byte except the first one and the checksum) & 0xFF

Frame types:

    0x06  write command   payload = 0x34 c1 c2 data          (len 4)
    0x07  read command    payload = 0x34 0x00 0x00 c1 c2     (len 5)
    0x03  ACK             (no payload)
    0x05  read reply      payload = 0x00 0x00 value[n]       (value little-endian)
    0x00  "function disabled / greyed out" (no payload)

Commands marked ``documented`` below appear verbatim in ViewSonic's command
list and are checked byte-for-byte in ``tests/test_protocol.py``. Commands
marked ``inferred`` follow the documented read pattern but are not in the
published list; ``x100ctl probe`` tells you whether your unit answers them.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional

HEADER_MAGIC = b"\x14\x00"

TYPE_DISABLED = 0x00
TYPE_ACK = 0x03
TYPE_READ_REPLY = 0x05
TYPE_WRITE = 0x06
TYPE_READ = 0x07

KNOWN_FRAME_TYPES = {TYPE_DISABLED, TYPE_ACK, TYPE_READ_REPLY, TYPE_WRITE, TYPE_READ}


class ProtocolError(Exception):
    """A frame could not be parsed or failed its checksum."""


def checksum(frame_without_checksum: bytes) -> int:
    """Checksum over everything after the first byte."""
    return sum(frame_without_checksum[1:]) & 0xFF


def build_frame(frame_type: int, payload: bytes) -> bytes:
    length = len(payload)
    body = bytes([frame_type, 0x14, 0x00, length & 0xFF, (length >> 8) & 0xFF]) + payload
    return body + bytes([checksum(body)])


@dataclass(frozen=True)
class Write:
    """A write ("set") command: ``06 14 00 04 00 34 c1 c2 data cs``."""

    c1: int
    c2: int
    data: int = 0

    def encode(self) -> bytes:
        for name, v in (("c1", self.c1), ("c2", self.c2), ("data", self.data)):
            if not 0 <= v <= 0xFF:
                raise ValueError(f"{name}={v!r} is not a byte")
        return build_frame(TYPE_WRITE, bytes([0x34, self.c1, self.c2, self.data]))


@dataclass(frozen=True)
class Read:
    """A read ("get") command: ``07 14 00 05 00 34 00 00 c1 c2 cs``."""

    c1: int
    c2: int

    def encode(self) -> bytes:
        return build_frame(TYPE_READ, bytes([0x34, 0x00, 0x00, self.c1, self.c2]))


@dataclass(frozen=True)
class Frame:
    """A parsed frame received from (or sent to) the projector."""

    type: int
    payload: bytes
    raw: bytes

    @property
    def is_ack(self) -> bool:
        return self.type == TYPE_ACK

    @property
    def is_disabled(self) -> bool:
        return self.type == TYPE_DISABLED

    @property
    def is_reply(self) -> bool:
        return self.type == TYPE_READ_REPLY

    @property
    def value(self) -> int:
        """Value carried by a read reply (little-endian, after two 0x00 bytes)."""
        if not self.is_reply:
            raise ProtocolError(f"frame type 0x{self.type:02X} carries no value")
        if len(self.payload) < 3:
            raise ProtocolError(f"read reply too short: {self.raw.hex(' ')}")
        return int.from_bytes(self.payload[2:], "little")

    @property
    def value_signed(self) -> int:
        data = self.payload[2:]
        return int.from_bytes(data, "little", signed=True)


def frame_length(header: bytes) -> int:
    """Total frame length given its 5-byte header."""
    if len(header) < 5:
        raise ProtocolError("header needs 5 bytes")
    return 5 + (header[3] | (header[4] << 8)) + 1


def looks_like_header(chunk: bytes) -> bool:
    return len(chunk) >= 3 and chunk[0] in KNOWN_FRAME_TYPES and chunk[1:3] == HEADER_MAGIC


def parse_frame(data: bytes) -> Frame:
    """Parse exactly one complete frame."""
    if not looks_like_header(data[:5]):
        raise ProtocolError(f"not a ViewSonic frame: {data.hex(' ')}")
    total = frame_length(data[:5])
    if len(data) != total:
        raise ProtocolError(f"expected {total} bytes, got {len(data)}: {data.hex(' ')}")
    if checksum(data[:-1]) != data[-1]:
        raise ProtocolError(
            f"bad checksum (got 0x{data[-1]:02X}, want 0x{checksum(data[:-1]):02X}): {data.hex(' ')}"
        )
    return Frame(type=data[0], payload=bytes(data[5:-1]), raw=bytes(data))


class PowerState(str, Enum):
    OFF = "OFF"
    ON = "ON"
    WARMING = "WARMING"
    COOLING = "COOLING"
    UNKNOWN = "UNKNOWN"

    @property
    def alexa(self) -> str:
        """Alexa only knows ON/OFF: a warming projector is on, a cooling one is off."""
        return "ON" if self in (PowerState.ON, PowerState.WARMING) else "OFF"


POWER_FROM_VALUE = {
    0: PowerState.OFF,
    1: PowerState.ON,
    2: PowerState.WARMING,
    3: PowerState.COOLING,
}


# ---------------------------------------------------------------------------
# Command catalogue
# ---------------------------------------------------------------------------

# documented
POWER_ON = Write(0x11, 0x00, 0x00)
POWER_OFF = Write(0x11, 0x01, 0x00)
POWER_STATUS = Read(0x11, 0x00)

MUTE_ON = Write(0x14, 0x00, 0x01)
MUTE_OFF = Write(0x14, 0x00, 0x00)
MUTE_STATUS = Read(0x14, 0x00)

VOLUME_UP = Write(0x14, 0x01, 0x00)
VOLUME_DOWN = Write(0x14, 0x02, 0x00)
VOLUME_STATUS = Read(0x14, 0x03)

BLANK_ON = Write(0x12, 0x09, 0x01)
BLANK_OFF = Write(0x12, 0x09, 0x00)

FREEZE_ON = Write(0x13, 0x00, 0x01)
FREEZE_OFF = Write(0x13, 0x00, 0x00)

LIGHT_SOURCE_HOURS = Read(0x15, 0x01)

# inferred from the documented read pattern (read c1/c2 == write c1/c2)
SOURCE_STATUS = Read(0x13, 0x01)
BLANK_STATUS = Read(0x12, 0x09)

VOLUME_MAX = 50  # X100-4K OSD range is 0-50


def source_select(code: int) -> Write:
    """documented for HDMI 1 (0x03) and HDMI 2 (0x07)."""
    return Write(0x13, 0x01, code)


def volume_set(level: int) -> Write:
    """documented ("Volume / Write Value"); level is the OSD value 0-50."""
    if not 0 <= level <= 0xFF:
        raise ValueError("volume out of range")
    return Write(0x13, 0x2A, level)


def remote_key(code: int) -> Write:
    """Simulate a remote-control key press (documented key codes below)."""
    return Write(0x02, 0x04, code)


# Source codes ViewSonic documents. Other inputs vary by model: use
# ``x100ctl learn-inputs`` to read the codes your projector reports.
DOCUMENTED_SOURCES: Dict[str, int] = {
    "HDMI 1": 0x03,
    "HDMI 2": 0x07,
}

# Remote-key codes from ViewSonic's RS-232 command list.
REMOTE_KEYS: Dict[str, int] = {
    "menu": 0x0F,
    "exit": 0x13,
    "up": 0x0B,
    "down": 0x0C,
    "left": 0x0D,
    "right": 0x0E,
    "source": 0x04,
    "enter": 0x15,
    "auto": 0x08,
}

# X100-4K infrared remote (NEC, address bytes 0x83 0xF4) from the X100-4K user
# guide's IR control table. Whether the RS-232/LAN "remote key" command accepts
# these codes is untested -- ``x100ctl key 0x92`` is a cheap experiment.
X100_IR_CODES: Dict[str, int] = {
    "power": 0x17,
    "home": 0x16,
    "focus": 0x92,
    "source": 0x40,
    "setting": 0x30,
    "up": 0x0B,
    "down": 0x0C,
    "left": 0x0E,
    "right": 0x0F,
    "ok": 0x15,
    "return": 0x28,
    "mute": 0x14,
    "volume_up": 0x82,
    "volume_down": 0x83,
    "audio_mode": 0x9E,
}
X100_IR_ADDRESS = (0x83, 0xF4)


def parse_hex_bytes(text: str) -> bytes:
    """Accept '06 14 00 ...', '0x06,0x14,...', '061400...' and similar."""
    cleaned = text.replace(",", " ").replace("0x", " ").replace("0X", " ").replace("\\x", " ")
    parts = cleaned.split()
    if len(parts) == 1 and len(parts[0]) > 2:
        token = parts[0]
        if len(token) % 2:
            raise ValueError("odd number of hex digits")
        return bytes.fromhex(token)
    return bytes(int(p, 16) for p in parts)


def parse_int(text: str) -> int:
    """Parse '146', '0x92' or '92h'."""
    t = text.strip().lower()
    if t.endswith("h"):
        return int(t[:-1], 16)
    return int(t, 0)


def describe(frame: Optional[Frame]) -> str:
    if frame is None:
        return "no reply"
    if frame.is_ack:
        return "ACK"
    if frame.is_disabled:
        return "function disabled/greyed out"
    if frame.is_reply:
        return f"value {frame.value} (0x{frame.value:02X})"
    return f"frame type 0x{frame.type:02X}: {frame.raw.hex(' ')}"
