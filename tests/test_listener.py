"""The listener with fake engines: wake word, command window, dispatch, chimes, cooldown, and audio input.

Frames carry a marker value in every sample (WAKE, PRO, ON...), and the fake
wake detector and spotter react to those, so each test is a short script of
80 ms frames. The real engines are exercised in test_voice_e2e.py.
"""

import os
import stat
import threading
import time
import wave

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")

from x100_listener import main as lmain  # noqa: E402
from x100_listener.audio import (FRAME, FRAME_SECONDS, RATE, FrameAssembler, MicError, MicSource,  # noqa: E402
                                 WavSource, read_wav, write_wav)
from x100_listener.chimes import CHIMES, NullChimes, duration, tone  # noqa: E402
from x100_listener.client import Result  # noqa: E402
from x100_listener.commands import Phrase, build_phrases, normalise  # noqa: E402
from x100_listener.config import ListenerConfig, parse_extra_phrases  # noqa: E402
from x100_listener.listener import GAIN_MAX, ClipSaver, Listener, Phase, Settings, level_gain  # noqa: E402
from x100_listener.models import KWS_FILES, OWW_FILES, ModelError, ModelPaths, download, verify  # noqa: E402

SIL, WAKE, PRO, ON, OFF, CHAT = 0, 1000, 2000, 3000, 4000, 5000
PHRASES = {p.text: p for p in build_phrases()}


def frame(marker):
    return np.full(FRAME, marker, dtype=np.int16)


class FakeWake:
    name = "fake_jarvis"

    def __init__(self):
        self.seen = []
        self.resets = 0

    def score(self, f):
        self.seen.append(int(f[0]))
        return 0.95 if f[0] == WAKE else 0.01

    def reset(self):
        self.resets += 1


class FakeSpotter:
    """Hears "projector on/off" once it has been fed PRO and then ON or OFF."""

    def __init__(self):
        self.phrases = list(PHRASES.values())
        self.active = False
        self.fed = []
        self.heard = []

    def start(self):
        self.active, self.heard = True, []

    def feed(self, f):
        assert self.active, "fed outside a command window"
        m = int(f[0])
        self.fed.append(m)
        self.heard.append(m)
        if PRO in self.heard:
            after = self.heard[self.heard.index(PRO) + 1:]
            if ON in after:
                return PHRASES["PROJECTOR ON"]
            if OFF in after:
                return PHRASES["PROJECTOR OFF"]
        return None

    def stop(self):
        self.active = False


class FakeChimes:
    def __init__(self):
        self.played = []
        self.busy_now = False

    def play(self, kind):
        self.played.append(kind)

    def busy(self):
        return self.busy_now


class Rig:
    def __init__(self, dispatch=None, **settings):
        settings.setdefault("command_gain", False)  # the markers must reach the fake spotter unchanged
        self.wake, self.spotter, self.chimes = FakeWake(), FakeSpotter(), FakeChimes()
        self.sent = []
        self.events = []

        def ok(phrase):
            self.sent.append(phrase.ops)
            return Result(True, seconds=0.05)

        self.listener = Listener(self.wake, self.spotter, dispatch or ok, self.chimes, Settings(**settings),
                                 run=lambda job: job(), on_event=self.events.append)

    def play(self, *markers, busy=()):
        for i, m in enumerate(markers):
            self.chimes.busy_now = i in busy
            self.listener.process(frame(m))
        self.chimes.busy_now = False

    def silence(self, seconds):
        self.play(*[SIL] * int(round(seconds / FRAME_SECONDS)))

    def kinds(self):
        return [e.kind for e in self.events]


# ---------------------------------------------------------------------------- the state machine


def test_wake_then_command_dispatches_and_chimes_ok():
    rig = Rig()
    rig.play(SIL, SIL, WAKE, SIL, PRO, ON, SIL)
    assert rig.kinds() == ["wake", "command", "result"]
    assert rig.sent == [[{"op": "power", "value": "ON"}]]
    assert rig.chimes.played == ["ok"]
    assert rig.listener.phase is Phase.COOLDOWN
    command, result = rig.events[1].detail, rig.events[2].detail
    assert command["phrase"] == "projector on" and command["ops"] == "power=ON"
    assert command["after_wake"] == pytest.approx(3 * FRAME_SECONDS)
    assert result["ok"] is True and result["bridge_ms"] == 50 and result["total_ms"] >= 0


