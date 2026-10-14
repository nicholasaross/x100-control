"""The listening state machine: wake word, command window, dispatch, chime, cooldown.

Pure logic. The wake detector, command spotter, dispatcher, chime player and
the way dispatches run are all injected, and time is counted in audio frames
(80 ms each), so tests can drive it frame by frame with fakes.

    IDLE ── wake score >= threshold ──► LISTENING (command window, default 3.5 s)
    LISTENING ── phrase heard ──► dispatch in the background ──► COOLDOWN (2 s)
    LISTENING ── window over ──► "unclear" chime ──► COOLDOWN (0.5 s)
    COOLDOWN ── time's up ──► IDLE

Only one engine runs at a time, because on a Pi 3 the two together take longer
than the 80 ms a frame lasts: the wake detector rests while the spotter listens
for the command, and is then reset to a quiet room. It hears the cooldown, so
it is up to date again by the time IDLE returns; its score only counts in IDLE.
While a chime plays the mic is replaced by silence, so the listener can't hear
itself. The last ``preroll`` seconds before the wake word fired are replayed
into the spotter, so a command said in the same breath as "Hey Jarvis" isn't
clipped.

The command spotter is sensitive to level (quiet commands were often missed in
testing), so the command window is amplified to match how loudly "Hey Jarvis"
was said: the loudest sample of the last 1.2 s is brought up to -6 dBFS, by at
most +30 dB and never turned down.
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Deque, Dict, List, Optional

import numpy as np

from .audio import FRAME_SECONDS, write_wav
from .chimes import ChimePlayer
from .client import Result
from .commands import CommandSpotter, Phrase
from .wake import WakeDetector

log = logging.getLogger("x100.listener")


GAIN_WINDOW = 1.2      # seconds before the wake word fired that set the command's level
GAIN_TARGET = 0.5      # peak, i.e. -6 dBFS
GAIN_MAX = 31.6        # +30 dB


def level_gain(frames: List[np.ndarray], target: float = GAIN_TARGET, max_gain: float = GAIN_MAX) -> float:
    """How much to amplify the command so it is as loud as the wake word was (never less than 1)."""
    peak = max((int(np.max(np.abs(f.astype(np.int32)))) for f in frames if len(f)), default=0) / 32768.0
    if peak <= 1e-4:
        return 1.0
    return float(min(max_gain, max(1.0, target / peak)))


class Phase(Enum):
    IDLE = "idle"
    LISTENING = "listening"
    COOLDOWN = "cooldown"


@dataclass
class Settings:
    wake_threshold: float = 0.4
    command_window: float = 3.5
    preroll: float = 0.5
    cooldown: float = 2.0
    unclear_cooldown: float = 0.5
    wake_chime: bool = False
    unclear_chime: bool = True
    command_gain: bool = True


@dataclass
class Event:
    kind: str                 # wake | command | result | timeout
    t: float                  # audio time, seconds since the listener started
    detail: Dict[str, Any] = field(default_factory=dict)


def run_in_thread(job: Callable[[], None]) -> None:
    threading.Thread(target=job, name="dispatch", daemon=True).start()


class ClipSaver:
    """Keeps a WAV of every trigger (SAVE_CLIPS_DIR), for tuning thresholds later."""

    def __init__(self, directory: str, keep: int = 500):
        self.dir = directory
        self.keep = keep
        os.makedirs(directory, exist_ok=True)

    def save(self, frames: List[np.ndarray], label: str) -> None:
        if not frames:
            return
        name = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}-{label}.wav"
        path = os.path.join(self.dir, name)
        audio = np.concatenate(frames)

        def job() -> None:
            try:
                write_wav(path, audio)
                clips = sorted(f for f in os.listdir(self.dir) if f.endswith(".wav"))
                for old in clips[:-self.keep]:
                    os.unlink(os.path.join(self.dir, old))
            except OSError as exc:
                log.warning("couldn't save clip %s: %s", path, exc)

        threading.Thread(target=job, daemon=True).start()


class Listener:
    def __init__(self, wake: WakeDetector, spotter: CommandSpotter, dispatch: Callable[[Phrase], Result],
                 chimes: ChimePlayer, settings: Optional[Settings] = None,
                 run: Callable[[Callable[[], None]], None] = run_in_thread,
                 on_event: Optional[Callable[[Event], None]] = None, clips: Optional[ClipSaver] = None,
                 clock: Callable[[], float] = time.monotonic):
        self.wake = wake
        self.spotter = spotter
        self.dispatch = dispatch
        self.chimes = chimes
        self.s = settings or Settings()
        self.run = run
        self.on_event = on_event
        self.clips = clips
        self.clock = clock
        self.phase = Phase.IDLE
        self.frames_seen = 0
        self.last_score = 0.0
        self._preroll: Deque[np.ndarray] = deque(maxlen=max(1, round(self.s.preroll / FRAME_SECONDS) + 1))
        self._level: Deque[np.ndarray] = deque(maxlen=round(GAIN_WINDOW / FRAME_SECONDS) + 1)
        self._gain = 1.0
        self._window_end = 0.0
        self._cooldown_end = 0.0
        self._wake_t = 0.0
        self._clip: List[np.ndarray] = []

    @property
    def now(self) -> float:
        return self.frames_seen * FRAME_SECONDS

    def _emit(self, kind: str, t: float, **detail: Any) -> None:
        if self.on_event is not None:
            self.on_event(Event(kind, t, detail))

    # ------------------------------------------------------------------ per frame
    def process(self, frame: np.ndarray) -> None:
        self.frames_seen += 1
        t = self.now
        muted = self.chimes.busy()
        heard = np.zeros_like(frame) if muted else frame
        # While the spotter listens for the command the wake word can rest: on a Pi 3 the two together
        # take longer than the 80 ms a frame lasts. The wake word starts again from a quiet room after.
        score = 0.0 if self.phase is Phase.LISTENING else self.wake.score(heard)
        self.last_score = score
        self._preroll.append(heard)
        self._level.append(heard)

        if self.phase is Phase.COOLDOWN and t >= self._cooldown_end:
            self.phase = Phase.IDLE
        if self.phase is Phase.IDLE:
            if not muted and score >= self.s.wake_threshold:
                self._on_wake(t, score)
            return
        if self.phase is Phase.LISTENING:
            if self.clips is not None:
                self._clip.append(heard)
            hit = self.spotter.feed(self._amplify(heard))
            if hit is not None:
                self._on_command(t, hit)
            elif t >= self._window_end:
                self._on_timeout(t)

    def _amplify(self, frame: np.ndarray) -> np.ndarray:
        if self._gain == 1.0:
            return frame
        return np.clip(frame.astype(np.float32) * self._gain, -32768, 32767).astype(np.int16)

    # ------------------------------------------------------------------ transitions
    def _on_wake(self, t: float, score: float) -> None:
        self.phase = Phase.LISTENING
        self._wake_t = t
        self._window_end = t + self.s.command_window
        self._gain = level_gain(list(self._level)) if self.s.command_gain else 1.0
        self._emit("wake", t, score=round(score, 3), gain_db=round(20 * math.log10(self._gain), 1))
        if self.s.wake_chime:
            self.chimes.play("wake")
        self.spotter.start()
        self._clip = list(self._preroll) if self.clips is not None else []
        for f in list(self._preroll):  # includes this frame
            hit = self.spotter.feed(self._amplify(f))
            if hit is not None:
                self._on_command(t, hit)
                return

    def _on_command(self, t: float, phrase: Phrase) -> None:
        self.spotter.stop()
        self.wake.reset()
        self.phase = Phase.COOLDOWN
        self._cooldown_end = t + self.s.cooldown
        heard_at = self.clock()
        self._emit("command", t, phrase=phrase.text.lower(), ops=phrase.describe(),
                   after_wake=round(t - self._wake_t, 2))
        self._save_clip("cmd-" + phrase.text.lower().replace(" ", "-").replace("'", ""))

        def job() -> None:
            try:
                result = self.dispatch(phrase)
            except Exception as exc:  # a dispatcher bug must not kill the listener
                log.exception("dispatch failed")
                result = Result(False, "LISTENER_ERROR", str(exc))
            self._emit("result", t, phrase=phrase.text.lower(), ok=result.ok, outcome=result.describe(),
                       bridge_ms=round(result.seconds * 1000), total_ms=round((self.clock() - heard_at) * 1000))
            self.chimes.play("ok" if result.ok else "fail")

        self.run(job)

    def _on_timeout(self, t: float) -> None:
        self.spotter.stop()
        self.wake.reset()
        self.phase = Phase.COOLDOWN
        self._cooldown_end = t + self.s.unclear_cooldown
        self._emit("timeout", t, waited=round(t - self._wake_t, 2))
        self._save_clip("no-command")
        if self.s.unclear_chime:
            self.chimes.play("unclear")

    def _save_clip(self, label: str) -> None:
        if self.clips is not None:
            self.clips.save(self._clip, label)
        self._clip = []
