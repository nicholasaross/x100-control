"""Stand-ins for AWS IoT so the bridge and Lambda can be tested together.

FakeShadow follows the documented classic-shadow merge rules: an update merges
keys into ``state.reported``, ``null`` deletes a key, arrays are replaced
wholesale, and every updated leaf gets a server-side ``metadata`` timestamp.
"""

from __future__ import annotations

import copy
import io
import json
import threading
import time
from typing import Any, Callable, Dict, List, Optional


def _merge(dst: Dict[str, Any], meta: Dict[str, Any], src: Dict[str, Any], ts: int) -> None:
    for key, value in src.items():
        if value is None:
            dst.pop(key, None)
            meta.pop(key, None)
        elif isinstance(value, dict):
            if not isinstance(dst.get(key), dict):
                dst[key] = {}
                meta[key] = {}
            _merge(dst[key], meta[key], value, ts)
        else:
            dst[key] = copy.deepcopy(value)
            meta[key] = [{"timestamp": ts} for _ in value] if isinstance(value, list) else {"timestamp": ts}


class FakeShadow:
    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self.reported: Dict[str, Any] = {}
        self.metadata: Dict[str, Any] = {}
        self.version = 0
        self.updates: List[Dict[str, Any]] = []
        self.lock = threading.Lock()

    def update_reported(self, reported: Dict[str, Any]) -> None:
        with self.lock:
            _merge(self.reported, self.metadata, reported, int(self.clock()))
            self.version += 1
            self.updates.append(copy.deepcopy(reported))

    def document(self) -> Dict[str, Any]:
        with self.lock:
            if self.version == 0:
                raise LookupError("no shadow")
            return {
                "state": {"reported": copy.deepcopy(self.reported)},
                "metadata": {"reported": copy.deepcopy(self.metadata)},
                "version": self.version,
                "timestamp": int(self.clock()),
            }


class ResourceNotFound(Exception):
    pass


class FakeIotData:
    """Mimics the parts of boto3's ``iot-data`` client the Lambda uses."""

    class exceptions:  # noqa: N801 - mirror boto3's attribute name
        ResourceNotFoundException = ResourceNotFound

    def __init__(self, shadow: FakeShadow, on_publish: Optional[Callable[[str, bytes], None]] = None):
        self.shadow = shadow
        self.on_publish = on_publish
        self.published: List[Dict[str, Any]] = []
        self.get_calls = 0

    def publish(self, topic: str, qos: int = 0, payload: bytes = b"") -> Dict[str, Any]:
        self.published.append({"topic": topic, "qos": qos, "payload": json.loads(payload)})
        if self.on_publish:
            self.on_publish(topic, payload)
        return {}

    def get_thing_shadow(self, thingName: str) -> Dict[str, Any]:  # noqa: N803 - boto3 casing
        self.get_calls += 1
        try:
            doc = self.shadow.document()
        except LookupError as exc:
            raise ResourceNotFound(str(exc)) from exc
        return {"payload": io.BytesIO(json.dumps(doc).encode())}