def test_off_works_the_same_way():
    rig = Rig()
    rig.play(WAKE, PRO, OFF)
    assert rig.sent == [[{"op": "power", "value": "OFF"}]]


def test_no_command_in_the_window_times_out_with_the_unclear_chime():
    rig = Rig(command_window=3.5)
    rig.play(WAKE)
    rig.silence(3.3)
    assert rig.kinds() == ["wake"]  # still listening
    rig.silence(0.4)
    assert rig.kinds() == ["wake", "timeout"]
    assert rig.events[1].t - rig.events[0].t == pytest.approx(3.5, abs=FRAME_SECONDS)
    assert rig.chimes.played == ["unclear"] and rig.sent == []
    assert not rig.spotter.active
    assert rig.wake.resets == 1  # back to a quiet room for the next "Hey Jarvis"


def test_unknown_speech_after_the_wake_word_is_ignored():
    rig = Rig()
    rig.play(WAKE, CHAT, PRO, CHAT, CHAT)  # "hey jarvis, what's the projector's... "
    rig.silence(4)
    assert rig.sent == [] and rig.kinds() == ["wake", "timeout"]


def test_the_command_window_only_opens_after_the_wake_word():
    rig = Rig()
    rig.play(PRO, ON, SIL, SIL, SIL, SIL, SIL, SIL, SIL, SIL)  # "projector on" with no "Hey Jarvis"
    assert rig.events == [] and rig.sent == [] and rig.spotter.fed == []


def test_cooldown_blocks_a_retrigger_then_lets_go():
    rig = Rig(cooldown=2.0)
    rig.play(WAKE, PRO, ON)
    rig.silence(1.0)
    rig.play(WAKE, PRO, OFF)  # inside the cooldown: ignored
    assert len(rig.sent) == 1
    rig.silence(1.2)
    rig.play(WAKE, PRO, OFF)
    assert rig.sent == [[{"op": "power", "value": "ON"}], [{"op": "power", "value": "OFF"}]]


def test_the_mic_is_ignored_while_a_chime_plays():
    rig = Rig()
    rig.play(WAKE, busy={0})  # the listener hearing its own chime
    assert rig.events == [] and rig.wake.seen[-1] == SIL  # the detector got silence, not the frame
    rig.play(WAKE, PRO, ON, busy={1, 2})  # a chime during the command window mutes that audio too
    assert rig.kinds() == ["wake"] and rig.spotter.fed[-2:] == [SIL, SIL]
    rig.play(PRO, ON)
    assert rig.kinds() == ["wake", "command", "result"]


def test_the_wake_word_rests_while_the_command_is_spotted():
    """On a Pi 3 the two engines together take longer than the 80 ms a frame lasts, so they take turns."""
    rig = Rig(cooldown=0.4)
    rig.play(SIL, WAKE, PRO, ON, SIL, SIL, SIL, SIL, SIL, SIL)
    assert rig.wake.seen == [SIL, WAKE] + [SIL] * 6  # not PRO or ON: those went to the spotter alone
    assert rig.spotter.fed == [SIL, WAKE, PRO, ON]  # the pre-roll, then the command
    assert rig.wake.resets == 1 and rig.listener.phase is Phase.IDLE


def test_the_wake_word_is_heard_again_straight_after_a_timeout():
    rig = Rig(command_window=1.0, unclear_cooldown=0.5)
    rig.play(WAKE)
    rig.silence(1.0 + 0.5 + 0.1)  # no command, then the short cooldown after the low note
    rig.play(WAKE, PRO, ON)
    assert rig.kinds() == ["wake", "timeout", "wake", "command", "result"]
    assert rig.sent == [[{"op": "power", "value": "ON"}]] and rig.wake.resets == 2


def test_preroll_catches_a_command_begun_before_the_wake_word_fired():
    # One breath: "Hey Jarvis projector on" -- the wake score can peak after "pro-" has started.
    with_preroll = Rig(preroll=0.5)
    with_preroll.play(SIL, PRO, WAKE, ON)
    assert with_preroll.sent == [[{"op": "power", "value": "ON"}]]
    assert with_preroll.spotter.fed[:3] == [SIL, PRO, WAKE]  # replayed into the spotter

    without = Rig(preroll=0.0)
    without.play(SIL, PRO, WAKE, ON)
    without.silence(4)
    assert without.sent == [] and "timeout" in without.kinds()


