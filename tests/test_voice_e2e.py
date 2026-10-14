"""Real speech, end to end: synthesised voices -> openWakeWord -> sherpa-onnx -> listener ->
the bridge's local API -> bridge core -> simulated projector over TCP.

Needs the listener's packages (a Python 3.11 venv with '.[listener]') plus two
folders, and is skipped without them:

    X100_TEST_MODELS   the listener's models:  MODELS_DIR=<dir> x100-listener --download-models
    X100_TEST_VOICES   Piper voices (sherpa-onnx builds) from
                       https://github.com/k2-fsa/sherpa-onnx/releases/tag/tts-models :
                       vits-piper-en_GB-vctk-medium, vits-piper-en_US-libritts_r-medium,
                       vits-piper-en_GB-alan-medium, vits-piper-en_US-lessac-medium (unpacked)

Run with -s to see the per-clip table and the latency figures. Synthesised clips
are cached (X100_TEST_CACHE, default tests/data/e2e-cache) so reruns hear exactly
the same audio; delete the folder to draw a fresh set.

Latency is "end of speech -> the projector receives the command": the audio
time from the end of the phrase to the frame on which the command is
recognised, plus the wall-clock time from there to the simulated projector
receiving the power write. A live mic adds the processing time of that last
frame, which is reported separately (it is much larger on a Pi 3 than here).
"""

from __future__ import annotations

import hashlib
import os
import statistics
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("openwakeword")
sherpa_onnx = pytest.importorskip("sherpa_onnx")

from scipy.signal import resample_poly  # noqa: E402

import x100.mock as mock_module  # noqa: E402
from x100.mock import MockServer  # noqa: E402
from x100.projector import Projector  # noqa: E402
from x100.protocol import PowerState  # noqa: E402
from x100.transport import TcpTransport  # noqa: E402
from x100_bridge.core import BridgeCore, Config  # noqa: E402
from x100_bridge.local_api import LocalApi  # noqa: E402
from x100_listener import main as lmain  # noqa: E402
from x100_listener.audio import RATE, WavSource, read_wav, write_wav  # noqa: E402
from x100_listener.chimes import NullChimes  # noqa: E402
from x100_listener.client import BridgeClient  # noqa: E402
from x100_listener.config import ListenerConfig  # noqa: E402
from x100_listener.listener import Listener  # noqa: E402
from x100_listener.models import ModelPaths  # noqa: E402

from .test_bridge import make_inputs  # noqa: E402

MODELS = os.environ.get("X100_TEST_MODELS", "")
VOICES = os.environ.get("X100_TEST_VOICES", "")
if not (MODELS and os.path.isdir(MODELS) and not ModelPaths(MODELS).missing()):
    pytest.skip("set X100_TEST_MODELS to a folder made by `x100-listener --download-models`", allow_module_level=True)
if not (VOICES and os.path.isdir(VOICES)):
    pytest.skip("set X100_TEST_VOICES to a folder of unpacked vits-piper-* voices", allow_module_level=True)

TOKEN = "e2e-token-0123456789"
CACHE = os.environ.get("X100_TEST_CACHE") or os.path.join(os.path.dirname(__file__), "data", "e2e-cache")
WRITE_ON, WRITE_OFF = "W 11 00 00", "W 11 01 00"

# voice folder -> speaker ids (the vctk and libritts voices are multi-speaker)
SPEAKERS = {
    "vits-piper-en_GB-vctk-medium": [0, 5, 12, 30, 60, 90],
    "vits-piper-en_US-libritts_r-medium": [0, 50, 120, 300, 600, 850],
    "vits-piper-en_GB-alan-medium": [0],
    "vits-piper-en_US-lessac-medium": [0],
}
POSITIVE = [
    ("Hey Jarvis, projector on.", "ON"),
    ("Hey Jarvis, projector off.", "OFF"),
    ("Hey Jarvis, turn on the projector.", "ON"),
    ("Hey Jarvis, turn the projector off.", "OFF"),
    ("Hey Jarvis projector on", "ON"),  # one breath, no pause
]
NEGATIVE = [
    "Hey Jarvis, what time is it?",          # the wake word, then no command
    "The projector is on the table.",        # command words, no wake word
    "Turn off the projector when you go.",   # the whole command, no wake word
    "Hey Travis, how are you?",              # a near miss for the wake word
]
NEAR_MISS = "Hey Travis, projector on."      # reported, not asserted: it rhymes with the real thing
BABBLE = [
    "The weather in London will be cloudy, with light rain later this afternoon.",
    "She opened the window and looked out across the garden for a while.",
    "We should leave early tomorrow to avoid the traffic on the motorway.",
    "The film starts at eight, so let's have dinner before we go.",
    "I'm not sure that's right, but we can check the map again later.",
    "Every morning he walks the dog along the river before breakfast.",
]


