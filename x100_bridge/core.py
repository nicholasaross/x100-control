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
from collections import Counter, deque
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
    # It misses a reply to the power query now and then (about once a day at one check every 2 s, from
    # 21 October), so a check waits power_check_wait for its answer, and silence counts as a cut only
    # after power_check_tries tries in a row.
    power_check_wait: float = 2.0
    power_check_tries: int = 2
    # Once, 31 s after its picture appeared, it didn't answer about its input in time while on (busy, perhaps
    # with its menu open), and so looked like standby. As standby means power checks only, that stuck. So a
    # standby reading straight after it was on is believed only once a second reading, standby_confirm_seconds
    # later and waiting confirm_input_wait for the answer about its input, agrees. 0: believe it at once.
    standby_confirm_seconds: float = 15.0
    confirm_input_wait: float = 2.0
    # Otherwise the bridge learns all this from the first reading that shows it: standby, or a start.
    # After a power cut that can be a while, as its standby then sometimes answers like on.
    x100_quirks: bool = False       # this is an X100-4K: the rules above apply from the start
    # A smart plug on the projector's mains sees every cut, however short (see plug.py). While it reports its
    # output off, nothing is sent; once it's back on, the quiet below. A plug that reconnects to its broker
    # may have restarted, which cuts the power too: the bridge sends nothing until the plug's next reading,
    # for up to plug_check_seconds, and takes it as a cut unless the projector is drawing plug_on_watts or
    # more. Then it's on, which it can't be so soon after a cut.
    plug_mqtt: str = ""             # the plug's MQTT broker, host:port; "" for none
    plug_prefix: str = ""           # the plug's MQTT prefix
    plug_check_seconds: float = 10.0
    plug_on_watts: float = 50.0     # an X100-4K draws about 150 W on, and about 12 W in standby
    # Telling on from standby by the plug's watts (plug_state, X100_PLUG_STATE; the X100-4K's rules then apply
    # from the start). While the plug's readings are fresh, the bridge asks the projector nothing except what a
    # command needs: no polls, no power checks, and no reading before "on". It still checks once, just after
    # "on", that the projector has stopped answering about its volume, so that a hang gets an answer at once.
    # At or below plug_standby_watts it's in standby, at plug_on_watts or more it's on, and in between it's
    # starting up or going off, whichever way it was heading. In between for longer than plug_follow_seconds,
    # it's on (a dim picture, say), unless the bridge switched it off. The plug sends a reading about once a
    # minute and on any change: with none for plug_stale_seconds, the bridge asks the projector as usual until
    # they're back. The idea: in every standby period so far, hung or not, the bridge asked it everything just
    # after it went off (the poll that saw the standby, and the check after it). Whether that matters isn't known.
    plug_state: bool = False
    plug_standby_watts: float = 25.0
    plug_follow_seconds: float = 90.0
    plug_stale_seconds: float = 180.0
    warmup_seconds: float = 90.0
    cooldown_seconds: float = 5.0
    settle_seconds: float = 15.0
    start_hold_seconds: float = 120.0
    power_up_quiet_seconds: float = 300.0
    outage_probe_seconds: float = 30.0
    start_check_seconds: float = 2.0   # after "on", when to check it started (it stops answering volume)
    volume_step_delay: float = 0.05
    pending_check_interval: float = 1.0
    summary_seconds: float = 86400.0          # how often to log what was asked and missed, and for how long
    scenes: Dict[str, int] = field(default_factory=dict)  # Alexa scene name -> remote key code
    shadow_topic_prefix: str = "$aws/things"  # only changed for tests against a local broker
    local_api_bind: str = "127.0.0.1:8661"    # "off" disables the local API
    local_api_token: str = ""
    local_api_timeout: float = 10.0           # how long a local command waits for its ack

    @property
    def aws_enabled(self) -> bool:
        return bool(self.iot_endpoint)

    @property
    def plug_enabled(self) -> bool:
        return bool(self.plug_mqtt.strip())

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
            plug_mqtt=e("X100_PLUG_MQTT", ""),
            plug_prefix=e("X100_PLUG_PREFIX", "").strip().strip("/"),
            plug_state=_env_bool("X100_PLUG_STATE", False),
            plug_standby_watts=_env_float("X100_PLUG_STANDBY_WATTS", 25.0),
            plug_on_watts=_env_float("X100_PLUG_ON_WATTS", 50.0),
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
    """An error with an Alexa ErrorResponse type attached. ``reason``, where there is one, says in a word what
    went wrong, for the listener to say so out loud (x100_listener/replies.py): "plug_off", "plug_checking",
    "booting", "unreachable", "not_started", "just_started", "looks_on", "projector_off" or "refused"."""

    def __init__(self, code: str, message: str, reason: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.reason = reason


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
        self._power_query_lies = cfg.x100_quirks or cfg.plug_state
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
        self._full_reading_due = False          # after the quiet that follows a cut: learn what it's doing
        self._confirm_standby_at: Optional[float] = None  # a standby reading after on, to be checked again
        # For the log, so that a hang, and a week of watching, explain themselves: what was asked and
        # missed since it last went to standby, and since the last daily summary, and time in each state.
        self._off_at: Optional[float] = None    # when it last went to standby (wall clock), and how
        self._off_how = ""
        self._since_off: "Counter[str]" = Counter()
        self._today: "Counter[str]" = Counter()
        self._time_in: Dict[str, float] = {}
        self._time_mark = time.monotonic()
        self._next_summary = time.monotonic() + cfg.summary_seconds
        self._uptime = uptime
        # The plug on its mains, if any (see Config): what it last said about its output, and when, after it
        # reconnects, its silence counts as a restart. Its reports arrive from another thread.
        self._plug_news: "queue.Queue[tuple]" = queue.Queue()
        self._plug_output: Optional[bool] = None
        self._plug_check_until: Optional[float] = None
        # With plug_state: its latest watts, and when, and whether the bridge is going by them (see Config).
        self._plug_watts: Optional[float] = None
        self._plug_read_at: Optional[float] = None
        self._plug_band = ""                    # "standby", "between" or "on", as last acted on
        self._by_watts = False
        self._watts_wait_from: Optional[float] = None  # when the bridge started waiting for the plug's first reading
        self._watts_said_missing = False

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

    def plug_status(self, output: bool, watts: Optional[float] = None, source: str = "") -> None:
        """Called from the plug's MQTT thread with each reading: whether its output is on, the watts the
        projector draws, and what last switched it ("button", "init" for its start-up, ...)."""
        self._plug_news.put(("status", output, watts, source, time.monotonic()))

    def plug_reconnected(self) -> None:
        """Called from the plug's MQTT thread when the plug reconnects to its broker: it may have restarted."""
        self._plug_news.put(("reconnected",))

    # ------------------------------------------------------------------ loop
    def start(self) -> None:
        if self._power_query_lies:
            self._say_quirky("X100_QUIRKS is set" if self.cfg.x100_quirks else "X100_PLUG_STATE is set")
        if self.cfg.plug_state:
            self._watts_wait_from = time.monotonic()
            # One reading first, as without the plug: its watts can't tell the standby that answers like on.
            self._full_reading_due = True
            log.info("X100_PLUG_STATE is set: while the plug's readings are fresh, telling on from standby by what "
                     "the projector draws (standby at %g W or less, on at %g W or more), and asking it nothing but "
                     "what a command needs", self.cfg.plug_standby_watts, self.cfg.plug_on_watts)
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
        self._take_plug_news()
        try:
            msg = self.queue.get(timeout=timeout) if timeout else self.queue.get_nowait()
        except queue.Empty:
            msg = None
        if msg is not None:
            self._take_plug_news()  # what the plug said while the bridge waited counts first
            self._handle(msg)
        now = time.monotonic()
        if self.pending and now >= self._next_pending_check:
            self._next_pending_check = now + self.cfg.pending_check_interval
            self._run_pending()
        if now >= self._next_poll:
            self.poll()
        if now >= self._next_summary:
            self._summarise()
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
        self._count("commands")
        error: Optional[BridgeError] = None
        deferred = False
        try:
            for op in ops:
                deferred = self._apply(op, cmd_id) or deferred
        except BridgeError as exc:
            error = exc
        except Unreachable as exc:
            self._link_failed()
            error = BridgeError("ENDPOINT_UNREACHABLE", f"projector unreachable: {exc}", "unreachable")
        except FunctionDisabled as exc:
            error = BridgeError("NOT_IN_OPERATION", f"projector refused the command: {exc}", "refused")
        except (ProjectorError, ValueError) as exc:
            error = BridgeError("INTERNAL_ERROR", str(exc))
        self._fast_poll()
        # Answer first. The state in the ack already reflects the write (_apply
        # records it), and reading everything back costs several round trips.
        self._ack(cmd_id, error, deferred=deferred)
        if error is None and not deferred and not self.cfg.one_way and not self._by_watts:
            self._refresh_quietly()
            self._publish_state()

    def _current_power(self) -> PowerState:
        if self.cfg.one_way:
            return self._optimistic_power()
        if self._watch_watts() and not self._full_reading_due:
            return self._power_from_watts()  # not a word to the projector
        if self._in_cooldown():
            return self.state.power  # nothing a reading could add: it already looks like standby
        return self._settle_power(self._read_power().state)

    def _read_power(self) -> PowerReading:
        self._count("full readings")
        self._full_reading_due = False  # whatever asked for this one, it's the reading that was due
        try:
            if self._confirm_standby_at is None:
                reading = self.proj.power_reading()
            else:  # checking a standby reading again: give it time to answer about its input
                reading = self.proj.power_reading(input_wait=self.cfg.confirm_input_wait)
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
        if (self._power_query_lies and self.cfg.standby_confirm_seconds > 0 and prev == PowerState.ON
                and self._sent_off is None):  # after the bridge's own "off", standby is what it expects
            if power == PowerState.OFF and self._confirm_standby_at is None:
                self._confirm_standby_at = now + self.cfg.standby_confirm_seconds
                log.info("the projector looks like standby, but it was on: checking again in %.0f s before "
                         "believing it", self.cfg.standby_confirm_seconds)
                self._seen, self._confirmed_at = seen, now
                return PowerState.ON  # for now
            if power != PowerState.OFF and self._confirm_standby_at is not None:
                log.info("the projector answered about its input this time: it's still on")
        self._confirm_standby_at = None  # a second reading agreed, or it isn't standby after all
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

    def _set_power(self, power: PowerState, why: str, how: str = "") -> None:
        """Every change in the power state the bridge acts on goes through here, and into the log: after an
        incident, that shows what the projector looked like and when. ``how``: it's in standby because of a
        power cut, which starts a new standby period even if it was in standby already."""
        old = self.state.power
        if power != old and old != PowerState.UNKNOWN:  # the first reading has its own line
            log.info("projector: %s -> %s (%s)", old.value, power.value, why)
        if power != old:
            self._tally_time()
        standby = (PowerState.OFF, PowerState.COOLING)
        if power in standby and (old not in standby or how):
            self._off_at, self._since_off = self.clock(), Counter()
            if how:
                self._off_how = how
            elif old == PowerState.UNKNOWN:
                self._off_how = "it was already in standby when the bridge started"
            elif why == "the bridge sent 'off'":
                self._off_how = "the bridge switched it off"
            else:
                self._off_how = "its remote, or the projector itself, switched it off"
        self.state.set_power(power)

    def _count(self, what: str) -> None:
        self._since_off[what] += 1
        self._today[what] += 1

    def _tally_time(self) -> None:
        now = time.monotonic()
        key = self.state.power.value
        self._time_in[key] = self._time_in.get(key, 0.0) + now - self._time_mark
        self._time_mark = now

    @staticmethod
    def _counts_text(counts: "Counter[str]") -> str:
        checks, unanswered = counts["power checks"], counts["unanswered"]
        text = f"{checks} power checks ({unanswered} unanswered), {counts['full readings']} full readings"
        if counts["outage probes"]:
            text += f", {counts['outage probes']} probes while it wasn't answering"
        if counts["plug readings"]:
            text += f", {counts['plug readings']} readings from the plug"
        return text

    def _since_off_text(self) -> str:
        """For a hang: how long it had been in standby, how it got there, and what was asked meanwhile."""
        if self._off_at is None or self.state.power not in (PowerState.WARMING, PowerState.OFF):
            return ""
        hours = (self.clock() - self._off_at) / 3600
        when = time.strftime("%H:%M on %d %b", time.localtime(self._off_at))
        return (f". It had been in standby for {hours:.1f} h, since {when} ({self._off_how}). Meanwhile the "
                f"bridge made {self._counts_text(self._since_off)}")

    def _summarise(self) -> None:
        """Once a day: what was asked and missed, and how long it spent in each state."""
        self._tally_time()
        times = ", ".join(f"{state} {seconds / 3600:.1f} h" for state, seconds in sorted(self._time_in.items())
                          if seconds >= 36)
        log.info("in the last %.0f h: %s, %d commands, %d outages; %s", self.cfg.summary_seconds / 3600,
                 self._counts_text(self._today), self._today["commands"], self._today["outages"], times or "-")
        self._today, self._time_in = Counter(), {}
        self._next_summary = time.monotonic() + self.cfg.summary_seconds

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
        log.warning("the projector acknowledged power on but hasn't started%s", self._since_off_text())
        raise BridgeError("NOT_IN_OPERATION", "the projector acknowledged 'on' but hasn't started. If it was "
                          "switched on at the wall in the last few minutes it may still start; if not, and its "
                          "remote can't start it either, switch it off at the wall for 30 seconds", "not_started")

    def _believed_off(self) -> bool:
        """Off by a recent reading: an "on" can go straight out without asking first."""
        s = self.state
        return (not self.cfg.one_way and not self._by_watts and s.connected and s.power == PowerState.OFF
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
                                          "started in a minute or two, switch it off at the wall for 30 seconds",
                                          "just_started")
                    return False  # it's starting up
                if power == PowerState.COOLING:
                    return self._defer(op, PowerState.OFF, cmd_id)
                self._check_not_cold(power)
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
                    raise BridgeError("NOT_IN_OPERATION", "projector is off", "projector_off")
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
                raise BridgeError("NOT_IN_OPERATION", "projector is off", "projector_off")
            self._run_simple(kind, value)
            return False

        raise BridgeError("INVALID_DIRECTIVE", f"unknown op {kind!r}")

    def _check_not_cold(self, power: PowerState) -> None:
        """After a power cut, the X100-4K's standby can answer just like on (see _doubt_on): until a reading
        has shown it in standby or starting, "on" isn't believed, and nothing is sent that would need it on.
        Going by its plug's watts, which can't tell one standby from another, the bridge sends no "on" from
        standby then either: whether that standby takes a serial "on" safely isn't known."""
        if not (self._power_query_lies and self._doubt_on):
            return
        if power == PowerState.ON:
            raise BridgeError("NOT_IN_OPERATION", "the projector looks on, but after a power cut its standby can "
                              "look just the same, and it hasn't been seen in standby or starting since "
                              f"{self._doubt_on}, so the bridge has sent nothing. If it isn't on, start it with "
                              "its remote this once: after that the bridge can tell again", "looks_on")
        if power == PowerState.OFF and self._by_watts:
            raise BridgeError("NOT_IN_OPERATION", "the projector's plug shows it in standby, but it answers as if "
                              "it were on, as its standby can after a power cut, and it hasn't been seen in an "
                              f"ordinary standby or starting since {self._doubt_on}. Whether it takes the bridge's "
                              "'on' from there isn't known, so the bridge has sent nothing. Start it with its remote "
                              "this once: after that the bridge can tell again", "looks_on")

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
        if self._quiet() or self._link_down() or self._plug_holding():
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
        elif self._plug_holding():
            self._next_poll = time.monotonic() + 1.0  # no power, or perhaps a cut: not a word until the plug says
        elif self._quiet():
            self._next_poll = self._quiet_until  # it has only just got power back: not a word
        elif self._link_down():
            self._probe_link()
        elif (self._watch_watts() or self._awaiting_watts()) and not self._full_reading_due:
            self._next_poll = time.monotonic() + 1.0  # not a word to the projector: the plug tells
            if self._by_watts:
                self._power_from_watts()  # for what time alone changes, as when its cool-down is over
        elif self._checking_power() and not self._full_reading_due and (
                self._in_standby() or time.monotonic() < self._next_full_poll):
            self._check_power()
        else:
            self._full_reading_due = False  # after a cut it may not be in standby: one missed reply looks the same
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
        if self._confirm_standby_at is not None:  # a standby reading to check again
            self._next_full_poll = min(self._next_full_poll, self._confirm_standby_at)
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
        may have lost power, however briefly: from then on it's treated as after a power cut. It misses a
        reply now and then, so that takes power_check_tries unanswered tries in a row."""
        tries = max(1, self.cfg.power_check_tries)
        for attempt in range(1, tries + 1):
            self._count("power checks")
            try:
                self.proj.power_probe(timeout=self.cfg.power_check_wait)
            except ProjectorError as exc:
                self._count("unanswered")
                if attempt < tries:
                    continue
                log.warning("projector unreachable: %s", exc)
                self._link_failed()
                self._next_poll = time.monotonic() + self.cfg.fast_poll_interval  # soon, still only about power
                return
            if attempt > 1:
                log.info("the projector missed %d %s to the power check, then answered: not a power cut",
                         attempt - 1, "reply" if attempt == 2 else "replies")
            break
        self._next_poll = time.monotonic() + self.cfg.power_check_seconds
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
        if self._plug_output is False:
            raise BridgeError("ENDPOINT_UNREACHABLE", "the projector's plug has switched its power off. Once it's "
                              "back on, the bridge leaves the projector alone for a few minutes while it boots",
                              "plug_off")
        if self._plug_check_until is not None:
            raise BridgeError("NOT_IN_OPERATION", "the projector's plug has just reconnected, and may have restarted, "
                              "which cuts the projector's power. The bridge is checking: try again in a few seconds",
                              "plug_checking")
        if self._quiet():
            raise BridgeError("NOT_IN_OPERATION", f"the projector may still be booting after a power cut, so it's "
                              f"being left alone until {self._quiet_end()}: questions while it boots can hang it. "
                              "Its remote works sooner, once it has started up", "booting")
        if self._link_down():
            raise BridgeError("ENDPOINT_UNREACHABLE", "the projector isn't answering (switched off at the wall?). "
                              "Once it's back, the bridge leaves it alone for a few minutes while it boots",
                              "unreachable")

    def _probe_link(self) -> None:
        """While it isn't answering, ask only the power query: the X100-4K answers that even while
        booting, when any other question can hang it."""
        now = time.monotonic()
        self._count("outage probes")
        try:
            self.proj.power_probe(timeout=self.cfg.power_check_wait)
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
        self._after_cut("answering again after a cut: it comes back in standby",
                        "after a power cut, or a missed reply taken for one")

    def _after_cut(self, why: str, how: str) -> None:
        """The power is back after a cut, and the projector may be booting: forget what it was doing, send it
        nothing for power_up_quiet_seconds, then take one full reading."""
        self._lost_since = None
        self._sent_on = self._sent_off = self._started_at = None
        self._start_confirmed = False
        self._confirm_standby_at = None
        self._doubt_on, self._confirmed_at = "a power cut", 0.0  # no reading from before the cut counts now
        self._set_power(PowerState.OFF, why, how)
        self._count("outages")
        self._full_reading_due = True  # once the quiet is over: if it was only a missed reply, it's still on
        self._quiet_for(self.cfg.power_up_quiet_seconds)

    # ------------------------------------------------------------------ the plug on its mains
    def _plug_holding(self) -> bool:
        """The plug says its power is off, or has just reconnected and may have cut it: send nothing."""
        return self._plug_output is False or self._plug_check_until is not None

    def _take_plug_news(self) -> None:
        news_came = False
        while True:
            try:
                news = self._plug_news.get_nowait()
            except queue.Empty:
                break
            news_came = True
            if news[0] == "reconnected":
                self._plug_reconnected()
            else:
                self._plug_says(*news[1:])
        if self._plug_check_until is not None and time.monotonic() >= self._plug_check_until:
            self._plug_check_until = None
            self._plug_restarted(f"there was no reading from it within {self.cfg.plug_check_seconds:.0f} s")
        if news_came and self._watch_watts() and not (self._quiet() or self._plug_holding() or self._link_down()
                                                      or self._full_reading_due):
            self._power_from_watts()

    def _plug_reconnected(self) -> None:
        if self._plug_output is False:
            return  # its power is off anyway: its next reading says whether it's back
        self._plug_check_until = time.monotonic() + self.cfg.plug_check_seconds
        log.warning("the projector's plug has reconnected, and may have restarted, which cuts the projector's "
                    "power: sending the projector nothing until the plug says what it's drawing")

    def _plug_says(self, output: bool, watts: Optional[float], source: str, at: Optional[float] = None) -> None:
        """``at``: when the reading arrived, as the bridge may have been busy with a command meanwhile."""
        was, self._plug_output = self._plug_output, output
        if watts is not None:
            self._plug_watts = watts if output else 0.0
            self._plug_read_at = at if at is not None else time.monotonic()
            self._count("plug readings")
        by = f" ({source})" if source else ""
        if not output:
            if was is not False:
                self._plug_check_until = None
                log.warning("the projector's plug has switched its power off%s: sending the projector nothing until "
                            "it's back", by)
                self.state.connected = False
                self._set_power(PowerState.OFF, "the plug switched its power off", "the plug switched its power off")
            return
        if was is False:
            self._plug_check_until = None
            self._after_cut("the plug switched its power back on: it comes back in standby",
                            "the plug switched its power off and on")
            log.warning("the projector's plug has switched its power back on%s: leaving the projector alone until "
                        "%s, as questions while it boots can hang it", by, self._quiet_end())
            return
        if self._plug_check_until is not None:
            self._plug_check_until = None
            if watts is not None and watts >= self.cfg.plug_on_watts:
                log.info("the projector's plug is back, and the projector is drawing %.0f W, so it's on: its power "
                         "wasn't cut", watts)
            else:
                self._plug_restarted("the projector is drawing " + (f"{watts:.0f} W" if watts is not None
                                                                     else "an unknown power"))

    def _plug_restarted(self, why: str) -> None:
        """The plug reconnected, and nothing shows the projector kept its power: take it as a cut."""
        self.state.connected = False
        self._after_cut("the plug reconnected, perhaps after restarting: it comes back in standby",
                        "the plug restarted, perhaps, which cuts its power")
        log.warning("taking the plug's reconnection for a restart, which cuts the projector's power (%s): leaving "
                    "the projector alone until %s, as questions while it boots can hang it", why, self._quiet_end())

    # ------------------------------------------------------------------ on or standby, by the plug's watts
    def _watts_fresh(self) -> bool:
        return self._plug_read_at is not None and time.monotonic() - self._plug_read_at < self.cfg.plug_stale_seconds

    def _awaiting_watts(self) -> bool:
        """Just started, with plug_state: the plug sends a reading about once a minute, so wait for one."""
        return (self.cfg.plug_state and self._plug_read_at is None and self._watts_wait_from is not None
                and time.monotonic() - self._watts_wait_from < self.cfg.plug_stale_seconds)

    def _watch_watts(self) -> bool:
        """Whether the bridge goes by the plug's watts now (see Config), saying so in the log when that changes."""
        if not self.cfg.plug_state:
            return False
        fresh = self._watts_fresh()
        if fresh != self._by_watts:
            self._by_watts = fresh
            if fresh:
                self._confirm_standby_at = None
                log.info("telling on from standby by what the projector draws (%.0f W now), and asking it nothing "
                         "but what a command needs", self._plug_watts or 0.0)
            else:
                log.warning("no reading from the projector's plug for %.0f s: asking the projector itself again, as "
                            "without the plug, until its readings are back", self.cfg.plug_stale_seconds)
                self._next_poll = min(self._next_poll, time.monotonic())
        elif not fresh and not self._awaiting_watts() and not self._watts_said_missing \
                and self._plug_read_at is None and self._watts_wait_from is not None:
            self._watts_said_missing = True
            log.warning("no reading from the projector's plug in the %.0f s since the bridge started: asking the "
                        "projector itself, as without the plug, until there is one", self.cfg.plug_stale_seconds)
        return fresh

    def _power_from_watts(self) -> PowerState:
        """The power state the bridge acts on, from what the projector draws and the bridge's own commands, whose
        effect the watts take a while to show. Standby at or below plug_standby_watts, on at plug_on_watts or
        more. In between, it's on its way up or down, whichever way it was heading (see Config)."""
        now = time.monotonic()
        watts = self._plug_watts or 0.0
        prev = self.state.power
        lasted = now - self.state.power_since
        ours_off = self._sent_off is not None and now - self._sent_off < self.cfg.plug_follow_seconds
        why = f"the plug: {watts:.0f} W"
        power = prev
        band = ("on" if watts >= self.cfg.plug_on_watts else
                "standby" if watts <= self.cfg.plug_standby_watts else "between")
        fell, self._plug_band = self._plug_band == "on" and band == "between", band
        if band == "on":
            self._seen = PowerState.ON
            if prev == PowerState.COOLING and self._sent_off is not None:
                if not ours_off:  # still on, long after the bridge's "off"
                    power = PowerState.ON
                    why += f", {now - self._sent_off:.0f} s after the bridge's 'off': it didn't go off"
            else:
                power = PowerState.ON
        elif band == "standby":
            self._seen = PowerState.OFF
            held = prev == PowerState.WARMING and self._start_held()  # just started: its draw hasn't risen yet
            if not held and not (prev == PowerState.COOLING and self._in_cooldown()):
                power = PowerState.OFF
                if prev == PowerState.ON and self._doubt_on:  # a reading said on, but it's drawing standby power
                    why += ": the standby that answers as if it were on, after a power cut"
        else:  # in between: on its way up from standby, or down from on
            self._seen = PowerState.WARMING
            if prev in (PowerState.OFF, PowerState.UNKNOWN):
                power = PowerState.WARMING
            elif prev == PowerState.ON:
                if fell:
                    power = PowerState.COOLING
            elif lasted >= self.cfg.plug_follow_seconds:  # no further after all
                after_off = prev == PowerState.COOLING and self._sent_off is not None
                power = PowerState.OFF if after_off else PowerState.ON
                why += f", in between for {lasted:.0f} s: taken as " + (
                    "standby after the bridge's 'off'" if after_off else "on")
        if power in (PowerState.OFF, PowerState.COOLING):
            if not self._start_held() or self._doubt_on:  # with doubt, what looked like a start was that standby
                self._started_at = None
        elif self._started_at is None and power != prev and (
                power == PowerState.WARMING or prev in (PowerState.OFF, PowerState.COOLING)):
            self._started_at = now  # out of standby without us: its remote, or its own button
            if prev == PowerState.COOLING:  # it never got as low as standby: held all the same, in case
                log.info("the projector is drawing as much as when on again: a dim picture, perhaps, or its remote "
                         "started it again. Holding power commands as after a start")
            else:
                log.info("the projector is %s, and the bridge didn't start it: its remote or its own button did",
                         "drawing more than in standby" if power == PowerState.WARMING else "on")
        if watts > self.cfg.plug_standby_watts:
            self._doubt_on = ""  # more than any standby draws: whatever it is, it isn't the standby that looks on
        self._confirm_standby_at = None
        self.state.connected = True
        self._set_power(power, why)
        self._confirmed_at = now
        return power

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
                if error.reason:
                    waiter.result["reason"] = error.reason
            waiter.event.set()
        self._publish_state(extra={"ack": ack})

    def offline_document(self) -> Dict[str, Any]:
        return {"bridge": {"heartbeat": int(self.clock()), "online": False, "version": __version__}}


def describe_ops(ops: List[Dict[str, Any]]) -> str:
    return ", ".join(f"{o.get('op')}={o.get('value')}" for o in ops)