def test_a_failed_command_plays_the_fail_chime():
    rig = Rig(dispatch=lambda phrase: Result(False, "BRIDGE_UNREACHABLE", "http://jetson:8661: refused"))
    rig.play(WAKE, PRO, ON)
    assert rig.chimes.played == ["fail"]
    assert rig.events[-1].detail["ok"] is False and "BRIDGE_UNREACHABLE" in rig.events[-1].detail["outcome"]


def test_a_dispatcher_crash_is_contained():
    def boom(phrase):
        raise RuntimeError("bug")

    rig = Rig(dispatch=boom)
    rig.play(WAKE, PRO, ON)
    assert rig.chimes.played == ["fail"] and "LISTENER_ERROR" in rig.events[-1].detail["outcome"]
    rig.silence(2.5)
    rig.play(WAKE)  # still alive
    assert rig.kinds()[-1] == "wake"


def test_the_unclear_chime_can_be_turned_off():
    rig = Rig(unclear_chime=False)
    rig.play(WAKE)
    rig.silence(4)
    assert rig.kinds() == ["wake", "timeout"] and rig.chimes.played == []


class LevelSpotter(FakeSpotter):
    """Records how loud each frame it is fed is."""

    def __init__(self):
        super().__init__()
        self.peaks = []

    def feed(self, f):
        self.peaks.append(int(np.max(np.abs(f.astype(np.int32)))))
        return None


def test_a_quiet_command_is_brought_up_to_the_wake_words_level():
    quiet = np.full(FRAME, 1000, dtype=np.int16)      # about -30 dBFS
    wake = FakeWake()
    wake.score = lambda f: 0.95 if f[0] == 1000 else 0.0
    spotter = LevelSpotter()
    events = []
    listener = Listener(wake, spotter, lambda p: Result(True), NullChimes(), Settings(command_gain=True),
                        run=lambda job: job(), on_event=events.append)
    for f in [np.zeros(FRAME, dtype=np.int16)] * 3 + [quiet] + [np.full(FRAME, 800, dtype=np.int16)] * 2:
        listener.process(f)
    gain = 0.5 * 32768 / 1000
    assert spotter.peaks[-3:] == [round(1000 * gain), round(800 * gain), round(800 * gain)]
    assert events[0].detail["gain_db"] == pytest.approx(24.3, abs=0.1)


def test_level_gain_limits():
    loud = [np.full(FRAME, 20000, dtype=np.int16)]
    assert level_gain(loud) == 1.0                                       # never turned down
    assert level_gain([np.zeros(FRAME, dtype=np.int16)]) == 1.0           # silence isn't amplified
    assert level_gain([np.full(FRAME, 100, dtype=np.int16)]) == GAIN_MAX  # at most +30 dB
    assert level_gain([]) == 1.0


def test_the_wake_chime_is_optional():
    rig = Rig(wake_chime=True)
    rig.play(WAKE, PRO, ON)
    assert rig.chimes.played == ["wake", "ok"]


def test_dispatch_runs_off_the_audio_thread():
    started, release = threading.Event(), threading.Event()

    def slow(phrase):
        started.set()
        release.wait(5)
        return Result(True)

    listener = Listener(FakeWake(), FakeSpotter(), slow, NullChimes(), Settings(command_gain=False))  # real runner
    t0 = time.monotonic()
    for m in (WAKE, PRO, ON, SIL, SIL):
        listener.process(frame(m))
    assert time.monotonic() - t0 < 1.0 and started.wait(2)  # frames kept flowing while the bridge was slow
    release.set()


def test_clips_are_saved_and_pruned(tmp_path):
    saver = ClipSaver(str(tmp_path), keep=2)
    rig = Rig()
    rig.listener.clips = saver
    for _ in range(3):
        rig.play(SIL, WAKE, PRO, ON)
        rig.silence(2.2)
        time.sleep(0.01)  # distinct file names

    def clips():
        return sorted(f for f in os.listdir(tmp_path) if f.endswith(".wav"))

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and len(clips()) != 2:
        time.sleep(0.05)
    assert len(clips()) == 2 and all(name.endswith("-cmd-projector-on.wav") for name in clips())
    data, rate = read_wav(str(tmp_path / clips()[-1]))
    preroll = round(0.5 / FRAME_SECONDS) + 1  # 0.5 s before the wake word fired, plus that frame
    assert rate == RATE and len(data) == (preroll + 2) * FRAME  # ...then PRO and ON


# ---------------------------------------------------------------------------- phrases and settings