# ---------------------------------------------------------------------------- speech


class Voices:
    def __init__(self, root: str):
        self.root = root
        self._engines: Dict[str, object] = {}
        self._cache: Dict[tuple, np.ndarray] = {}

    def available(self) -> List[tuple]:
        return [(v, sid) for v, sids in SPEAKERS.items() if os.path.isdir(os.path.join(self.root, v)) for sid in sids]

    def _engine(self, voice: str):
        if voice not in self._engines:
            d = os.path.join(self.root, voice)
            name = voice.replace("vits-piper-", "")
            cfg = sherpa_onnx.OfflineTtsConfig(model=sherpa_onnx.OfflineTtsModelConfig(
                vits=sherpa_onnx.OfflineTtsVitsModelConfig(model=f"{d}/{name}.onnx", tokens=f"{d}/tokens.txt",
                                                           data_dir=f"{d}/espeak-ng-data"),
                num_threads=4))
            self._engines[voice] = sherpa_onnx.OfflineTts(cfg)
        return self._engines[voice]

    def say(self, text: str, voice: str, sid: int) -> np.ndarray:
        """float32 at 16 kHz, peak-normalised to -3 dBFS, trimmed of the TTS's own leading/trailing silence."""
        key = (text, voice, sid)
        if key not in self._cache:
            path = os.path.join(CACHE, f"{voice}-{sid}-{hashlib.sha1(text.encode()).hexdigest()[:12]}.wav")
            if os.path.isfile(path):
                data, _ = read_wav(path)
                self._cache[key] = data.astype(np.float32) / 32768.0
            else:
                audio = self._engine(voice).generate(text, sid=sid, speed=1.0)
                x = np.asarray(audio.samples, dtype=np.float32)
                if audio.sample_rate != RATE:
                    x = resample_poly(x.astype(np.float64), RATE, audio.sample_rate).astype(np.float32)
                start, end = speech_bounds(x)
                x = x[start:end]
                x = (x / np.max(np.abs(x)) * 0.707).astype(np.float32)
                os.makedirs(CACHE, exist_ok=True)
                write_wav(path, np.round(x * 32767).astype(np.int16))
                self._cache[key] = np.round(x * 32767).astype(np.float32) / 32768.0
        return self._cache[key]


def speech_bounds(x: np.ndarray, floor_db: float = -40.0):
    """First and last sample of speech: 10 ms windows within ``floor_db`` of the loudest one."""
    win = RATE // 100
    n = len(x) // win
    rms = np.sqrt(np.mean(x[:n * win].reshape(n, win) ** 2, axis=1) + 1e-12)
    active = np.nonzero(20 * np.log10(rms / rms.max()) > floor_db)[0]
    return int(active[0] * win), int((active[-1] + 1) * win)


