"""Local HTTP API: lets devices on your network (the Pi listener) drive the projector.

    POST /command   {"ops": [{"op": "power", "value": "ON"}]}
                    200 {"id": "...", "ok": true, "deferred": false, "state": {...}}
                    200 {"ok": false, "code": "ENDPOINT_UNREACHABLE", "message": "...", ...}
    GET  /state     200 the full state: power, input, volume, muted, inputs, scenes
    GET  /healthz   200 {"ok": true, "version": "...", "build": "..."}  (no token needed)

The op schema is the bridge's own (see ``x100_bridge.core``). ``/command``
answers as soon as the projector accepts the write. ``deferred: true`` means
the command was queued until the projector finishes warming up.

Everything except ``/healthz`` needs ``Authorization: Bearer <LOCAL_API_TOKEN>``.
Other status codes: 400 malformed request, 401 missing or wrong token, 404/405
wrong path or method, 411/413 missing or oversized body, 504 the bridge didn't
answer in time.

Without a token the API refuses to listen anywhere but loopback. Plain HTTP on
your LAN: anyone who can see the traffic could replay the token, so keep the
bridge and the listener on your home network, not on a guest or public one.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

from x100 import BUILD, __version__

log = logging.getLogger("x100.bridge.api")

DEFAULT_PORT = 8661
MAX_BODY = 16 * 1024
MAX_OPS = 20
MIN_TOKEN_LENGTH = 16


class ApiConfigError(ValueError):
    """LOCAL_API_BIND / LOCAL_API_TOKEN can't be used as given."""


def parse_bind(text: str, default_port: int = DEFAULT_PORT) -> Tuple[str, int]:
    """``0.0.0.0:8661``, ``127.0.0.1``, ``:8661``, ``[::]:8661`` or ``::1`` -> (host, port)."""
    text = text.strip()
    if text.startswith("["):
        host, _, rest = text[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else rest
    elif text.count(":") == 1:
        host, port = text.split(":")
    else:  # a bare IPv4 address or name, or a bare IPv6 address
        host, port = text, ""
    host = host or "0.0.0.0"
    try:
        port_num = int(port) if port else default_port
    except ValueError:
        raise ApiConfigError(f"LOCAL_API_BIND={text!r}: the port isn't a number") from None
    if not 0 <= port_num <= 65535:
        raise ApiConfigError(f"LOCAL_API_BIND={text!r}: the port must be 0-65535")
    return host, port_num


def is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_settings(bind: str, token: str) -> Tuple[str, int]:
    """Validate the API settings; returns (host, port) or raises ApiConfigError."""
    host, port = parse_bind(bind)
    if not token and not is_loopback(host):
        raise ApiConfigError(f"LOCAL_API_TOKEN must be set to listen on {host}:{port} "
                             f"(or set LOCAL_API_BIND=127.0.0.1:{port} to keep the API on this machine)")
    if token and len(token) < MIN_TOKEN_LENGTH:
        raise ApiConfigError(f"LOCAL_API_TOKEN is too short: use at least {MIN_TOKEN_LENGTH} characters, "
                             "for example the output of `openssl rand -hex 16`")
    return host, port


def validate_ops(body: Any) -> List[Dict[str, Any]]:
    """Structural check only. The bridge core validates each op's value."""
    if not isinstance(body, dict) or not isinstance(body.get("ops"), list) or not body["ops"]:
        raise ValueError('expected {"ops": [{"op": "power", "value": "ON"}, ...]}')
    ops = body["ops"]
    if len(ops) > MAX_OPS:
        raise ValueError(f"at most {MAX_OPS} ops per command")
    for op in ops:
        if not isinstance(op, dict) or not isinstance(op.get("op"), str):
            raise ValueError('each op must be an object with an "op" name')
    return ops


def describe_ops(ops: List[Dict[str, Any]]) -> str:
    return ", ".join(f"{o.get('op')}={o.get('value')}" for o in ops)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: Tuple[str, int], handler: type, family: int):
        self.address_family = family
        super().__init__(address, handler)