def test_phrases_normalise_and_extend():
    assert normalise("  Projector-on ") == "PROJECTOR ON"
    with pytest.raises(ValueError, match="numbers as words"):
        normalise("projector HDMI 2")
    phrases = {p.text: p.ops for p in build_phrases({"projector HDMI two": [{"op": "input", "value": "HDMI 2"}],
                                                      "projector on": [{"op": "key", "value": 1}]})}
    assert phrases["PROJECTOR HDMI TWO"] == [{"op": "input", "value": "HDMI 2"}]
    assert phrases["PROJECTOR ON"] == [{"op": "key", "value": 1}]  # an extra phrase can redefine a default
    assert phrases["TURN OFF THE PROJECTOR"] == [{"op": "power", "value": "OFF"}]
    assert Phrase("PROJECTOR ON", [{"op": "power", "value": "ON"}]).describe() == "power=ON"


def test_extra_phrases_setting():
    assert parse_extra_phrases('{"projector mute": {"op": "mute", "value": true}}') == \
        {"projector mute": [{"op": "mute", "value": True}]}
    for bad in ('["x"]', '{"projector mute": []}', '{"projector mute": [{"value": true}]}'):
        with pytest.raises(ValueError):
            parse_extra_phrases(bad)


def test_config_from_env(monkeypatch):
    for key, value in {"BRIDGE_URL": "http://192.168.1.50:8661/", "BRIDGE_TOKEN": "t" * 20, "MIC_DEVICE": "Jabra",
                       "MIC_CHANNEL": "1", "MIC_RATE": "48000", "WAKE_THRESHOLD": "0.6", "CHIMES": "off",
                       "KWS_THREADS": "2", "WAKE_THREADS": "3", "SAVE_CLIPS_DIR": "clips"}.items():
        monkeypatch.setenv(key, value)
    cfg = ListenerConfig.from_env()
    assert cfg.bridge_url == "http://192.168.1.50:8661" and cfg.mic_device == "Jabra"
    assert (cfg.mic_channel, cfg.mic_rate, cfg.wake_threshold, cfg.kws_threads, cfg.wake_threads) == \
        (1, 48000, 0.6, 2, 3)
    assert cfg.chimes is False and cfg.save_clips_dir == "clips" and cfg.problems() == []
    assert ListenerConfig(wake_threshold=1.5, bridge_url="jetson:8661").problems() == [
        "BRIDGE_URL must start with http:// (got 'jetson:8661')", "WAKE_THRESHOLD must be between 0 and 1"]
    assert ListenerConfig(wake_threads=0).problems() == ["WAKE_THREADS must be 1 or more"]


@pytest.mark.parametrize("cores, threads", [(1, 1), (2, 1), (3, 1), (4, 2), (8, 2)])
def test_thread_defaults_follow_the_core_count(monkeypatch, cores, threads):
    """Measured on the Pi 3 (4 cores): 2 threads fastest for both engines, 4 much slower. 2 cores: 1."""
    import x100_listener.config as config

    monkeypatch.setattr(config.os, "sched_getaffinity", lambda pid: set(range(cores)), raising=False)
    for key in ("WAKE_THREADS", "KWS_THREADS"):
        monkeypatch.delenv(key, raising=False)
    assert config.default_threads() == threads
    cfg = ListenerConfig.from_env()
    assert (cfg.wake_threads, cfg.kws_threads) == (threads, threads)
    assert (ListenerConfig().wake_threads, ListenerConfig().kws_threads) == (threads, threads)
    monkeypatch.setenv("WAKE_THREADS", "1")
    assert ListenerConfig.from_env().wake_threads == 1  # a setting always wins


# ---------------------------------------------------------------------------- audio in


class FakeStream:
    """Stands in for sounddevice.InputStream: plays an array through the callback in blocks."""

    def __init__(self, data, callback, channels, samplerate, dtype, blocksize, status=None):
        self.data, self.callback, self.blocksize, self.status = data, callback, blocksize, status
        self.active = False

    def start(self):
        self.active = True

        def run():
            for i in range(0, len(self.data), self.blocksize):
                block = self.data[i:i + self.blocksize]
                self.callback(block.reshape(len(block), -1), len(block), None, self.status)
            self.active = False

        threading.Thread(target=run, daemon=True).start()

    def stop(self):
        self.active = False

    def close(self):
        pass


