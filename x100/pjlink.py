"""Minimal PJLink Class 1 client (TCP 4352).

ViewSonic's LAN-control module speaks PJLink on many models. It is optional
here: the probe reports whether your X100-4K answers, and the bridge can use it
as a two-way power-status source if the ViewSonic port (4661) turns out to be
write-only on your unit.
"""

from __future__ import annotations

import hashlib
import socket
from typing import Dict, List, Optional

from .protocol import PowerState

DEFAULT_PORT = 4352

ERRORS = {
    "ERR1": "undefined command",
    "ERR2": "out of parameter",
    "ERR3": "unavailable time (projector busy or in standby)",
    "ERR4": "projector/display failure",
}

POWER = {"0": PowerState.OFF, "1": PowerState.ON, "2": PowerState.COOLING, "3": PowerState.WARMING}


class PJLinkError(Exception):
    pass


class PJLinkAuthError(PJLinkError):
    pass


class PJLink:
    def __init__(self, host: str, port: int = DEFAULT_PORT, password: Optional[str] = None, timeout: float = 3.0):
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout

    def describe(self) -> str:
        return f"pjlink://{self.host}:{self.port}"

    def _readline(self, sock: socket.socket) -> str:
        buf = b""
        while not buf.endswith(b"\r"):
            chunk = sock.recv(1)
            if not chunk:
                break
            buf += chunk
            if len(buf) > 512:
                break
        return buf.decode("ascii", "replace").strip()

    def run(self, *commands: str) -> List[str]:
        """Run commands like 'POWR ?' on one connection; returns the raw values."""
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as exc:
            raise PJLinkError(f"cannot connect to {self.describe()}: {exc}") from exc
        try:
            sock.settimeout(self.timeout)
            greeting = self._readline(sock)
            parts = greeting.split()
            if len(parts) < 2 or parts[0].upper() != "PJLINK":
                raise PJLinkError(f"unexpected greeting {greeting!r}")
            prefix = ""
            if parts[1] == "1":
                if not self.password:
                    raise PJLinkAuthError("projector requires a PJLink password")
                seed = parts[2] if len(parts) > 2 else ""
                prefix = hashlib.md5((seed + self.password).encode("ascii")).hexdigest()
            elif parts[1] != "0":
                raise PJLinkError(f"unexpected greeting {greeting!r}")
            results = []
            for i, cmd in enumerate(commands):
                line = (prefix if i == 0 else "") + "%1" + cmd + "\r"
                sock.sendall(line.encode("ascii"))
                reply = self._readline(sock)
                if reply.upper() == "PJLINK ERRA":
                    raise PJLinkAuthError("PJLink password rejected")
                body = cmd.split()[0]
                head = "%1" + body + "="
                if not reply.upper().startswith(head.upper()):
                    raise PJLinkError(f"unexpected reply to {cmd!r}: {reply!r}")
                value = reply[len(head):]
                if value.upper() in ERRORS:
                    raise PJLinkError(f"{cmd}: {ERRORS[value.upper()]}")
                results.append(value)
            return results
        except socket.timeout as exc:
            raise PJLinkError(f"{self.describe()}: timed out") from exc
        except OSError as exc:
            raise PJLinkError(f"{self.describe()}: {exc}") from exc
        finally:
            sock.close()

    def query(self, cmd: str) -> str:
        return self.run(cmd + " ?")[0]

    # Convenience wrappers ------------------------------------------------------
    def power(self) -> PowerState:
        return POWER.get(self.query("POWR"), PowerState.UNKNOWN)

    def power_on(self) -> None:
        self.run("POWR 1")

    def power_off(self) -> None:
        self.run("POWR 0")

    def input(self) -> str:
        return self.query("INPT")

    def select_input(self, code: str) -> None:
        self.run(f"INPT {code}")

    def info(self) -> Dict[str, str]:
        """Best-effort identification; individual errors are reported inline."""
        out: Dict[str, str] = {}
        for key in ("NAME", "INF1", "INF2", "CLSS", "INST", "POWR", "INPT", "AVMT"):
            try:
                out[key] = self.query(key)
            except PJLinkAuthError:
                raise
            except PJLinkError as exc:
                out[key] = f"<{exc}>"
        return out
