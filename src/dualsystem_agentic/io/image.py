"""Image normalization helpers for VLM planners."""

from __future__ import annotations

import base64
import mimetypes
from io import BytesIO
from pathlib import Path
from typing import Any

from dualsystem_agentic.core.types import ImageInput


def parse_image_spec(spec: str) -> tuple[str, ImageInput]:
    """Parse ``key=path`` CLI image syntax into ``(key, ImageInput)``."""
    if "=" not in spec:
        raise ValueError(f"Image spec must use key=path syntax: {spec}")
    key, value = spec.split("=", 1)
    key = key.strip()
    if not key:
        raise ValueError(f"Image key must not be empty: {spec}")
    return key, normalize_image(value.strip())


def normalize_image(value: Any, mime_type: str | None = None) -> ImageInput:
    """Normalize path/url/base64/bytes/PIL inputs into ``ImageInput``."""
    if isinstance(value, ImageInput):
        return value
    if isinstance(value, bytes):
        return image_from_bytes(value, mime_type=mime_type)
    if isinstance(value, str):
        if value.startswith(("http://", "https://")):
            return ImageInput(type="url", data=value)
        if value.startswith("data:image/"):
            header, data = value.split(",", 1)
            return ImageInput(
                type="base64",
                data=data,
                mime_type=header.removeprefix("data:").split(";")[0],
            )
        path = Path(value).expanduser()
        if path.exists():
            return image_from_path(path)
        return ImageInput(type="base64", data=value, mime_type=mime_type)
    if _looks_like_pil(value):
        return image_from_pil(value)
    raise TypeError(f"Unsupported image input type: {type(value)}")


def image_from_path(path: str | Path) -> ImageInput:
    """Read a local image path as a base64 payload."""
    image_path = Path(path).expanduser()
    mime_type = mimetypes.guess_type(str(image_path))[0] or "image/jpeg"
    data = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return ImageInput(type="base64", data=data, mime_type=mime_type, path=str(image_path))


def image_from_bytes(data: bytes, mime_type: str | None = None) -> ImageInput:
    """Create a base64 image payload from bytes."""
    return ImageInput(
        type="base64",
        data=base64.b64encode(data).decode("ascii"),
        mime_type=mime_type or "image/jpeg",
    )


def image_from_pil(image, image_format: str = "JPEG") -> ImageInput:
    """Create an image payload from a PIL image."""
    buffer = BytesIO()
    image.save(buffer, format=image_format)
    return image_from_bytes(buffer.getvalue(), mime_type=f"image/{image_format.lower()}")


def image_to_openai_content(image: ImageInput) -> dict[str, Any]:
    """Convert ``ImageInput`` to OpenAI-compatible message content."""
    if image.type == "url":
        url = image.data
    elif image.type == "base64":
        mime_type = image.mime_type or "image/jpeg"
        url = f"data:{mime_type};base64,{image.data}"
    elif image.type == "path":
        converted = image_from_path(image.data)
        mime_type = converted.mime_type or "image/jpeg"
        url = f"data:{mime_type};base64,{converted.data}"
    else:
        raise ValueError(f"Unsupported image type for OpenAI content: {image.type}")
    return {"type": "image_url", "image_url": {"url": url}}


def image_input_to_pil(image: ImageInput):
    """Convert a base64 or path image input to PIL."""
    try:
        from PIL import Image
    except ImportError as exc:
        raise ImportError("Pillow is required for PIL conversion") from exc

    if image.type == "base64":
        raw = base64.b64decode(image.data)
        return Image.open(BytesIO(raw)).convert("RGB")
    if image.type == "path":
        return Image.open(Path(image.data).expanduser()).convert("RGB")
    raise ValueError(f"Cannot convert image type to PIL: {image.type}")


def _looks_like_pil(value: Any) -> bool:
    return hasattr(value, "save") and hasattr(value, "mode")
