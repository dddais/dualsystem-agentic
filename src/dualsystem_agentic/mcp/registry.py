"""Lightweight service/tool registry for MCP-backed robot capabilities."""

from __future__ import annotations

from dataclasses import dataclass, field

from dualsystem_agentic.core.tool_names import make_canonical_tool_name, split_qualified_tool_name
from dualsystem_agentic.core.types import JsonDict, ensure_jsonable


@dataclass(frozen=True)
class ToolIdentifier:
    """Resolved internal tool route."""

    namespace: str
    name: str


@dataclass
class ToolSpec:
    """One MCP tool exposed by one service namespace."""

    namespace: str
    name: str
    description: str = ""
    parameters: JsonDict = field(default_factory=dict)
    active: bool = True

    @property
    def canonical_name(self) -> str:
        return make_canonical_tool_name(self.name, self.namespace)

    def to_dict(self, *, service_description: str = "") -> JsonDict:
        return ensure_jsonable(
            {
                "namespace": self.namespace,
                "name": self.name,
                "canonical_name": self.canonical_name,
                "description": self.description,
                "parameters": self.parameters,
                "active": self.active,
                "service_description": service_description,
            }
        )  # type: ignore[return-value]


@dataclass
class ServiceSpec:
    """One MCP service namespace and its discovered tools."""

    namespace: str
    description: str = ""
    active: bool = True
    tools: dict[str, ToolSpec] = field(default_factory=dict)

    def to_dict(self, *, include_tools: bool = False) -> JsonDict:
        payload: dict[str, object] = {
            "namespace": self.namespace,
            "description": self.description,
            "active": self.active,
        }
        if include_tools:
            payload["tools"] = [
                tool.to_dict(service_description=self.description) for tool in self.tools.values()
            ]
        return ensure_jsonable(payload)  # type: ignore[return-value]


class ToolRegistry:
    """Registry that exposes canonical names while routing by namespace/name."""

    def __init__(self) -> None:
        self._services: dict[str, ServiceSpec] = {}

    def register_service(self, namespace: str, *, description: str = "", active: bool = True) -> None:
        service = self._services.get(namespace)
        if service is None:
            self._services[namespace] = ServiceSpec(
                namespace=namespace,
                description=description,
                active=active,
            )
            return
        if description:
            service.description = description
        service.active = active

    def register_tool(
        self,
        *,
        namespace: str,
        name: str,
        description: str = "",
        parameters: JsonDict | None = None,
        active: bool = True,
    ) -> None:
        if namespace not in self._services:
            self.register_service(namespace)
        self._services[namespace].tools[name] = ToolSpec(
            namespace=namespace,
            name=name,
            description=description,
            parameters=parameters or {},
            active=active,
        )

    def list_services(self, *, include_tools: bool = False, active_only: bool = False) -> list[JsonDict]:
        return [
            service.to_dict(include_tools=include_tools)
            for service in self._services.values()
            if not active_only or service.active
        ]

    def list_tools(self, *, active_only: bool = True) -> list[JsonDict]:
        tools: list[JsonDict] = []
        for service in self._services.values():
            if active_only and not service.active:
                continue
            for tool in service.tools.values():
                if active_only and not tool.active:
                    continue
                tools.append(tool.to_dict(service_description=service.description))
        return tools

    def resolve(
        self,
        name: str,
        namespace: str | None = None,
        *,
        active_only: bool = False,
    ) -> ToolIdentifier | None:
        qualified_namespace, tool_name = split_qualified_tool_name(name)
        if qualified_namespace:
            if namespace is not None and namespace != qualified_namespace:
                return None
            namespace = qualified_namespace

        if namespace is not None:
            return self._resolve_in_namespace(namespace, tool_name, active_only=active_only)

        matches = [
            ToolIdentifier(service.namespace, tool.name)
            for service in self._services.values()
            if not active_only or service.active
            for tool in service.tools.values()
            if tool.name == tool_name and (not active_only or tool.active)
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    def resolve_error(self, name: str, namespace: str | None = None) -> str:
        qualified_namespace, tool_name = split_qualified_tool_name(name)
        if qualified_namespace and namespace is not None and namespace != qualified_namespace:
            return (
                f"tool namespace mismatch: name {name!r} points to "
                f"{qualified_namespace!r}, but namespace={namespace!r}"
            )
        namespace = namespace or qualified_namespace
        if namespace is not None:
            if namespace not in self._services:
                return f"unknown tool namespace: {namespace}"
            if tool_name not in self._services[namespace].tools:
                return f"unknown tool in namespace {namespace!r}: {tool_name}"
            return f"tool is not available: {make_canonical_tool_name(tool_name, namespace)}"

        matches = [
            service.namespace
            for service in self._services.values()
            if tool_name in service.tools
        ]
        if len(matches) > 1:
            choices = ", ".join(make_canonical_tool_name(tool_name, item) for item in matches)
            return f"tool {tool_name!r} exists on multiple services; use one of: {choices}"
        return f"unknown tool: {tool_name}"

    def _resolve_in_namespace(
        self,
        namespace: str,
        name: str,
        *,
        active_only: bool,
    ) -> ToolIdentifier | None:
        service = self._services.get(namespace)
        if service is None or (active_only and not service.active):
            return None
        tool = service.tools.get(name)
        if tool is None or (active_only and not tool.active):
            return None
        return ToolIdentifier(namespace, name)
