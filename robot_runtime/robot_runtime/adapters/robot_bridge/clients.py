"""The scheduler uses JSON WebSocket; the robot uses robot-bridge's codec."""

from __future__ import annotations

import json
import math
import threading
from urllib.parse import urlparse


def duration(value: float, name: str, *, allow_zero: bool = False) -> float:
    value = float(value)
    if not math.isfinite(value) or value < 0 or (not allow_zero and value == 0):
        raise ValueError(f"{name} must be finite and {'nonnegative' if allow_zero else 'positive'}")
    return value


class BridgeClient:
    """Lazy, serialized connection. Never replay a command after a lost reply."""

    def __init__(self, url: str, *, timeout_s: float = 5.0, json_protocol: bool = False):
        if urlparse(url).scheme not in {"ws", "wss"} or not urlparse(url).hostname:
            raise ValueError("robot-bridge URL must use ws:// or wss://")
        self.url = url
        self.timeout_s = duration(timeout_s, "timeout_s")
        self.json_protocol = json_protocol
        self._connection = None
        self._lock = threading.Lock()
        self._request_id = 0

    def call(self, request: dict) -> dict:
        # Connect directly, with bounded timeouts, using the upstream codec
        # for binary RPC. Importing robot-bridge never instantiates a controller.
        from websockets.sync.client import connect

        with self._lock:
            try:
                if self._connection is None:
                    self._connection = connect(self.url, open_timeout=self.timeout_s,
                                               close_timeout=1, compression=None, max_size=None)
                if self.json_protocol:
                    self._request_id += 1
                    request = {**request, "id": self._request_id}
                    self._connection.send(json.dumps(request))
                    result = json.loads(self._connection.recv(timeout=self.timeout_s))
                    if not isinstance(result, dict) or result.get("id") != self._request_id:
                        raise RuntimeError("scheduler returned a mismatched request ID")
                else:
                    from robot_bridge.transport.codec import packb, unpackb

                    self._connection.send(packb(request))
                    result = unpackb(self._connection.recv(timeout=self.timeout_s))
                if not isinstance(result, dict) or result.get("status") != "ok":
                    raise RuntimeError(f"robot-bridge {request.get('cmd')} failed: {result}")
                return result
            except Exception:
                self._disconnect()
                raise

    def _disconnect(self):
        connection, self._connection = self._connection, None
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass

    def close(self):
        with self._lock:
            self._disconnect()