class LocalApi:
    """The HTTP front end. ``core`` needs ``submit_and_wait(ops, timeout)`` and ``snapshot()``."""

    def __init__(self, core: Any, bind: str = f"127.0.0.1:{DEFAULT_PORT}", token: str = "",
                 timeout: float = 10.0):
        host, port = check_settings(bind, token)
        self.core = core
        self.token = token
        self.timeout = timeout
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        self.httpd = _Server((host, port), self._handler_class(), family)
        self.host = host
        self.port = self.httpd.server_address[1]
        self._thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"

    @property
    def local_url(self) -> str:
        """A URL that reaches the API from this machine, even when bound to all interfaces."""
        if self.host in ("0.0.0.0", ""):
            return f"http://127.0.0.1:{self.port}"
        if self.host == "::":
            return f"http://[::1]:{self.port}"
        return self.url

    def start(self) -> "LocalApi":
        self._thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.25},
                                        name="local-api", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._thread is not None:
            self.httpd.shutdown()
            self._thread.join(timeout=5)
            self._thread = None
        self.httpd.server_close()

    def __enter__(self) -> "LocalApi":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # ------------------------------------------------------------------ requests
    def _handler_class(self) -> type:
        api = self

        class Handler(BaseHTTPRequestHandler):
            server_version = f"x100-bridge/{__version__}"
            sys_version = ""
            timeout = 10  # seconds per socket read, so a stalled client can't hold a thread

            def log_message(self, fmt: str, *args: Any) -> None:
                log.debug("%s %s", self.client_address[0], fmt % args)

            def _send(self, status: int, body: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> None:
                data = json.dumps(body, separators=(",", ":")).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(data)

            def _error(self, status: int, code: str, message: str,
                       headers: Optional[Dict[str, str]] = None) -> None:
                self._send(status, {"ok": False, "code": code, "message": message}, headers)

            def _path(self) -> str:
                return self.path.split("?", 1)[0].rstrip("/") or "/"

            def _authorised(self) -> bool:
                if not api.token:
                    return True  # only possible on loopback (enforced at start-up)
                scheme, _, given = self.headers.get("Authorization", "").partition(" ")
                if scheme.lower() == "bearer" and hmac.compare_digest(given.strip().encode(), api.token.encode()):
                    return True
                log.warning("local API: refused %s %s from %s (missing or wrong token)",
                            self.command, self._path(), self.client_address[0])
                self._error(401, "UNAUTHORIZED", "missing or wrong bearer token",
                            {"WWW-Authenticate": 'Bearer realm="x100-bridge"'})
                return False

            def do_GET(self) -> None:  # noqa: N802 - http.server naming
                path = self._path()
                if path == "/healthz":
                    self._send(200, {"ok": True, "version": __version__, "build": BUILD})
                elif path == "/state":
                    if self._authorised():
                        self._send(200, api.core.snapshot())
                elif path == "/command":
                    self._error(405, "METHOD_NOT_ALLOWED", "use POST", {"Allow": "POST"})
                else:
                    self._error(404, "NOT_FOUND", f"no such endpoint: {path}")

            def _read_body(self) -> Optional[bytes]:
                """Read the whole body before answering anything, even an error:
                closing with unread data makes the client see a reset, not our reply."""
                try:
                    length = int(self.headers.get("Content-Length", ""))
                except ValueError:
                    length = -1
                if length < 0:
                    self.close_connection = True
                    self._error(411, "LENGTH_REQUIRED", "send a Content-Length header")
                    return None
                if length > MAX_BODY:
                    # Drain a bounded amount so the client gets our reply, not a reset.
                    remaining = min(length, 4 * MAX_BODY)
                    while remaining > 0:
                        chunk = self.rfile.read(min(remaining, 65536))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                    self.close_connection = True
                    self._error(413, "TOO_LARGE", f"the body must be at most {MAX_BODY} bytes")
                    return None
                return self.rfile.read(length)

            def do_POST(self) -> None:  # noqa: N802
                raw = self._read_body()
                if raw is None:
                    return
                path = self._path()
                if path != "/command":
                    if path in ("/state", "/healthz"):
                        self._error(405, "METHOD_NOT_ALLOWED", "use GET", {"Allow": "GET"})
                    else:
                        self._error(404, "NOT_FOUND", f"no such endpoint: {path}")
                    return
                if not self._authorised():
                    return
                try:
                    ops = validate_ops(json.loads(raw or b"null"))
                except ValueError as exc:  # includes JSON decode errors
                    self._error(400, "BAD_REQUEST", str(exc))
                    return
                started = time.monotonic()
                try:
                    result = api.core.submit_and_wait(ops, timeout=api.timeout)
                except TimeoutError as exc:
                    log.warning("local API: %s from %s timed out", describe_ops(ops), self.client_address[0])
                    self._error(504, "TIMEOUT", str(exc))
                    return
                outcome = ("ok" + (" (queued until the projector is ready)" if result.get("deferred") else "")
                           if result.get("ok") else f"{result.get('code')}: {result.get('message')}")
                log.info("local API: %s from %s -> %s in %.0f ms", describe_ops(ops), self.client_address[0],
                         outcome, (time.monotonic() - started) * 1000)
                self._send(200, result)

            def _not_allowed(self) -> None:
                self._error(405, "METHOD_NOT_ALLOWED", f"{self.command} isn't supported")

            do_PUT = do_DELETE = do_PATCH = _not_allowed

        return Handler
