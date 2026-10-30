"""Short tones that tell you what happened, without looking at anything.

    ok       two rising notes      the projector accepted the command
    queued   two level notes       accepted, but it waits until the projector is ready (it has only just
                                   started, say): nothing happens yet
    fail     two falling notes     the bridge or projector said no, or couldn't be reached
    unclear  one low note          heard "Hey Jarvis" but no command after it
    wake     one short blip        heard "Hey Jarvis" (off by default: it would talk over you)

``listener.py`` only needs the ``ChimePlayer`` interface, so a remote speaker
(an ESP32 board, say) can play them instead.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Dict, Optional, Protocol

import numpy as np

log = logging.getLogger("x100.listener.chimes")

NOTES = {"A4": 440.0, "C5": 523.25, "E5": 659.25, "A5": 880.0, "E4": 329.63, "C4": 261.63}
CHIMES = {
    "ok": [("E5", 0.09), ("A5", 0.14)],
    "queued": [("E5", 0.09), ("E5", 0.14)],
    "fail": [("A4", 0.12), ("E4", 0.2)],
    "unclear": [("C4", 0.16)],
    "wake": [("A5", 0.06)],
}
TAIL = 0.15  # keep ignoring the mic this long after a chime ends (room echo)


class ChimePlayer(Protocol):
    def play(self, kind: str) -> None: ...

    def busy(self) -> bool:
        """True while a chime (plus a short echo tail) is playing: the mic is ignored."""
        ...


def tone(kind: str, rate: int, volume: float = 0.3) -> np.ndarray:
    parts = []
    for note, seconds in CHIMES[kind]:
        n = int(rate * seconds)
        t = np.arange(n) / rate
        wave = np.sin(2 * np.pi * NOTES[note] * t) + 0.25 * np.sin(4 * np.pi * NOTES[note] * t)
        fade = min(n // 2, int(rate * 0.012))
        env = np.ones(n)
        env[:fade] = 0.5 - 0.5 * np.cos(np.linspace(0, np.pi, fade))
        env[-fade:] = env[:fade][::-1]
        parts.append(wave * env)
        parts.append(np.zeros(int(rate * 0.025)))
    out = np.concatenate(parts)
    return (out / np.max(np.abs(out)) * volume).astype(np.float32)


def duration(kind: str) -> float:
    return sum(s for _, s in CHIMES[kind]) + 0.025 * len(CHIMES[kind])


class NullChimes:
    def play(self, kind: str) -> None:
        log.debug("chime %s (chimes are off)", kind)

    def busy(self) -> bool:
        return False


class SoundDeviceChimes:
    def __init__(self, device: Optional[int] = None, volume: float = 0.3):
        import sounddevice as sd

        self._sd = sd
        self.device = device
        info = sd.query_devices(device, "output")
        self.name = info["name"]
        self.rate = int(info["default_samplerate"])
        self._tones: Dict[str, np.ndarray] = {k: tone(k, self.rate, volume) for k in CHIMES}
        self._busy_until = 0.0
        self._lock = threading.Lock()
        log.info("chimes on %s", self.name)

    def play(self, kind: str) -> None:
        with self._lock:
            self._busy_until = time.monotonic() + duration(kind) + TAIL
        try:
            # Its own short-lived stream, so it never fights the mic's input stream.
            stream = self._sd.OutputStream(device=self.device, samplerate=self.rate, channels=1, dtype="float32")
            stream.start()
            threading.Thread(target=self._write, args=(stream, self._tones[kind]), daemon=True).start()
        except Exception as exc:  # a chime must never take the listener down
            log.warning("couldn't play the %s chime: %s", kind, exc)

    @staticmethod
    def _write(stream, data: np.ndarray) -> None:
        try:
            stream.write(data.reshape(-1, 1))
            stream.stop()
        finally:
            stream.close()

    def busy(self) -> bool:
        with self._lock:
            return time.monotonic() < self._busy_until
