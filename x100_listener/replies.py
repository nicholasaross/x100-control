"""Spoken replies: what happened, said out loud in a gruff voice, instead of a beep.

Each reply is a short clip, made once by ``x100-listener --make-replies`` (install.sh runs it). The voice
(models.py: Piper's semaine voice, as its gloomy "Obadiah") says each line, which is then pitched lower and
roughened up. The clips go in models/replies/, each named after a hash of everything that made it, so a
changed line, voice or effect gets a fresh clip. A reply with no clip falls back to its chime (chimes.py),
as all of them do with REPLIES=chimes.

"ok" keeps its two rising notes, and "unclear" its low note: they're heard most, and films set the low
note off now and then.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import wave
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .client import Result
from .models import VOICE_MODEL, VOICE_NAME, ModelPaths, download_voice

log = logging.getLogger("x100.listener.replies")

# What to say, by the bridge's reason (BridgeError in x100_bridge/core.py) or the listener's own.
LINES: Dict[str, str] = {
    "queued_off": "Righto. It'll go off in a minute or two.",       # held after a start: it waits
    "queued_on": "Righto. It'll come on in a few seconds.",          # just switched off: it waits
    "plug_off": "The plug's switched off, so it's got no power. Not my fault, mate.",
    "plug_checking": "The plug's just restarted. Try again in a few seconds.",
    "booting": "The power's only just come back. Give it a few minutes.",
    "not_started": "It didn't start. It might need switching off at the wall.",
    "just_started": "It's only just been started. Give it a minute.",
    "looks_on": "Can't tell if it's on, mate. Use the remote this time.",
    "unreachable": "Can't reach the projector. Is it plugged in?",
    "projector_off": "It's switched off, mate.",
    "refused": "The projector said no.",
    "bridge_down": "Can't reach the bridge. Is it running?",
    "fail": "That didn't work. Have a look at the log.",
}
SPEAKER = 2  # the semaine voice's speakers: prudence 0, spike 1, obadiah 2, poppy 3


@dataclass(frozen=True)
class Gruff:
    semitones: float = 3.5  # this much lower, at the same pace: a bigger voice, not a slower one
    tempo: float = 0.95     # and a touch slower
    drive: float = 2.0      # gravel: soft clipping
    growl: float = 0.18     # a rough flutter, at GROWL_HZ


GRUFF = Gruff()
GROWL_HZ = 31.0
PEAK = 0.9

Synth = Callable[[str, float], Tuple[int, np.ndarray]]  # (text, speed) -> (sample rate, audio)


def reply_for(result: Result, ops: Sequence[Dict[str, Any]]) -> str:
    """What to play for the bridge's answer: a chime's name ("ok"), or a spoken reply's."""
    if result.ok:
        if not result.deferred:
            return "ok"
        power = next((str(o.get("value")).upper() for o in ops if o.get("op") == "power"), "")
        return {"OFF": "queued_off", "ON": "queued_on"}.get(power, "queued")
    if result.reason in LINES:
        return result.reason
    if result.code == "BRIDGE_UNREACHABLE":
        return "bridge_down"
    if result.code == "ENDPOINT_UNREACHABLE":
        return "unreachable"
    return "fail"


def clip_name(kind: str, line: Optional[str] = None) -> str:
    made_of = f"{VOICE_NAME}|{SPEAKER}|{GRUFF}|{GROWL_HZ}|{line if line is not None else LINES[kind]}"
    return f"{kind}-{hashlib.sha1(made_of.encode()).hexdigest()[:10]}.wav"


def clip_path(paths: ModelPaths, kind: str) -> str:
    return os.path.join(paths.replies_dir, clip_name(kind))


def missing(paths: ModelPaths) -> List[str]:
    return [kind for kind in LINES if not os.path.isfile(clip_path(paths, kind))]


