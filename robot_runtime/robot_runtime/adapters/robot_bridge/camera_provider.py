"""Read three JPEG views via get_obs; expose immutable HTTP snapshots."""

from __future__ import annotations

import base64
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import math
import threading
import time

from robot_runtime.core.types import ObservationFrame, ObservationImage
from .clients import BridgeClient, duration

CAMERA_VIEWS = {
    "cam_high": "face_view",
    "cam_left_wrist": "left_wrist_view",
    "cam_right_wrist": "right_wrist_view",
}


@dataclass
class _Snapshot:
    frame_id: str
    images: dict[str, bytes]
    image_ids: dict[str, str]
    received_at: float
    received_monotonic: float
    obs_lag_ms: float | None


class RobotBridgeCameraProvider:
    def __init__(self, robot_url: str = "ws://127.0.0.1:9946", *,
                 timeout_s: float = 5.0, cache_s: float = 0.5,
                 snapshot_ttl_s: float = 30.0, max_snapshots: int = 32,
                 max_obs_lag_s: float = 3.0, quality: int = 90,
                 size: list[int] | None = None, client=None):
        self.cache_s = duration(cache_s, "camera.cache_s", allow_zero=True)
        self.snapshot_ttl_s = duration(snapshot_ttl_s, "camera.snapshot_ttl_s")
        self.max_obs_lag_s = duration(max_obs_lag_s, "camera.max_obs_lag_s")
        if self.snapshot_ttl_s <= self.cache_s:
            raise ValueError("snapshot_ttl_s must exceed cache_s")
        if not isinstance(max_snapshots, int) or max_snapshots < 1:
            raise ValueError("max_snapshots must be a positive integer")
        if not isinstance(quality, int) or not 1 <= quality <= 100:
            raise ValueError("camera.quality must be an integer in 1..100")
        if size is not None and (len(size) != 2 or any(
            not isinstance(v, int) or v <= 0 for v in size
        )):
            raise ValueError("camera.size must be [height, width] with positive integers")
        self.max_snapshots = max_snapshots
        self.robot_url = robot_url
        self._client = client or BridgeClient(robot_url, timeout_s=timeout_s)
        self._request = {"cmd": "get_obs", "image_ts": [0.0],
                         "image_format": {"encoding": "jpeg", "quality": quality}}
        if size is not None:
            self._request["image_format"]["size"] = list(size)
        self._lock = threading.RLock()
        self._snapshots: OrderedDict[str, _Snapshot] = OrderedDict()
        self._latest: _Snapshot | None = None
        self._error: str | None = None

    def _capture(self) -> _Snapshot:
        with self._lock:
            if self._latest and time.monotonic() - self._latest.received_monotonic < self.cache_s:
                return self._latest
            try:
                response = self._client.call(self._request)
                if response.get("status") != "ok":
                    raise ValueError(f"get_obs failed: {response}")
                obs = response["obs"]
                lag = obs.get("obs_lag_ms")
                if lag is not None:
                    lag = float(lag)
                    if not math.isfinite(lag) or lag < 0 or lag > self.max_obs_lag_s * 1000:
                        raise ValueError(f"invalid or stale observation: obs_lag_ms={lag}")
                import cv2
                import numpy as np

                images = {}
                for camera, view in CAMERA_VIEWS.items():
                    frames = obs["images"][view]
                    if not isinstance(frames, (list, tuple)) or len(frames) != 1:
                        raise ValueError(f"expected one JPEG for {view}")
                    jpeg = frames[0]
                    if not isinstance(jpeg, bytes) or not jpeg.startswith(b"\xff\xd8"):
                        raise ValueError(f"invalid JPEG for {view}")
                    if cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR) is None:
                        raise ValueError(f"undecodable JPEG for {view}")
                    images[camera] = jpeg
                image_ids = {name: hashlib.sha256(data).hexdigest() for name, data in images.items()}
                frame_id = hashlib.sha256("".join(image_ids.values()).encode()).hexdigest()
                snapshot = _Snapshot(frame_id, images, image_ids, time.time(), time.monotonic(), lag)
                self._snapshots[frame_id] = snapshot
                self._snapshots.move_to_end(frame_id)
                for key, old in list(self._snapshots.items()):
                    if snapshot.received_monotonic - old.received_monotonic > self.snapshot_ttl_s:
                        del self._snapshots[key]
                while len(self._snapshots) > self.max_snapshots:
                    self._snapshots.popitem(last=False)
                self._latest, self._error = snapshot, None
                return snapshot
            except Exception as exc:
                self._error = str(exc)
                # Never turn an RPC failure into an apparently fresh old frame.
                raise FileNotFoundError(f"robot-bridge camera unavailable: {exc}") from exc

    def latest(self) -> ObservationFrame:
        snapshot = self._capture()
        images = {name: base64.b64encode(data).decode("ascii") for name, data in snapshot.images.items()}
        images["concatenated_image"] = images["cam_high"]
        return ObservationFrame(
            images=images, timestamp=None, frame_id=snapshot.frame_id,
            mime_types={name: "image/jpeg" for name in images},
            metadata={"provider": "robot_bridge", "received_at": snapshot.received_at,
                      "obs_lag_ms": snapshot.obs_lag_ms, "timestamp_source": "unavailable",
                      "synchronization_verified": False, "image_ids": dict(snapshot.image_ids)},
        )

    def binary_endpoints(self, frame_id: str) -> dict[str, str]:
        return {camera: f"/observations/frames/{frame_id}/{camera}.jpg"
                for camera in [*CAMERA_VIEWS, "concatenated_image"]}

    def latest_image(self, camera: str) -> ObservationImage:
        self._normalize(camera)
        return self._image(self._capture(), camera)

    def snapshot_image(self, frame_id: str, camera: str) -> ObservationImage:
        self._normalize(camera)
        with self._lock:
            snapshot = self._snapshots.get(frame_id)
            if snapshot is None or time.monotonic() - snapshot.received_monotonic > self.snapshot_ttl_s:
                raise FileNotFoundError("camera snapshot expired; fetch metadata again")
            return self._image(snapshot, camera)

    @staticmethod
    def _normalize(camera):
        camera = "cam_high" if camera == "concatenated_image" else camera
        if camera not in CAMERA_VIEWS:
            raise KeyError(f"unknown camera: {camera}")
        return camera

    def _image(self, snapshot: _Snapshot, camera: str) -> ObservationImage:
        camera = self._normalize(camera)
        # Current Robot Server omits source timestamps. Do not emit a receipt
        # timestamp as X-Timestamp: GRM would misreport camera synchronization.
        return ObservationImage(camera=camera, data=snapshot.images[camera], timestamp=None,
                                frame_id=snapshot.image_ids[camera],
                                metadata={"snapshot_id": snapshot.frame_id})

    def camera_names(self) -> list[str]:
        return list(CAMERA_VIEWS)

    def health(self) -> dict:
        with self._lock:
            age = None if self._latest is None else time.monotonic() - self._latest.received_monotonic
            return {"provider": "robot_bridge", "robot_url": self.robot_url,
                    "ready": age is not None and age <= self.snapshot_ttl_s and self._error is None,
                    "last_fetch_age_s": age, "error": self._error,
                    "source_timestamps_available": False}

    def close(self):
        self._client.close()
