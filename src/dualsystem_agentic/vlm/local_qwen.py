"""Local Qwen-VL planner running through HuggingFace Transformers."""

from __future__ import annotations

import re
from typing import Any

from dualsystem_agentic.core.prompts import build_agentic_prompt
from dualsystem_agentic.core.types import AgenticPlannerInput, ImageInput
from dualsystem_agentic.io.image import image_input_to_pil

_THINK_END_TOKEN_ID = 151668


class LocalQwenVLMPlanner:
    """Run Qwen2.5-VL or Qwen3-VL locally as a high-level planner."""

    def __init__(
        self,
        model_path: str,
        model_family: str = "auto",
        dtype: str = "bf16",
        device: str = "auto",
        min_pixels: int = 256 * 28 * 28,
        max_pixels: int = 1280 * 28 * 28,
        default_sampling_params: dict[str, Any] | None = None,
    ) -> None:
        self.model_path = model_path
        self.model_family = _infer_model_family(model_path, model_family)
        self.dtype = dtype
        self.device = device
        self.min_pixels = min_pixels
        self.max_pixels = max_pixels
        self.default_sampling_params = default_sampling_params or {}
        self._model = None
        self._processor = None
        self._process_vision_info = None

    def generate(self, planner_input: AgenticPlannerInput) -> str:
        import torch

        self._ensure_loaded()
        with torch.no_grad():
            return self._generate_impl(planner_input)

    def generate_text(
        self,
        prompt: str,
        *,
        images: dict[str, ImageInput] | None = None,
        sampling_params: dict[str, Any] | None = None,
    ) -> str:
        import torch

        self._ensure_loaded()
        with torch.no_grad():
            return self._generate_message(
                self._build_text_message(prompt, images or {}),
                sampling_params=sampling_params,
            )

    def _generate_impl(self, planner_input: AgenticPlannerInput) -> str:
        return self._generate_message(self._build_message(planner_input))

    def _generate_message(
        self,
        message: dict[str, Any],
        *,
        sampling_params: dict[str, Any] | None = None,
    ) -> str:
        text = self._processor.apply_chat_template(
            [message],
            tokenize=False,
            add_generation_prompt=True,
        )
        image_inputs, video_inputs = self._process_vision_info([[message]])
        inputs = self._processor(
            text=[text],
            images=image_inputs if image_inputs else None,
            videos=video_inputs if video_inputs else None,
            padding=True,
            return_tensors="pt",
        )
        inputs = inputs.to(self._model.device)

        params = dict(self.default_sampling_params)
        if sampling_params:
            params.update(sampling_params)
        generated_ids = self._model.generate(**inputs, **params)
        input_ids = inputs.input_ids[0]
        output_ids = generated_ids[0][len(input_ids) :].tolist()
        if self.model_family == "qwen3":
            return self._strip_thinking(output_ids)
        return self._processor.decode(
            output_ids,
            skip_special_tokens=True,
            clean_up_tokenization_spaces=False,
        )

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return

        import torch
        from qwen_vl_utils import process_vision_info
        from transformers import AutoProcessor

        torch_dtype = _torch_dtype(torch, self.dtype)
        if self.model_family == "qwen3":
            from transformers import Qwen3VLForConditionalGeneration

            model_cls = Qwen3VLForConditionalGeneration
        else:
            from transformers import Qwen2_5_VLForConditionalGeneration

            model_cls = Qwen2_5_VLForConditionalGeneration

        self._model = model_cls.from_pretrained(self.model_path, torch_dtype=torch_dtype)
        self._processor = AutoProcessor.from_pretrained(
            self.model_path,
            min_pixels=self.min_pixels,
            max_pixels=self.max_pixels,
        )
        self._processor.tokenizer.padding_side = "left"
        if self.device != "auto":
            self._model.to(self.device)
        self._model.eval()
        self._process_vision_info = process_vision_info

    def _build_message(self, planner_input: AgenticPlannerInput) -> dict[str, Any]:
        return self._build_text_message(
            build_agentic_prompt(planner_input),
            planner_input.images,
        )

    def _build_text_message(
        self,
        prompt: str,
        images: dict[str, ImageInput],
    ) -> dict[str, Any]:
        content: list[dict[str, Any]] = []
        for image in images.values():
            content.append({"type": "image", "image": _to_pil(image)})
        content.append({"type": "text", "text": prompt})
        return {"role": "user", "content": content}

    def _strip_thinking(self, output_ids: list[int]) -> str:
        try:
            idx = len(output_ids) - 1 - output_ids[::-1].index(_THINK_END_TOKEN_ID)
            content_ids = output_ids[idx + 1 :]
        except ValueError:
            content_ids = output_ids
        text = self._processor.decode(
            content_ids,
            skip_special_tokens=True,
            clean_up_tokenization_spaces=False,
        ).strip()
        return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _to_pil(image: ImageInput):
    try:
        return image_input_to_pil(image)
    except ImportError as exc:
        raise RuntimeError("Pillow is required to use LocalQwenVLMPlanner") from exc


def _infer_model_family(model_path: str, model_family: str) -> str:
    if model_family != "auto":
        return model_family.lower()
    return "qwen3" if "qwen3" in model_path.lower() else "qwen2.5"


def _torch_dtype(torch_module, dtype: str):
    normalized = dtype.lower()
    if normalized in {"bf16", "bfloat16"}:
        return torch_module.bfloat16
    if normalized in {"fp16", "float16", "half"}:
        return torch_module.float16
    if normalized in {"fp32", "float32"}:
        return torch_module.float32
    raise ValueError(f"Unsupported dtype: {dtype}")
