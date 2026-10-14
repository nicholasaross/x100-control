"""Audio in. Every source yields 80 ms frames of 16 kHz mono int16 (what openWakeWord wants).

``listener.py`` only sees the ``AudioSource`` interface, so other sources (an
ESP32 board streaming over Wi-Fi, say) can be added without touching it.
"""

from __future__ import annotations

import logging
import queue
import threading
import wave
from typing import Any, Callable, Iterator, List, Optional, Protocol, Tuple, Union

import numpy as np

log = logging.getLogger("x100.listener.audio")

RATE = 16000
FRAME = 1280                     # samples per frame
FRAME_SECONDS = FRAME / RATE     # 0.08
CANDIDATE_RATES = (16000, 48000, 32000, 96000)


class MicError(RuntimeError):
    """The microphone can't be opened, or stopped delivering audio."""


class AudioSource(Protocol):
    """Anything that yields 16 kHz mono int16 frames of FRAME samples."""

    def frames(self) -> Iterator[np.ndarray]: ...

    def close(self) -> None: ...

    @property
    def overruns(self) -> int: ...


# ---------------------------------------------------------------------------- conversion


def to_float(x: np.ndarray) -> np.ndarray:
    """int16 / int32 / float samples -> float32 in [-1, 1)."""
    if x.dtype == np.int16:
        return x.astype(np.float32) / 32768.0
    if x.dtype == np.int32:
        return (x.astype(np.float64) / 2147483648.0).astype(np.float32)
    if x.dtype == np.uint8:  # 8-bit WAV is unsigned
        return (x.astype(np.float32) - 128.0) / 128.0
    return x.astype(np.float32)


def to_int16(x: np.ndarray) -> np.ndarray:
    return np.clip(np.round(x * 32768.0), -32768, 32767).astype(np.int16)


class FrameAssembler:
    """Turns capture blocks into frames.

    Blocks may have any number of channels (``channel`` picks one), be int16,
    int32 or float, and run at 16 kHz or an integer multiple of it (32, 48,
    96 kHz), which is decimated with a streaming low-pass filter so block
    boundaries leave no clicks.
    """

    def __init__(self, in_rate: int = RATE, channels: int = 1, channel: int = 0):
        if not 0 <= channel < channels:
            raise ValueError(f"channel {channel} doesn't exist on a {channels}-channel stream")
        if in_rate < RATE or in_rate % RATE:
            raise ValueError(f"{in_rate} Hz can't be used: the mic needs to offer 16000 Hz or a multiple of it "
                             "(32000, 48000, 96000). Set MIC_RATE to one it supports.")
        self.in_rate, self.channels, self.channel = in_rate, channels, channel
        self.factor = in_rate // RATE
        self._pos = 0  # input samples consumed so far, for the decimation phase
        self._pending = np.zeros(0, dtype=np.int16)
        if self.factor > 1:
            from scipy.signal import firwin

            self._taps = firwin(16 * self.factor + 1, cutoff=7000, fs=in_rate)
            self._zi = np.zeros(len(self._taps) - 1)

    def push(self, block: np.ndarray) -> List[np.ndarray]:
        x = block[:, self.channel] if block.ndim == 2 else block
        if self.factor == 1:
            mono = x if x.dtype == np.int16 else to_int16(to_float(x))
        else:
            from scipy.signal import lfilter

            filtered, self._zi = lfilter(self._taps, 1.0, to_float(x).astype(np.float64), zi=self._zi)
            start = (-self._pos) % self.factor
            mono = to_int16(filtered[start::self.factor])
        self._pos += len(x)
        buf = np.concatenate([self._pending, mono]) if len(self._pending) else mono
        n = len(buf) // FRAME
        frames = [buf[i * FRAME:(i + 1) * FRAME].copy() for i in range(n)]
        self._pending = buf[n * FRAME:].copy()
        return frames


# ---------------------------------------------------------------------------- WAV files


