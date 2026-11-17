"""Reads the smart plug on the projector's mains, so the bridge sees every power cut, however short.

The X100-4K hangs if it's asked anything but the power query while it boots after a power cut, and
a cut that falls between two of the bridge's checks goes unseen. A Shelly Gen2 plug (a Shelly Plus
Plug, say) reports every change of its output over MQTT, with the watts it's passing. The bridge
leaves the projector alone while the plug's output is off, and for a while after it comes back on
(X100_POWER_UP_QUIET_SECONDS: see Config in core.py). With X100_PLUG_STATE, it also tells on from standby by the watts,
and asks the projector nothing but what a command needs.

It only reads: it subscribes to the plug's status and publishes nothing, so it can't switch the
plug, and nothing is sent to the projector because of it. If the broker can't be reached, the
bridge carries on as it would without a plug.

    X100_PLUG_MQTT=127.0.0.1:1883        the broker, host:port (no sign-in)
    X100_PLUG_PREFIX=shellyplusplug-...  the plug's MQTT prefix (its settings: MQTT > MQTT prefix)

Turn on "Generic status update over MQTT" in the plug's MQTT settings. It then publishes
``<prefix>/status/switch:0`` on every change and about once a minute, and ``<prefix>/online``
when it connects (and the broker publishes ``false`` there when it drops out).
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional, Tuple

log = logging.getLogger("x100.bridge.plug")


@dataclass(frozen=True)
class PlugStatus:
    output: bool                   # its output is on: the projector has power
    watts: Optional[float] = None  # what the projector is drawing
    source: str = ""               # what last switched it: "button", "init" (its start-up), "timer", ...
    switch_ons: Optional[int] = None  # how many times its output has been switched on, ever


def parse_broker(text: str) -> Tuple[str, int]:
    """'127.0.0.1:1883' -> ('127.0.0.1', 1883). The port defaults to 1883."""
    host, sep, port = text.strip().rpartition(":")
    if not sep:
        host, port = text.strip(), "1883"
    host = host.strip("[]")
    if not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError(f"X100_PLUG_MQTT should be host:port, not {text!r}")
    return host, int(port)


def parse_switch_status(payload: bytes) -> Optional[PlugStatus]:
    """A Shelly Gen2 ``status/switch:0`` message, or None if it isn't one."""
    try:
        doc = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("output"), bool):
        return None
    watts = doc.get("apower")
    if not isinstance(watts, (int, float)) or isinstance(watts, bool):
        watts = None
    source = doc.get("source")
    counts = doc.get("counts")
    switch_ons = counts.get("switch_on") if isinstance(counts, dict) else None
    if not isinstance(switch_ons, int) or isinstance(switch_ons, bool):
        switch_ons = None
    return PlugStatus(doc["output"], None if watts is None else float(watts), source if isinstance(source, str) else "",
                      switch_ons)


def format_message(topic: str, payload: bytes, retained: bool = False) -> str:
    """One message from the plug, as one line for the record (see record): its status in KEY=VALUE form, or
    anything else as it came. ``topic`` is without the plug's prefix."""
    tail = " (retained)" if retained else ""
    if topic == "online":
        return f"online {payload.decode('utf-8', 'replace').strip()}{tail}"
    if topic == "status/switch:0":
        try:
            doc = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            doc = None
        if isinstance(doc, dict) and isinstance(doc.get("output"), bool):
            counts = doc.get("counts") if isinstance(doc.get("counts"), dict) else {}
            temp = doc.get("temperature") if isinstance(doc.get("temperature"), dict) else {}
            fields = [("output", "on" if doc["output"] else "off"), ("watts", doc.get("apower")),
                      ("amps", doc.get("current")), ("volts", doc.get("voltage")), ("source", doc.get("source")),
                      ("switch_on", counts.get("switch_on")), ("celsius", temp.get("tC"))]
            return "switch:0 " + " ".join(f"{k}={v}" for k, v in fields if v is not None) + tail
    return f"{topic} {payload[:300].decode('utf-8', 'replace')}{tail}"


