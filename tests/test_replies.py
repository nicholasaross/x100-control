"""Spoken replies (x100_listener/replies.py): the bridge says in a word why it refused, the listener picks
what to say, and the clips are made, named, loaded and played. A stand-in voice is used here: the real one
is only fetched by --make-replies. Needs numpy and scipy, as the listener does.
"""

import hashlib
import io
import os
import re
import tarfile

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")

from x100.mock import MockServer  # noqa: E402
from x100_bridge.core import BridgeError  # noqa: E402
from x100_bridge.local_api import LocalApi  # noqa: E402
from x100_listener import models  # noqa: E402
from x100_listener import replies  # noqa: E402
from x100_listener.chimes import CHIMES, chime_for, pick_sound, tone  # noqa: E402
from x100_listener.client import BridgeClient, Result  # noqa: E402
from x100_listener.config import ListenerConfig  # noqa: E402
from x100_listener.models import VOICE_MODEL, VOICE_NAME, ModelError, ModelPaths, download_voice  # noqa: E402
from x100_listener.replies import GRUFF, LINES, clip_name, gruffen, load_clips, make_replies, reply_for  # noqa: E402

from .test_power_quirks import ON, POWER_CUT, bridge, wait_for  # noqa: E402

OFF = [{"op": "power", "value": "OFF"}]
TOKEN = "0123456789abcdef0123456789abcdef"
RATE = 22050


def stand_in(text, speed, f0=120.0):
    """A stand-in voice: a buzz at f0, 50 ms per character at speed 1."""
    t = np.arange(int(RATE * 0.05 * len(text) / speed)) / RATE
    return RATE, (0.3 * sum(np.sin(2 * np.pi * f0 * h * t) / h for h in (1, 2, 3))).astype(np.float32)


def main_pitch(audio, rate):
    spectrum = np.abs(np.fft.rfft(audio))
    freqs = np.fft.rfftfreq(len(audio), 1 / rate)
    band = (freqs > 50) & (freqs < 400)
    return float(freqs[band][np.argmax(spectrum[band])])


# ---------------------------------------------------------------------------- what to say


def test_the_bridge_says_in_a_word_why_it_refused():
    with MockServer(warmup=1.2, cooldown=0.4, quirks=True) as srv:
        core = bridge(srv, **POWER_CUT)
        try:
            core.plug_status(False, 0.0, "button")
            assert core.submit_and_wait(ON)["reason"] == "plug_off"
            core.plug_status(True, 1.0, "button")
            assert wait_for(core._quiet)
            assert core.submit_and_wait(ON)["reason"] == "booting"
            assert wait_for(lambda: not core._quiet() and core.state.connected, limit=4)
            with srv.state.lock:
                srv.state.hung = True
            with LocalApi(core, "127.0.0.1:0", TOKEN, timeout=8) as api:
                result = BridgeClient(api.url, TOKEN, timeout=10).command(ON)
            assert not result.ok and result.reason == "not_started"
            assert reply_for(result, ON) == "not_started"
        finally:
            core.stop()


@pytest.mark.parametrize("result, ops, said", [
    (Result(True), ON, "ok"),
    (Result(True, deferred=True), OFF, "queued_off"),
    (Result(True, deferred=True), ON, "queued_on"),
    (Result(True, deferred=True), [{"op": "input", "value": "HDMI 2"}], "queued"),
    (Result(False, "ENDPOINT_UNREACHABLE", "plug off", reason="plug_off"), ON, "plug_off"),
    (Result(False, "ENDPOINT_UNREACHABLE", "an older bridge"), ON, "unreachable"),
    (Result(False, "BRIDGE_UNREACHABLE", "refused"), ON, "bridge_down"),
    (Result(False, "NOT_IN_OPERATION", "a newer bridge", reason="something_new"), ON, "fail"),
    (Result(False, "UNAUTHORIZED", "wrong token"), ON, "fail"),
])
def test_what_the_listener_says_for_each_answer(result, ops, said):
    assert reply_for(result, ops) == said


def test_every_reason_the_bridge_gives_has_a_line():
    named = set(re.findall(r'"(\w+)"', BridgeError.__doc__))
    assert named and named <= set(LINES)


def test_without_its_clip_a_reply_plays_the_chime_it_stands_in_for():
    tones = {k: tone(k, 16000) for k in CHIMES}
    clip = np.ones(100, dtype=np.float32)
    assert pick_sound("plug_off", {"plug_off": clip}, tones) is clip
    assert pick_sound("plug_off", {}, tones) is tones["fail"]
    assert pick_sound("queued_off", {}, tones) is tones["queued"]
    assert pick_sound("ok", {"plug_off": clip}, tones) is tones["ok"]
    assert chime_for("unclear") == "unclear" and chime_for("anything_else") == "fail"


def test_the_setting(monkeypatch):
    assert ListenerConfig.from_env().replies == "voice"
    monkeypatch.setenv("REPLIES", "Chimes")
    assert ListenerConfig.from_env().replies == "chimes" and not ListenerConfig.from_env().problems()
    monkeypatch.setenv("REPLIES", "kazoo")
    assert any("REPLIES" in p for p in ListenerConfig.from_env().problems())


# ---------------------------------------------------------------------------- the clips