class WavSource:
    """Frames from a WAV file (or an int16 array), for tests and `--wav` dry runs."""

    def __init__(self, path: Union[str, np.ndarray], channel: int = 0, rate: int = RATE,
                 lead_silence: float = 0.0, tail_silence: float = 1.0):
        if isinstance(path, np.ndarray):
            data, in_rate = path, rate
        else:
            data, in_rate = read_wav(path, channel)
        if in_rate != RATE:
            from math import gcd

            from scipy.signal import resample_poly

            g = gcd(RATE, in_rate)
            data = to_int16(resample_poly(to_float(data).astype(np.float64), RATE // g, in_rate // g))
        elif data.dtype != np.int16:
            data = to_int16(to_float(data))
        pad = lambda s: np.zeros(int(round(s * RATE)), dtype=np.int16)  # noqa: E731
        self.samples = np.concatenate([pad(lead_silence), data, pad(tail_silence)])
        rem = (-len(self.samples)) % FRAME
        if rem:
            self.samples = np.concatenate([self.samples, np.zeros(rem, dtype=np.int16)])
        self.lead = lead_silence

    @property
    def overruns(self) -> int:
        return 0

    @property
    def duration(self) -> float:
        return len(self.samples) / RATE

    def frames(self) -> Iterator[np.ndarray]:
        for i in range(0, len(self.samples), FRAME):
            yield self.samples[i:i + FRAME]

    def close(self) -> None:
        pass


def read_wav(path: str, channel: int = 0) -> Tuple[np.ndarray, int]:
    with wave.open(path, "rb") as w:
        nch, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
        raw = w.readframes(w.getnframes())
    if width == 3:  # 24-bit packed -> int32
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
        data = (b[:, 0].astype(np.int32) << 8 | b[:, 1].astype(np.int32) << 16 | b[:, 2].astype(np.int32) << 24)
    else:
        data = np.frombuffer(raw, dtype={1: np.uint8, 2: np.int16, 4: np.int32}[width])
    data = data.reshape(-1, nch)
    if channel >= nch:
        raise ValueError(f"{path} has {nch} channel(s); channel {channel} doesn't exist")
    return np.ascontiguousarray(data[:, channel]), rate


def write_wav(path: str, samples: np.ndarray, rate: int = RATE) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(np.asarray(samples, dtype=np.int16).tobytes())


# ---------------------------------------------------------------------------- microphone


def _sd():
    try:
        import sounddevice as sd
    except OSError as exc:  # PortAudio missing
        raise MicError(f"can't load PortAudio ({exc}); on Raspberry Pi OS: sudo apt install libportaudio2") from exc
    return sd


def rescan_devices() -> None:
    """PortAudio lists the audio devices once, when it starts: restart it to see one plugged in since.
    Only while no stream is open."""
    try:
        sd = _sd()
        sd._terminate()
        sd._initialize()
    except Exception as exc:  # pragma: no cover - PortAudio missing, or a sounddevice without these
        log.debug("couldn't rescan the audio devices: %s", exc)


def resolve_device(spec: Optional[str], kind: str = "input") -> Optional[int]:
    """``None`` (system default), a device number, or part of a device name."""
    if spec is None or str(spec).strip() == "":
        return None
    spec = str(spec).strip()
    sd = _sd()
    devices = sd.query_devices()
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    if spec.isdigit():
        idx = int(spec)
        if idx >= len(devices) or devices[idx][key] <= 0:
            raise MicError(f"device {idx} has no {kind}s; see `x100-listener --list-devices`")
        return idx
    matches = [i for i, d in enumerate(devices) if spec.lower() in d["name"].lower() and d[key] > 0]
    if not matches:
        names = ", ".join(f"{i}: {d['name']}" for i, d in enumerate(devices) if d[key] > 0) or "none"
        raise MicError(f"no {kind} device matches {spec!r}. {kind.capitalize()} devices: {names}")
    if len(matches) > 1:
        log.info("%d %s devices match %r; using %d (%s)", len(matches), kind, spec, matches[0],
                 devices[matches[0]]["name"])
    return matches[0]


def supported_rates(device: Optional[int], channels: int = 1, dtype: str = "int16") -> List[int]:
    sd = _sd()
    rates = []
    for rate in CANDIDATE_RATES:
        try:
            sd.check_input_settings(device=device, channels=channels, samplerate=rate, dtype=dtype)
            rates.append(rate)
        except Exception:  # sounddevice raises PortAudioError / ValueError
            pass
    return rates


def list_devices() -> str:
    sd = _sd()
    lines = []
    default_in, default_out = sd.default.device
    for i, d in enumerate(sd.query_devices()):
        ins, outs = d["max_input_channels"], d["max_output_channels"]
        if not ins and not outs:
            continue
        flags = []
        if i == default_in:
            flags.append("default input")
        if i == default_out:
            flags.append("default output")
        extra = ""
        if ins:
            rates = supported_rates(i)
            extra = f", capture at {'/'.join(str(r // 1000) + 'k' for r in rates) or 'no usable rate'}"
        lines.append(f"{i:3d}  {d['name']}  (in {ins}, out {outs}{extra})" + (f"  [{', '.join(flags)}]"
                                                                                if flags else ""))
    return "\n".join(lines) or "no audio devices found"


class MicSource:
    """Live capture through PortAudio (sounddevice).

    The PortAudio callback only queues raw blocks; channel selection, format
    conversion and decimation happen in the consumer. If the consumer falls
    behind, blocks are dropped and counted in ``overruns`` (so are PortAudio's
    own input overflows).
    """

    def __init__(self, device: Optional[str] = None, channel: int = 0, rate: Optional[int] = None,
                 dtype: str = "int16", queue_seconds: float = 4.0,
                 stream_factory: Optional[Callable[..., Any]] = None):
        self.channel = channel
        self.dtype = dtype
        self._overruns = 0
        self._closed = threading.Event()
        block = 0.04
        self._q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=max(4, int(queue_seconds / block)))
        if stream_factory is not None:  # tests: no PortAudio needed
            self.device, self.channels, self.rate = device, channel + 1, rate or RATE
            self.name = str(device or "test stream")
            self.assembler = FrameAssembler(self.rate, self.channels, channel)
            self.stream = stream_factory(callback=self._callback, channels=self.channels, samplerate=self.rate,
                                         dtype=dtype, blocksize=int(self.rate * block))
            return
        sd = _sd()
        self.device = resolve_device(device, "input")
        try:
            info = sd.query_devices(self.device, "input")
        except Exception as exc:  # no default input device: PortAudio says "Error querying device -1"
            raise MicError(f"no microphone found ({exc}); see `x100-listener --list-devices`") from exc
        self.name = info["name"]
        max_ch = int(info["max_input_channels"])
        if channel >= max_ch:
            raise MicError(f"{self.name} has {max_ch} input channel(s); MIC_CHANNEL={channel} doesn't exist")
        self.channels, self.rate = self._pick_format(sd, channel, max_ch, rate)
        self.assembler = FrameAssembler(self.rate, self.channels, channel)
        try:
            self.stream = sd.InputStream(device=self.device, channels=self.channels, samplerate=self.rate,
                                         dtype=dtype, blocksize=int(self.rate * block), callback=self._callback)
        except Exception as exc:
            raise MicError(f"can't open {self.name}: {exc}") from exc
        log.info("microphone: %s, channel %d of %d, %d Hz%s", self.name, channel, self.channels, self.rate,
                 "" if self.rate == RATE else " (decimated to 16 kHz)")

    def _pick_format(self, sd: Any, channel: int, max_ch: int, rate: Optional[int]) -> Tuple[int, int]:
        tried = []
        for channels in dict.fromkeys((channel + 1, max_ch)):
            for r in ([rate] if rate else CANDIDATE_RATES):
                try:
                    sd.check_input_settings(device=self.device, channels=channels, samplerate=r, dtype=self.dtype)
                    return channels, int(r)
                except Exception as exc:
                    tried.append(f"{channels} ch @ {r} Hz: {exc}")
        raise MicError(f"{self.name} won't capture in any usable format ({'; '.join(tried[:4])}). "
                       "It needs 16 kHz or a multiple of it; try MIC_RATE=48000, or another MIC_DEVICE.")

    def _callback(self, indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
        if status and getattr(status, "input_overflow", False):
            self._overruns += 1
        try:
            self._q.put_nowait(indata.copy())
        except queue.Full:
            self._overruns += 1

    @property
    def overruns(self) -> int:
        return self._overruns

    def frames(self) -> Iterator[np.ndarray]:
        if self._closed.is_set():
            return
        self.stream.start()
        idle = 0
        while not self._closed.is_set():
            try:
                block = self._q.get(timeout=1.0)
            except queue.Empty:
                idle += 1
                if not getattr(self.stream, "active", True) or idle >= 5:
                    raise MicError(f"{self.name} stopped delivering audio (unplugged?)") from None
                continue
            idle = 0
            for frame in self.assembler.push(block):
                yield frame

    def close(self) -> None:
        self._closed.set()
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:  # pragma: no cover - already gone
            pass
