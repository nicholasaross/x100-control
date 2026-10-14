"""Runs the real bridge process against a local MQTT broker and the simulated projector.

This exercises the paho-mqtt link, env-file loading, subscribe/publish topics and
graceful shutdown -- everything except TLS and AWS itself. Skipped if amqtt
isn't installed (pip install amqtt).
"""

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

amqtt = pytest.importorskip("amqtt")
mqtt = pytest.importorskip("paho.mqtt.client")

from x100.mock import MockServer  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
THING = "x100-projector"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class BrokerThread:
    def __init__(self, port):
        self.port = port
        self.loop = asyncio.new_event_loop()
        self.config = {"listeners": {"default": {"type": "tcp", "bind": f"127.0.0.1:{port}"}},
                       "sys_interval": 0, "auth": {"allow-anonymous": True}, "topic-check": {"enabled": False}}
        self.broker = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.ready = threading.Event()

    def _run(self):
        from amqtt.broker import Broker

        # Build the broker inside its own loop: it creates futures at construction time,
        # and on Python 3.10 those must belong to the loop that runs it.
        asyncio.set_event_loop(self.loop)
        self.broker = Broker(self.config, loop=self.loop)
        self.loop.run_until_complete(self.broker.start())
        self.ready.set()
        self.loop.run_forever()

    def start(self):
        self.thread.start()
        assert self.ready.wait(10), "broker didn't start"

    def stop(self):
        fut = asyncio.run_coroutine_threadsafe(self.broker.shutdown(), self.loop)
        try:
            fut.result(timeout=5)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)


class Watcher:
    """Test-side MQTT client playing the part of AWS IoT + the Lambda."""

    def __init__(self, port):
        self.messages = []
        self.cond = threading.Condition()
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="watcher-" + uuid.uuid4().hex[:6])
        self.client.on_message = self._on_message
        self.client.connect("127.0.0.1", port)
        self.client.subscribe(f"test/things/{THING}/shadow/update", qos=1)
        self.client.loop_start()

    def _on_message(self, client, userdata, msg):
        with self.cond:
            self.messages.append(json.loads(msg.payload)["state"]["reported"])
            self.cond.notify_all()

    def wait_for(self, predicate, limit=10.0):
        deadline = time.monotonic() + limit
        with self.cond:
            while True:
                for m in self.messages:
                    if predicate(m):
                        return m
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(f"timed out; saw {self.messages[-3:]}")
                self.cond.wait(remaining)

    def command(self, *ops):
        cmd_id = str(uuid.uuid4())
        payload = {"id": cmd_id, "ts": int(time.time() * 1000), "ops": [{"op": o, "value": v} for o, v in ops]}
        self.client.publish(f"x100/{THING}/cmd", json.dumps(payload), qos=1)
        return cmd_id

    def close(self):
        self.client.loop_stop()
        self.client.disconnect()


def test_bridge_process_over_mqtt(tmp_path):
    port = free_port()
    broker = BrokerThread(port)
    broker.start()
    srv = MockServer(warmup=0.5, cooldown=0.3).start()
    env_file = tmp_path / "bridge.env"
    env_file.write_text("\n".join([
        f"THING_NAME={THING}",
        "AWS_IOT_ENDPOINT=127.0.0.1",
        f"AWS_IOT_PORT={port}",
        "MQTT_TLS=false",
        "SHADOW_TOPIC_PREFIX=test/things",  # local brokers reserve $-topics; AWS uses $aws/things
        "X100_HOST=127.0.0.1",
        f"X100_PORT={srv.port}",
        f"X100_INPUTS_FILE={tmp_path / 'inputs.json'}",
        "POLL_INTERVAL=1",
        "X100_POWER_UP_QUIET_SECONDS=0",  # a test machine may have only just started
        "HEARTBEAT_INTERVAL=30",
        'X100_SCENES={"Projector Focus": "0x92"}',
        "LOG_LEVEL=INFO",
        "LOCAL_API_BIND=off",  # AWS-only, as in the original Alexa set-up
    ]) + "\n")
    watcher = Watcher(port)
    proc = subprocess.Popen([sys.executable, "-m", "x100_bridge", "--env-file", str(env_file)], cwd=ROOT,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            env={k: v for k, v in os.environ.items() if not k.startswith(("X100_", "AWS_IOT"))})
    try:
        hello = watcher.wait_for(lambda m: m.get("bridge", {}).get("online") is True)
        assert hello["power"] == "OFF" and hello["connected"] is True
        assert hello["scenes"] == [{"name": "Projector Focus", "key": 0x92}]

        cmd_id = watcher.command(("input", "HDMI 2"))
        ack = watcher.wait_for(lambda m: m.get("ack", {}).get("id") == cmd_id)["ack"]
        assert ack["ok"] is True and ack["deferred"] is True
        watcher.wait_for(lambda m: m.get("input") == "HDMI 2" and m.get("power") == "ON")
        assert srv.state.source == 0x07

        cmd_id = watcher.command(("volume", 7))
        msg = watcher.wait_for(lambda m: m.get("ack", {}).get("id") == cmd_id)
        assert msg["ack"]["ok"] and msg["volume"] == 7

        proc.send_signal(signal.SIGTERM)
        watcher.wait_for(lambda m: m.get("bridge", {}).get("online") is False)
        assert proc.wait(timeout=10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        out = proc.stdout.read() if proc.stdout else ""
        watcher.close()
        srv.stop()
        broker.stop()
        print(out)


def _make_certs(d: Path):
    """CA + server cert for localhost + client cert, like AWS IoT's mutual TLS."""
    def run(*args):
        subprocess.run(["openssl", *args], check=True, capture_output=True)
    # Python 3.13 verifies strictly (VERIFY_X509_STRICT), so the test CA needs proper extensions,
    # as Amazon's real root CA has.
    run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", d / "ca.key", "-out", d / "ca.pem",
        "-days", "2", "-subj", "/CN=Test Root CA",
        "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign",
        "-addext", "subjectKeyIdentifier=hash")
    (d / "san.cnf").write_text("subjectAltName=DNS:localhost,IP:127.0.0.1\n"
                               "keyUsage=critical,digitalSignature,keyEncipherment\n"
                               "extendedKeyUsage=serverAuth\n"
                               "authorityKeyIdentifier=keyid\nsubjectKeyIdentifier=hash\n")
    (d / "client.cnf").write_text("keyUsage=critical,digitalSignature\nextendedKeyUsage=clientAuth\n"
                                  "authorityKeyIdentifier=keyid\n")
    run("req", "-newkey", "rsa:2048", "-nodes", "-keyout", d / "server.key", "-out", d / "server.csr",
        "-subj", "/CN=localhost")
    run("x509", "-req", "-in", d / "server.csr", "-CA", d / "ca.pem", "-CAkey", d / "ca.key", "-CAcreateserial",
        "-out", d / "server.pem", "-days", "2", "-extfile", d / "san.cnf")
    # client key + CSR exactly as deploy.sh makes them
    run("req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", d / "bridge.key", "-out", d / "bridge.csr",
        "-subj", f"/CN={THING}")
    run("x509", "-req", "-in", d / "bridge.csr", "-CA", d / "ca.pem", "-CAkey", d / "ca.key", "-CAcreateserial",
        "-out", d / "bridge.crt", "-days", "2", "-extfile", d / "client.cnf")


