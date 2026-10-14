"""Every byte string here is copied from ViewSonic's "Projector RS-232/LAN Control
Protocol" command list. If an encoder drifts, these fail."""

import pytest

from x100 import protocol as p


def hx(s: str) -> bytes:
    return bytes(int(x, 16) for x in s.split())


DOCUMENTED_COMMANDS = [
    (p.POWER_ON, "06 14 00 04 00 34 11 00 00 5D"),
    (p.POWER_OFF, "06 14 00 04 00 34 11 01 00 5E"),
    (p.POWER_STATUS, "07 14 00 05 00 34 00 00 11 00 5E"),
    (p.BLANK_ON, "06 14 00 04 00 34 12 09 01 68"),
    (p.BLANK_OFF, "06 14 00 04 00 34 12 09 00 67"),
    (p.source_select(0x03), "06 14 00 04 00 34 13 01 03 63"),  # HDMI 1
    (p.source_select(0x07), "06 14 00 04 00 34 13 01 07 67"),  # HDMI 2
    (p.MUTE_ON, "06 14 00 04 00 34 14 00 01 61"),
    (p.MUTE_OFF, "06 14 00 04 00 34 14 00 00 60"),
    (p.MUTE_STATUS, "07 14 00 05 00 34 00 00 14 00 61"),
    (p.VOLUME_UP, "06 14 00 04 00 34 14 01 00 61"),
    (p.VOLUME_DOWN, "06 14 00 04 00 34 14 02 00 62"),
    (p.volume_set(0x11), "06 14 00 04 00 34 13 2A 11 9A"),
    (p.VOLUME_STATUS, "07 14 00 05 00 34 00 00 14 03 64"),
    (p.LIGHT_SOURCE_HOURS, "07 14 00 05 00 34 00 00 15 01 63"),
    (p.FREEZE_ON, "06 14 00 04 00 34 13 00 01 60"),
    (p.FREEZE_OFF, "06 14 00 04 00 34 13 00 00 5F"),
    (p.remote_key(p.REMOTE_KEYS["menu"]), "06 14 00 04 00 34 02 04 0F 61"),
    (p.remote_key(p.REMOTE_KEYS["exit"]), "06 14 00 04 00 34 02 04 13 65"),
    (p.remote_key(p.REMOTE_KEYS["up"]), "06 14 00 04 00 34 02 04 0B 5D"),
    (p.remote_key(p.REMOTE_KEYS["down"]), "06 14 00 04 00 34 02 04 0C 5E"),
    (p.remote_key(p.REMOTE_KEYS["left"]), "06 14 00 04 00 34 02 04 0D 5F"),
    (p.remote_key(p.REMOTE_KEYS["right"]), "06 14 00 04 00 34 02 04 0E 60"),
    (p.remote_key(p.REMOTE_KEYS["source"]), "06 14 00 04 00 34 02 04 04 56"),
    (p.remote_key(p.REMOTE_KEYS["enter"]), "06 14 00 04 00 34 02 04 15 67"),
    (p.remote_key(p.REMOTE_KEYS["auto"]), "06 14 00 04 00 34 02 04 08 5A"),
]


@pytest.mark.parametrize("cmd,expected", DOCUMENTED_COMMANDS, ids=[e for _, e in DOCUMENTED_COMMANDS])
def test_encoder_matches_viewsonic_command_list(cmd, expected):
    assert cmd.encode() == hx(expected)


@pytest.mark.parametrize("_cmd,expected", DOCUMENTED_COMMANDS)
def test_documented_commands_parse_with_valid_checksums(_cmd, expected):
    frame = p.parse_frame(hx(expected))
    assert frame.raw == hx(expected)


DOCUMENTED_REPLIES = [
    ("03 14 00 00 00 14", "ack", None),
    ("05 14 00 03 00 00 00 00 17", "reply", 0),  # power off
    ("05 14 00 03 00 00 00 01 18", "reply", 1),  # power on
    ("05 14 00 03 00 00 00 02 19", "reply", 2),  # warm up
    ("05 14 00 03 00 00 00 03 1A", "reply", 3),  # cool down
    ("05 14 00 03 00 00 00 32 49", "reply", 50),
    ("05 14 00 03 00 00 00 E9 00", "reply", 233),  # checksum wraps
    ("05 14 00 06 00 00 00 B8 0B 00 00 DD", "reply", 3000),  # light-source hours
    ("05 14 00 04 00 00 00 64 00 7C", "reply", 100),  # 2-byte format
    ("00 14 00 00 00 14", "disabled", None),
]


@pytest.mark.parametrize("raw,kind,value", DOCUMENTED_REPLIES)
def test_reply_parsing(raw, kind, value):
    f = p.parse_frame(hx(raw))
    assert {"ack": f.is_ack, "reply": f.is_reply, "disabled": f.is_disabled}[kind]
    if value is not None:
        assert f.value == value


def test_two_byte_negative_value():
    f = p.parse_frame(hx("05 14 00 04 00 00 00 18 FF 2F"))
    assert f.value_signed == -232


def test_bad_checksum_rejected():
    with pytest.raises(p.ProtocolError):
        p.parse_frame(hx("06 14 00 04 00 34 11 00 00 5C"))


def test_power_state_alexa_mapping():
    assert p.PowerState.WARMING.alexa == "ON"
    assert p.PowerState.COOLING.alexa == "OFF"


@pytest.mark.parametrize("text", ["06 14 00 04", "0x06,0x14,0x00,0x04", "06140004", "\\x06\\x14\\x00\\x04"])
def test_parse_hex_bytes(text):
    assert p.parse_hex_bytes(text) == hx("06 14 00 04")


def test_parse_int():
    assert p.parse_int("0x92") == 0x92 == p.parse_int("92h") == p.parse_int("146")