def test_gruff_is_lower_at_much_the_same_pace():
    rate, plain = stand_in("x" * 40, 1.0)
    rate, gruff = gruffen(stand_in, "x" * 40)
    assert len(gruff) / rate == pytest.approx(len(plain) / rate / GRUFF.tempo, rel=0.02)
    assert main_pitch(gruff, rate) == pytest.approx(120 / 2 ** (GRUFF.semitones / 12), rel=0.03)
    assert np.max(np.abs(gruff)) == pytest.approx(0.9, abs=0.01)
    with pytest.raises(ValueError):
        gruffen(lambda text, speed: (RATE, np.zeros(10, dtype=np.float32)), "silence")


def test_the_clips_are_made_once_and_remade_when_a_line_changes(tmp_path, monkeypatch):
    paths = ModelPaths(str(tmp_path))
    said = []

    def voice(text, speed):
        said.append(text)
        return stand_in(text, speed)

    assert make_replies(paths, voice, report=None) == len(LINES) == len(said)
    assert sorted(os.listdir(paths.replies_dir)) == sorted(clip_name(k) for k in LINES)
    assert make_replies(paths, voice, report=None) == 0 and len(said) == len(LINES)  # all there already
    stale = os.path.join(paths.replies_dir, "fail-0000000000.wav")
    open(stale, "wb").close()
    monkeypatch.setitem(replies.LINES, "fail", "Nope.")
    assert make_replies(paths, voice, report=None) == 1 and said[-1] == "Nope."
    assert sorted(os.listdir(paths.replies_dir)) == sorted(clip_name(k) for k in LINES)  # old ones gone


def test_the_clips_play_at_the_speakers_rate_and_volume(tmp_path):
    paths = ModelPaths(str(tmp_path))
    make_replies(paths, stand_in, report=None)
    clips = load_clips(paths, 48000, peak=0.6)
    assert set(clips) == set(LINES)
    for kind, clip in clips.items():
        rate, made = gruffen(stand_in, LINES[kind])
        assert len(clip) == pytest.approx(len(made) * 48000 / rate, abs=2)
        assert np.max(np.abs(clip)) == pytest.approx(0.6, abs=0.001)
    os.unlink(os.path.join(paths.replies_dir, clip_name("plug_off")))
    assert "plug_off" not in load_clips(paths, 16000, peak=0.6) and replies.missing(paths) == ["plug_off"]


# ---------------------------------------------------------------------------- fetching the voice


def voice_archive(*entries):
    """A tar.bz2 like the voice's. Each entry: (name, bytes) for a file, (name, None) for a folder, or a
    TarInfo of some other type."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:bz2") as tar:
        for entry in entries:
            if isinstance(entry, tarfile.TarInfo):
                tar.addfile(entry)
                continue
            name, data = entry
            info = tarfile.TarInfo(name)
            if data is None:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


GOOD = [(VOICE_NAME, None), (f"{VOICE_NAME}/{VOICE_MODEL}", b"model"), (f"{VOICE_NAME}/tokens.txt", b"tokens"),
        (f"{VOICE_NAME}/espeak-ng-data", None), (f"{VOICE_NAME}/espeak-ng-data/en_dict", b"dict")]


@pytest.fixture
def fake_voice(monkeypatch):
    monkeypatch.setattr(models, "VOICE_FILES", {VOICE_MODEL: hashlib.sha256(b"model").hexdigest(),
                                                "tokens.txt": hashlib.sha256(b"tokens").hexdigest()})

    def serve(data):
        monkeypatch.setattr(models, "VOICE_SHA256", hashlib.sha256(data).hexdigest())
        return lambda url, out: out.write(data)

    return serve


def test_the_voice_is_checked_and_unpacked_once(tmp_path, fake_voice):
    paths = ModelPaths(str(tmp_path))
    download_voice(paths, fake_voice(voice_archive(*GOOD)), report=None)
    with open(paths.voice("espeak-ng-data/en_dict"), "rb") as fh:
        assert fh.read() == b"dict"
    assert models.have_voice(paths) and os.listdir(tmp_path) == ["voice"]  # nothing left lying about

    def no_fetching(url, out):
        raise AssertionError("it had the voice already")

    download_voice(paths, no_fetching, report=None)


def test_a_voice_that_doesnt_match_its_checksum_is_refused(tmp_path, fake_voice):
    paths = ModelPaths(str(tmp_path))
    fetch = fake_voice(voice_archive(*GOOD))
    models.VOICE_SHA256 = "0" * 64  # monkeypatched by fake_voice, so put back afterwards
    with pytest.raises(ModelError, match="checksum"):
        download_voice(paths, fetch, report=None)
    assert os.listdir(tmp_path) == []


@pytest.mark.parametrize("bad", [
    (f"{VOICE_NAME}/../escaped", b"x"),
    ("elsewhere/file", b"x"),
    "link",
])
def test_an_archive_that_reaches_outside_its_folder_is_refused(tmp_path, fake_voice, bad):
    if bad == "link":
        bad = tarfile.TarInfo(f"{VOICE_NAME}/espeak-ng-data/link")
        bad.type, bad.linkname = tarfile.SYMTYPE, "/etc/passwd"
    paths = ModelPaths(str(tmp_path / "models"))
    with pytest.raises(ModelError, match="unexpected entry"):
        download_voice(paths, fake_voice(voice_archive(*GOOD, bad)), report=None)
    assert os.listdir(tmp_path) == ["models"] and os.listdir(tmp_path / "models") == []
