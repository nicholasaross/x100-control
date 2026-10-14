"""Byte transports for the ViewSonic protocol: TCP (LAN-control port) and RS-232."""

from __future__ import annotations

import logging
import socket
import threading
import time
from typing import Optional

from .protocol import Frame, ProtocolError, frame_length, looks_like_header, parse_frame

log = logging.getLogger(__name__)

DEFAULT_TCP_PORT = 4661
DEFAULT_BAUD = 115200


class TransportError(Exception):
    """The projector could not be reached or stopped answering."""


class ReplyTimeout(TransportError):
    """Connected, but no complete reply arrived in time."""


class Transport:
    """Serialises request/response exchanges and handles framing.

    Subclasses provide ``_open``, ``_close``, ``_send``, ``_recv`` and ``_drain``.
    """

    def __init__(self, timeout: float = 2.0, min_gap: float = 0.1):
        self.timeout = timeout
        self.min_gap = min_gap
        self._lock = threading.RLock()
        self._last_exchange = 0.0
        self.last_raw_reply: Optional[bytes] = None

    # -- subclass hooks -----------------------------------------------------
    def _open(self) -> None:
        raise NotImplementedError

    def _close(self) -> None:
        raise NotImplementedError

    def _is_open(self) -> bool:
        raise NotImplementedError

    def _send(self, data: bytes) -> None:
        raise NotImplementedError

    def _recv(self, n: int, deadline: float) -> bytes:
        """Return up to n bytes, b'' on timeout. Raise TransportError on EOF/IO error."""
        raise NotImplementedError

    def _drain(self) -> bytes:
        """Discard anything already buffered (late replies to earlier commands)."""
        raise NotImplementedError

    # -- public API ---------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            try:
                self._close()
            except Exception:  # pragma: no cover - best effort
                pass

    def describe(self) -> str:
        raise NotImplementedError

    def listen(self, seconds: float) -> bytes:
        """Whatever the projector sends of its own accord within ``seconds`` (up to 4 KB): nothing is
        sent to it. For finding out what it says on the cable as it powers up."""
        with self._lock:
            if not self._is_open():
                self._open()
            try:
                return self._recv(4096, time.monotonic() + seconds)
            except TransportError:
                self.close()
                raise

    def transact(self, packet: bytes, expect_reply: bool = True, timeout: Optional[float] = None,
                 retry: bool = True) -> Optional[Frame]:
        """Send one packet and (optionally) wait for one reply frame.

        Retries once on a fresh connection if the link dropped (projectors
        commonly close idle TCP sessions). ``retry=False`` is for probes, where
        silence is an answer: no retry, and the link stays open.
        """
        with self._lock:
            if not retry:
                try:
                    return self._transact_once(packet, expect_reply, timeout)
                except ReplyTimeout:
                    raise
                except (TransportError, OSError) as exc:
                    self.close()
                    if isinstance(exc, TransportError):
                        raise
                    raise TransportError(f"{self.describe()}: {exc}") from exc
            attempts = 2
            for attempt in range(1, attempts + 1):
                fresh = not self._is_open()
                try:
                    return self._transact_once(packet, expect_reply, timeout)
                except ReplyTimeout:
                    # A silent reply on a *reused* connection often means the
                    # projector dropped the session without telling us.
                    self.close()
                    if fresh or attempt == attempts:
                        raise
                    log.debug("no reply on reused connection; retrying on a fresh one")
                except (TransportError, OSError) as exc:
                    self.close()
                    if attempt == attempts:
                        if isinstance(exc, TransportError):
                            raise
                        raise TransportError(f"{self.describe()}: {exc}") from exc
                    log.debug("link error (%s); reconnecting", exc)
            return None  # pragma: no cover

    def _transact_once(self, packet: bytes, expect_reply: bool, timeout: Optional[float]) -> Optional[Frame]:
        if not self._is_open():
            self._open()
        gap = self.min_gap - (time.monotonic() - self._last_exchange)
        if gap > 0:
            time.sleep(gap)
        stale = self._drain()
        if stale:
            log.debug("discarded %d stale bytes: %s", len(stale), stale.hex(" "))
        log.debug("TX %s", packet.hex(" "))
        self._send(packet)
        try:
            if not expect_reply:
                return None
            frame = self._read_frame(time.monotonic() + (timeout or self.timeout))
            log.debug("RX %s", frame.raw.hex(" "))
            self.last_raw_reply = frame.raw
            return frame
        finally:
            self._last_exchange = time.monotonic()

    def _read_exact(self, n: int, deadline: float) -> bytes:
        buf = b""
        while len(buf) < n:
            if time.monotonic() >= deadline:
                break
            chunk = self._recv(n - len(buf), deadline)
            if chunk:
                buf += chunk
        return buf

    def _read_frame(self, deadline: float) -> Frame:
        window = self._read_exact(5, deadline)
        skipped = b""
        # Resynchronise: slide until the window looks like a header.
        while len(window) == 5 and not looks_like_header(window):
            skipped += window[:1]
            more = self._read_exact(1, deadline)
            if not more:
                break
            window = window[1:] + more
        if skipped:
            log.debug("skipped %d junk bytes: %s", len(skipped), skipped.hex(" "))
        if len(window) < 5 or not looks_like_header(window):
            got = skipped + window
            self.last_raw_reply = got or None
            if got:
                raise ReplyTimeout(f"incomplete/unrecognised reply: {got.hex(' ')}")
            raise ReplyTimeout("no reply")
        total = frame_length(window)
        rest = self._read_exact(total - 5, deadline)
        data = window + rest
        if len(data) < total:
            self.last_raw_reply = data
            raise ReplyTimeout(f"truncated reply: {data.hex(' ')}")
        try:
            return parse_frame(data)
        except ProtocolError:
            self.last_raw_reply = data
            raise


