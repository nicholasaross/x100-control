"""x100-bridge: the one program that talks to the ViewSonic X100-4K.

Local mode (the default): voice commands arrive from the listener on your LAN
through the bridge's HTTP API (LOCAL_API_BIND, LOCAL_API_TOKEN). Nothing leaves
the house.

Optional Alexa mode: set AWS_IOT_ENDPOINT (see alexa/README.md) and the bridge
also keeps one outbound MQTT-over-TLS connection to AWS IoT Core. It needs the
``aws`` extra: pip install 'x100-control[aws]'.

    x100-bridge --env-file bridge.env           # run
    x100-bridge --env-file bridge.env --check   # test the projector and the API, then exit
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Optional

from x100 import version_line
from x100.envfile import load_env_file
from x100.inputs import InputMap
from x100.pjlink import PJLink, PJLinkError
from x100.projector import projector_from_env

from .core import BridgeCore, Config, system_uptime
from .local_api import ApiConfigError, LocalApi, check_settings

log = logging.getLogger("x100.bridge")


class MqttLink:
    """The optional AWS IoT link used by the Alexa skill (see alexa/README.md)."""

    def __init__(self, cfg: Config, on_command: Callable[[bytes], None], on_connected: Callable[[], None]):
        try:
            import paho.mqtt.client as mqtt
        except ImportError as exc:  # pragma: no cover - depends on the install
            raise SystemExit("AWS_IOT_ENDPOINT is set but paho-mqtt isn't installed: "
                             "pip install 'x100-control[aws]'") from exc

        self.cfg = cfg
        self.on_command = on_command
        self.on_connected = on_connected
        self.connected = threading.Event()
        self._mqtt = mqtt
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cfg.thing_name,
                                  clean_session=True, protocol=mqtt.MQTTv311)
        if cfg.mqtt_tls:
            cafile = cfg.ca_file if cfg.ca_file and os.path.exists(cfg.ca_file) else None
            ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=cafile)
            ctx.load_cert_chain(cfg.cert_file, cfg.key_file)
            if cfg.iot_port == 443:
                ctx.set_alpn_protocols(["x-amzn-mqtt-ca"])
            self.client.tls_set_context(ctx)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(min_delay=1, max_delay=60)

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if reason_code.is_failure:
            log.error("AWS IoT refused the connection: %s (check certificate, policy and endpoint)", reason_code)
            return
        log.info("connected to %s:%d as %s", self.cfg.iot_endpoint, self.cfg.iot_port, self.cfg.thing_name)
        client.subscribe(self.cfg.cmd_topic, qos=1)
        client.subscribe(self.cfg.shadow_update_topic + "/rejected", qos=0)
        self.connected.set()
        self.on_connected()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None) -> None:
        self.connected.clear()
        if reason_code != 0:
            log.warning("disconnected from AWS IoT (%s); reconnecting", reason_code)

    def _on_message(self, client, userdata, msg) -> None:
        if msg.topic == self.cfg.cmd_topic:
            self.on_command(msg.payload)
        elif msg.topic.endswith("/rejected"):
            log.warning("shadow update rejected: %s", msg.payload[:300])

    def publish_reported(self, reported: Dict[str, Any]):
        payload = json.dumps({"state": {"reported": reported}}, separators=(",", ":"))
        return self.client.publish(self.cfg.shadow_update_topic, payload, qos=1)

    def start(self) -> None:
        self.client.connect_async(self.cfg.iot_endpoint, self.cfg.iot_port, keepalive=60)
        self.client.loop_start()

    def stop(self, final: Optional[Dict[str, Any]] = None) -> None:
        if final is not None and self.connected.is_set():
            try:
                self.publish_reported(final).wait_for_publish(timeout=3)
            except Exception:  # pragma: no cover
                pass
        self.client.disconnect()
        self.client.loop_stop()


def check_config(cfg: Config) -> list:
    problems = []
    if not (os.environ.get("X100_HOST") or os.environ.get("X100_SERIAL")):
        problems.append("set X100_HOST (projector CONTROL-port IP) or X100_SERIAL")
    if cfg.local_api_enabled:
        try:
            check_settings(cfg.local_api_bind, cfg.local_api_token)
        except ApiConfigError as exc:
            problems.append(str(exc))
    if cfg.aws_enabled and cfg.mqtt_tls:
        for label, path in (("CERT_FILE", cfg.cert_file), ("KEY_FILE", cfg.key_file)):
            if not os.path.exists(path):
                problems.append(f"{label} {path} not found")
        if cfg.ca_file and not os.path.exists(cfg.ca_file):
            log.info("CA_FILE %s not found; using the system trust store", cfg.ca_file)
    if not cfg.aws_enabled and not cfg.local_api_enabled:
        problems.append("nothing to do: LOCAL_API_BIND is off and AWS_IOT_ENDPOINT isn't set")
    return problems


def _get_json(url: str, token: str = "", timeout: float = 3.0) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def run_check(cfg: Config, core: BridgeCore, api: Optional[LocalApi], link: Optional[MqttLink]) -> int:
    ok = True
    print(version_line())
    print(f"Projector link: {core.proj.transport.describe()} ({'one-way' if cfg.one_way else 'two-way'})")
    core.poll()
    s = core.state
    if s.connected:
        print(f"  reachable -- power {s.power.value}" + (f", input {s.input}, volume {s.volume}, muted {s.muted}"
                                                       if s.power.value == "ON" else ""))
    else:
        ok = False
        print("  NOT reachable -- run `x100ctl probe`")
    if core.pjlink is not None:
        try:
            print(f"  PJLink power: {core.pjlink.power().value}")
        except PJLinkError as exc:
            print(f"  PJLink: {exc}")
    named = [d["name"] for d in core.inputs.discovery_inputs()]
    print(f"Inputs with known codes: {', '.join(named) or 'none'}")

    if api is not None:
        print(f"Local API: {api.url} ({'token required' if api.token else 'no token: this machine only'})")
        api.start()
        try:
            _get_json(api.local_url + "/healthz")
            state = _get_json(api.local_url + "/state", api.token)
            print(f"  answering, and the token works (reports power {state.get('power')})")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            ok = False
            print(f"  not answering on {api.local_url}: {exc}")
        finally:
            api.stop()
    else:
        print("Local API: off (LOCAL_API_BIND=off)")

    if link is not None:
        print(f"AWS IoT: {cfg.iot_endpoint}:{cfg.iot_port} as {cfg.thing_name}")
        link.start()
        if link.connected.wait(timeout=15):
            info = link.publish_reported({"bridge": {"lastCheck": int(time.time())}})
            info.wait_for_publish(timeout=5)
            print("  connected and published to the device shadow" if info.is_published()
                  else "  publish not confirmed")
            ok = ok and info.is_published()
        else:
            ok = False
            print("  could not connect -- check AWS_IOT_ENDPOINT, the certificate files, and outbound port "
                  f"{cfg.iot_port}")
        link.stop()
    print("All good." if ok else "Problems found (see above).")
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="x100-bridge", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env-file", help="load settings from a KEY=VALUE file (e.g. bridge.env)")
    ap.add_argument("--check", action="store_true", help="test the projector and the API, then exit")
    ap.add_argument("--version", action="version", version=version_line(),
                    help="show which release is installed, then exit")
    args = ap.parse_args(argv)
    if args.env_file:
        load_env_file(args.env_file)
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg = Config.from_env()
    except ValueError as exc:
        log.error("bad setting: %s", exc)
        return 2
    problems = check_config(cfg)
    if problems:
        for p in problems:
            log.error(p)
        return 2

    projector = projector_from_env(one_way=cfg.one_way)
    try:
        inputs = InputMap.load(cfg.inputs_file)
    except (ValueError, KeyError) as exc:
        log.error("bad inputs file %s: %s", cfg.inputs_file, exc)
        return 2
    pjlink = PJLink(cfg.pjlink_host, cfg.pjlink_port, cfg.pjlink_password) if cfg.pjlink_host else None

    link_holder: Dict[str, MqttLink] = {}

    def publish(doc: Dict[str, Any]) -> None:
        link = link_holder.get("link")
        if link is not None:  # local-only mode has nowhere to publish to
            link.publish_reported(doc)

    core = BridgeCore(cfg, projector, inputs, publish=publish, pjlink=pjlink, uptime=system_uptime)
    link = MqttLink(cfg, on_command=core.submit, on_connected=core.request_full_publish) if cfg.aws_enabled else None
    if link is not None:
        link_holder["link"] = link
    api: Optional[LocalApi] = None
    if cfg.local_api_enabled:
        try:
            api = LocalApi(core, cfg.local_api_bind, cfg.local_api_token, timeout=cfg.local_api_timeout)
        except OSError as exc:
            log.error("can't listen on %s: %s (is another bridge already running?)", cfg.local_api_bind, exc)
            return 2

    if args.check:
        return run_check(cfg, core, api, link)

    stop = threading.Event()

    def _signal(signum, _frame):
        log.info("signal %d received, shutting down", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _signal)
    signal.signal(signal.SIGINT, _signal)
    mode = " + ".join(m for m, on in (("local API", api is not None), ("AWS IoT", link is not None)) if on)
    log.info("x100-bridge starting (%s): projector %s, %d inputs with known codes", mode,
             projector.transport.describe(), len(inputs.discovery_inputs()))
    log.info("running %s", version_line())
    core.start()
    if api is not None:
        api.start()
        log.info("local API listening on %s%s", api.url, "" if api.token else " (loopback only, no token)")
    if link is not None:
        link.start()
    try:
        while not stop.wait(1.0):
            pass
    finally:
        if api is not None:
            api.stop()
        core.stop()
        if link is not None:
            link.stop(final=core.offline_document())
        projector.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
