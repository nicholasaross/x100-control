"""x100-listener: "Hey Jarvis, projector on" for the ViewSonic X100-4K, with no cloud.

Listens on a USB mic, spots the wake word and then a command, and sends the
command to x100-bridge's local API. Audio is processed on this machine and
never stored (unless SAVE_CLIPS_DIR is set) or sent anywhere.

    x100-listener --env-file listener.env                    run (what the systemd unit does)
    x100-listener --env-file listener.env --download-models  fetch the models (once)
    x100-listener --env-file listener.env --list-devices     find your mic
    x100-listener --env-file listener.env --test-mic         live scores, nothing is sent
    x100-listener --env-file listener.env --wav clip.wav     run a recording through, nothing is sent
    x100-listener --env-file listener.env --check            models, mic, bridge, token, power supply
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import signal
import statistics
import subprocess
import sys
import threading
import time
from typing import Callable, Dict, List, Optional

import numpy as np

from x100 import BUILD, version_line
from x100.envfile import load_env_file

from .audio import FRAME_SECONDS, MicError, MicSource, WavSource, list_devices, rescan_devices, resolve_device
from .chimes import NullChimes, SoundDeviceChimes
from .client import BridgeClient, Result
from .commands import Phrase, SherpaKeywordSpotter, build_phrases
from .config import ListenerConfig
from .listener import ClipSaver, Event, Listener, Phase, Settings
from .models import ModelError, ModelPaths, download, verify
from .wake import OpenWakeWordDetector

log = logging.getLogger("x100.listener")


# ---------------------------------------------------------------------------- building blocks


def settings_from(cfg: ListenerConfig) -> Settings:
    return Settings(wake_threshold=cfg.wake_threshold, command_window=cfg.command_window, preroll=cfg.preroll,
                    cooldown=cfg.cooldown, wake_chime=cfg.wake_chime, unclear_chime=cfg.unclear_chime,
                    command_gain=cfg.command_gain)


def load_engines(cfg: ListenerConfig):
    paths = ModelPaths(cfg.models_dir)
    missing = paths.missing(cfg.wake_model, vad=cfg.vad_threshold > 0)
    if missing:
        raise ModelError("missing model files (run `x100-listener --download-models`): " + ", ".join(missing))
    wake = OpenWakeWordDetector(paths, cfg.wake_model, vad_threshold=cfg.vad_threshold, speex_ns=cfg.speex_ns,
                                threads=cfg.wake_threads)
    spotter = SherpaKeywordSpotter(paths, build_phrases(cfg.extra_phrases), threshold=cfg.kws_threshold,
                                   boost=cfg.kws_boost, threads=cfg.kws_threads, max_active_paths=cfg.kws_paths,
                                   trailing_blanks=cfg.kws_blanks)
    return wake, spotter


def make_chimes(cfg: ListenerConfig):
    if not cfg.chimes:
        return NullChimes()
    spec = cfg.chime_device or cfg.mic_device
    try:
        device = resolve_device(spec, "output") if spec else None
    except MicError as exc:
        if cfg.chime_device:
            raise
        log.info("the mic has no speaker (%s); chimes go to the default output", exc)
        device = None
    try:
        return SoundDeviceChimes(device, volume=cfg.chime_volume)
    except Exception as exc:
        log.warning("no chimes: can't use the audio output (%s). Set CHIME_DEVICE, or CHIMES=off.", exc)
        return NullChimes()


def dry_run(phrase: Phrase) -> Result:
    return Result(True, "DRY_RUN", f"would send {phrase.describe()}")


def describe(event: Event) -> str:
    d = event.detail
    if event.kind == "wake":
        gain = d.get("gain_db", 0)
        return f"wake word (score {d['score']:.2f})" + (f", command boosted +{gain:.0f} dB" if gain >= 1 else "")
    if event.kind == "command":
        return f"heard \"{d['phrase']}\" {d['after_wake']:.1f} s after the wake word -> {d['ops']}"
    if event.kind == "result":
        if d.get("outcome") == "dry run, nothing sent":
            return "  (dry run: nothing sent to the bridge)"
        return f"  bridge: {d['outcome']}, {d['bridge_ms']} ms from hearing the command to the bridge's answer"
    if event.kind == "timeout":
        return f"no command within {d['waited']:.1f} s of the wake word"
    return f"{event.kind}: {d}"


def log_event(event: Event) -> None:
    level = logging.WARNING if event.kind == "result" and not event.detail.get("ok") else logging.INFO
    log.log(level, "%s", describe(event))


class Meter:
    """Frame timing and CPU use, for --test-mic and the periodic stats line."""

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self.times: List[float] = []
        self.wall0 = time.monotonic()
        self.cpu0 = time.process_time()

    def add(self, seconds: float) -> None:
        self.times.append(seconds)

    def cpu_percent(self) -> float:
        wall = time.monotonic() - self.wall0
        return 100.0 * (time.process_time() - self.cpu0) / wall if wall > 0 else 0.0

    def summary(self) -> str:
        if not self.times:
            return "no frames"
        ms = [t * 1000 for t in self.times]
        p95 = sorted(ms)[int(0.95 * (len(ms) - 1))]
        return (f"{len(ms)} frames, {statistics.mean(ms):.1f} ms average / {p95:.1f} ms p95 / {max(ms):.1f} ms max "
                f"per 80 ms frame, {self.cpu_percent():.0f}% of one CPU core")


def level_dbfs(frame: np.ndarray) -> float:
    rms = float(np.sqrt(np.mean(frame.astype(np.float64) ** 2))) / 32768.0
    return 20 * np.log10(rms) if rms > 1e-7 else -140.0


# ---------------------------------------------------------------------------- modes


def wait_for_mic(open_mic: Callable[[], MicSource], stop: threading.Event, every: float = 5.0,
                 rescan: Callable[[], None] = rescan_devices) -> Optional[MicSource]:
    """The mic, waiting for it to be plugged in if need be (None if told to stop first).

    The service waits rather than exiting, because systemd would start it again straight away and
    every start loads the models: about 20 s of a Pi 3's time, over and over, while there's no mic."""
    waiting_since: Optional[float] = None
    while not stop.is_set():
        try:
            mic = open_mic()
        except MicError as exc:
            if waiting_since is None:
                waiting_since = time.monotonic()
                log.warning("%s. Waiting for the mic: plug it in, or check MIC_DEVICE", exc)
        else:
            if waiting_since is not None:
                log.info("found the mic after %.0f s", time.monotonic() - waiting_since)
            return mic
        if stop.wait(every):
            break
        rescan()
    return None


def run_service(cfg: ListenerConfig) -> int:
    log.info("running %s", version_line())
    stop = threading.Event()
    mic: Optional[MicSource] = None

    def _signal(signum, _frame):
        log.info("signal %d received, stopping", signum)
        stop.set()
        if mic is not None:
            mic.close()

    signal.signal(signal.SIGTERM, _signal)
    signal.signal(signal.SIGINT, _signal)
    # The mic first: there's nothing to do without it, and loading the models takes a while.
    mic = wait_for_mic(lambda: MicSource(cfg.mic_device, cfg.mic_channel, cfg.mic_rate), stop)
    if mic is None:
        return 0
    try:
        wake, spotter = load_engines(cfg)
    except BaseException:
        mic.close()
        raise
    if stop.is_set():  # stopped while the models loaded
        return 0
    client = BridgeClient(cfg.bridge_url, cfg.bridge_token, cfg.bridge_timeout)
    try:
        client.health()
        log.info("bridge at %s is answering", cfg.bridge_url)
    except Exception as exc:  # keep going: the bridge may come up later
        log.warning("bridge at %s isn't answering yet (%s); commands will fail until it does", cfg.bridge_url, exc)
    chimes = make_chimes(cfg)
    clips = ClipSaver(cfg.save_clips_dir) if cfg.save_clips_dir else None
    listener = Listener(wake, spotter, lambda p: client.command(p.ops), chimes, settings_from(cfg),
                        on_event=log_event, clips=clips)
    log.info("listening for \"hey jarvis\" (threshold %.2f), then a command for %.1f s", cfg.wake_threshold,
             cfg.command_window)
    meter = Meter()
    next_stats = time.monotonic() + 600
    try:
        for frame in mic.frames():
            started = time.perf_counter()
            listener.process(frame)
            meter.add(time.perf_counter() - started)
            if time.monotonic() >= next_stats:
                log.info("stats: %s, %d mic overruns so far", meter.summary(), mic.overruns)
                meter.reset()
                next_stats = time.monotonic() + 600
    except MicError as exc:
        if not stop.is_set():
            log.error("%s", exc)
            return 1
    finally:
        mic.close()
    return 0


def run_wav(cfg: ListenerConfig, path: str, seconds: Optional[float] = None) -> int:
    started = time.monotonic()
    wake, spotter = load_engines(cfg)
    loaded = time.monotonic() - started
    source = WavSource(path, channel=cfg.mic_channel)
    events: List[Event] = []

    def record(e: Event) -> None:
        events.append(e)
        print(f"{e.t:6.2f} s  {describe(e)}")

    listener = Listener(wake, spotter, dry_run, NullChimes(), settings_from(cfg), run=lambda job: job(),
                        on_event=record)
    best, best_t = 0.0, 0.0
    took: Dict[str, List[float]] = {"wake": [], "command": [], "woke": []}
    for frame in source.frames():
        if seconds is not None and listener.now >= seconds:
            break
        before = listener.phase
        t0 = time.perf_counter()
        listener.process(frame)
        spent = time.perf_counter() - t0
        if before is Phase.LISTENING:
            took["command"].append(spent)
        elif listener.phase is Phase.LISTENING:
            took["woke"].append(spent)  # this frame also replays the pre-roll into the spotter
        else:
            took["wake"].append(spent)
        if listener.last_score > best:
            best, best_t = listener.last_score, listener.now
    heard = listener.now if seconds is not None else source.duration
    print(f"{path}: {heard:.1f} s, highest wake score {best:.2f} at {best_t:.2f} s "
          f"(threshold {cfg.wake_threshold:.2f}); {sum(e.kind == 'command' for e in events)} command(s) heard")
    print(speed_report(took, loaded, cfg))
    return 0


def speed_report(took: Dict[str, List[float]], loaded: float, cfg: ListenerConfig) -> str:
    """How long each 80 ms frame took to process: under 80 ms keeps up live, and well under is the aim."""
    budget = FRAME_SECONDS * 1000

    def stats(values: List[float]) -> str:
        ms = [v * 1000 for v in values]
        return f"{statistics.mean(ms):.0f} ms ({statistics.mean(ms) / budget:.0%} of the time; worst {max(ms):.0f})"

    lines = [f"speed (WAKE_THREADS={cfg.wake_threads}, KWS_THREADS={cfg.kws_threads}), per {budget:.0f} ms frame:"]
    if took["wake"]:
        lines.append(f"  listening for the wake word:  {stats(took['wake'])}")
    if took["command"]:
        lines.append(f"  listening for a command:      {stats(took['command'])}")
    if took["woke"]:
        lines.append(f"  the frame that woke it:       {statistics.mean(took['woke']) * 1000:.0f} ms "
                     f"(includes replaying {cfg.preroll:.1f} s into the command spotter)")
    lines.append(f"  models loaded in {loaded:.1f} s")
    busiest = max((statistics.mean(took[k]) * 1000 / budget for k in ("wake", "command") if took[k]), default=0.0)
    if busiest < 0.75:
        lines.append("  verdict: fast enough for a live mic")
    elif busiest < 1.0:
        lines.append("  verdict: fast enough for a live mic, but only just")
    else:
        lines.append("  verdict: TOO SLOW for a live mic: it would fall further and further behind")
    return "\n".join(lines)


def run_test_mic(cfg: ListenerConfig, seconds: Optional[float]) -> int:
    wake, spotter = load_engines(cfg)
    mic = MicSource(cfg.mic_device, cfg.mic_channel, cfg.mic_rate)
    chimes = make_chimes(cfg)
    lines: List[str] = []
    listener = Listener(wake, spotter, dry_run, chimes, settings_from(cfg), run=lambda job: job(),
                        on_event=lambda e: lines.append(f"{e.t:7.2f} s  {describe(e)}"))
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: (stop.set(), mic.close()))
    meter = Meter()
    peak, n = 0.0, 0
    tty = sys.stdout.isatty()
    print(f"Mic: {mic.name}, channel {cfg.mic_channel}, {mic.rate} Hz. Nothing is sent to the bridge.")
    print("Say \"Hey Jarvis, projector on\". Ctrl-C to stop.\n")
    try:
        for frame in mic.frames():
            started = time.perf_counter()
            listener.process(frame)
            meter.add(time.perf_counter() - started)
            n += 1
            peak = max(peak, listener.last_score)
            for line in lines:
                print(("\r" + " " * 100 + "\r" if tty else "") + line)
            lines.clear()
            if n % 3 == 0:
                bar = "#" * int(round(listener.last_score * 20))
                status = (f"wake {listener.last_score:4.2f} [{bar:<20}] level {level_dbfs(frame):6.1f} dBFS  "
                          f"frame {meter.times[-1] * 1000:5.1f} ms  cpu {meter.cpu_percent():3.0f}%  "
                          f"overruns {mic.overruns}  {listener.phase.value}")
                if tty:
                    print("\r" + status, end="", flush=True)
                elif n % 25 == 0:
                    print(status)
            if seconds is not None and n * FRAME_SECONDS >= seconds:
                break
    except MicError as exc:
        if not stop.is_set():
            print(f"\n{exc}")
            return 1
    finally:
        mic.close()
    print(f"\n\nHighest wake score {peak:.2f} (threshold {cfg.wake_threshold:.2f}). {meter.summary()}. "
          f"Mic overruns: {mic.overruns}" + (" -- this machine is falling behind." if mic.overruns else "."))
    return 0


def is_local_url(url: str) -> bool:
    """A bridge on this machine (127.0.0.1, ::1, localhost) can run without a token."""
    import ipaddress
    from urllib.parse import urlparse

    host = urlparse(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_power() -> Optional[str]:
    """Raspberry Pi under-voltage / throttling, from the firmware (None when not a Pi)."""
    tool = shutil.which("vcgencmd")
    if not tool:
        return None
    try:
        out = subprocess.run([tool, "get_throttled"], capture_output=True, text=True, timeout=5).stdout
        flags = int(out.strip().split("=")[1], 16)
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None
    if flags & 0x1:
        return "UNDER-VOLTAGE NOW: use a proper 5 V 2.5 A supply (the mic draws from the Pi's USB too)"
    if flags & 0x4:
        return "THROTTLED NOW: the CPU is slowed down (power or heat)"
    if flags & 0x50000:
        return "under-voltage or throttling happened since boot; check the power supply"
    return "ok"


def run_check(cfg: ListenerConfig) -> int:
    ok = True

    def bad(msg: str) -> None:
        nonlocal ok
        ok = False
        print(f"  PROBLEM: {msg}")

    print(version_line())
    print("Settings")
    for p in cfg.problems():
        bad(p)
    if not cfg.bridge_token and not is_local_url(cfg.bridge_url):
        bad("BRIDGE_TOKEN is empty (use the LOCAL_API_TOKEN from the bridge's bridge.env)")

    print(f"Models in {os.path.abspath(cfg.models_dir)}")
    paths = ModelPaths(cfg.models_dir)
    missing = paths.missing(cfg.wake_model, vad=cfg.vad_threshold > 0)
    if missing:
        bad("missing: " + ", ".join(os.path.basename(m) for m in missing) + " -- run --download-models")
    else:
        for problem in verify(paths):
            bad(problem)
        try:
            started = time.monotonic()
            wake, spotter = load_engines(cfg)
            print(f"  loaded the wake word and {len(spotter.phrases)} command phrases "
                  f"in {time.monotonic() - started:.1f} s")
        except Exception as exc:
            bad(f"can't load the models: {exc}")

    print("Microphone")
    try:
        mic = MicSource(cfg.mic_device, cfg.mic_channel, cfg.mic_rate)
        print(f"  {mic.name}, channel {cfg.mic_channel}, {mic.rate} Hz")
        levels = []
        for i, frame in enumerate(mic.frames()):
            levels.append(level_dbfs(frame))
            if i >= 12:
                break
        mic.close()
        print(f"  recording: level {max(levels):.0f} dBFS peak over 1 s" +
              (" -- silent; is the mic muted?" if max(levels) < -90 else ""))
        if mic.overruns:
            bad(f"{mic.overruns} overruns while recording")
    except MicError as exc:
        bad(str(exc))

    print(f"Bridge at {cfg.bridge_url}")
    client = BridgeClient(cfg.bridge_url, cfg.bridge_token, cfg.bridge_timeout)
    try:
        rtts = []
        for _ in range(5):
            started = time.monotonic()
            health = client.health()
            rtts.append((time.monotonic() - started) * 1000)
        print(f"  answering; round trip {statistics.median(rtts):.0f} ms (median of 5)")
        bridge_build = health.get("build", "unknown (older than 2.0.1)")
        if bridge_build == BUILD:
            print(f"  running the same build as this listener ({BUILD})")
        else:  # not a fault in itself: a bridge on another machine may be on another release
            print(f"  NOTE: the bridge is on build {bridge_build}, this listener on {BUILD}. If they share a "
                  "machine, restart both services (or re-run deploy/install.sh)")
        try:
            state = client.state()
            print(f"  token accepted; projector power {state.get('power')}, "
                  f"{'reachable' if state.get('connected') else 'NOT reachable from the bridge'}")
        except Exception as exc:
            bad(f"token refused or state unavailable: {exc}")
    except Exception as exc:
        bad(f"not answering: {exc} (is x100-bridge running, with LOCAL_API_BIND=0.0.0.0:8661?)")

    power = check_power()
    if power is not None:
        print("Power supply")
        if power == "ok":
            print("  no under-voltage or throttling since boot")
        else:
            bad(power)
    print("All good." if ok else "Problems found (see above).")
    return 0 if ok else 1


# ---------------------------------------------------------------------------- entry point


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="x100-listener", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env-file", help="load settings from a KEY=VALUE file (e.g. listener.env)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--list-devices", action="store_true", help="list audio devices and exit")
    mode.add_argument("--test-mic", action="store_true", help="show live wake scores; sends nothing")
    mode.add_argument("--wav", metavar="FILE", help="run a WAV recording through the listener; sends nothing")
    mode.add_argument("--check", action="store_true", help="check models, mic, bridge, token and power, then exit")
    mode.add_argument("--download-models", action="store_true", help="download and verify the model files")
    ap.add_argument("--seconds", type=float, help="with --test-mic or --wav: stop after this long")
    ap.add_argument("--force", action="store_true", help="with --download-models: fetch again even if present")
    ap.add_argument("--version", action="version", version=version_line(),
                    help="show which release is installed, then exit")
    args = ap.parse_args(argv)
    if args.env_file:
        load_env_file(args.env_file)
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.test_mic or args.wav or args.check:
        for name in ("x100.listener.wake", "x100.listener.commands"):
            logging.getLogger(name).setLevel(logging.WARNING)  # keep the interactive output readable
    try:
        cfg = ListenerConfig.from_env()
    except ValueError as exc:
        log.error("bad setting: %s", exc)
        return 2

    try:
        if args.list_devices:
            print(list_devices())
            return 0
        if args.download_models:
            print(f"Models go in {os.path.abspath(cfg.models_dir)}")
            download(ModelPaths(cfg.models_dir), force=args.force)
            print("Done.")
            return 0
        if args.check:
            return run_check(cfg)
        problems = cfg.problems()
        if problems:
            for p in problems:
                log.error(p)
            return 2
        if args.wav:
            return run_wav(cfg, args.wav, args.seconds)
        if args.test_mic:
            return run_test_mic(cfg, args.seconds)
        return run_service(cfg)
    except (ModelError, MicError) as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