def capture(data, seconds, status=None, **mic_kwargs):
    mic = MicSource(stream_factory=lambda **kw: FakeStream(data, status=status, **kw), **mic_kwargs)
    out = []
    for f in mic.frames():
        out.append(f)
        if len(out) * FRAME_SECONDS >= seconds:
            break
    mic.close()
    return np.concatenate(out), mic


def peak_hz(x):
    spectrum = np.abs(np.fft.rfft(x.astype(np.float64) * np.hanning(len(x))))
    return np.fft.rfftfreq(len(x), 1 / RATE)[int(np.argmax(spectrum))], spectrum


def test_mic_takes_the_chosen_channel_from_a_2_channel_32_bit_stream():
    """The XVF3800's USB format: 2 channels of 32-bit, speech-recognition beam on channel 1."""
    t = np.arange(RATE) / RATE
    stereo = np.stack([0.5 * np.sin(2 * np.pi * 500 * t), 0.25 * np.sin(2 * np.pi * 1000 * t)], axis=1)
    data = (stereo * 2 ** 31).astype(np.int32)
    out, mic = capture(data, 0.8, channel=1, dtype="int32")
    assert out.dtype == np.int16 and len(out) % FRAME == 0
    hz, _ = peak_hz(out)
    assert hz == pytest.approx(1000, abs=20)
    assert np.max(np.abs(out)) == pytest.approx(0.25 * 32768, rel=0.02)


def test_mic_at_48_khz_is_decimated_cleanly():
    t = np.arange(48000) / 48000
    mono = 0.4 * np.sin(2 * np.pi * 1000 * t) + 0.4 * np.sin(2 * np.pi * 12000 * t)  # 12 kHz would alias to 4 kHz
    out, mic = capture((mono * 32767).astype(np.int16), 0.8, rate=48000)
    assert mic.rate == 48000
    x = out[FRAME:]  # past the filter's start-up
    hz, spectrum = peak_hz(x)
    freqs = np.fft.rfftfreq(len(x), 1 / RATE)
    alias = spectrum[np.argmin(np.abs(freqs - 4000))]
    assert hz == pytest.approx(1000, abs=20)
    assert 20 * np.log10(alias / spectrum.max()) < -30
    assert np.max(np.abs(x)) == pytest.approx(0.4 * 32767, rel=0.1)


def test_rates_that_are_not_a_multiple_of_16k_are_refused_with_advice():
    with pytest.raises(ValueError, match="MIC_RATE"):
        FrameAssembler(44100)
    with pytest.raises(ValueError, match="channel 2"):
        FrameAssembler(16000, channels=2, channel=2)


def test_overruns_are_counted():
    class Status:
        input_overflow = True

    data = np.zeros(RATE, dtype=np.int16)
    _, mic = capture(data, 0.3, status=Status())
    assert mic.overruns > 0


def test_a_mic_that_stops_delivering_raises():
    mic = MicSource(stream_factory=lambda **kw: FakeStream(np.zeros(0, dtype=np.int16), **kw))
    with pytest.raises(MicError, match="stopped delivering"):
        for _ in mic.frames():
            pass


def test_no_microphone_at_all_is_a_mic_error(monkeypatch):
    import x100_listener.audio as audio

    class NoDevices:  # sounddevice on a Pi with nothing plugged in
        @staticmethod
        def query_devices(device=None, kind=None):
            if kind is None:
                return []
            raise RuntimeError("Error querying device -1")

    monkeypatch.setattr(audio, "_sd", lambda: NoDevices)
    with pytest.raises(MicError, match="no microphone found"):
        MicSource()


def test_the_service_waits_for_the_mic_instead_of_exiting(caplog):
    """Exiting would have systemd start it again, reloading the models (about 20 s on a Pi 3) each time."""
    import logging

    attempts, rescans = [], []

    def open_mic():
        attempts.append(1)
        if len(attempts) < 4:
            raise MicError("no input device matches 'Jabra'")
        return "the mic"

    with caplog.at_level(logging.INFO, logger="x100.listener"):
        assert lmain.wait_for_mic(open_mic, threading.Event(), every=0.01, rescan=lambda: rescans.append(1)) == \
            "the mic"
    assert len(attempts) == 4 and len(rescans) == 3  # PortAudio rescanned for newly plugged devices each time
    messages = [r.getMessage() for r in caplog.records]
    assert sum("Waiting for the mic" in m for m in messages) == 1 and any("found the mic" in m for m in messages)

    stop = threading.Event()
    threading.Timer(0.1, stop.set).start()

    def never():
        raise MicError("no mic")

    started = time.monotonic()
    assert lmain.wait_for_mic(never, stop, every=0.02, rescan=lambda: None) is None
    assert time.monotonic() - started < 2