class TcpTransport(Transport):
    """ViewSonic binary protocol over TCP (LAN-control port 4661)."""

    def __init__(self, host: str, port: int = DEFAULT_TCP_PORT, timeout: float = 2.0,
                 connect_timeout: float = 3.0, min_gap: float = 0.1):
        super().__init__(timeout=timeout, min_gap=min_gap)
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self._sock: Optional[socket.socket] = None

    def describe(self) -> str:
        return f"tcp://{self.host}:{self.port}"

    def _open(self) -> None:
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        except OSError as exc:
            raise TransportError(f"cannot connect to {self.describe()}: {exc}") from exc
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock = sock

    def _close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def _is_open(self) -> bool:
        return self._sock is not None

    def _send(self, data: bytes) -> None:
        assert self._sock is not None
        self._sock.settimeout(self.connect_timeout)
        self._sock.sendall(data)

    def _recv(self, n: int, deadline: float) -> bytes:
        assert self._sock is not None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return b""
        self._sock.settimeout(remaining)
        try:
            chunk = self._sock.recv(n)
        except socket.timeout:
            return b""
        if not chunk:
            raise TransportError("connection closed by projector")
        return chunk

    def _drain(self) -> bytes:
        assert self._sock is not None
        drained = b""
        self._sock.setblocking(False)
        try:
            while True:
                try:
                    chunk = self._sock.recv(4096)
                except (BlockingIOError, InterruptedError):
                    break
                if not chunk:
                    raise TransportError("connection closed by projector")
                drained += chunk
        finally:
            self._sock.setblocking(True)
        return drained


class SerialTransport(Transport):
    """ViewSonic binary protocol over RS-232 (needs ``pyserial``)."""

    def __init__(self, device: str, baudrate: int = DEFAULT_BAUD, timeout: float = 2.0, min_gap: float = 0.1):
        super().__init__(timeout=timeout, min_gap=min_gap)
        self.device = device
        self.baudrate = baudrate
        self._ser = None

    def describe(self) -> str:
        return f"serial://{self.device}@{self.baudrate}"

    def _open(self) -> None:
        try:
            import serial  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise TransportError("pyserial is not installed (pip install pyserial)") from exc
        try:
            self._ser = serial.serial_for_url(
                self.device, baudrate=self.baudrate, bytesize=8, parity="N", stopbits=1,
                timeout=0.1, write_timeout=2,
            )
        except Exception as exc:
            raise TransportError(f"cannot open {self.describe()}: {exc}") from exc

    def _close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None

    def _is_open(self) -> bool:
        return self._ser is not None

    def _send(self, data: bytes) -> None:
        assert self._ser is not None
        self._ser.write(data)
        self._ser.flush()

    def _recv(self, n: int, deadline: float) -> bytes:
        assert self._ser is not None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return b""
        self._ser.timeout = min(remaining, 0.2)
        try:
            return self._ser.read(n)
        except Exception as exc:
            raise TransportError(str(exc)) from exc

    def _drain(self) -> bytes:
        assert self._ser is not None
        waiting = getattr(self._ser, "in_waiting", 0)
        data = self._ser.read(waiting) if waiting else b""
        self._ser.reset_input_buffer()
        return data
