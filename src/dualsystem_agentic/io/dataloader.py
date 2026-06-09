"""DataLoader — real-time image acquisition layer.

Separates image fetching from the MCP tool surface (MCP carries structured data;
the DataLoader carries visual observations). Modelled on RoboClaw's DataLoader
layer but kept transport-agnostic.

Implementations:
    StaticDataLoader   — wraps CLI ``--image`` files (backward compatible)
    HTTPDataLoader     — polls an HTTP endpoint (e.g. x2robot bridge ``/cameras/latest``)
    MockDataLoader     — generates synthetic images for offline testing
"""

from __future__ import annotations

import base64
import io
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from dualsystem_agentic.core.types import ImageInput

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class CameraFrame:
    """One captured observation frame, possibly multi-view."""

    images: dict[str, ImageInput] = field(default_factory=dict)
    timestamp: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

class DataLoader(Protocol):
    """Acquire the latest camera observation."""

    def capture(self) -> CameraFrame | None:
        """Return the latest frame, or ``None`` if unavailable."""


# ---------------------------------------------------------------------------
# StaticDataLoader — wraps pre-loaded images (CLI ``--image`` fallback)
# ---------------------------------------------------------------------------

class StaticDataLoader:
    """Return the same images on every call. Used when no live camera exists."""

    def __init__(self, images: dict[str, ImageInput] | None = None) -> None:
        self._frame = CameraFrame(
            images=dict(images or {}),
            timestamp=time.time(),
        ) if images else None

    def capture(self) -> CameraFrame | None:
        return self._frame


# ---------------------------------------------------------------------------
# HTTPDataLoader — polls a bridge/camera HTTP endpoint
# ---------------------------------------------------------------------------

class HTTPDataLoader:
    """Fetch concatenated or individual camera images from an HTTP endpoint.

    Expects a JSON response like:
        {"concatenated_image": "<base64 jpeg>", "timestamp": 1234.5}
    or:
        {"head": "<base64>", "left_wrist": "<base64>", ...}

    Configured via YAML ``dataloader`` section.
    """

    def __init__(
        self,
        url: str,
        *,
        timeout: float = 10.0,
        image_key: str = "concatenated_image",
        label: str = "main",
    ) -> None:
        self.url = url
        self.timeout = timeout
        self.image_key = image_key
        self.label = label

    def capture(self) -> CameraFrame | None:
        import urllib.error
        import urllib.request

        try:
            request = urllib.request.Request(self.url, method="GET")
            import json
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            logger.warning("HTTPDataLoader: failed to fetch %s: %s", self.url, exc)
            return None

        return self._parse_response(_unwrap_bridge_response(data))

    def _parse_response(self, data: dict[str, Any]) -> CameraFrame | None:
        data = _unwrap_bridge_response(data)
        images: dict[str, ImageInput] = {}

        # Try the configured single-key first (e.g. concatenated_image).
        b64 = data.get(self.image_key)
        if isinstance(b64, str) and b64:
            images[self.label] = ImageInput(
                type="base64", data=b64, mime_type="image/jpeg",
            )

        # Also pick up any top-level keys whose values look like base64 images
        # (e.g. "head", "left_wrist", "right_wrist").
        for key, value in data.items():
            if key in ("timestamp", "success", "message") or key == self.image_key:
                continue
            if isinstance(value, str) and len(value) > 100:
                images[key] = ImageInput(type="base64", data=value, mime_type="image/jpeg")

        if not images:
            return None

        return CameraFrame(
            images=images,
            timestamp=float(data.get("timestamp") or time.time()),
        )


# ---------------------------------------------------------------------------
# MockDataLoader — synthetic images for offline testing
# ---------------------------------------------------------------------------

class MockDataLoader:
    """Generate simple synthetic JPEG frames for offline loop verification."""

    def __init__(self, width: int = 320, height: int = 240) -> None:
        self.width = width
        self.height = height
        self._frame_count = 0

    def capture(self) -> CameraFrame:
        self._frame_count += 1
        img = self._generate_frame(self._frame_count)
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return CameraFrame(
            images={"main": ImageInput(type="base64", data=b64, mime_type="image/jpeg")},
            timestamp=time.time(),
            metadata={"frame_count": self._frame_count},
        )

    def _generate_frame(self, frame_count: int):
        """Build a simple PIL image with frame counter text."""
        try:
            from PIL import Image, ImageDraw
        except ImportError as exc:
            raise ImportError("Pillow is required for MockDataLoader") from exc

        img = Image.new("RGB", (self.width, self.height), color=(60, 60, 60))
        draw = ImageDraw.Draw(img)
        draw.text(
            (10, self.height // 2 - 10),
            f"Mock frame #{frame_count}",
            fill=(200, 200, 200),
        )
        draw.text(
            (10, self.height // 2 + 15),
            time.strftime("%H:%M:%S"),
            fill=(150, 150, 150),
        )
        return img


def _unwrap_bridge_response(data: Any) -> dict[str, Any]:
    """Accept either raw image JSON or ``{"success": true, "data": {...}}``."""
    if not isinstance(data, dict):
        return {}
    payload = data.get("data")
    if isinstance(payload, dict) and (data.get("success") is True or data.get("ok") is True):
        return payload
    return data