def test_the_service_only_loads_the_models_once_it_has_a_mic(monkeypatch):
    calls = []
    monkeypatch.setattr(lmain.signal, "signal", lambda *args: None)
    monkeypatch.setattr(lmain, "wait_for_mic", lambda open_mic, stop: calls.append("mic"))  # stopped first
    monkeypatch.setattr(lmain, "load_engines", lambda cfg: calls.append("models"))
    assert lmain.run_service(ListenerConfig()) == 0
    assert calls == ["mic"]


def test_wav_files_are_resampled_and_framed(tmp_path):
    path = str(tmp_path / "stereo48k.wav")
    t = np.arange(24000) / 48000
    left, right = np.zeros_like(t), 0.5 * np.sin(2 * np.pi * 440 * t)
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes((np.stack([left, right], axis=1) * 32767).astype(np.int16).tobytes())
    src = WavSource(path, channel=1, tail_silence=0.0)
    frames = list(src.frames())
    assert all(len(f) == FRAME and f.dtype == np.int16 for f in frames)
    assert len(frames) == int(np.ceil(0.5 * RATE / FRAME))
    hz, _ = peak_hz(np.concatenate(frames)[:RATE // 2])
    assert hz == pytest.approx(440, abs=20)
    write_wav(str(tmp_path / "out.wav"), np.concatenate(frames))
    assert read_wav(str(tmp_path / "out.wav"))[1] == RATE


# ---------------------------------------------------------------------------- chimes, models, power


def test_chime_tones():
    for kind in CHIMES:
        x = tone(kind, 48000, volume=0.3)
        assert len(x) == pytest.approx(duration(kind) * 48000, abs=48 * len(CHIMES[kind]))
        assert np.max(np.abs(x)) == pytest.approx(0.3, rel=1e-3)
    assert NullChimes().busy() is False


def test_model_downloads_are_checksummed(tmp_path):
    paths = ModelPaths(str(tmp_path))
    with pytest.raises(ModelError, match="checksum mismatch"):
        download(paths, fetch=lambda url: b"not the real file", report=None)
    assert not os.path.exists(paths.oww("melspectrogram.onnx"))  # nothing half-installed
    assert set(os.path.basename(p) for p in paths.missing()) == \
        {"melspectrogram.onnx", "embedding_model.onnx", "hey_jarvis_v0.1.onnx", *KWS_FILES}
    with open(paths.oww("melspectrogram.onnx"), "wb") as fh:
        fh.write(b"tampered")
    assert len(verify(paths)) == 1 and "checksum" in verify(paths)[0]
    assert set(OWW_FILES) >= {"hey_jarvis_v0.1.onnx", "silero_vad.onnx"}


@pytest.mark.parametrize("flags, expected", [
    ("0x0", "ok"),
    ("0x50005", "UNDER-VOLTAGE NOW"),
    ("0x4", "THROTTLED NOW"),
    ("0x50000", "since boot"),
])
def test_power_supply_check(tmp_path, monkeypatch, flags, expected):
    tool = tmp_path / "vcgencmd"
    tool.write_text(f"#!/bin/sh\necho throttled={flags}\n")
    tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert expected in lmain.check_power()


def test_power_supply_check_is_skipped_off_a_pi(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert lmain.check_power() is None


def test_onnxruntime_telemetry_is_switched_off_before_it_can_load():
    """Official onnxruntime builds upload usage telemetry to Microsoft unless told not to."""
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if k != "ORT_DISABLE_TELEMETRY"}
    out = subprocess.run([sys.executable, "-c", "import os, x100_listener; print(os.environ['ORT_DISABLE_TELEMETRY'])"],
                         capture_output=True, text=True, env=env, cwd=os.path.dirname(os.path.dirname(__file__)))
    assert out.stdout.strip() == "1", out.stderr


def test_a_bridge_on_this_machine_needs_no_token():
    assert lmain.is_local_url("http://127.0.0.1:8661") and lmain.is_local_url("http://localhost:8661")
    assert lmain.is_local_url("http://[::1]:8661")
    assert not lmain.is_local_url("http://192.168.1.50:8661")
    assert not lmain.is_local_url("http://x100-pi.local:8661")


@pytest.mark.parametrize("same", [True, False])
def test_check_says_whether_the_bridge_runs_the_same_build(same, tmp_path, monkeypatch, capsys):
    import http.server
    import json

    import x100

    bridge_build = x100.BUILD if same else "0ld0ld00"

    class Bridge(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = {"ok": True, "version": "2.0.1", "build": bridge_build} if self.path == "/healthz" else \
                {"power": "OFF", "connected": True}
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Bridge)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def no_mic(*args, **kwargs):
        raise MicError("no mic in tests")

    monkeypatch.setattr(lmain, "MicSource", no_mic)
    monkeypatch.setattr(lmain, "check_power", lambda: None)
    try:
        lmain.run_check(ListenerConfig(models_dir=str(tmp_path), bridge_url=f"http://127.0.0.1:{server.server_port}",
                                       bridge_token="t"))
    finally:
        server.shutdown()
        server.server_close()
    out = capsys.readouterr().out
    assert out.startswith(x100.version_line())
    if same:
        assert f"running the same build as this listener ({x100.BUILD})" in out
    else:
        assert f"NOTE: the bridge is on build 0ld0ld00, this listener on {x100.BUILD}" in out
    assert "token accepted; projector power OFF, reachable" in out


def test_meter_and_levels():
    meter = lmain.Meter()
    for ms in (2, 3, 4, 50):
        meter.add(ms / 1000)
    assert meter.summary().startswith("4 frames, 14.8 ms average")
    assert lmain.level_dbfs(np.zeros(FRAME, dtype=np.int16)) == -140.0
    assert lmain.level_dbfs(np.full(FRAME, 32767, dtype=np.int16)) == pytest.approx(0, abs=0.01)


def test_the_speed_report():
    took = {"wake": [0.002, 0.004], "command": [0.004, 0.012], "woke": [0.019]}
    assert lmain.speed_report(took, 2.14, ListenerConfig(wake_threads=2, kws_threads=1)).splitlines() == [
        "speed (WAKE_THREADS=2, KWS_THREADS=1), per 80 ms frame:",
        "  listening for the wake word:  3 ms (4% of the time; worst 4)",
        "  listening for a command:      8 ms (10% of the time; worst 12)",
        "  the frame that woke it:       19 ms (includes replaying 0.5 s into the command spotter)",
        "  models loaded in 2.1 s",
        "  verdict: fast enough for a live mic"]
    # A clip with no wake word in it has no command lines; the verdict goes by the busiest phase.
    no_wake = lmain.speed_report({"wake": [0.068], "command": [], "woke": []}, 19.0, ListenerConfig())
    assert "command" not in no_wake and "68 ms (85% of the time" in no_wake and no_wake.endswith("but only just")
    slow = lmain.speed_report({"wake": [0.04], "command": [0.096], "woke": [0.3]}, 19.0, ListenerConfig())
    assert slow.endswith("TOO SLOW for a live mic: it would fall further and further behind")


def test_a_wav_dry_run_can_stop_early_and_reports_its_speed(tmp_path, monkeypatch, capsys):
    """`--wav clip.wav --seconds 2`: the Pi benchmark. The second wake word comes after 2 s, so isn't heard."""
    path = str(tmp_path / "clip.wav")
    write_wav(path, np.concatenate([frame(m) for m in [SIL] * 5 + [WAKE, PRO, ON] + [SIL] * 40 + [WAKE, PRO, OFF]]))
    monkeypatch.setattr(lmain, "load_engines", lambda cfg: (FakeWake(), FakeSpotter()))
    monkeypatch.setenv("COMMAND_GAIN", "false")  # the markers must reach the fake spotter unchanged
    monkeypatch.setenv("WAKE_THREADS", "2")
    monkeypatch.setenv("KWS_THREADS", "1")
    assert lmain.main(["--wav", path, "--seconds", "2"]) == 0
    out = capsys.readouterr().out
    assert 'heard "projector on"' in out and "projector off" not in out
    assert f"{path}: 2.0 s, highest wake score 0.95 at 0.48 s (threshold 0.40); 1 command(s) heard" in out
    assert "speed (WAKE_THREADS=2, KWS_THREADS=1), per 80 ms frame:" in out
    for phase in ("listening for the wake word:", "listening for a command:", "the frame that woke it:"):
        assert phase in out
    assert "verdict: fast enough for a live mic" in out
