"""YAML/JSON configuration loading and component factories."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dualsystem_agentic.core.types import JsonDict, ensure_jsonable
from dualsystem_agentic.executor.base import ExecutorClient
from dualsystem_agentic.mcp.base import MCPToolClient
from dualsystem_agentic.mcp.connection import MCPServerConfig
from dualsystem_agentic.vlm.base import VLMPlanner


@dataclass
class VLMConfig:
    provider: str = "openai_compatible"  # openai_compatible | local_qwen | scripted
    model: str | None = None
    model_path: str | None = None
    model_family: str = "auto"
    base_url: str | None = None
    api_key: str | None = None
    timeout: float = 60.0
    dtype: str = "bf16"
    device: str = "auto"
    min_pixels: int = 256 * 28 * 28
    max_pixels: int = 1280 * 28 * 28
    sampling_params: JsonDict = field(default_factory=dict)
    script: list[Any] = field(default_factory=list)
    script_file: str | None = None
    repeat_last: bool = False
    reset_on_new_task: bool = True


@dataclass
class ExecutorConfig:
    provider: str = "http"  # http
    endpoint: str | None = None
    timeout: float = 30.0


@dataclass
class MCPConfig:
    provider: str = "sdk"  # sdk | fake
    servers: list[JsonDict] = field(default_factory=list)
    servers_file: str | None = None
    tools: list[JsonDict] = field(default_factory=list)


@dataclass
class LoopConfig:
    max_steps: int = 20
    monitor_tool_name: str = "monitor"
    execute_tool_name: str = "execute"
    fetch_env_tool_name: str = "fetch_env"
    tool_roles: JsonDict = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Allow optional role mapping without requiring repeated defaults."""
        monitor = self.tool_roles.get("monitor")
        execute = self.tool_roles.get("execute")
        fetch_env = self.tool_roles.get("fetch_env")
        if monitor:
            self.monitor_tool_name = str(monitor)
        if execute:
            self.execute_tool_name = str(execute)
        if fetch_env:
            self.fetch_env_tool_name = str(fetch_env)


@dataclass
class DataLoaderConfig:
    provider: str = "static"  # static | http | mock | none
    url: str | None = None
    timeout: float = 10.0
    image_key: str = "concatenated_image"
    label: str = "main"


@dataclass
class InteractionConfig:
    provider: str = "console"  # console | tui
    show_raw_json: bool = False
    prompt: str = "task> "
    max_log_lines: int = 1000


@dataclass
class LoggingConfig:
    enabled: bool = False
    root_dir: str = "runs"
    save_images: bool = True