def gruffen(synth: Synth, text: str, g: Gruff = GRUFF) -> Tuple[int, np.ndarray]:
    """Say it, then make it gruff: stretched by k it's k times lower, so it's first said k times faster."""
    from scipy.signal import resample_poly

    k = 2 ** (g.semitones / 12)
    rate, audio = synth(text, k * g.tempo)
    audio = np.asarray(audio, dtype=np.float32)
    if not audio.size or not np.any(audio):
        raise ValueError(f"the voice said nothing for {text!r}")
    f = Fraction(k).limit_denominator(200)
    audio = resample_poly(audio, f.numerator, f.denominator)
    t = np.arange(len(audio)) / rate
    audio = audio * (1 + g.growl * np.sin(2 * np.pi * GROWL_HZ * t))
    audio = np.tanh(g.drive * audio / np.max(np.abs(audio))) / np.tanh(g.drive)
    return rate, (audio / np.max(np.abs(audio)) * PEAK).astype(np.float32)


def sherpa_synth(paths: ModelPaths, threads: int = 2) -> Synth:
    import sherpa_onnx

    cfg = sherpa_onnx.OfflineTtsConfig(model=sherpa_onnx.OfflineTtsModelConfig(
        vits=sherpa_onnx.OfflineTtsVitsModelConfig(model=paths.voice(VOICE_MODEL), tokens=paths.voice("tokens.txt"),
                                                   data_dir=paths.voice("espeak-ng-data")),
        num_threads=threads))
    tts = sherpa_onnx.OfflineTts(cfg)

    def synth(text: str, speed: float) -> Tuple[int, np.ndarray]:
        out = tts.generate(text, sid=SPEAKER, speed=speed)
        return out.sample_rate, np.asarray(out.samples, dtype=np.float32)

    return synth


def make_replies(paths: ModelPaths, synth: Optional[Synth] = None, force: bool = False,
                 report: Optional[Callable[[str], None]] = print) -> int:
    """Make the clips that are missing (all of them with ``force``), and clear out any no longer used.
    Returns how many it made. Without ``synth``, it fetches the voice first if need be."""
    say = report or (lambda _msg: None)
    os.makedirs(paths.replies_dir, exist_ok=True)
    todo = list(LINES) if force else missing(paths)
    if todo and synth is None:
        download_voice(paths, report=report)
        synth = sherpa_synth(paths)
    for kind in todo:
        assert synth is not None
        rate, audio = gruffen(synth, LINES[kind])
        _write_wav(clip_path(paths, kind), rate, audio)
        say(f"  made {kind} ({len(audio) / rate:.1f} s): {LINES[kind]}")
    keep = {clip_name(kind) for kind in LINES}
    for name in os.listdir(paths.replies_dir):
        if name.endswith(".wav") and name not in keep:
            os.unlink(os.path.join(paths.replies_dir, name))
    if not todo:
        say(f"  have the spoken replies ({len(LINES)})")
    return len(todo)


def load_clips(paths: ModelPaths, rate: int, peak: float) -> Dict[str, np.ndarray]:
    """The clips there are, at the speaker's sample rate, peaking at ``peak``."""
    from math import gcd

    from scipy.signal import resample_poly

    clips: Dict[str, np.ndarray] = {}
    for kind in LINES:
        path = clip_path(paths, kind)
        if not os.path.isfile(path):
            continue
        try:
            clip_rate, audio = _read_wav(path)
        except (OSError, wave.Error, ValueError) as exc:
            log.warning("can't read the spoken reply %s: %s", path, exc)
            continue
        if clip_rate != rate:
            g = gcd(rate, clip_rate)
            audio = resample_poly(audio, rate // g, clip_rate // g)
        top = float(np.max(np.abs(audio))) or 1.0
        clips[kind] = (audio / top * peak).astype(np.float32)
    return clips


def _write_wav(path: str, rate: int, audio: np.ndarray) -> None:
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".part-", suffix=".wav")
    try:
        with os.fdopen(fd, "wb") as fh, wave.open(fh, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _read_wav(path: str) -> Tuple[int, np.ndarray]:
    with wave.open(path, "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError("not 16-bit mono")
        rate = w.getframerate()
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32767
    return rate, audio
