"""x100ctl -- command-line control and diagnostics for the ViewSonic X100-4K.

Examples::

    x100ctl --host 192.168.1.60 probe
    x100ctl status
    x100ctl on --wait
    x100ctl input "HDMI 2"
    x100ctl volume 18        # or: volume +3 / volume -2
    x100ctl mute on
    x100ctl learn-inputs     # discover HDMI 3/4, Computer In, Home codes
    x100ctl key 0x92         # experiment: does the LAN accept IR key codes?
    x100ctl raw "07 14 00 05 00 34 00 00 11 00 5E"
    x100ctl trace off        # turn it off, then log what it reports every second
    x100ctl listen           # send nothing; print whatever the projector says by itself
    x100ctl --version        # which release is installed

Settings come from ./bridge.env (or --env-file), then X100_* environment
variables, then flags -- so the bridge and x100ctl share one config file.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import logging
import os
import socket
import sys
import time
from typing import List, Optional, Tuple

from . import protocol as p
from . import version_line
from .envfile import env_file_from_argv, load_env_file
from .inputs import InputMap
from .pjlink import PJLink, PJLinkAuthError, PJLinkError
from .projector import (
    FunctionDisabled,
    NotSupported,
    Projector,
    ProjectorError,
    Unreachable,
    make_transport,
)
from .protocol import PowerState
from .transport import DEFAULT_BAUD, DEFAULT_TCP_PORT, SerialTransport

EXTRA_PROBE_PORTS = [
    (41794, "Crestron"),
    (23, "telnet"),
    (5000, "vController"),
]
BAUD_RATES = [115200, 9600, 19200, 38400, 57600]


def env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="x100ctl", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=env("X100_HOST"), help="projector CONTROL-port IP (env X100_HOST)")
    ap.add_argument("--port", type=int, default=int(env("X100_PORT", str(DEFAULT_TCP_PORT))))
    ap.add_argument("--serial", default=env("X100_SERIAL"), help="serial device, e.g. /dev/ttyUSB0 (env X100_SERIAL)")
    ap.add_argument("--baud", type=int, default=int(env("X100_BAUD", str(DEFAULT_BAUD))))
    ap.add_argument("--timeout", type=float, default=float(env("X100_TIMEOUT", "2.0")))
    ap.add_argument("--one-way", action="store_true",
                    default=env("X100_ONE_WAY", "false").lower() in ("1", "true", "yes", "on"),
                    help="don't wait for replies (write-only links)")
    ap.add_argument("--inputs", default=env("X100_INPUTS_FILE", "inputs.json"), help="input map JSON")
    ap.add_argument("--pjlink-host", default=env("X100_PJLINK_HOST"))
    ap.add_argument("--pjlink-port", type=int, default=int(env("X100_PJLINK_PORT", "4352")))
    ap.add_argument("--pjlink-password", default=env("X100_PJLINK_PASSWORD"))
    ap.add_argument("-v", "--verbose", action="store_true", help="log every TX/RX frame")
    ap.add_argument("--env-file", help="settings file (default: ./bridge.env if present)")
    ap.add_argument("--version", action="version", version=version_line(),
                    help="show which release is installed, then exit")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("probe", help="find out which control channels this projector answers on")
    sp.add_argument("--scan-baud", action="store_true", help="serial only: try common baud rates")

    sub.add_parser("status", help="power, input, volume, mute, light-source hours")
    for name in ("on", "off"):
        sp = sub.add_parser(name, help=f"power {name}")
        sp.add_argument("--wait", action="store_true", help="wait until warm-up/cool-down completes")
    sp = sub.add_parser("input", help="select an input by Alexa name, friendly name or code")
    sp.add_argument("name")
    sp = sub.add_parser("volume", help="N (0-50), +N, -N, up or down")
    sp.add_argument("value")
    sp = sub.add_parser("mute", help="on | off | toggle")
    sp.add_argument("value", choices=["on", "off", "toggle"])
    sp = sub.add_parser("key", help="press a remote key: " + ", ".join(p.REMOTE_KEYS) + ", or a code like 0x92")
    sp.add_argument("name")
    sp = sub.add_parser("learn-inputs", help="discover the source codes your projector uses")
    sp.add_argument("--scan", action="store_true", help="also try codes 0x00-0x30 to find every accepted input")
    sp.add_argument("--only", nargs="*", help="Alexa names to learn, e.g. 'HDMI 3' 'MEDIA PLAYER'")
    sp.add_argument("--settle", type=float, default=1.5, help="seconds to wait after each switch")
    sp = sub.add_parser("raw", help="send raw hex; a missing checksum byte is added with --checksum")
    sp.add_argument("hex")
    sp.add_argument("--checksum", action="store_true", help="append the checksum byte")
    sp.add_argument("--no-reply", action="store_true")
    sp = sub.add_parser("watch", help="print state changes until Ctrl-C")
    sp.add_argument("--interval", type=float, default=1.0)
    sp = sub.add_parser("trace", help="send power on/off, then log what the projector reports every second "
                                      "until it settles")
    sp.add_argument("action", nargs="?", choices=["on", "off"], help="what to send; leave out to just log")
    sp.add_argument("--seconds", type=float, help="stop after this long at most (default 300, or 120 "
                                                  "with no action)")
    sp.add_argument("--settle", type=float, default=15.0,
                    help="with an action: stop once the projector has stayed in the new state this long")
    sp.add_argument("--interval", type=float, default=1.0)
    sp.add_argument("--power-only", action="store_true",
                    help="ask only the power question (the X100-4K answers it even while booting)")
    sp = sub.add_parser("listen", help="send nothing at all; print whatever the projector says of its own "
                                       "accord (for example as it powers up)")
    sp.add_argument("--seconds", type=float, default=180.0, help="how long to listen (default 180)")
    sp = sub.add_parser("pjlink", help="run a PJLink command, e.g. 'POWR ?' or 'INST ?'")
    sp.add_argument("command")
    return ap


# ---------------------------------------------------------------------------


def open_projector(args) -> Projector:
    transport = make_transport(host=args.host, port=args.port, serial=args.serial, baud=args.baud,
                               timeout=args.timeout)
    return Projector(transport, one_way=args.one_way)


def try_call(fn, *a) -> Tuple[Optional[object], Optional[str]]:
    try:
        return fn(*a), None
    except FunctionDisabled:
        return None, "refused (disabled in current state)"
    except NotSupported as exc:
        return None, str(exc)
    except Unreachable as exc:
        return None, f"no answer ({exc})"
    except ProjectorError as exc:
        return None, str(exc)


def cmd_status(args, proj: Projector, inputs: InputMap) -> int:
    reading, err = try_call(proj.power_reading)
    if err:
        print(f"power:   {err}")
        return 1
    power = reading.state
    print(f"power:   {reading.describe()}")
    # In standby the X100-4K doesn't answer this one: skip the wait
    hours, herr = (None, "only while it's on") if reading.quirky else try_call(proj.light_source_hours)
    if power == PowerState.ON:
        code, cerr = (reading.source, None) if reading.source is not None else try_call(proj.source)
        if cerr:
            print(f"input:   {cerr}")
        else:
            name = inputs.name_for_code(code) or "unmapped (run learn-inputs)"
            print(f"input:   code 0x{code:02X} -> {name}")
        vol, verr = try_call(proj.volume)
        print(f"volume:  {verr or vol}")
        muted, merr = try_call(proj.muted)
        print(f"muted:   {merr or muted}")
    print(f"light source hours: {herr or hours}")
    return 0


def wait_for(proj: Projector, target: PowerState, limit: float = 90.0) -> bool:
    deadline = time.monotonic() + limit
    last = None
    while time.monotonic() < deadline:
        state, err = try_call(proj.power_state)
        if state != last:
            print(f"  power: {state.value if state else err}")
            last = state
        if state == target:
            return True
        time.sleep(1.0)
    return False


def cmd_power(args, proj: Projector, on: bool) -> int:
    (proj.power_on if on else proj.power_off)()
    print("sent power " + ("on" if on else "off"))
    if args.wait and not args.one_way:
        ok = wait_for(proj, PowerState.ON if on else PowerState.OFF)
        return 0 if ok else 1
    return 0


def cmd_input(args, proj: Projector, inputs: InputMap) -> int:
    d = inputs.find(args.name)
    if d is not None:
        if d.code is None:
            print(f"{d.alexa} has no code yet -- run `x100ctl learn-inputs`")
            return 2
        code = d.code
    else:
        try:
            code = p.parse_int(args.name)
        except ValueError:
            print(f"unknown input {args.name!r}; known: " + ", ".join(x.alexa for x in inputs.defs))
            return 2
    proj.select_source(code)
    print(f"selected source 0x{code:02X}" + (f" ({d.alexa})" if d else ""))
    return 0


def cmd_volume(args, proj: Projector) -> int:
    v = args.value.strip().lower()
    if v in ("up", "down"):
        (proj.volume_up if v == "up" else proj.volume_down)()
    elif v[0] in "+-":
        steps = int(v)
        for _ in range(abs(steps)):
            (proj.volume_up if steps > 0 else proj.volume_down)()
    else:
        proj.set_volume(int(v))
    if not args.one_way:
        vol, err = try_call(proj.volume)
        print(f"volume now {err or vol}")
    return 0


def cmd_mute(args, proj: Projector) -> int:
    if args.value == "toggle":
        state = proj.muted()
        proj.set_mute(not state)
    else:
        proj.set_mute(args.value == "on")
    if not args.one_way:
        print(f"muted: {proj.muted()}")
    return 0


def cmd_key(args, proj: Projector) -> int:
    name = args.name.lower()
    code = p.REMOTE_KEYS.get(name)
    if code is None:
        code = p.parse_int(name)
    proj.key(code)
    print(f"sent key 0x{code:02X}")
    return 0


def cmd_raw(args, proj: Projector) -> int:
    data = p.parse_hex_bytes(args.hex)
    if args.checksum:
        data += bytes([p.checksum(data)])
    print("TX", data.hex(" "))
    try:
        frame = proj.exchange(data, expect_reply=not args.no_reply)
    except Unreachable as exc:
        raw = proj.transport.last_raw_reply
        print(f"RX {raw.hex(' ') if raw else '(nothing)'} -- {exc}")
        return 1
    if frame is not None:
        print("RX", frame.raw.hex(" "), "->", p.describe(frame))
    return 0


def cmd_watch(args, proj: Projector, inputs: InputMap) -> int:
    last = None
    try:
        while True:
            power, err = try_call(proj.power_state)
            snap: Tuple = (power.value if power else err,)
            if power == PowerState.ON:
                code, _ = try_call(proj.source)
                vol, _ = try_call(proj.volume)
                muted, _ = try_call(proj.muted)
                snap += (inputs.name_for_code(code) or (f"0x{code:02X}" if code is not None else None), vol, muted)
            if snap != last:
                print(time.strftime("%H:%M:%S"), "power=%s" % snap[0],
                      *(f"{k}={v}" for k, v in zip(("input", "volume", "muted"), snap[1:])))
                last = snap
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def _short(err: str) -> str:
    return "refused" if err.startswith("refused") else "no answer" if err.startswith("no answer") else err


def _trace_sample(proj: Projector, inputs: InputMap, power_only: bool = False) -> dict:
    if power_only:
        value, perr = try_call(proj.power_probe)
        state = p.POWER_FROM_VALUE.get(value) if perr is None else None  # type: ignore[arg-type]
        shown = _short(perr) if perr is not None else (state.value if state is not None else str(value))
        return {"looks": "answers" if perr is None else "?", "power": shown,
                "input": "not asked", "volume": "not asked", "mute": "not asked"}
    power, perr = try_call(proj.power)
    # The others the way the bridge asks them: a short wait, as silence is an answer too
    code, cerr = try_call(proj.probe, p.SOURCE_STATUS)
    vol, verr = try_call(proj.probe, p.VOLUME_STATUS)
    muted, merr = try_call(proj.probe, p.MUTE_STATUS)
    muted = None if muted is None else bool(muted)
    name = inputs.name_for_code(code) if cerr is None else None
    if perr is not None or power != PowerState.ON:
        looks = power.value.lower() if perr is None else "?"
    elif cerr is None or cerr.startswith("refused"):
        looks = "on"
    elif verr is None and merr is None:
        looks = "standby"
    else:
        looks = "starting"
    return {
        "looks": looks,  # same rules as Projector.power_reading
        "power": power.value if perr is None else _short(perr),
        "input": (f"0x{code:02X}" + (f" {name}" if name else "")) if cerr is None else _short(cerr),
        "volume": str(vol) if verr is None else _short(verr),
        "mute": ("on" if muted else "off") if merr is None else _short(merr),
    }


def _trace_line(s: dict) -> str:
    return (f"{s['looks']:<9} power {s['power']:<9} input {s['input']:<13} volume {s['volume']:<9} "
            f"mute {s['mute']}")


def _segments(history: List[Tuple[float, dict]], label) -> str:
    runs: List[list] = []
    for t, s in history:
        value = label(s)
        if runs and runs[-1][0] == value:
            runs[-1][2] = t
        else:
            runs.append([value, t, t])
    shown = [f"{v} t+{a:.1f}-{b:.1f} s" for v, a, b in runs[:10]]
    if len(runs) > 10:
        shown.append(f"and {len(runs) - 10} more changes")
    return ", ".join(shown)


def cmd_listen(args, proj: Projector) -> int:
    """Sends nothing at all, and prints whatever arrives: what the projector says of its own accord."""
    limit = args.seconds
    print(f"Listening on {proj.transport.describe()} for {limit:.0f} s. Nothing is sent to the projector.")
    t0 = time.monotonic()
    total, first = 0, None
    try:
        while True:
            left = limit - (time.monotonic() - t0)
            if left <= 0:
                break
            data = proj.transport.listen(min(0.5, left))
            if not data:
                continue
            t = time.monotonic() - t0
            first = t if first is None else first
            total += len(data)
            for i in range(0, len(data), 24):
                chunk = data[i:i + 24]
                text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
                print(f"t+{t:6.1f}s  {chunk.hex(' '):<71}  |{text}|")
    except KeyboardInterrupt:
        pass
    print(f"\n{total} bytes received" + (f", the first at t+{first:.1f} s" if first is not None else
                                         ": nothing at all"))
    return 0


def cmd_trace(args, proj: Projector, inputs: InputMap) -> int:
    """For finding out how a projector reports a power change: one line a second, times from the command."""
    limit = args.seconds or (300.0 if args.action else 120.0)
    settled_when = {
        "off": lambda s: s["looks"] in ("off", "standby"),
        "on": lambda s: s["looks"] == "on",
    }.get(args.action)
    power_only = getattr(args, "power_only", False)
    print(f"before    {_trace_line(_trace_sample(proj, inputs, power_only))}")
    t0 = time.monotonic()
    if args.action:
        try:
            (proj.power_on if args.action == "on" else proj.power_off)()
        except ProjectorError as exc:
            print(f"sending power {args.action} failed: {exc}")
            return 1
        print(f"t+{time.monotonic() - t0:5.1f}s  sent power {args.action}; the projector acknowledged it")
    settle_note = f" once it has settled for {args.settle:.0f} s" if args.action else ""
    print(f"(a line a second; stops by itself{settle_note}, or after {limit:.0f} s)")
    history: List[Tuple[float, dict]] = []
    since: Optional[float] = None
    reason = f"stopped after {limit:.0f} s without settling" if args.action else f"stopped after {limit:.0f} s"
    tick = t0
    try:
        while time.monotonic() - t0 < limit:
            t = time.monotonic() - t0
            s = _trace_sample(proj, inputs, power_only)
            history.append((t, s))
            print(f"t+{t:5.1f}s  {_trace_line(s)}")
            if settled_when is not None and settled_when(s):
                since = t if since is None else since
                if t - since >= args.settle:
                    reason = f"settled: unchanged in the new state for {args.settle:.0f} s"
                    break
            else:
                since = None
            tick += args.interval
            time.sleep(max(0.0, tick - time.monotonic()))
    except KeyboardInterrupt:
        reason = "stopped with Ctrl+C"
    if history:
        print(f"\n{reason}. Summary, in seconds after the command:")
        print("  looks like:    " + _segments(history, lambda s: s["looks"]))
        print("  power query:   " + _segments(history, lambda s: s["power"]))
        print("  input:         " + _segments(history, lambda s: s["input"]))
    return 0


def cmd_learn(args, proj: Projector, inputs: InputMap) -> int:
    power, err = try_call(proj.power)
    if err:
        print(f"Can't read power state: {err}. learn-inputs needs a two-way link.")
        return 1
    if power != PowerState.ON:
        print("Turning the projector on first...")
        proj.power_on()
        if not wait_for(proj, PowerState.ON):
            print("Projector didn't reach ON")
            return 1
        time.sleep(2)

    if args.scan:
        print("\nScanning source codes 0x00-0x30 (the projector will switch inputs as we go)...")
        accepted: List[int] = []
        original, _ = try_call(proj.source)
        for code in range(0x00, 0x31):
            try:
                proj.select_source(code)
            except FunctionDisabled:
                continue
            except ProjectorError as exc:
                print(f"  0x{code:02X}: {exc}")
                continue
            time.sleep(args.settle)
            now, _ = try_call(proj.source)
            accepted.append(code)
            known = inputs.name_for_code(code)
            note = f"reads back 0x{now:02X}" if now is not None else "no read-back"
            print(f"  0x{code:02X}: accepted ({note})" + (f" -> {known}" if known else ""))
        if original is not None:
            proj.select_source(original)
        print("Accepted codes:", ", ".join(f"0x{c:02X}" for c in accepted) or "none")

    wanted = [inputs.find(n) for n in args.only] if args.only else inputs.defs
    if any(w is None for w in wanted):
        print("Unknown input in --only")
        return 2
    print("\nFor each input: select it on the projector with the remote, then press Enter.")
    print("Type 's' + Enter to skip an input you don't use.\n")
    changed = False
    for d in wanted:  # type: ignore[union-attr]
        current = f" (currently 0x{d.code:02X})" if d.code is not None else ""
        answer = input(f"Switch to {d.label or d.alexa}{current}, then Enter: ").strip().lower()
        if answer == "s":
            continue
        time.sleep(min(args.settle, 1.0))
        code, cerr = try_call(proj.source)
        if cerr:
            print(f"  couldn't read the source: {cerr}")
            continue
        other = inputs.name_for_code(code)
        if other and other != d.alexa:
            print(f"  code 0x{code:02X} is already mapped to {other}; it will move to {d.alexa}")
        inputs.set_code(d.alexa, code)
        changed = True
        print(f"  {d.alexa} = 0x{code:02X}")
    if changed:
        inputs.save(args.inputs)
        print(f"\nSaved {args.inputs}. Restart the bridge, then say \"Alexa, discover devices\".")
    return 0


def check_port(host: str, port: int, timeout: float = 1.5) -> str:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return "open"
    except socket.timeout:
        return "no response"
    except ConnectionRefusedError:
        return "closed"
    except OSError as exc:
        return f"error ({exc.strerror or exc})"


def probe_viewsonic(proj: Projector) -> Tuple[bool, str]:
    """Returns (two_way, summary)."""
    try:
        frame = proj.exchange(p.POWER_STATUS.encode(), expect_reply=True, timeout=3.0)
    except Unreachable as exc:
        raw = proj.transport.last_raw_reply
        if raw:
            return False, f"replied with unrecognised bytes: {raw.hex(' ')}"
        return False, f"no reply to a power-status read ({exc})"
    assert frame is not None
    if frame.is_reply:
        state = p.POWER_FROM_VALUE.get(frame.value, PowerState.UNKNOWN)
        described = state.value
        if state == PowerState.ON:  # the X100-4K says ON in standby too: ask the questions that tell
            reading, _ = try_call(proj.power_reading)
            described = reading.describe() if reading else described
        return True, f"two-way OK -- power is {described} (reply {frame.raw.hex(' ')})"
    return True, f"answered: {p.describe(frame)} ({frame.raw.hex(' ')})"


def cmd_probe(args, proj: Optional[Projector]) -> int:
    findings = []
    if args.serial:
        rates = BAUD_RATES if args.scan_baud else [args.baud]
        working = None
        for baud in rates:
            t = SerialTransport(args.serial, baudrate=baud, timeout=1.5)
            two_way, summary = probe_viewsonic(Projector(t))
            t.close()
            print(f"RS-232 {args.serial} @ {baud}: {summary}")
            if two_way:
                working = baud
                break
        if working:
            findings.append(f"Use X100_SERIAL={args.serial} X100_BAUD={working}")
        else:
            findings.append("No RS-232 answer: turn on Setting > Advanced Settings > RS-232 Control, and check "
                            "the cable. It should be null-modem (pins 2 and 3 crossed); if it is, try a "
                            "straight-through one, as projectors differ. Then try --scan-baud.")
    if args.host:
        print(f"Checking {args.host} ...")
        ports = [("web", 80, "web UI (LAN control settings)"),
                 ("viewsonic", args.port, "ViewSonic RS-232-over-LAN"),
                 ("pjlink", args.pjlink_port, "PJLink")]
        ports += [(f"extra{n}", pp, label) for n, (pp, label) in enumerate(EXTRA_PROBE_PORTS)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(ports)) as ex:
            states = list(ex.map(lambda item: check_port(args.host, item[1]), ports))
        results = {key: state for (key, _, _), state in zip(ports, states)}
        for (_key, port, label), state in zip(ports, states):
            print(f"  tcp/{port:<5} {label:32} {state}")
        if results["viewsonic"] == "open" and proj is not None:
            two_way, summary = probe_viewsonic(proj)
            print(f"ViewSonic protocol on {args.port}: {summary}")
            if two_way:
                findings.append(f"Use X100_HOST={args.host} (two-way LAN control works).")
                power, _ = try_call(proj.power)
                if power == PowerState.ON:
                    code, _ = try_call(proj.source)
                    vol, _ = try_call(proj.volume)
                    if code is not None:
                        print(f"  current source code 0x{code:02X}, volume {vol}")
            else:
                findings.append(f"Port {args.port} accepts connections but doesn't answer: try `x100ctl on` and "
                                "watch the projector. If it obeys, set X100_ONE_WAY=true (plus PJLink below for "
                                "power status).")
        if results["pjlink"] == "open":
            pj = PJLink(args.host, port=args.pjlink_port, password=args.pjlink_password)
            try:
                info = pj.info()
                print(f"PJLink on {args.pjlink_port}:")
                for k, v in info.items():
                    print(f"  {k}: {v}")
                findings.append(f"PJLink works: X100_PJLINK_HOST={args.host} gives the bridge a power-status source.")
            except PJLinkAuthError as exc:
                print(f"PJLink on {args.pjlink_port}: {exc} -- pass --pjlink-password (the web UI password)")
            except PJLinkError as exc:
                print(f"PJLink on {args.pjlink_port}: {exc}")
        if results["viewsonic"] != "open" and results["pjlink"] != "open":
            if results["web"] == "open":
                findings.append(f"Only the web UI answers. Open http://{args.host}/ , complete the password set-up, "
                                "and check Standby LAN Control is On (Settings > Network Settings).")
            else:
                findings.append("Nothing answers. Check the cable is in the projector's CONTROL port (not LAN), and "
                                "that Wired LAN Control shows an IP and Standby LAN Control is On.")
    if not args.host and not args.serial:
        print("Give --host (CONTROL-port IP) and/or --serial.")
        return 2
    print("\nNext:")
    for f in findings:
        print(" -", f)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    env_file = env_file_from_argv(argv)
    if env_file and "--version" not in argv:  # --version needs no settings, so works for any user
        try:
            load_env_file(env_file)
        except PermissionError:
            print(f"x100ctl: can't read {env_file} (it holds the API token). Run it as the service user: "
                  "sudo -u x100 .venv/bin/x100ctl --env-file bridge.env ...", file=sys.stderr)
            return 2
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    inputs = InputMap.load(args.inputs)

    if args.cmd == "pjlink":
        host = args.pjlink_host or args.host
        if not host:
            print("give --host or --pjlink-host")
            return 2
        try:
            body = args.command.strip()
            values = PJLink(host, port=args.pjlink_port, password=args.pjlink_password).run(body)
            print(values[0])
            return 0
        except PJLinkError as exc:
            print(f"PJLink error: {exc}")
            return 1

    if args.cmd == "probe":
        proj = None
        if args.host:
            proj = Projector(make_transport(host=args.host, port=args.port, timeout=3.0))
        try:
            return cmd_probe(args, proj)
        finally:
            if proj:
                proj.close()

    try:
        proj = open_projector(args)
    except ValueError as exc:
        print(exc)
        return 2
    try:
        if args.cmd == "status":
            return cmd_status(args, proj, inputs)
        if args.cmd in ("on", "off"):
            return cmd_power(args, proj, args.cmd == "on")
        if args.cmd == "input":
            return cmd_input(args, proj, inputs)
        if args.cmd == "volume":
            return cmd_volume(args, proj)
        if args.cmd == "mute":
            return cmd_mute(args, proj)
        if args.cmd == "key":
            return cmd_key(args, proj)
        if args.cmd == "raw":
            return cmd_raw(args, proj)
        if args.cmd == "trace":
            return cmd_trace(args, proj, inputs)
        if args.cmd == "watch":
            return cmd_watch(args, proj, inputs)
        if args.cmd == "listen":
            return cmd_listen(args, proj)
        if args.cmd == "learn-inputs":
            return cmd_learn(args, proj, inputs)
    except FunctionDisabled as exc:
        print(f"Projector refused the command ({exc}). Is it on, with a source showing?")
        return 1
    except Unreachable as exc:
        print(f"Projector unreachable: {exc}")
        return 1
    except (ProjectorError, ValueError) as exc:
        print(f"Error: {exc}")
        return 1
    finally:
        proj.close()
    return 2


if __name__ == "__main__":
    sys.exit(main())
