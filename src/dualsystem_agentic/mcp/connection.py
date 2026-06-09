"""Single MCP server connection (one robot / capability set per namespace).

Mirrors the connection layer of RoboClaw's ``ORMCPServiceConnection`` but stays
transport-agnostic (stdio or SSE) and converts results into ``ToolResult``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from dualsystem_agentic.core.types import JsonDict, ToolResult


@dataclass
class MCPServerConfig:
    """Connection config for one MCP server, keyed by ``namespace``."""

    namespace: str
    description: str = ""
    active: bool = True
    transport: str = "stdio"
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None

    @classmethod
    def from_dict(cls, data: JsonDict) -> "MCPServerConfig":
        namespace = data.get("namespace") or data.get("name")
        if not namespace:
            raise ValueError("MCP server config must define a 'namespace'")
        return cls(
            namespace=str(namespace),
            description=str(data.get("description") or ""),
            active=bool(data.get("active", True)),
            transport=str(data.get("transport") or "stdio").lower(),
            command=data.get("command"),  # type: ignore[arg-type]
            args=[str(item) for item in data.get("args", [])],
            env={str(k): str(v) for k, v in (data.get("env") or {}).items()},
            url=data.get("url"),  # type: ignore[arg-type]
        )


class MCPServerConnection:
    """Own one MCP ``ClientSession`` for the lifetime of a background loop task."""

    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self.session = None
        self.tools: list[JsonDict] = []
        self._stop_event: asyncio.Event | None = None
        self._error: BaseException | None = None

    @property
    def namespace(self) -> str:
        return self.config.namespace

    async def serve(self, ready_event: asyncio.Event) -> None:
        """Open the session, list tools, signal readiness, then wait for shutdown.

        Connecting and disconnecting happen in this single task so anyio task
        scopes (used by the stdio/SSE clients) stay consistent.
        """
        self._stop_event = asyncio.Event()
        try:
            async with self._client_streams() as (read, write):
                from mcp import ClientSession

                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self.session = session
                    self.tools = await self._list_tools(session)
                    ready_event.set()
                    await self._stop_event.wait()
        except BaseException as exc:  # noqa: BLE001 - report startup failure to caller
            self._error = exc
            ready_event.set()
            raise

    def _client_streams(self):
        transport = self.config.transport
        if transport == "stdio":
            from mcp import StdioServerParameters
            from mcp.client.stdio import get_default_environment, stdio_client

            if not self.config.command:
                raise ValueError(f"stdio MCP server '{self.namespace}' requires a 'command'")
            # Merge over the default environment so custom env vars don't drop PATH etc.
            env = {**get_default_environment(), **self.config.env} if self.config.env else None
            params = StdioServerParameters(
                command=self.config.command,
                args=self.config.args,
                env=env,
            )
            return stdio_client(params)
        if transport == "sse":
            from mcp.client.sse import sse_client

            if not self.config.url:
                raise ValueError(f"sse MCP server '{self.namespace}' requires a 'url'")
            return sse_client(self.config.url)
        raise ValueError(f"Unsupported MCP transport: {transport}")

    async def _list_tools(self, session) -> list[JsonDict]:
        response = await session.list_tools()
        return [
            {
                "namespace": self.namespace,
                "name": tool.name,
                "description": tool.description or "",
                "parameters": tool.inputSchema or {},
            }
            for tool in response.tools
        ]

    async def call(
        self,
        name: str,
        arguments: JsonDict | None = None,
        *,
        call_id: str | None = None,
    ) -> ToolResult:
        if self.session is None:
            return ToolResult.failure(
                name, f"MCP server '{self.namespace}' not connected", namespace=self.namespace, call_id=call_id
            )
        result = await self.session.call_tool(name, arguments or {})
        return _convert_result(result, name, self.namespace, call_id)

    def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()

    @property
    def startup_error(self) -> BaseException | None:
        return self._error


def _convert_result(result: Any, tool_name: str, namespace: str, call_id: str | None) -> ToolResult:
    text = _join_text_content(getattr(result, "content", None))
    if getattr(result, "isError", False):
        return ToolResult.failure(
            tool_name, text or "tool returned an error", namespace=namespace, call_id=call_id
        )
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        data = structured
    else:
        data = _coerce_text_to_dict(text)
    return ToolResult.success(tool_name, data, namespace=namespace, call_id=call_id)


def _join_text_content(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    parts = [getattr(item, "text", "") for item in content if getattr(item, "type", None) == "text"]
    return "\n".join(part for part in parts if part)


def _coerce_text_to_dict(text: str) -> JsonDict:
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return {"text": text}
    if isinstance(parsed, dict):
        return parsed
    return {"result": parsed}