class MutualTlsFrontEnd:
    """Terminates TLS like AWS IoT does -- server cert plus a *required* client certificate --
    and forwards the plain MQTT bytes to the local broker."""

    def __init__(self, ca: Path, cert: Path, key: Path, upstream_port: int):
        import select
        import ssl

        self._select = select
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(str(cert), str(key))
        self.ctx.verify_mode = ssl.CERT_REQUIRED
        self.ctx.load_verify_locations(str(ca))
        self.upstream_port = upstream_port
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.handshakes = []  # (ok, peer CN or error)
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        import ssl

        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            try:
                tls = self.ctx.wrap_socket(conn, server_side=True)
                subject = dict(x[0] for x in tls.getpeercert()["subject"])
                self.handshakes.append((True, subject.get("commonName")))
            except (ssl.SSLError, OSError) as exc:
                self.handshakes.append((False, str(exc)))
                conn.close()
                continue
            threading.Thread(target=self._pump, args=(tls,), daemon=True).start()

    def _pump(self, tls):
        up = socket.create_connection(("127.0.0.1", self.upstream_port))
        try:
            while True:
                if tls.pending():
                    readable = [tls]
                else:
                    readable, _, _ = self._select.select([tls, up], [], [], 1.0)
                for s in readable:
                    data = s.recv(65536)
                    if not data:
                        return
                    (up if s is tls else tls).sendall(data)
        except OSError:
            return
        finally:
            up.close()
            tls.close()

    def close(self):
        self.sock.close()


def test_bridge_check_over_mutual_tls(tmp_path):
    """`x100-bridge --check` over TLS with a client certificate, against an AWS-like mutual-TLS endpoint."""
    _make_certs(tmp_path)
    broker = BrokerThread(free_port())
    broker.start()
    front = MutualTlsFrontEnd(tmp_path / "ca.pem", tmp_path / "server.pem", tmp_path / "server.key", broker.port)
    srv = MockServer().start()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("X100_", "AWS_IOT"))}
    env.update({
        "THING_NAME": THING, "AWS_IOT_ENDPOINT": "localhost", "AWS_IOT_PORT": str(front.port), "MQTT_TLS": "true",
        "CERT_FILE": str(tmp_path / "bridge.crt"), "KEY_FILE": str(tmp_path / "bridge.key"),
        "CA_FILE": str(tmp_path / "ca.pem"), "SHADOW_TOPIC_PREFIX": "test/things",
        "X100_HOST": "127.0.0.1", "X100_PORT": str(srv.port), "X100_INPUTS_FILE": str(tmp_path / "inputs.json"),
        "LOCAL_API_BIND": "off",
    })
    check = [sys.executable, "-m", "x100_bridge", "--check"]
    try:
        out = subprocess.run(check, cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stdout + out.stderr
        assert "connected and published to the device shadow" in out.stdout
        assert front.handshakes[-1] == (True, THING)  # the endpoint saw our client certificate

        # A certificate from some other CA must be refused by the endpoint ...
        (tmp_path / "other").mkdir()
        _make_certs(tmp_path / "other")
        stranger = dict(env, CERT_FILE=str(tmp_path / "other" / "bridge.crt"),
                        KEY_FILE=str(tmp_path / "other" / "bridge.key"))
        out = subprocess.run(check, cwd=ROOT, env=stranger, capture_output=True, text=True, timeout=60)
        assert out.returncode == 1 and "could not connect" in out.stdout
        assert front.handshakes[-1][0] is False

        # ... and the bridge must refuse an endpoint it can't verify.
        impostor = dict(env, CA_FILE=str(tmp_path / "other" / "ca.pem"))
        out = subprocess.run(check, cwd=ROOT, env=impostor, capture_output=True, text=True, timeout=60)
        assert out.returncode == 1 and "could not connect" in out.stdout
    finally:
        front.close()
        srv.stop()
        broker.stop()
