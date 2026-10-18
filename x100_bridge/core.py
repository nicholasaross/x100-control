"""Bridge logic: turns command messages into projector actions and reports state.

Nothing in here knows about HTTP or MQTT. Commands arrive through ``submit``
(the optional AWS link) or ``submit_and_wait`` (the local API, which blocks
until the command is acknowledged). State goes out through a ``publish``
callable that receives the "reported" document; in local-only mode it is a
no-op. That keeps the logic testable with the simulated projector.

Command message (local API, or Lambda -> bridge on ``x100/<thing>/cmd``)::

    {"id": "<uuid>", "ts": <epoch ms>, "ops": [{"op": "power", "value": "ON"}]}

    op            value
    power         "ON" | "OFF"
    input         Alexa input name ("HDMI 2") or a friendly name
    volume        0-50 (absolute, OSD scale)
    volume_delta  signed int
    mute          true | false
    key           remote key code (int)

Every command is answered by an ack that echoes the command id, together with
the projector state after the command. The ack goes out as soon as the
projector accepts the write; reading the full state back comes afterwards.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import socket
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional

from x100 import __version__
from x100.inputs import InputMap
from x100.pjlink import PJLink, PJLinkError
from x100.projector import FunctionDisabled, NotSupported, PowerReading, Projector, ProjectorError, Unreachable
from x100.protocol import VOLUME_MAX, PowerState
from x100.transport import TcpTransport

log = logging.getLogger("x100.bridge")

OP_KINDS = ("power", "input", "volume", "volume_delta", "mute", "key")


def system_uptime() -> Optional[float]:
    """Seconds since this computer started (Linux), or None if that can't be told."""
    try:
        with open("/proc/uptime") as fh:
            return float(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    v = os.environ.get(name)
    return float(v) if v not in (None, "") else default


@dataclass
class Config:
    thing_name: str = "x100-projector"
    iot_endpoint: str = ""
    iot_port: int = 8883
    cert_file: str = "certs/bridge.crt"
    key_file: str = "certs/bridge.key"
    ca_file: str = "certs/AmazonRootCA1.pem"
    mqtt_tls: bool = True
    inputs_file: str = "inputs.json"
    one_way: bool = False
    pjlink_host: Optional[str] = None
    pjlink_port: int = 4352
    pjlink_password: Optional[str] = None
    poll_interval: float = 30.0
    fast_poll_interval: float = 2.0
    fast_poll_window: float = 60.0
    heartbeat_interval: float = 60.0
    cmd_max_age: float = 60.0
    defer_timeout: float = 120.0
    auto_power_on: bool = True
    # How long the projector takes to start up and to shut down. One-way mode assumes these; with a
    # projector whose power query says "on" even in standby (the X100-4K), the bridge holds back an
    # "on" for cooldown_seconds after its own "off" (5 s tested fine on a real X100-4K), allows
    # warmup_seconds for start-up (a real one took about a minute), and lets the projector settle for
    # settle_seconds once on before running commands that were waiting for it.
    # A real X100-4K hangs if it's asked things while its main processor boots after a power cut:
    # every time the bridge (or x100ctl trace) polled it as it powered up, it ended in a standby it
    # wouldn't start from, even with its remote, until it was unplugged; left alone, it started fine.
    # It answers the power query throughout, so once it stops answering the bridge asks only that,
    # every outage_probe_seconds, and when it answers again sends nothing at all for
    # power_up_quiet_seconds. The same after this computer starts up, in case the mains went off.
    # It also hung after an "off" 66 s after "on", so for start_hold_seconds after it starts, by any
    # means, no power command goes to it: an "off" waits, and a repeated "on" isn't sent.
    # A power cut short enough to fall between two polls goes unseen, and booting looks just like
    # starting up, so the next poll's questions would hang it. So in standby the bridge asks only the
    # power query, every power_check_seconds, and asks the rest only when a command comes. Once it's on,
    # the same check runs between the usual polls. Any cut longer than this gets the quiet time above.
    # The price: in standby, it doesn't notice the remote starting it until a command comes. 0: off.
    power_check_seconds: float = 2.0
    # Otherwise the bridge learns all this from the first reading that shows it: standby, or a start.
    # After a power cut that can be a while, as its standby then sometimes answers like on.
    x100_quirks: bool = False       # this is an X100-4K: the rules above apply from the start
    warmup_seconds: float = 90.0
    cooldown_seconds: float = 5.0
    settle_seconds: float = 15.0
    start_hold_seconds: float = 120.0
    power_up_quiet_seconds: float = 300.0
    outage_probe_seconds: float = 30.0
    start_check_seconds: float = 2.0   # after "on", when to check it started (it stops answering volume)
    volume_step_delay: float = 0.05
    pending_check_interval: float = 1.0
    scenes: Dict[str, int] = field(default_factory=dict)  # Alexa scene name -> remote key code
    shadow_topic_prefix: str = "$aws/things"  # only changed for tests against a local broker
    local_api_bind: str = "127.0.0.1:8661"    # "off" disables the local API
    local_api_token: str = ""
    local_api_timeout: float = 10.0           # how long a local command waits for its ack

    @property
    def aws_enabled(self) -> bool:
        return bool(self.iot_endpoint)

    @property
    def local_api_enabled(self) -> bool:
        return self.local_api_bind.strip().lower() not in ("", "off", "none", "false", "0")

    @property
    def cmd_topic(self) -> str:
        return f"x100/{self.thing_name}/cmd"

    @property
    def shadow_update_topic(self) -> str:
        return f"{self.shadow_topic_prefix}/{self.thing_name}/shadow/update"

    @classmethod
    def from_env(cls) -> "Config":
        e = os.environ.get
        return cls(
            thing_name=e("THING_NAME", "x100-projector"),
            iot_endpoint=e("AWS_IOT_ENDPOINT", ""),
            iot_port=int(e("AWS_IOT_PORT", "8883")),
            cert_file=e("CERT_FILE", "certs/bridge.crt"),
            key_file=e("KEY_FILE", "certs/bridge.key"),
            ca_file=e("CA_FILE", "certs/AmazonRootCA1.pem"),
            mqtt_tls=_env_bool("MQTT_TLS", True),
            inputs_file=e("X100_INPUTS_FILE", "inputs.json"),
            one_way=_env_bool("X100_ONE_WAY", False),
            pjlink_host=e("X100_PJLINK_HOST") or None,
            pjlink_port=int(e("X100_PJLINK_PORT", "4352")),
            pjlink_password=e("X100_PJLINK_PASSWORD") or None,
            poll_interval=_env_float("POLL_INTERVAL", 30.0),
            heartbeat_interval=_env_float("HEARTBEAT_INTERVAL", 60.0),
            cmd_max_age=_env_float("CMD_MAX_AGE", 60.0),
            auto_power_on=_env_bool("AUTO_POWER_ON", True),
            warmup_seconds=_env_float("X100_WARMUP_SECONDS", 90.0),
            cooldown_seconds=_env_float("X100_COOLDOWN_SECONDS", 5.0),
            settle_seconds=_env_float("X100_SETTLE_SECONDS", 15.0),
            start_hold_seconds=_env_float("X100_START_HOLD_SECONDS", 120.0),
            power_up_quiet_seconds=_env_float("X100_POWER_UP_QUIET_SECONDS", 300.0),
            outage_probe_seconds=_env_float("X100_OUTAGE_PROBE_SECONDS", 30.0),
            power_check_seconds=_env_float("X100_POWER_CHECK_SECONDS", 2.0),
            x100_quirks=_env_bool("X100_QUIRKS", False),
            scenes=parse_scenes(e("X100_SCENES", "")),
            shadow_topic_prefix=e("SHADOW_TOPIC_PREFIX", "$aws/things"),
            local_api_bind=e("LOCAL_API_BIND", "127.0.0.1:8661"),
            local_api_token=e("LOCAL_API_TOKEN", ""),
            local_api_timeout=_env_float("LOCAL_API_TIMEOUT", 10.0),
        )


def parse_scenes(text: str) -> Dict[str, int]:
    """X100_SCENES='{"Projector Focus": "0x92"}' -> {"Projector Focus": 146}."""
    if not text or not text.strip():
        return {}
    raw = json.loads(text)
    if not isinstance(raw, dict):
        raise ValueError("X100_SCENES must be a JSON object of name -> key code")
    scenes: Dict[str, int] = {}
    for name, code in raw.items():
        value = int(code, 0) if isinstance(code, str) else int(code)
        if not 0 <= value <= 0xFF:
            raise ValueError(f"scene {name!r}: key code must be a byte")
        if not name.strip() or len(name) > 128:
            raise ValueError(f"scene name {name!r} must be 1-128 characters")
        scenes[name.strip()] = value
    return scenes


class BridgeError(Exception):
    """An error with an Alexa ErrorResponse type attached."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Pending:
    op: Dict[str, Any]
    wait_for: PowerState
    deadline: float
    cmd_id: str


@dataclass
class _Waiter:
    """A local-API caller blocked in ``submit_and_wait``."""
    event: threading.Event = field(default_factory=threading.Event)
    result: Optional[Dict[str, Any]] = None


@dataclass
class State:
    power: PowerState = PowerState.UNKNOWN
    power_since: float = field(default_factory=time.monotonic)
    input: Optional[str] = None
    input_code: Optional[int] = None
    volume: Optional[int] = None
    muted: Optional[bool] = None
    connected: bool = False

    def set_power(self, power: PowerState) -> None:
        if power != self.power:
            self.power = power
            self.power_since = time.monotonic()


class BridgeCore:
    def __init__(self, cfg: Config, projector: Projector, inputs: InputMap,
                 publish: Callable[[Dict[str, Any]], None], pjlink: Optional[PJLink] = None,
                 clock: Callable[[], float] = time.time, uptime: Optional[Callable[[], Optional[float]]] = None):
        """``uptime``: how long this computer has been running (see system_uptime), so that a bridge
        starting with the computer, perhaps after a mains cut, leaves a booting projector alone."""
        self.cfg = cfg
        self.proj = projector
        self.inputs = inputs
        self.publish = publish
        self.pjlink = pjlink
        self.clock = clock
        self.state = State()
        self.pending: Deque[Pending] = deque()
        self.queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self.seen_ids: Deque[str] = deque(maxlen=128)
        self._last_published: Optional[Dict[str, Any]] = None
        self._next_poll = 0.0
        self._next_full_poll = 0.0              # between full polls, just the power query (see Config)
        self._next_heartbeat = 0.0
        self._fast_until = 0.0
        self._next_pending_check = 0.0
        self._full_publish = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._waiters: Dict[str, _Waiter] = {}
        self._waiters_lock = threading.Lock()
        # For projectors whose power query says "on" in standby too (see Projector.power_reading):
        self._power_query_lies = cfg.x100_quirks
        self._sent_on: Optional[float] = None   # when we last switched it on / off (monotonic)
        self._sent_off: Optional[float] = None
        self._confirmed_at = 0.0                # when a reading last confirmed the power state
        self._started_at: Optional[float] = None  # when it last came out of standby, if we know
        self._seen = PowerState.UNKNOWN         # the latest reading, before _settle_power's adjustments
        self._last_reading: Optional[PowerReading] = None  # what it answered, for the log
        self._start_confirmed = False           # our last "on" was seen to start it
        # The link: it stopped answering (power queries only, now and then), or it has just started
        # answering again and may be booting (silence).
        self._lost_since: Optional[float] = None
        self._lost_said = False                 # logged that it isn't answering
        self._quiet_until = 0.0
        # After a power cut its standby can answer just like on, for hours. So since a cut, or since the
        # bridge started (it can't know what happened before), until it's seen in standby or starting, a
        # reading that says "on" may be wrong. Holds what it's been doubted since, or "" once it can tell.
        self._doubt_on = ""
        self._uptime = uptime

    # ------------------------------------------------------------------ intake
    def submit(self, payload: Any) -> None:
        """Called from the MQTT thread. Validates and queues; never blocks."""
        try:
            msg = json.loads(payload) if isinstance(payload, (bytes, str)) else dict(payload)
        except (ValueError, TypeError):
            log.warning("ignoring non-JSON command: %r", payload[:200] if payload else payload)
            return
        if not isinstance(msg, dict):
            log.warning("ignoring command that isn't an object: %r", msg)
            return
        self.queue.put(msg)

    def submit_and_wait(self, ops: List[Dict[str, Any]], timeout: float = 10.0) -> Dict[str, Any]:
        """Queue a command and block until the bridge loop acknowledges it.

        Used by the local API, from its own threads. Returns ``ok``, ``deferred``,
        ``code``/``message`` on failure, and the projector ``state`` straight
        after the command. Raises ``TimeoutError`` if no ack arrives in time
        (for example, the bridge loop isn't running).
        """
        cmd_id = uuid.uuid4().hex
        waiter = _Waiter()
        with self._waiters_lock:
            self._waiters[cmd_id] = waiter
        try:
            self.queue.put({"id": cmd_id, "ts": int(self.clock() * 1000), "ops": ops})
            if not waiter.event.wait(timeout):
                raise TimeoutError(f"the bridge didn't answer within {timeout:g}s")
            assert waiter.result is not None
            return waiter.result
        finally:
            with self._waiters_lock:
                self._waiters.pop(cmd_id, None)

    def request_full_publish(self) -> None:
        """Called after (re)connecting to AWS: push the whole state again."""
        self._full_publish.set()

    # ------------------------------------------------------------------ loop
    def start(self) -> None:
        if self._power_query_lies:
            self._say_quirky("X100_QUIRKS is set")
        self._doubt_on = "the bridge started"
        up = self._uptime() if self._uptime is not None else None
        if up is not None and up < self.cfg.power_up_quiet_seconds:
            self._quiet_for(self.cfg.power_up_quiet_seconds - up)
            self._doubt_on = "this computer started"
            log.info("this computer started %.0f s ago, perhaps with the projector after a power cut: leaving the "
                     "projector alone until %s, as questions while it boots can hang it", up, self._quiet_end())
        self._thread = threading.Thread(target=self._run, name="bridge-core", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.step(timeout=0.25)
            except Exception:  # pragma: no cover - keep the bridge alive
                log.exception("unexpected error in bridge loop")
                time.sleep(1)

    def step(self, timeout: float = 0.0) -> None:
        """One iteration: handle a command if one arrives, otherwise housekeeping."""
        try:
            msg = self.queue.get(timeout=timeout) if timeout else self.queue.get_nowait()
        except queue.Empty:
            msg = None
        if msg is not None:
            self._handle(msg)
        now = time.monotonic()
        if self.pending and now >= self._next_pending_check:
            self._next_pending_check = now + self.cfg.pending_check_interval
            self._run_pending()
        if now >= self._next_poll:
            self.poll()
        if self._full_publish.is_set() or now >= self._next_heartbeat:
            self._full_publish.clear()
            self._publish_state(force=True)

    # ------------------------------------------------------------------ commands
    def _handle(self, msg: Dict[str, Any]) -> None:
        cmd_id = msg.get("id")
        if not isinstance(cmd_id, str) or not cmd_id:
            log.warning("ignoring command without id: %s", msg)
            return
        if cmd_id in self.seen_ids:
            log.info("duplicate delivery of %s ignored", cmd_id)
            return
        self.seen_ids.append(cmd_id)
        ts = msg.get("ts")
        if isinstance(ts, (int, float)) and self.cfg.cmd_max_age > 0:
            age = self.clock() - ts / 1000.0
            if age > self.cfg.cmd_max_age:
                log.warning("dropping %s: %.0fs old (check the bridge clock if this repeats)", cmd_id, age)
                return
            if age < -5:
                log.warning("command %s is from %.0fs in the future -- is this machine's clock right?", cmd_id, -age)
        ops = msg.get("ops")
        if not isinstance(ops, list) or not ops:
            self._ack(cmd_id, BridgeError("INVALID_DIRECTIVE", "no ops"))
            return
        if not all(isinstance(op, dict) for op in ops):
            self._ack(cmd_id, BridgeError("INVALID_DIRECTIVE", "each op must be an object"))
            return
        log.info("command %s: %s", cmd_id, ops)
        error: Optional[BridgeError] = None
        deferred = False
        try:
            for op in ops:
                deferred = self._apply(op, cmd_id) or deferred
        except BridgeError as exc:
            error = exc
        except Unreachable as exc:
            self._link_failed()
            error = BridgeError("ENDPOINT_UNREACHABLE", f"projector unreachable: {exc}")
        except FunctionDisabled as exc:
            error = BridgeError("NOT_IN_OPERATION", f"projector refused the command: {exc}")
        except (ProjectorError, ValueError) as exc:
            error = BridgeError("INTERNAL_ERROR", str(exc))
        self._fast_poll()
        # Answer first. The state in the ack already reflects the write (_apply
        # records it), and reading everything back costs several round trips.
        self._ack(cmd_id, error, deferred=deferred)
        if error is None and not deferred and not self.cfg.one_way:
            self._refresh_quietly()
            self._publish_state()

    def _current_power(self) -> PowerState:
        if self.cfg.one_way:
            return self._optimistic_power()
        if self._in_cooldown():
            return self.state.power  # nothing a reading could add: it already looks like standby
        return self._settle_power(self._read_power().state)

    def _read_power(self) -> PowerReading:
        try:
            reading = self.proj.power_reading()
        except Unreachable:
            self._link_failed()
            raise
        self.state.connected = True
        if reading.quirky and not self._power_query_lies:
            self._power_query_lies = True
            self._say_quirky("this projector answers 'on' to the power query in standby too")
        s = self.state
        if reading.source is not None:
            s.input_code = reading.source
            s.input = self.inputs.name_for_code(reading.source)
        if reading.volume is not None:
            s.volume = reading.volume
        if reading.muted is not None:
            s.muted = reading.muted
        self._last_reading = reading
        return reading

    def _say_quirky(self, why: str) -> None:
        log.info("%s: telling standby, start-up and on apart from its other answers", why)
        if self.cfg.power_check_seconds > 0:
            log.info("in standby, only asking it whether it has power, every %g s, as other questions "
                     "hang it if it's booting after a power blip: the rest waits for a command",
                     self.cfg.power_check_seconds)

    def _in_cooldown(self) -> bool:
        return (self._power_query_lies and self._sent_off is not None
                and time.monotonic() - self._sent_off < self.cfg.cooldown_seconds)

    def _settle_power(self, seen: PowerState) -> PowerState:
        """The state the bridge acts on: a reading, adjusted for our own recent power commands.

        Only for a projector whose power query can't be trusted (the X100-4K). Straight after
        "off" it already looks like standby, but needs a moment before it takes an "on". While it
        starts up its answers are patchy, sometimes looking like standby for a few seconds, so
        within warmup_seconds of our "on" only an answer about its input counts.
        """
        now = time.monotonic()
        self._seen = seen
        power = seen
        adjusted = ""  # why the bridge takes it as something other than the reading, for the log
        if self._power_query_lies:
            if seen in (PowerState.OFF, PowerState.WARMING):
                self._doubt_on = ""  # a standby, or a start, that can't be mistaken for on
            if self._in_cooldown():
                power = PowerState.ON if seen == PowerState.ON else PowerState.COOLING
                adjusted = "the bridge has just sent 'off'"
            elif self._sent_on is not None and now - self._sent_on < self.cfg.warmup_seconds:
                adjusted = f"the bridge sent 'on' {now - self._sent_on:.0f} s ago"
                if seen == PowerState.ON:
                    power = PowerState.ON
                elif self.state.power == PowerState.ON:
                    # Just after it first answers, a gap is a start-up hiccup; later, believe it.
                    settling = now - self.state.power_since < self.cfg.settle_seconds
                    power = PowerState.ON if settling else seen
                else:
                    power = PowerState.WARMING  # never resent: a second "on" while it starts can hang it
        prev = self.state.power
        if power == PowerState.OFF:
            if not self._start_held():
                self._started_at = None
        elif self._started_at is None and (
                (power == PowerState.WARMING and prev != PowerState.WARMING)  # from on too: see _doubt_on
                or (power == PowerState.ON and prev in (PowerState.OFF, PowerState.COOLING))):
            self._started_at = now  # out of standby without us: its remote, or its own button
            if power == PowerState.WARMING and self._power_query_lies:
                # As it looks while it boots after a power cut, too: then the log can tell which it was.
                log.info("only the projector's power query answers, and the bridge didn't start it: its remote or "
                         "its own button did, or it's booting after a power blip")
            else:
                log.info("the projector is %s, and the bridge didn't start it: its remote or its own button did",
                         "starting up" if power == PowerState.WARMING else "on")
        reading = self._last_reading
        looked = reading.describe() if reading is not None and reading.state == seen else seen.value
        self._set_power(power, f"reading {looked}" + (f", taken as {power.value}: {adjusted}" if power != seen else ""))
        self._confirmed_at = now
        return power

    def _set_power(self, power: PowerState, why: str) -> None:
        """Every change in the power state the bridge acts on goes through here, and into the log: after an
        incident, that shows what the projector looked like and when."""
        old = self.state.power
        if power != old and old != PowerState.UNKNOWN:  # the first reading has its own line
            log.info("projector: %s -> %s (%s)", old.value, power.value, why)
        self.state.set_power(power)

    def _settling(self) -> bool:
        """Only just on after starting up: the X100-4K's answers keep changing for a few seconds."""
        s = self.state
        return (self._power_query_lies and s.power == PowerState.ON
                and time.monotonic() - s.power_since < self.cfg.settle_seconds)

    def _start_held(self) -> bool:
        """Within start_hold_seconds of it starting: no power command goes to an X100-4K (see Config)."""
        return (self._power_query_lies and self._started_at is not None
                and time.monotonic() - self._started_at < self.cfg.start_hold_seconds)

    def _confirm_start(self) -> None:
        """Only say "ok" to an "on" once the X100-4K has started: hung, it acknowledges "on" and stays
        in standby. Starting from standby, it stops answering about its volume at once, so a couple of
        seconds tell. If it hasn't, say so, but don't send another "on": that can hang it."""
        time.sleep(self.cfg.start_check_seconds)
        if not self.proj.answers_volume():
            self._start_confirmed = True
            return
        log.warning("the projector acknowledged power on but hasn't started")
        raise BridgeError("NOT_IN_OPERATION", "the projector acknowledged 'on' but hasn't started. If it was "
                          "switched on at the wall in the last few minutes it may still start; if not, and its "
                          "remote can't start it either, switch it off at the wall for 30 seconds")

    def _believed_off(self) -> bool:
        """Off by a recent reading: an "on" can go straight out without asking first."""
        s = self.state
        return (not self.cfg.one_way and s.connected and s.power == PowerState.OFF
                and time.monotonic() - self._confirmed_at < self.cfg.poll_interval + 5)

    def _optimistic_power(self) -> PowerState:
        if self.pjlink is not None:
            try:
                power = self.pjlink.power()
                self._set_power(power, "PJLink says so")
                self.state.connected = True
                return power
            except PJLinkError as exc:
                log.debug("PJLink power read failed: %s", exc)
        s = self.state
        elapsed = time.monotonic() - s.power_since
        if s.power == PowerState.WARMING and elapsed >= self.cfg.warmup_seconds:
            self._set_power(PowerState.ON, "one-way: its warm-up time has passed")
        elif s.power == PowerState.COOLING and elapsed >= self.cfg.cooldown_seconds:
            self._set_power(PowerState.OFF, "one-way: its cool-down time has passed")
        return s.power

    def _defer(self, op: Dict[str, Any], wait_for: PowerState, cmd_id: str) -> bool:
        deadline = time.monotonic() + self.cfg.defer_timeout
        held_off = op.get("op") == "power" and wait_for == PowerState.ON and self._start_held()
        if held_off and self._started_at is not None:  # it may wait out the whole hold, then expire as usual
            deadline = max(deadline, self._started_at + self.cfg.start_hold_seconds + self.cfg.defer_timeout)
        self.pending.append(Pending(op, wait_for, deadline, cmd_id))
        if held_off:
            log.info("deferring %s until %.0f s after the projector started (sooner, it can hang)",
                     op, self.cfg.start_hold_seconds)
        elif wait_for == PowerState.ON and self._settling():
            log.info("deferring %s until the projector has settled (it has only just come on)", op)
        else:
            log.info("deferring %s until projector is %s", op, wait_for.value)
        return True

    def _power_on_pending(self) -> bool:
        return any(p.op.get("op") == "power" and p.op.get("value") == "ON" for p in self.pending)

    def _apply(self, op: Dict[str, Any], cmd_id: str, from_pending: bool = False) -> bool:
        """Run (or defer) one op. Returns True if it was deferred.

        ``from_pending`` is set when the op is being replayed from the deferred
        queue, so it must not queue itself behind ops that came after it.
        """
        kind = op.get("op")
        value = op.get("value")
        if kind not in OP_KINDS:  # before touching the projector, so a typo isn't reported as "unreachable"
            raise BridgeError("INVALID_DIRECTIVE", f"unknown op {kind!r}")
        if not self.cfg.one_way:
            self._check_link()
        if kind == "power" and str(value).upper() == "ON" and self._believed_off():
            power = PowerState.OFF  # just switch it on: asking first would only add a delay
        else:
            power = self._current_power()
        queued = bool(self.pending) and not from_pending

        if kind == "power":
            want = str(value).upper()
            if want not in ("ON", "OFF"):
                raise BridgeError("INVALID_VALUE", f"power must be ON or OFF, not {value!r}")
            if not from_pending:  # the latest power command wins over any still waiting
                waiting = [p for p in self.pending if p.op.get("op") == "power"]
                if waiting:
                    self.pending = deque(p for p in self.pending if p.op.get("op") != "power")
                    log.info("dropping queued %s: superseded by power %s", [p.op for p in waiting], want)
            if want == "ON":
                if power == PowerState.ON:
                    self._check_not_cold(power)
                    return False
                if power == PowerState.WARMING or self._start_held():
                    if self._seen == PowerState.OFF and not self._start_confirmed:
                        # Started (or asked to) but it looks like standby: another "on" this soon can hang it.
                        since = self._started_at or self._sent_on
                        ago = f" {time.monotonic() - since:.0f} s ago" if since is not None else ""
                        raise BridgeError("NOT_IN_OPERATION", f"the projector was started{ago} but looks like "
                                          "it's in standby, and another 'on' this soon can hang it. If it hasn't "
                                          "started in a minute or two, switch it off at the wall for 30 seconds")
                    return False  # it's starting up
                if power == PowerState.COOLING:
                    return self._defer(op, PowerState.OFF, cmd_id)
                self.proj.power_on()
                self._set_power(PowerState.WARMING, "the bridge sent 'on'")
                self._sent_on, self._sent_off, self._start_confirmed = time.monotonic(), None, False
                self._doubt_on = ""
                self._started_at = self._sent_on
                if self._power_query_lies:
                    self._confirm_start()
                return False
            if power in (PowerState.OFF, PowerState.COOLING):
                return False
            if power == PowerState.WARMING or self._settling() or self._start_held():
                return self._defer(op, PowerState.ON, cmd_id)
            self.pending.clear()  # nothing queued should run after we turn off
            self.proj.power_off()
            self._set_power(PowerState.COOLING, "the bridge sent 'off'")
            self._sent_off, self._sent_on, self._started_at = time.monotonic(), None, None
            return False

        if kind == "input":
            d = self.inputs.find(str(value))
            if d is None or d.code is None:
                raise BridgeError("INVALID_VALUE", f"input {value!r} isn't configured")
            if power in (PowerState.OFF, PowerState.COOLING):
                if not self.cfg.auto_power_on:
                    raise BridgeError("NOT_IN_OPERATION", "projector is off")
                self._apply({"op": "power", "value": "ON"}, cmd_id)
                return self._defer({"op": "input", "value": d.alexa}, PowerState.ON, cmd_id)
            if power == PowerState.WARMING or queued:
                return self._defer({"op": "input", "value": d.alexa}, PowerState.ON, cmd_id)
            self._check_not_cold(power)
            self._select_input(d.alexa)
            return False

        if kind in ("volume", "volume_delta", "mute", "key"):
            self._validate(kind, value)
            if power == PowerState.WARMING or (queued and power != PowerState.OFF) or \
                    (power in (PowerState.OFF, PowerState.COOLING) and self._power_on_pending()):
                return self._defer(op, PowerState.ON, cmd_id)
            if power in (PowerState.OFF, PowerState.COOLING):
                raise BridgeError("NOT_IN_OPERATION", "projector is off")
            self._run_simple(kind, value)
            return False

        raise BridgeError("INVALID_DIRECTIVE", f"unknown op {kind!r}")

    def _check_not_cold(self, power: PowerState) -> None:
        """After a power cut, the X100-4K's standby can answer just like on (see _doubt_on): until a reading
        has shown it in standby or starting, "on" isn't believed, and nothing is sent that would need it on."""
        if self._power_query_lies and self._doubt_on and power == PowerState.ON:
            raise BridgeError("NOT_IN_OPERATION", "the projector looks on, but after a power cut its standby can "
                              "look just the same, and it hasn't been seen in standby or starting since "
                              f"{self._doubt_on}, so the bridge has sent nothing. If it isn't on, start it with "
                              "its remote this once: after that the bridge can tell again")

    @staticmethod
    def _validate(kind: str, value: Any) -> None:
        if kind == "volume" and (not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= VOLUME_MAX):
            raise BridgeError("VALUE_OUT_OF_RANGE", f"volume must be 0-{VOLUME_MAX}")
        if kind == "volume_delta" and (not isinstance(value, int) or isinstance(value, bool)):
            raise BridgeError("INVALID_VALUE", "volume_delta must be an integer")
        if kind == "mute" and not isinstance(value, bool):
            raise BridgeError("INVALID_VALUE", "mute must be true or false")
        if kind == "key" and (not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 0xFF):
            raise BridgeError("INVALID_VALUE", "key must be a byte")

    def _select_input(self, alexa_name: str) -> None:
        d = self.inputs.find(alexa_name)
        assert d is not None and d.code is not None
        self.proj.select_source(d.code)
        self.state.input, self.state.input_code = d.alexa, d.code
        if self.cfg.one_way:
            self.state.muted = False

    def _run_simple(self, kind: str, value: Any) -> None:
        s = self.state
        if kind == "mute":
            self.proj.set_mute(bool(value))
            s.muted = bool(value)
        elif kind == "key":
            self.proj.key(int(value))
        elif kind == "volume":
            self._set_volume(int(value))
        elif kind == "volume_delta":
            delta = int(value)
            if self.cfg.one_way:
                for _ in range(min(abs(delta), VOLUME_MAX)):
                    (self.proj.volume_up if delta > 0 else self.proj.volume_down)()
                    time.sleep(self.cfg.volume_step_delay)
                if s.volume is not None:
                    s.volume = max(0, min(VOLUME_MAX, s.volume + delta))
            else:
                current = self.proj.volume()
                self._set_volume(max(0, min(VOLUME_MAX, current + delta)), current=current)

    def _set_volume(self, target: int, current: Optional[int] = None) -> None:
        try:
            self.proj.set_volume(target)
        except FunctionDisabled:
            # Some firmware may not take absolute volume: step there instead.
            if self.cfg.one_way:
                raise
            if current is None:
                current = self.proj.volume()
            delta = target - current
            log.info("absolute volume refused; stepping %+d", delta)
            for _ in range(abs(delta)):
                (self.proj.volume_up if delta > 0 else self.proj.volume_down)()
                time.sleep(self.cfg.volume_step_delay)
        self.state.volume = target

    def _run_pending(self) -> None:
        """Run deferred ops in order once the projector reaches the state they need."""
        if self._quiet() or self._link_down():
            return  # nothing may be asked: they wait (and expire as usual)
        while self.pending:
            head = self.pending[0]
            if time.monotonic() > head.deadline:
                log.warning("giving up on deferred %s (projector never became %s)", head.op, head.wait_for.value)
                self.pending.popleft()
                continue
            try:
                power = self._current_power()
            except Unreachable:
                return
            if power != head.wait_for:
                return
            is_power = head.op.get("op") == "power"
            if power == PowerState.ON and (self._settling() or (is_power and self._start_held())):
                return  # just started: give its input and settings a few seconds to settle, and an "off" longer
            self.pending.popleft()
            try:
                self._apply(head.op, head.cmd_id, from_pending=True)
                log.info("ran deferred %s", head.op)
            except (BridgeError, ProjectorError) as exc:
                log.warning("deferred %s failed: %s", head.op, exc)
            self._fast_poll()
            self._publish_state()

    # ------------------------------------------------------------------ polling
    def _fast_poll(self) -> None:
        self._fast_until = time.monotonic() + self.cfg.fast_poll_window
        self._next_full_poll = min(self._next_full_poll, time.monotonic() + self.cfg.fast_poll_interval)
        self._next_poll = min(self._next_poll, self._next_full_poll)

    def poll(self) -> None:
        if self.cfg.one_way:
            self._poll_one_way()
            self._schedule_poll()
        elif self._quiet():
            self._next_poll = self._quiet_until  # it has only just got power back: not a word
        elif self._link_down():
            self._probe_link()
        elif self._checking_power() and (self._in_standby() or time.monotonic() < self._next_full_poll):
            self._check_power()
        else:
            self._refresh_quietly()
            if self._link_down():  # unanswered: check again shortly, asking only about power
                self._next_poll = time.monotonic() + self.cfg.fast_poll_interval
            else:
                self._schedule_poll()
        self._publish_state()

    def _schedule_poll(self) -> None:
        # From the *new* state, so a warm-up started with the remote is followed closely too.
        now = time.monotonic()
        busy = self.pending or self.state.power in (PowerState.WARMING, PowerState.COOLING)
        fast = busy or now < self._fast_until
        self._next_full_poll = now + (self.cfg.fast_poll_interval if fast else self.cfg.poll_interval)
        self._next_poll = self._next_full_poll
        if self._checking_power():
            self._next_poll = min(self._next_poll, now + self.cfg.power_check_seconds)

    def _checking_power(self) -> bool:
        """An X100-4K: in standby, only the power query; when it's on, that between full polls (see Config)."""
        return self._power_query_lies and self.state.connected and self.cfg.power_check_seconds > 0

    def _in_standby(self) -> bool:
        """As far as the bridge can tell, and not just seen starting: its answers dip as it starts."""
        return self.state.power == PowerState.OFF and not self._start_held()

    def _check_power(self) -> None:
        """Just the power query, which the X100-4K answers even while it boots. If it goes unanswered, it
        may have lost power, however briefly: from then on it's treated as after a power cut."""
        now = time.monotonic()
        try:
            self.proj.power_probe()
        except ProjectorError as exc:
            log.warning("projector unreachable: %s", exc)
            self._link_failed()
            self._next_poll = now + self.cfg.fast_poll_interval  # ask again shortly, still only about power
            return
        self._next_poll = now + self.cfg.power_check_seconds
        if not self._in_standby():
            self._next_poll = min(self._next_poll, self._next_full_poll)

    # ------------------------------------------------------------------ the link to the projector
    def _quiet(self) -> bool:
        return time.monotonic() < self._quiet_until

    def _quiet_for(self, seconds: float) -> None:
        self._quiet_until = time.monotonic() + seconds
        self._next_poll = self._quiet_until

    def _quiet_end(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.clock() + self._quiet_until - time.monotonic()))

    def _link_down(self) -> bool:
        return self._lost_since is not None

    def _link_failed(self) -> None:
        """It didn't answer. Until it answers the power query again, only that is asked: soon, then every
        outage_probe_seconds. Even a moment's silence counts, as it may have restarted."""
        self.state.connected = False
        if self._lost_since is None:
            self._lost_since, self._lost_said = time.monotonic(), False
            self._next_poll = min(self._next_poll, self._lost_since + self.cfg.fast_poll_interval)

    def _check_link(self) -> None:
        """Before anything is sent: not a word to a projector that's booting, or isn't answering."""
        if self._quiet():
            raise BridgeError("NOT_IN_OPERATION", f"the projector may still be booting after a power cut, so it's "
                              f"being left alone until {self._quiet_end()}: questions while it boots can hang it. "
                              "Its remote works sooner, once it has started up")
        if self._link_down():
            raise BridgeError("ENDPOINT_UNREACHABLE", "the projector isn't answering (switched off at the wall?). "
                              "Once it's back, the bridge leaves it alone for a few minutes while it boots")

    def _probe_link(self) -> None:
        """While it isn't answering, ask only the power query: the X100-4K answers that even while
        booting, when any other question can hang it."""
        now = time.monotonic()
        try:
            self.proj.power_probe()
        except (Unreachable, ProjectorError):
            if not self._lost_said:
                self._lost_said = True
                log.warning("the projector isn't answering (switched off at the wall?): until it does, only "
                            "asking whether it has power, every %.0f s", self.cfg.outage_probe_seconds)
            self._next_poll = now + self.cfg.outage_probe_seconds
            return
        # Answering again, and perhaps booting (a power cut, or a restart): whatever it was doing is over.
        assert self._lost_since is not None
        self.state.connected = True
        log.info("the projector is answering again after %.0f s: leaving it alone until %s in case it's "
                 "booting (questions then can hang it)", now - self._lost_since,
                 time.strftime("%H:%M:%S", time.localtime(self.clock() + self.cfg.power_up_quiet_seconds)))
        self._lost_since = None
        self._sent_on = self._sent_off = self._started_at = None
        self._start_confirmed = False
        self._doubt_on, self._confirmed_at = "a power cut", 0.0  # no reading from before the cut counts now
        self._set_power(PowerState.OFF, "answering again after a cut: it comes back in standby")
        self._quiet_for(self.cfg.power_up_quiet_seconds)

    def _refresh_quietly(self) -> None:
        s = self.state
        if self._in_cooldown():
            return  # it looks like standby straight away; there's nothing to learn until it can restart
        was_connected = s.connected
        try:
            reading = self._read_power()
        except Unreachable as exc:
            if was_connected:
                log.warning("projector unreachable: %s", exc)
            return
        except ProjectorError as exc:
            log.warning("power read failed: %s", exc)
            return
        power = self._settle_power(reading.state)
        if not was_connected:
            log.info("projector reachable (power %s)", reading.describe())
        if power != PowerState.ON:
            return
        getters = [("volume", self.proj.volume), ("muted", self.proj.muted)]
        if reading.source is None:
            getters.insert(0, ("input_code", self.proj.source))
        for name, getter in getters:
            if not self.queue.empty():
                return  # a command is waiting: let it go first, the next poll finishes the job
            try:
                setattr(s, name, getter())
            except FunctionDisabled:
                # e.g. mute/volume unavailable with no signal: leave last value
                pass
            except (Unreachable, ProjectorError, NotSupported) as exc:
                log.debug("%s read failed: %s", name, exc)
        s.input = self.inputs.name_for_code(s.input_code)

    def _poll_one_way(self) -> None:
        s = self.state
        self._optimistic_power()
        if self.pjlink is not None and s.connected:
            return
        transport = self.proj.transport
        if isinstance(transport, TcpTransport):
            try:
                with socket.create_connection((transport.host, transport.port), timeout=2.0):
                    s.connected = True
            except OSError:
                s.connected = False
        else:
            s.connected = True

    # ------------------------------------------------------------------ publishing
    def snapshot(self) -> Dict[str, Any]:
        s = self.state
        return {
            "connected": s.connected,
            "power": s.power.value,
            "input": s.input,
            "volume": s.volume,
            "muted": s.muted,
            "inputs": self.inputs.discovery_inputs(),
            "scenes": [{"name": n, "key": k} for n, k in sorted(self.cfg.scenes.items())],
        }

    def _publish_state(self, force: bool = False, extra: Optional[Dict[str, Any]] = None) -> None:
        snap = self.snapshot()
        if not force and extra is None and snap == self._last_published:
            return
        doc = dict(snap)
        if force or extra is not None:
            doc["bridge"] = {
                "heartbeat": int(self.clock()),
                "online": True,
                "version": __version__,
                "mode": "one-way" if self.cfg.one_way else "two-way",
                "link": self.proj.transport.describe(),
            }
            self._next_heartbeat = time.monotonic() + self.cfg.heartbeat_interval
        if extra:
            doc.update(extra)
        try:
            self.publish(doc)
            self._last_published = snap
        except Exception as exc:  # pragma: no cover - publish failures are retried by the next update
            log.warning("publish failed: %s", exc)

    def compact_state(self) -> Dict[str, Any]:
        s = self.state
        return {"connected": s.connected, "power": s.power.value, "input": s.input,
                "volume": s.volume, "muted": s.muted}

    def _ack(self, cmd_id: str, error: Optional[BridgeError], deferred: bool = False) -> None:
        ack: Dict[str, Any] = {"id": cmd_id, "ok": error is None, "ts": int(self.clock() * 1000)}
        if deferred:
            ack["deferred"] = True
        if error is not None:
            ack["code"], ack["message"] = error.code, error.message
            log.warning("command %s failed: %s %s", cmd_id, error.code, error.message)
        with self._waiters_lock:
            waiter = self._waiters.get(cmd_id)
        if waiter is not None:  # a local-API caller is blocked on this command
            waiter.result = {"id": cmd_id, "ok": ack["ok"], "deferred": deferred,
                             "state": self.compact_state()}
            if error is not None:
                waiter.result["code"], waiter.result["message"] = error.code, error.message
            waiter.event.set()
        self._publish_state(extra={"ack": ack})

    def offline_document(self) -> Dict[str, Any]:
        return {"bridge": {"heartbeat": int(self.clock()), "online": False, "version": __version__}}


def describe_ops(ops: List[Dict[str, Any]]) -> str:
    return ", ".join(f"{o.get('op')}={o.get('value')}" for o in ops)
