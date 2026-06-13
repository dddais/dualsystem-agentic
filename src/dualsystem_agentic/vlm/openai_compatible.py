"""OpenAI-compatible multimodal chat VLM planner (commercial / self-hosted APIs)."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from dualsystem_agentic.core.prompts import build_agentic_prompt
from dualsystem_agentic.core.types import AgenticPlannerInput, ImageInput
from dualsystem_agentic.io.image import image_to_openai_content


class OpenAICompatibleVLMPlanner:
    """Call an OpenAI-compatible chat completions endpoint as a planner."""

    def __init__(
        self,
        model: str,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 60.0,
        default_sampling_params: dict[str, Any] | None = None,
    ) -> None:
        self.model = model
        self.base_url = base_url or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.timeout = timeout
        self.default_sampling_params = default_sampling_params or {}

    def generate(self, planner_input: AgenticPlannerInput) -> str:
        payload = self._build_payload(planner_input)
        return self._request_content(payload)

    def generate_text(
        self,
        prompt: str,
        *,
        images: dict[str, ImageInput] | None = None,
        sampling_params: dict[str, Any] | None = None,
    ) -> str:
        payload = self._build_text_payload(
            prompt,
            images or {},
            sampling_params=sampling_params,
        )
        return self._request_content(payload)

    def _request_content(self, payload: dict[str, Any]) -> str:
        request = urllib.request.Request(
            self._chat_url(),
            data=json.dumps(payload).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response_data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"VLM request failed: HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"VLM request failed: {exc}") from exc
        return self._extract_content(response_data)

    def _build_payload(self, planner_input: AgenticPlannerInput) -> dict[str, Any]:
        return self._build_text_payload(
            build_agentic_prompt(planner_input),
            planner_input.images,
        )

    def _build_text_payload(
        self,
        prompt: str,
        images: dict[str, ImageInput],
        *,
        sampling_params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        content: list[dict[str, Any]] = []
        for image in images.values():
            content.append(image_to_openai_content(image))
        content.append({"type": "text", "text": prompt})
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
        }
        payload.update(_normalize_sampling_params(self.default_sampling_params))
        if sampling_params:
            payload.update(_normalize_sampling_params(sampling_params))
        return payload

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _chat_url(self) -> str:
        base_url = self.base_url.rstrip("/")
        if base_url.endswith("/chat/completions"):
            return base_url
        return f"{base_url}/chat/completions"

    @staticmethod
    def _extract_content(response_data: dict[str, Any]) -> str:
        choices = response_data.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        content = message.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
            return "".join(parts)
        return str(content)


def _normalize_sampling_params(params: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    if params.get("temperature") is not None:
        normalized["temperature"] = params["temperature"]
    if params.get("top_p") is not None:
        normalized["top_p"] = params["top_p"]
    if params.get("max_tokens") is not None:
        normalized["max_tokens"] = params["max_tokens"]
    elif params.get("max_new_tokens") is not None:
        normalized["max_tokens"] = params["max_new_tokens"]
    if params.get("stop") is not None:
        normalized["stop"] = params["stop"]
    if params.get("seed") is not None:
        normalized["seed"] = params["seed"]
    return normalized
