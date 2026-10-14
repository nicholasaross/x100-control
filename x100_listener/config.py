"""Listener settings, read from the environment (``listener.env``)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    v = os.environ.get(name)
    return float(v) if v not in (None, "") else default


def _int(name: str, default: int) -> int:
    v = os.environ.get(name)
    return int(v) if v not in (None, "") else default


def _opt(name: str) -> Optional[str]:
    v = os.environ.get(name, "").strip()
    return v or None


def default_threads() -> int:
    """Cores for each engine when WAKE_THREADS / KWS_THREADS aren't set: 2 with 4 or more cores, else 1.
    On a Pi 3, 2 was the fastest for both (4 was much slower); on a 2-core PC, 1 was."""
    try:
        cores = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):  # not Linux
        cores = os.cpu_count() or 1
    return 2 if cores >= 4 else 1


@dataclass
class ListenerConfig:
    # bridge
    bridge_url: str = "http://127.0.0.1:8661"
    bridge_token: str = ""
    bridge_timeout: float = 10.0       # "on" to an X100-4K is confirmed before the ack: ~3 s
    # models
    models_dir: str = "models"
    wake_model: str = "hey_jarvis_v0.1"
    # microphone
    mic_device: Optional[str] = None    # a number or part of the name from --list-devices
    mic_channel: int = 0
    mic_rate: Optional[int] = None      # force the capture rate (16000, 32000, 48000, ...)
    # wake word
    wake_threshold: float = 0.4
    vad_threshold: float = 0.0          # 0 = off; else Silero VAD must agree it's speech
    wake_threads: int = field(default_factory=default_threads)  # cores the wake word's models may use
    speex_ns: bool = False              # needs `pip install speexdsp-ns`
    # commands
    kws_threshold: float = 0.25         # lower = easier to trigger
    kws_boost: float = 2.0              # sherpa-onnx "keywords score"
    kws_threads: int = field(default_factory=default_threads)
    kws_paths: int = 6                  # beam width
    kws_blanks: int = 2                 # trailing blanks before a phrase counts (each ~40 ms)
    command_gain: bool = True           # bring the command up to the wake word's loudness
    command_window: float = 3.5
    preroll: float = 0.5
    cooldown: float = 2.0
    extra_phrases: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    # chimes
    chimes: bool = True
    chime_device: Optional[str] = None  # default: the mic's own device (right for a speakerphone)
    chime_volume: float = 0.3
    wake_chime: bool = False
    unclear_chime: bool = True          # the low note after "Hey Jarvis" with no command
    # diagnostics
    save_clips_dir: Optional[str] = None

    @classmethod
    def from_env(cls) -> "ListenerConfig":
        extra = os.environ.get("EXTRA_PHRASES", "").strip()
        rate = _opt("MIC_RATE")
        return cls(
            bridge_url=os.environ.get("BRIDGE_URL", "http://127.0.0.1:8661").rstrip("/"),
            bridge_token=os.environ.get("BRIDGE_TOKEN", ""),
            bridge_timeout=_float("BRIDGE_TIMEOUT", 10.0),
            models_dir=os.environ.get("MODELS_DIR", "models"),
            wake_model=os.environ.get("WAKE_MODEL", "hey_jarvis_v0.1"),
            mic_device=_opt("MIC_DEVICE"),
            mic_channel=_int("MIC_CHANNEL", 0),
            mic_rate=int(rate) if rate else None,
            wake_threshold=_float("WAKE_THRESHOLD", 0.4),
            vad_threshold=_float("VAD_THRESHOLD", 0.0),
            wake_threads=_int("WAKE_THREADS", default_threads()),
            speex_ns=_bool("SPEEX_NS", False),
            kws_threshold=_float("KWS_THRESHOLD", 0.25),
            kws_boost=_float("KWS_BOOST", 2.0),
            kws_threads=_int("KWS_THREADS", default_threads()),
            kws_paths=_int("KWS_PATHS", 6),
            kws_blanks=_int("KWS_TRAILING_BLANKS", 2),
            command_gain=_bool("COMMAND_GAIN", True),
            command_window=_float("COMMAND_WINDOW", 3.5),
            preroll=_float("PREROLL", 0.5),
            cooldown=_float("COOLDOWN", 2.0),
            extra_phrases=parse_extra_phrases(extra),
            chimes=_bool("CHIMES", True),
            chime_device=_opt("CHIME_DEVICE"),
            chime_volume=_float("CHIME_VOLUME", 0.3),
            wake_chime=_bool("WAKE_CHIME", False),
            unclear_chime=_bool("UNCLEAR_CHIME", True),
            save_clips_dir=_opt("SAVE_CLIPS_DIR"),
        )

    def problems(self) -> List[str]:
        out = []
        if not self.bridge_url.startswith(("http://", "https://")):
            out.append(f"BRIDGE_URL must start with http:// (got {self.bridge_url!r})")
        if not 0 < self.wake_threshold < 1:
            out.append("WAKE_THRESHOLD must be between 0 and 1")
        if not 0 < self.kws_threshold < 1:
            out.append("KWS_THRESHOLD must be between 0 and 1")
        if self.wake_threads < 1:
            out.append("WAKE_THREADS must be 1 or more")
        if not 0 <= self.vad_threshold < 1:
            out.append("VAD_THRESHOLD must be 0 (off) or between 0 and 1")
        if self.kws_paths < 1 or self.kws_blanks < 1 or self.kws_threads < 1:
            out.append("KWS_PATHS, KWS_TRAILING_BLANKS and KWS_THREADS must be at least 1")
        if self.mic_channel < 0:
            out.append("MIC_CHANNEL must be 0 or more")
        if self.command_window < 1 or self.preroll < 0 or self.cooldown < 0:
            out.append("COMMAND_WINDOW must be at least 1 s, and PREROLL / COOLDOWN not negative")
        if not 0 <= self.chime_volume <= 1:
            out.append("CHIME_VOLUME must be between 0 and 1")
        return out


def parse_extra_phrases(text: str) -> Dict[str, List[Dict[str, Any]]]:
    """EXTRA_PHRASES='{"projector hdmi two": [{"op": "input", "value": "HDMI 2"}]}'."""
    if not text:
        return {}
    raw = json.loads(text)
    if not isinstance(raw, dict):
        raise ValueError("EXTRA_PHRASES must be a JSON object of phrase -> list of ops")
    out: Dict[str, List[Dict[str, Any]]] = {}
    for phrase, ops in raw.items():
        if isinstance(ops, dict):
            ops = [ops]
        if not isinstance(ops, list) or not ops or not all(isinstance(o, dict) and "op" in o for o in ops):
            raise ValueError(f"EXTRA_PHRASES[{phrase!r}] must be a list of ops like "
                             '{"op": "input", "value": "HDMI 2"}')
        out[str(phrase)] = ops
    return out