def record(broker: str, prefix: str, write: Callable[[str], None], stop: threading.Event) -> None:
    """Write a line for every message the plug sends (``<prefix>/#``) until ``stop`` is set, for looking back on:
    each start, "off", press, boot and fan run shows in its watts, even while the bridge is stopped. It only
    subscribes. ``x100-bridge --record-plug`` runs it, as the x100-plug-recorder service."""
    import paho.mqtt.client as mqtt

    prefix = prefix.strip().strip("/")
    if not prefix:
        raise ValueError("X100_PLUG_MQTT is set, but X100_PLUG_PREFIX isn't: set it to the plug's MQTT prefix")
    host, port = parse_broker(broker)
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="x100-plug-recorder-" + uuid.uuid4().hex[:8],
                         clean_session=True, protocol=mqtt.MQTTv311)

    def on_connect(client, userdata, flags, reason_code, properties=None) -> None:
        if reason_code.is_failure:
            write(f"recorder: the broker at {host}:{port} said {reason_code}")
            return
        client.subscribe(f"{prefix}/#", qos=1)
        write(f"recorder: recording {prefix}/# from {host}:{port}")

    def on_disconnect(client, userdata, flags, reason_code, properties=None) -> None:
        if not stop.is_set():
            write(f"recorder: lost the broker ({reason_code}): trying again")

    def on_message(client, userdata, msg) -> None:
        topic = msg.topic[len(prefix) + 1:] if msg.topic.startswith(prefix + "/") else msg.topic
        write(format_message(topic, msg.payload, msg.retain))

    client.on_connect, client.on_disconnect, client.on_message = on_connect, on_disconnect, on_message
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    client.connect_async(host, port, keepalive=60)
    client.loop_start()
    try:
        stop.wait()
    finally:
        client.disconnect()
        client.loop_stop()


class PlugWatcher:
    """Subscribes to the plug's status and passes it on: ``on_status(output, watts, source, switch_ons)`` for
    each reading, and ``on_reconnected()`` when the plug reconnects to the broker, which it does after a restart,
    and after a moment out of touch. Both are called from paho's network thread."""

    def __init__(self, broker: str, prefix: str,
                 on_status: Callable[[bool, Optional[float], str, Optional[int]], None],
                 on_reconnected: Callable[[], None]):
        import paho.mqtt.client as mqtt  # here, so that a bridge without a plug never needs it

        prefix = prefix.strip().strip("/")
        if not prefix:
            raise ValueError("X100_PLUG_MQTT is set, but X100_PLUG_PREFIX isn't: set it to the plug's MQTT prefix")
        self.host, self.port = parse_broker(broker)
        self.online_topic = f"{prefix}/online"
        self.status_topic = f"{prefix}/status/switch:0"
        self.on_status = on_status
        self.on_reconnected = on_reconnected
        self.connected = threading.Event()
        self.plug_online: Optional[bool] = None  # as the broker last told it
        self._said_unreachable = False
        self._ever_connected = False
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="x100-bridge-" + uuid.uuid4().hex[:8],
                                  clean_session=True, protocol=mqtt.MQTTv311)
        self.client.on_connect = self._on_connect
        self.client.on_connect_fail = self._on_connect_fail
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(min_delay=1, max_delay=60)

    def describe(self) -> str:
        return f"MQTT {self.host}:{self.port}, {self.status_topic}"

    def start(self) -> None:
        self.client.connect_async(self.host, self.port, keepalive=60)
        self.client.loop_start()

    def stop(self) -> None:
        self.client.disconnect()
        self.client.loop_stop()

    def _unreachable(self, why: Any) -> None:
        if not self._said_unreachable:  # once, until it connects: it keeps trying meanwhile
            self._said_unreachable = True
            log.warning("can't read the projector's plug at %s:%d (%s): carrying on without it, and trying again "
                        "now and then", self.host, self.port, why)

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if reason_code.is_failure:
            self._unreachable(f"the broker said {reason_code}")
            return
        client.subscribe([(self.online_topic, 1), (self.status_topic, 1)])
        self._said_unreachable = False
        self.connected.set()
        log.info("reading the projector's plug%s: %s", " again" if self._ever_connected else "", self.describe())
        self._ever_connected = True

    def _on_connect_fail(self, client, userdata) -> None:
        self._unreachable("no answer")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if not self.connected.is_set():
            return
        self.connected.clear()
        if reason_code != 0:  # not our own stop()
            log.warning("lost the projector's plug's broker (%s): until it's back, a power cut between the "
                        "bridge's own checks goes unseen", reason_code)

    def _on_message(self, client, userdata, msg) -> None:
        if msg.topic == self.online_topic:
            online = msg.payload.strip().lower() == b"true"
            self.plug_online = online
            if msg.retain:  # the broker's note of how things stand, on subscribing: nothing has just happened
                if not online:
                    log.warning("the projector's plug isn't connected to its broker at the moment")
            elif online:
                self.on_reconnected()  # a restart looks like this, and so does a Wi-Fi drop-out
            else:
                log.warning("the projector's plug has dropped out of touch with its broker")
        elif msg.topic == self.status_topic:
            status = parse_switch_status(msg.payload)
            if status is None:
                log.debug("ignoring a plug status that isn't one: %r", msg.payload[:200])
                return
            self.on_status(status.output, status.watts, status.source, status.switch_ons)
