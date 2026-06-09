"""Namespace-aware MCP tool client protocol."""

from __future__ import annotations

from typing import Protocol

from dualsystem_agentic.core.types import JsonDict, ToolResult


class MCPToolClient(Protocol):
    """Protocol for MCP-compatible tool adapters.

    A client may aggregate several MCP servers; each server is addressed by a
    ``namespace`` (one robot / capability set per namespace). Tools are routed by
    ``(namespace, name)``.
    """

    def list_tools(self) -> list[JsonDict]:
        """Return available tools as ``{"namespace", "name", "description"}`` dicts."""

    def call_tool(
        self,
        name: str,
        arguments: JsonDict | None = None,
        *,
        namespace: str | None = None,
        call_id: str | None = None,
    ) -> ToolResult:
        """Call one named tool on the given namespace and return a structured result."""