def pink_noise(n: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    spectrum = np.fft.rfft(rng.standard_normal(n))
    f = np.fft.rfftfreq(n, 1 / RATE)
    spectrum[1:] /= np.sqrt(f[1:])
    spectrum[0] = 0
    x = np.fft.irfft(spectrum, n)
    return (x / np.sqrt(np.mean(x ** 2))).astype(np.float32)


def film_noise(n: int, voices: Voices, seed: int) -> np.ndarray:
    """Pink noise plus overlapping unrelated speech: a stand-in for a film's soundtrack."""
    rng = np.random.default_rng(seed)
    out = 0.5 * pink_noise(n, seed)
    pool = voices.available()
    for k in range(3):  # three talkers, staggered
        pos = int(rng.integers(0, RATE))
        while pos < n:
            voice, sid = pool[int(rng.integers(len(pool)))]
            s = voices.say(BABBLE[int(rng.integers(len(BABBLE)))], voice, sid)
            seg = s[:n - pos]
            out[pos:pos + len(seg)] += seg / np.sqrt(np.mean(s ** 2)) * (0.6 if k else 0.8)
            pos += len(s) + int(rng.integers(RATE // 4, RATE))
    return (out / np.sqrt(np.mean(out ** 2))).astype(np.float32)


def session(speech: np.ndarray, lead: float = 1.0, tail: float = 3.0, snr_db: Optional[float] = None,
            noise: Optional[np.ndarray] = None, level_db: float = -3.0):
    """Lead silence + speech + tail, optionally under noise at ``snr_db``; returns (int16, speech_end_s)."""
    pad = lambda s: np.zeros(int(s * RATE), dtype=np.float32)  # noqa: E731
    gain = 10 ** (level_db / 20) / max(1e-9, float(np.max(np.abs(speech))))
    x = np.concatenate([pad(lead), speech * gain, pad(tail)])
    if snr_db is not None:
        assert noise is not None and len(noise) >= len(x)
        speech_rms = float(np.sqrt(np.mean((speech * gain) ** 2)))
        x = x + noise[:len(x)] * speech_rms / 10 ** (snr_db / 20)
    peak = float(np.max(np.abs(x)))
    if peak > 0.99:
        x = x / peak * 0.99
    return np.clip(np.round(x * 32767), -32768, 32767).astype(np.int16), lead + len(speech) / RATE


# ---------------------------------------------------------------------------- the rig


@dataclass
class Outcome:
    label: str
    expected: Optional[str]          # "ON" / "OFF", or None when nothing should happen
    woke: bool = False
    wake_score: float = 0.0
    commands: List[str] = field(default_factory=list)
    detect_s: Optional[float] = None   # end of speech -> command recognised (audio time)
    bridge_s: Optional[float] = None   # command recognised -> projector received the switch (wall clock)
    frame_ms_max: float = 0.0

    @property
    def acted(self) -> bool:
        return bool(self.commands)

    @property
    def correct(self) -> bool:
        return self.commands == [self.expected] if self.expected else not self.commands

    @property
    def total_s(self) -> Optional[float]:
        return None if self.detect_s is None or self.bridge_s is None else self.detect_s + self.bridge_s


class Rig:
    """The listener exactly as x100-listener builds it from default settings, minus the mic and chimes."""

    def __init__(self, srv: MockServer, api: LocalApi, writes: List[tuple]):
        self.srv, self.writes = srv, writes
        self.cfg = ListenerConfig(models_dir=MODELS)
        self.wake, self.spotter = lmain.load_engines(self.cfg)
        self.settings = lmain.settings_from(self.cfg)
        self.client = BridgeClient(api.url, TOKEN, timeout=8)

    def run(self, label: str, audio: np.ndarray, speech_end: float, expected: Optional[str]) -> Outcome:
        out = Outcome(label, expected)
        with self.srv.state.lock:  # start from the opposite state, so a correct command must write
            self.srv.state.power = PowerState.ON if expected == "OFF" else PowerState.OFF
            self.srv.state.until = 0.0
        self.writes.clear()
        self.wake.reset()
        heard_at: Dict[str, float] = {}

        def on_event(e):
            if e.kind == "wake":
                out.woke = True
            elif e.kind == "command":
                heard_at["wall"] = time.monotonic()
                if out.detect_s is None:
                    out.detect_s = e.t - speech_end
                out.commands.append(e.detail["ops"].split("=")[1])

        dispatching = [0.0]

        def dispatch(phrase):
            # The real listener dispatches on its own thread; here it's inline, so keep its
            # time out of the per-frame figure.
            started = time.perf_counter()
            try:
                return self.client.command(phrase.ops)
            finally:
                dispatching[0] += time.perf_counter() - started

        listener = Listener(self.wake, self.spotter, dispatch, NullChimes(), self.settings,
                            run=lambda job: job(), on_event=on_event)
        for frame in WavSource(audio, tail_silence=0.0).frames():
            started, before = time.perf_counter(), dispatching[0]
            listener.process(frame)
            spent = time.perf_counter() - started - (dispatching[0] - before)
            out.frame_ms_max = max(out.frame_ms_max, spent * 1000)
            out.wake_score = max(out.wake_score, listener.last_score)
        first_write = next((t for t, entry in self.writes if entry.startswith("W ")), None)
        if out.commands and first_write is not None:
            out.bridge_s = first_write - heard_at["wall"]  # until the projector receives the switch itself
        want = {"ON": WRITE_ON, "OFF": WRITE_OFF}.get(expected or "")
        if out.correct and want:
            assert [w for _, w in self.writes if w.startswith("W 11")] == [want], self.writes
        return out


@pytest.fixture(scope="module")
def voices():
    v = Voices(VOICES)
    if not v.available():
        pytest.skip(f"no known voices in {VOICES}")
    return v


@pytest.fixture(scope="module")
def rig():
    writes: List[tuple] = []
    real = mock_module.handle_frame

    def recording(state, data):
        """The simulated projector's frame handler, noting when each write arrives."""
        with state.lock:
            before = len(state.log)
            reply = real(state, data)
            writes.extend((time.monotonic(), entry) for entry in state.log[before:])
        return reply

    mock_module.handle_frame = recording
    srv = MockServer(warmup=0.05, cooldown=0.05).start()
    cfg = Config(poll_interval=30, fast_poll_interval=5, heartbeat_interval=600, volume_step_delay=0)
    # The production pause between commands (X100_MIN_GAP, 0.1 s): the bridge reads the power state
    # before switching, so it counts in the latency.
    core = BridgeCore(cfg, Projector(TcpTransport("127.0.0.1", srv.port, timeout=1.0, min_gap=0.1)), make_inputs(),
                      publish=lambda doc: None)
    core.start()
    api = LocalApi(core, "127.0.0.1:0", TOKEN, timeout=8).start()
    try:
        yield Rig(srv, api, writes)
    finally:
        api.stop()
        core.stop()
        srv.stop()
        mock_module.handle_frame = real


def table(outcomes: List[Outcome]) -> str:
    lines = [f"{'clip':58} {'want':>4} {'wake':>5} {'heard':>9} {'detect':>7} {'bridge':>7} {'total':>6}"]
    for o in outcomes:
        fmt = lambda v: f"{v * 1000:5.0f}ms" if v is not None else "      -"  # noqa: E731
        lines.append(f"{o.label[:58]:58} {o.expected or '-':>4} {o.wake_score:5.2f} "
                     f"{','.join(o.commands) or '-':>9} {fmt(o.detect_s):>7} {fmt(o.bridge_s):>7} "
                     f"{fmt(o.total_s):>6}{'' if o.correct else '   <-- WRONG'}")
    return "\n".join(lines)


def latency_summary(outcomes: List[Outcome]) -> str:
    totals = sorted(o.total_s for o in outcomes if o.total_s is not None)
    detects = sorted(o.detect_s for o in outcomes if o.detect_s is not None)
    bridges = sorted(o.bridge_s for o in outcomes if o.bridge_s is not None)
    if not totals:
        return "no commands acted on"
    p90 = lambda xs: xs[min(len(xs) - 1, int(round(0.9 * (len(xs) - 1))))]  # noqa: E731
    return (f"end of speech -> projector gets the command: median {statistics.median(totals) * 1000:.0f} ms, "
            f"90th percentile {p90(totals) * 1000:.0f} ms, worst {totals[-1] * 1000:.0f} ms "
            f"(recognition {statistics.median(detects) * 1000:.0f} ms median, "
            f"bridge {statistics.median(bridges) * 1000:.0f} ms median)")


# ---------------------------------------------------------------------------- tests


def test_clean_commands_act_quickly(rig, voices):
    outcomes = []
    for voice, sid in voices.available():
        for text, want in POSITIVE:
            audio, end = session(voices.say(text, voice, sid))
            outcomes.append(rig.run(f"{voice.replace('vits-piper-', '')}#{sid}: {text}", audio, end, want))
    print("\n" + table(outcomes) + "\n" + latency_summary(outcomes))
    print(f"slowest frame here: {max(o.frame_ms_max for o in outcomes):.1f} ms (budget 80 ms per frame)")
    hit = sum(o.correct for o in outcomes)
    print(f"clean: {hit}/{len(outcomes)} right, wake word missed on {sum(not o.woke for o in outcomes)}")
    # Measured 60-64/70 on independent sets of synthesised clips; most misses are the wake word
    # on some synthetic British voices (see the README).
    assert hit / len(outcomes) >= 0.8, f"only {hit}/{len(outcomes)} clean commands were right"
    assert not any(o.acted and not o.correct for o in outcomes), "a command did the wrong thing"
    totals = [o.total_s for o in outcomes if o.total_s is not None]
    # About 0.37 s of recognition plus the bridge's cautious 0.1 s pause between commands.
    assert statistics.median(totals) < 0.6, latency_summary(outcomes)


def test_quiet_far_away_speech_still_works(rig, voices):
    """-30 dBFS peak: roughly a quiet talker a few metres from a speakerphone, before its gain control."""
    outcomes = []
    for voice, sid in voices.available()[::2]:
        for text, want in POSITIVE[:2]:
            audio, end = session(voices.say(text, voice, sid), level_db=-30)
            outcomes.append(rig.run(f"quiet {voice.replace('vits-piper-', '')}#{sid}: {text}", audio, end, want))
    print("\n" + table(outcomes))
    hit = sum(o.correct for o in outcomes)
    print(f"quiet: {hit}/{len(outcomes)} right")
    assert hit / len(outcomes) >= 0.7, f"only {hit}/{len(outcomes)} quiet commands were right"
    assert not any(o.acted and not o.correct for o in outcomes), "a command did the wrong thing"


def test_nothing_else_acts(rig, voices):
    outcomes = []
    for voice, sid in voices.available():
        for text in NEGATIVE:
            audio, end = session(voices.say(text, voice, sid))
            outcomes.append(rig.run(f"{voice.replace('vits-piper-', '')}#{sid}: {text}", audio, end, None))
    # A minute of film-like noise and chatter, and of silence.
    noise = film_noise(60 * RATE, voices, seed=7)
    outcomes.append(rig.run("60 s of film-like noise and chatter", np.clip(noise * 0.1 * 32767, -32768, 32767)
                            .astype(np.int16), 0.0, None))
    outcomes.append(rig.run("60 s of silence", np.zeros(60 * RATE, dtype=np.int16), 0.0, None))
    near = [rig.run(f"{voice.replace('vits-piper-', '')}#{sid}: {NEAR_MISS}", *session(voices.say(NEAR_MISS, voice, sid)),
                    None) for voice, sid in voices.available()]
    print("\n" + table(outcomes) + "\n" + table(near)[table(near).index("\n") + 1:])
    woke = sum(o.woke for o in outcomes)
    print(f"wake word fired on {woke} of {len(outcomes)} non-commands (harmless without a command after it)")
    print(f"near miss \"{NEAR_MISS}\" acted {sum(o.acted for o in near)} of {len(near)} times")
    wrong = [o.label for o in outcomes if o.acted]
    assert not wrong, f"acted on: {wrong}"


@pytest.mark.parametrize("snr_db", [20, 15, 10])
def test_commands_over_film_noise(rig, voices, snr_db):
    noise = film_noise(20 * RATE, voices, seed=snr_db)
    outcomes = []
    for i, (voice, sid) in enumerate(voices.available()):
        text, want = POSITIVE[i % len(POSITIVE)]
        audio, end = session(voices.say(text, voice, sid), snr_db=snr_db, noise=noise[i * RATE // 2:])
        outcomes.append(rig.run(f"{snr_db} dB {voice.replace('vits-piper-', '')}#{sid}: {text}", audio, end, want))
    print("\n" + table(outcomes) + "\n" + latency_summary(outcomes))
    hit = sum(o.correct for o in outcomes)
    swapped = [o.label for o in outcomes if o.acted and not o.correct]
    print(f"{snr_db} dB SNR: {hit}/{len(outcomes)} right, {len(swapped)} heard as the other command")
    assert hit / len(outcomes) >= {20: 0.8, 15: 0.75, 10: 0.5}[snr_db], f"{snr_db} dB: only {hit}/{len(outcomes)}"
    # Under heavy noise "on" and "off" can be confused (the "f" is masked). Not at 20 dB.
    assert len(swapped) <= {20: 0, 15: 1, 10: 4}[snr_db], swapped


def test_the_same_audio_scores_the_same_every_time(rig, voices):
    """openWakeWord seeds its history with random noise; the listener seeds it with silence instead."""
    voice, sid = voices.available()[0]
    audio, end = session(voices.say("Hey Jarvis, projector on.", voice, sid), lead=0.0)
    first, second = (rig.run("repeat", audio, end, "ON") for _ in range(2))
    assert first.wake_score == second.wake_score and first.commands == second.commands


def test_a_reset_wake_detector_scores_like_a_new_one(rig, voices):
    """reset() reuses the quiet-room history worked out at load, instead of recomputing it (about a
    second's work on a Pi 3), so after any audio at all it must behave exactly like a new detector."""
    from x100_listener.wake import OpenWakeWordDetector

    voice, sid = next((v for v in voices.available() if "lessac" in v[0]), voices.available()[0])
    command, _ = session(voices.say("Hey Jarvis, projector on.", voice, sid))
    chatter = np.clip(film_noise(5 * RATE, voices, seed=3) * 0.2 * 32767, -32768, 32767).astype(np.int16)

    def scores(detector, audio):
        return [detector.score(f) for f in WavSource(audio, tail_silence=0.0).frames()]

    used = rig.wake
    scores(used, chatter)
    used.reset()
    fresh = OpenWakeWordDetector(ModelPaths(MODELS), rig.cfg.wake_model)
    after_reset, new = scores(used, command), scores(fresh, command)
    assert after_reset == new
    assert max(new) > rig.cfg.wake_threshold  # ...and the clip really does wake it


def test_a_shorter_raw_audio_history_scores_the_same(rig, voices):
    """openWakeWord keeps 10 s of raw audio and copies it all on every frame, but only reads the last
    0.11 s; the listener keeps 1 s. Over 12 s of chatter (enough to fill openWakeWord's own buffer),
    then a command, not a single score may change."""
    from collections import deque

    from x100_listener.wake import RAW_HISTORY, OpenWakeWordDetector

    voice, sid = next((v for v in voices.available() if "lessac" in v[0]), voices.available()[0])
    command, _ = session(voices.say("Hey Jarvis, projector on.", voice, sid))
    chatter = np.clip(film_noise(12 * RATE, voices, seed=5) * 0.2 * 32767, -32768, 32767).astype(np.int16)
    audio = np.concatenate([chatter, command])
    ours, theirs = OpenWakeWordDetector(ModelPaths(MODELS)), OpenWakeWordDetector(ModelPaths(MODELS))
    theirs.model.preprocessor.raw_data_buffer = deque(maxlen=16000 * 10)  # as openWakeWord makes it
    assert ours.model.preprocessor.raw_data_buffer.maxlen == RAW_HISTORY
    ours_scores, their_scores = ([d.score(f) for f in WavSource(audio, tail_silence=0.0).frames()]
                                 for d in (ours, theirs))
    assert ours_scores == their_scores
    assert max(ours_scores) > rig.cfg.wake_threshold


def test_wake_threads_change_the_speed_not_the_scores(rig, voices):
    from x100_listener.wake import OpenWakeWordDetector

    voice, sid = next((v for v in voices.available() if "lessac" in v[0]), voices.available()[0])
    audio, _ = session(voices.say("Hey Jarvis, projector off.", voice, sid))
    runs = [[d.score(f) for f in WavSource(audio, tail_silence=0.0).frames()]
            for d in (OpenWakeWordDetector(ModelPaths(MODELS), threads=n) for n in (1, 2))]
    assert runs[0] == pytest.approx(runs[1], abs=1e-4)


def test_extra_wake_threads_sleep_between_frames(rig, voices):
    """With spin-waiting on (openWakeWord's way), 2 threads used 236% of a Pi 3 core and 112% of a PC's
    for the same work as 1 thread at 5%. Paced like a live mic, they must now sleep between frames."""
    from x100_listener.wake import OpenWakeWordDetector

    detector = OpenWakeWordDetector(ModelPaths(MODELS), threads=2)
    for session_ in (detector.model.preprocessor.melspec_model, detector.model.preprocessor.embedding_model):
        options = session_.get_session_options()
        assert options.intra_op_num_threads == 2
        assert options.get_session_config_entry("session.intra_op.allow_spinning") == "0"
    voice, sid = voices.available()[0]
    audio, _ = session(voices.say("Hey Jarvis, projector off.", voice, sid))
    frames = list(WavSource(audio, tail_silence=0.0).frames())[:30]  # 2.4 s
    wall, cpu, due = time.monotonic(), time.process_time(), time.monotonic()
    for f in frames:
        detector.score(f)
        due += 0.08
        time.sleep(max(0.0, due - time.monotonic()))
    share = (time.process_time() - cpu) / (time.monotonic() - wall)
    assert share < 0.6, f"{share:.0%} of a core for one wake word paced in real time"


def test_wav_dry_run_from_the_command_line(rig, voices, tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    voice, sid = next((v for v in voices.available() if "lessac" in v[0]), voices.available()[0])
    audio, _ = session(voices.say("Hey Jarvis, projector off.", voice, sid))
    path = tmp_path / "clip.wav"
    write_wav(str(path), audio)
    env = dict(os.environ, MODELS_DIR=MODELS)
    proc = subprocess.run([sys.executable, "-m", "x100_listener", "--wav", str(path)], capture_output=True,
                          text=True, timeout=120, env=env, cwd=Path(__file__).resolve().parents[1])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert 'heard "projector off"' in proc.stdout and "1 command(s) heard" in proc.stdout
