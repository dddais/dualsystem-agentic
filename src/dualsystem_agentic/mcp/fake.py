"""In-process fake MCP tool client for tests and offline demos."""

from __future__ import annotations

from typing import Callable

from dualsystem_agentic.core.types import JsonDict, ToolResult
from dualsystem_agentic.mcp.registry import ToolRegistry

ToolFn = Callable[[JsonDict], object]


class FakeMCPToolClient:
    """Register and call tools in-process, grouped by namespace.

    Mirrors the routing behaviour of the real ``MCPServiceManager`` without any
    transport, so the full loop can be exercised in tests.
    """

    def __init__(self) -> None:
        self._tools: dict[str, dict[str, ToolFn]] = {}
        self._registry = ToolRegistry()

    def register(
        self,
        name: str,
        fn: ToolFn,
        *,
        namespace: str = "default",
        description: str = "",
        parameters: JsonDict | None = None,
        service_description: str = "",
        active: bool = True,
    ) -> None:
        self._tools.setdefault(namespace, {})[name] = fn
        self._registry.register_service(
            namespace,
            description=service_description,
            active=active,
        )
        self._registry.register_tool(
            namespace=namespace,
            name=name,
            description=description,
            parameters=parameters,
            active=active,
        )

    def list_tools(self) -> list[JsonDict]:
        return self._registry.list_tools(active_only=True)

    def call_tool(
        self,
        name: str,
        arguments: JsonDict | None = None,
        *,
        namespace: str | None = None,
        call_id: str | None = None,
    ) -> ToolResult:
        resolved = self._registry.resolve(name, namespace, active_only=True)
        if resolved is None:
            error = self._registry.resolve_error(name, namespace)
            return ToolResult.failure(name, error, namespace=namespace, call_id=call_id)
        fn = self._tools[resolved.namespace][resolved.name]
        try:
            data = fn(arguments or {})
        except Exception as exc:  # noqa: BLE001 - surface tool errors as structured results
            return ToolResult.failure(resolved.name, str(exc), namespace=resolved.namespace, call_id=call_id)
        return ToolResult.success(resolved.name, data, namespace=resolved.namespace, call_id=call_id)