@dataclass
class AppConfig:
    vlm: VLMConfig = field(default_factory=VLMConfig)
    executor: ExecutorConfig = field(default_factory=ExecutorConfig)
    mcp: MCPConfig = field(default_factory=MCPConfig)
    loop: LoopConfig = field(default_factory=LoopConfig)
    dataloader: DataLoaderConfig = field(default_factory=DataLoaderConfig)
    interaction: InteractionConfig = field(default_factory=InteractionConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    @classmethod
    def from_dict(cls, data: JsonDict) -> "AppConfig":
        return cls(
            vlm=VLMConfig(**_expand_env(data.get("vlm") or {})),
            executor=ExecutorConfig(**_expand_env(data.get("executor") or {})),
            mcp=MCPConfig(**_expand_env(data.get("mcp") or {})),
            loop=LoopConfig(**(data.get("loop") or {})),
            dataloader=DataLoaderConfig(**_expand_env(data.get("dataloader") or {})),
            interaction=InteractionConfig(**(data.get("interaction") or {})),
            logging=LoggingConfig(**_expand_env(data.get("logging") or {})),
        )


def load_config(path: str | Path) -> AppConfig:
    """Load an ``AppConfig`` from a YAML or JSON file."""
    text = Path(path).expanduser().read_text(encoding="utf-8")
    if str(path).endswith((".yaml", ".yml")):
        import yaml

        data = yaml.safe_load(text) or {}
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("Config root must be a mapping")
    return AppConfig.from_dict(data)


def build_vlm(config: VLMConfig) -> VLMPlanner:
    provider = config.provider
    if provider == "scripted":
        from dualsystem_agentic.vlm.scripted import ScriptedVLMPlanner

        outputs = list(config.script)
        if config.script_file:
            outputs.extend(_load_script_file(config.script_file))
        return ScriptedVLMPlanner(
            outputs,
            repeat_last=config.repeat_last,
            reset_on_new_task=config.reset_on_new_task,
        )
    if provider == "openai_compatible":
        from dualsystem_agentic.vlm.openai_compatible import OpenAICompatibleVLMPlanner

        if not config.model:
            raise ValueError("vlm.model is required for the openai_compatible provider")
        return OpenAICompatibleVLMPlanner(
            model=config.model,
            base_url=config.base_url,
            api_key=config.api_key,
            timeout=config.timeout,
            default_sampling_params=config.sampling_params,
        )
    if provider == "local_qwen":
        from dualsystem_agentic.vlm.local_qwen import LocalQwenVLMPlanner

        if not config.model_path:
            raise ValueError("vlm.model_path is required for the local_qwen provider")
        return LocalQwenVLMPlanner(
            model_path=config.model_path,
            model_family=config.model_family,
            dtype=config.dtype,
            device=config.device,
            min_pixels=config.min_pixels,
            max_pixels=config.max_pixels,
            default_sampling_params=config.sampling_params,
        )
    raise ValueError(f"Unsupported VLM provider: {provider}")


def build_executor(config: ExecutorConfig) -> ExecutorClient:
    if config.provider in ("noop", "none"):
        from dualsystem_agentic.executor.base import NoopExecutorClient

        return NoopExecutorClient()
    if config.provider == "http":
        if not config.endpoint:
            raise ValueError("executor.endpoint is required for the http provider")
        from dualsystem_agentic.executor.http_executor import HTTPExecutorClient

        return HTTPExecutorClient(endpoint=config.endpoint, timeout=config.timeout)
    raise ValueError(f"Unsupported executor provider: {config.provider}")


def build_mcp_client(config: MCPConfig) -> MCPToolClient:
    if config.provider == "fake":
        from dualsystem_agentic.mcp.fake import FakeMCPToolClient

        client = FakeMCPToolClient()
        for tool in config.tools:
            _register_fake_tool(client, tool)
        return client
    if config.provider == "sdk":
        from dualsystem_agentic.mcp.manager import MCPServiceManager

        servers = list(config.servers)
        if config.servers_file:
            servers.extend(_load_servers_file(config.servers_file))
        configs = [MCPServerConfig.from_dict(server) for server in servers]
        return MCPServiceManager(configs).start()
    raise ValueError(f"Unsupported MCP provider: {config.provider}")


def build_dataloader(
    config: DataLoaderConfig,
    static_images: dict | None = None,
) -> "DataLoader | None":
    """Build a DataLoader from config. ``static_images`` is used when
    ``provider == "static"`` (i.e. CLI ``--image``)."""
    from dualsystem_agentic.io.dataloader import (
        HTTPDataLoader,
        MockDataLoader,
        StaticDataLoader,
    )

    if config.provider == "none":
        return None
    if config.provider == "static":
        if not static_images:
            return None
        return StaticDataLoader(static_images)
    if config.provider == "http":
        if not config.url:
            raise ValueError("dataloader.url is required for the http provider")
        return HTTPDataLoader(
            url=config.url,
            timeout=config.timeout,
            image_key=config.image_key,
            label=config.label,
        )
    if config.provider == "mock":
        return MockDataLoader()
    raise ValueError(f"Unsupported dataloader provider: {config.provider}")


def build_interaction(config: InteractionConfig) -> "InteractionLayer":
    if config.provider == "console":
        from dualsystem_agentic.interaction import ConsoleInteractionLayer

        return ConsoleInteractionLayer(
            show_raw_json=config.show_raw_json,
            prompt=config.prompt,
        )
    if config.provider == "tui":
        from dualsystem_agentic.interaction import TuiInteractionLayer

        return TuiInteractionLayer(
            show_raw_json=config.show_raw_json,
            prompt=config.prompt,
            max_log_lines=config.max_log_lines,
        )
    raise ValueError(f"Unsupported interaction provider: {config.provider}")


def build_run_logger(config: LoggingConfig) -> "RunLogger":
    if not config.enabled:
        from dualsystem_agentic.run_logger import NullRunLogger

        return NullRunLogger()
    from dualsystem_agentic.run_logger import JsonlRunLogger

    return JsonlRunLogger(root_dir=config.root_dir, save_images=config.save_images)


def _load_servers_file(path: str) -> list[JsonDict]:
    data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("servers", [])
    if not isinstance(data, list):
        raise ValueError("MCP servers file must be a list or {'servers': [...]}")
    return data


def _load_script_file(path: str) -> list[Any]:
    text = Path(path).expanduser().read_text(encoding="utf-8")
    if path.endswith((".yaml", ".yml")):
        import yaml

        data = yaml.safe_load(text) or []
    else:
        data = json.loads(text)
    if isinstance(data, dict):
        data = data.get("script", [])
    if not isinstance(data, list):
        raise ValueError("VLM script file must be a list or {'script': [...]}")
    return data


def _expand_env(data: dict[str, Any]) -> dict[str, Any]:
    return {key: _expand_env_value(value) for key, value in data.items()}


def _expand_env_value(value: Any) -> Any:
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, list):
        return [_expand_env_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_env_value(item) for key, item in value.items()}
    return value


def _register_fake_tool(client: Any, spec: JsonDict) -> None:
    name = str(spec.get("name") or "")
    if not name:
        raise ValueError("fake MCP tool config requires a name")
    namespace = str(spec.get("namespace") or "default")
    description = str(spec.get("description") or "")
    parameters = _json_dict(spec.get("parameters") or {})
    echo_args = bool(spec.get("echo_args", False))
    repeat_last = bool(spec.get("repeat_last", True))
    cycle_results = bool(spec.get("cycle_results", False))
    sequence = spec.get("results")
    state = {"index": 0}

    def fake_tool(arguments: JsonDict) -> JsonDict:
        if isinstance(sequence, list):
            if state["index"] >= len(sequence):
                if cycle_results and sequence:
                    state["index"] = 0
                elif not repeat_last:
                    raise RuntimeError(f"fake MCP tool '{name}' results exhausted")
                else:
                    payload = sequence[-1] if sequence else {}
                    data = _json_dict(payload)
                    if echo_args:
                        data = {**data, **(arguments or {})}
                    return data
            payload = sequence[state["index"]] if sequence else {}
            state["index"] += 1
        else:
            payload = spec.get("result") or {}

        data = _json_dict(payload)
        if echo_args:
            data = {**data, **(arguments or {})}
        return data

    client.register(
        name,
        fake_tool,
        namespace=namespace,
        description=description,
        parameters=parameters,
    )


def _json_dict(value: Any) -> JsonDict:
    converted = ensure_jsonable(value)
    if not isinstance(converted, dict):
        raise TypeError("Configured payload must be a JSON object")
    return converted
