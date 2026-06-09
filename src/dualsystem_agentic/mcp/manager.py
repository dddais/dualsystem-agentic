"""MCP service manager: namespace routing over a background asyncio loop.

Mirrors RoboClaw's ``ORMCPServiceManager``: it owns one connection per namespace
(one robot / MCP server), builds a tool routing table, and exposes a synchronous
``MCPToolClient`` API backed by a dedicated event-loop thread.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import Future
from typing import Iterable

from dualsystem_agentic.core.types import JsonDict, ToolResult
from dualsystem_agentic.mcp.connection import MCPServerConfig, MCPServerConnection
from dualsystem_agentic.mcp.registry import ToolRegistry


class MCPServiceManager:
    """Connect to one or more MCP servers and route tool calls by namespace."""

    def __init__(self, server_configs: Iterable[MCPServerConfig | JsonDict], *, ready_timeout: float = 30.0) -> None:
        self._configs = [_as_config(item) for item in server_configs]
        self._ready_timeout = ready_timeout
        self._connections: dict[str, MCPServerConnection] = {}
        self._registry = ToolRegistry()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="mcp-loop", daemon=True)
        self._serve_futures: list[Future] = []
        self._started = False

    def start(self) -> "MCPServiceManager":
        if self._started:
            return self
        self._thread.start()
        for config in self._configs:
            self._connect(config)
        self._build_routes()
        self._started = True
        return self

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
        connection = self._connections[resolved.namespace]
        future = asyncio.run_coroutine_threadsafe(
            connection.call(resolved.name, arguments, call_id=call_id), self._loop
        )
        try:
            return future.result()
        except Exception as exc:  # noqa: BLE001 - surface transport errors as results
            return ToolResult.failure(resolved.name, str(exc), namespace=resolved.namespace, call_id=call_id)

    def close(self) -> None:
        if not self._started:
            return
        for connection in self._connections.values():
            self._loop.call_soon_threadsafe(connection.stop)
        for future in self._serve_futures:
            try:
                future.result(timeout=self._ready_timeout)
            except Exception:  # noqa: BLE001 - shutdown best-effort
                pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=self._ready_timeout)
        self._started = False

    def __enter__(self) -> "MCPServiceManager":
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _connect(self, config: MCPServerConfig) -> None:
        connection = MCPServerConnection(config)
        ready_event = _make_event(self._loop)
        future = asyncio.run_coroutine_threadsafe(connection.serve(ready_event), self._loop)
        self._serve_futures.append(future)
        asyncio.run_coroutine_threadsafe(
            _wait_event(ready_event), self._loop
        ).result(timeout=self._ready_timeout)
        if connection.startup_error is not None:
            raise RuntimeError(
                f"Failed to start MCP server '{config.namespace}': {connection.startup_error}"
            ) from connection.startup_error
        self._connections[config.namespace] = connection

    def _build_routes(self) -> None:
        for connection in self._connections.values():
            self._registry.register_service(
                connection.namespace,
                description=connection.config.description,
                active=connection.config.active,
            )
            for tool in connection.tools:
                self._registry.register_tool(
                    namespace=connection.namespace,
                    name=str(tool["name"]),
                    description=str(tool.get("description") or ""),
                    parameters=tool.get("parameters") if isinstance(tool.get("parameters"), dict) else {},
                    active=connection.config.active,
                )


def _as_config(item: MCPServerConfig | JsonDict) -> MCPServerConfig:
    if isinstance(item, MCPServerConfig):
        return item
    return MCPServerConfig.from_dict(item)


def _make_event(loop: asyncio.AbstractEventLoop) -> asyncio.Event:
    future: Future = Future()
    loop.call_soon_threadsafe(lambda: future.set_result(asyncio.Event()))
    return future.result()


async def _wait_event(event: asyncio.Event) -> None:
    await event.wait()
