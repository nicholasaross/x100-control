"""Talks to x100-bridge's local API (see x100_bridge/local_api.py)."""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class Result:
    ok: bool
    code: str = ""
    message: str = ""
    deferred: bool = False
    seconds: float = 0.0
    state: Optional[Dict[str, Any]] = None

    def describe(self) -> str:
        if self.code == "DRY_RUN":
            return "dry run, nothing sent"
        if self.ok:
            return "ok" + (" (queued until the projector is ready)" if self.deferred else "")
        return f"{self.code}: {self.message}"


class BridgeClient:
    def __init__(self, url: str, token: str = "", timeout: float = 10.0):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None,
                 auth: bool = True) -> Dict[str, Any]:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        if auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read() or b"{}")

    def command(self, ops: List[Dict[str, Any]]) -> Result:
        started = time.monotonic()
        try:
            doc = self._request("POST", "/command", {"ops": ops})
        except urllib.error.HTTPError as exc:
            try:
                err = json.loads(exc.read() or b"{}")
            except ValueError:
                err = {}
            return Result(False, err.get("code") or f"HTTP_{exc.code}", err.get("message") or str(exc),
                          seconds=time.monotonic() - started)
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            return Result(False, "BRIDGE_UNREACHABLE", f"{self.url}: {reason}", seconds=time.monotonic() - started)
        except ValueError as exc:  # not JSON
            return Result(False, "BAD_RESPONSE", str(exc), seconds=time.monotonic() - started)
        return Result(bool(doc.get("ok")), doc.get("code", ""), doc.get("message", ""), bool(doc.get("deferred")),
                      time.monotonic() - started, doc.get("state"))

    def health(self) -> Dict[str, Any]:
        return self._request("GET", "/healthz", auth=False)

    def state(self) -> Dict[str, Any]:
        return self._request("GET", "/state")
