"""Dual-Franka camera provider implementations."""

from __future__ import annotations

import base64
import mimetypes
import time
from pathlib import Path

from robot_runtime.core.types import JsonDict, ObservationFrame, ObservationImage

DEFAULT_IMAGE_DIR = Path("/tmp/img")

CAMERA_FILES = {
    "cam_high": "base_0_rgb.jpg",
    "cam_left_wrist": "left_wrist_0_rgb.jpg",
    "cam_right_wrist": "right_wrist_0_rgb.jpg",
}


class DualFrankaLocalFileCameraProvider:
    """Reads latest camera JPEGs from local files behind the runtime boundary."""

    def __init__(self, image_dir: str | Path = DEFAULT_IMAGE_DIR) -> None:
        self.image_dir = Path(image_dir).expanduser()

    def latest(self) -> ObservationFrame:
        images: dict[str, str] = {}
        missing: list[str] = []
        latest_mtime = 0.0
        mime_types: dict[str, str] = {}
        for camera_name, filename in CAMERA_FILES.items():
            path = self.image_dir / filename
            if not path.exists():
                missing.append(f"{camera_name}:{path}")
                continue
            latest_mtime = max(latest_mtime, path.stat().st_mtime)
            images[camera_name] = base64.b64encode(path.read_bytes()).decode("ascii")
            mime_types[camera_name] = _mime_type(path)
        if missing:
            raise FileNotFoundError(f"missing camera image(s): {', '.join(missing)}")
        timestamp = latest_mtime or time.time()
        frame_id = f"frame-{int(timestamp * 1000)}"
        mime_types["concatenated_image"] = mime_types["cam_high"]
        return ObservationFrame(
            images={"concatenated_image": images["cam_high"], **images},
            timestamp=timestamp,
            frame_id=frame_id,
            metadata={"image_dir": str(self.image_dir)},
            mime_types=mime_types,
        )

    def latest_image(self, camera: str) -> ObservationImage:
        normalized_camera = "cam_high" if camera == "concatenated_image" else camera
        filename = CAMERA_FILES.get(normalized_camera)
        if filename is None:
            raise KeyError(f"unknown camera: {camera}")
        path = self.image_dir / filename
        if not path.exists():
            raise FileNotFoundError(f"missing camera image: {normalized_camera}:{path}")
        timestamp = path.stat().st_mtime
        return ObservationImage(
            camera=normalized_camera,
            data=path.read_bytes(),
            timestamp=timestamp,
            frame_id=f"frame-{int(timestamp * 1000)}",
            mime_type=_mime_type(path),
            metadata={"image_dir": str(self.image_dir), "path": str(path)},
        )

    def health(self) -> JsonDict:
        available: list[str] = []
        missing: list[str] = []
        for camera_name, filename in CAMERA_FILES.items():
            path = self.image_dir / filename
            if path.exists():
                available.append(camera_name)
            else:
                missing.append(f"{camera_name}:{path}")
        return {
            "provider": "local_files",
            "image_dir": str(self.image_dir),
            "available_cameras": available,
            "missing_cameras": missing,
        }

    def camera_names(self) -> list[str]:
        return list(CAMERA_FILES)


def _mime_type(path: Path) -> str:
    return mimetypes.guess_type(str(path))[0] or "image/jpeg"
