"""Application builders for config-driven online robot runs."""

from __future__ import annotations

from dataclasses import dataclass

from dualsystem_agentic.config import (
    AppConfig,
    build_dataloader,
    build_executor,
    build_interaction,
    build_mcp_client,
    build_run_logger,
    build_vlm,
)
from dualsystem_agentic.core.loop import AgenticRobotLoop
from dualsystem_agentic.core.types import ImageInput
from dualsystem_agentic.io.dataloader import DataLoader, StaticDataLoader
from dualsystem_agentic.mcp.base import MCPToolClient
from dualsystem_agentic.runtime import OnlineAgentRuntime


@dataclass
class OnlineRobotApp:
    """Constructed components for one online agentic robot process."""

    config: AppConfig
    loop: AgenticRobotLoop
    runtime: OnlineAgentRuntime
    mcp_client: MCPToolClient
    dataloader: DataLoader | None = None

    def serve_forever(self):
        """Run the online lifecycle and close resources on exit."""
        try:
            return self.runtime.serve_forever()
        finally:
            self.close()

    def close(self) -> None:
        """Close best-effort resources owned by the app."""
        close = getattr(self.mcp_client, "close", None)
        if callable(close):
            close()


def build_online_robot_app(
    config: AppConfig,
    *,
    static_images: dict[str, ImageInput] | None = None,
    max_steps: int | None = None,
    interaction=None,
    logger=None,
) -> OnlineRobotApp:
    """Build the full online robot application from config.

    CLI commands and robot-specific example scripts should call this function
    instead of re-implementing component wiring.
    """
    mcp_client = build_mcp_client(config.mcp)
    dataloader = build_configured_dataloader(config, static_images=static_images)
    loop = AgenticRobotLoop(
        planner=build_vlm(config.vlm),
        tool_client=mcp_client,
        executor=build_executor(config.executor),
        monitor_tool_name=config.loop.monitor_tool_name,
        execute_tool_name=config.loop.execute_tool_name,
        fetch_env_tool_name=config.loop.fetch_env_tool_name,
        dataloader=dataloader,
        include_metadata_in_prompt=config.loop.include_metadata_in_prompt,
    )
    runtime = OnlineAgentRuntime(
        loop,
        interaction=interaction or build_interaction(config.interaction),
        logger=logger or build_run_logger(config.logging),
        max_steps=max_steps if max_steps is not None else config.loop.max_steps,
        reason_interval_s=config.loop.reason_interval_s,
        monitor_poll_interval_s=config.loop.monitor_poll_interval_s,
        max_monitor_polls=config.loop.max_monitor_polls,
    )
    return OnlineRobotApp(
        config=config,
        loop=loop,
        runtime=runtime,
        mcp_client=mcp_client,
        dataloader=dataloader,
    )


def build_agentic_robot_loop_app(
    config: AppConfig,
    *,
    static_images: dict[str, ImageInput] | None = None,
) -> tuple[AgenticRobotLoop, MCPToolClient]:
    """Build a one-shot loop and its closeable MCP client from config."""
    mcp_client = build_mcp_client(config.mcp)
    dataloader = build_configured_dataloader(config, static_images=static_images)
    loop = AgenticRobotLoop(
        planner=build_vlm(config.vlm),
        tool_client=mcp_client,
        executor=build_executor(config.executor),
        monitor_tool_name=config.loop.monitor_tool_name,
        execute_tool_name=config.loop.execute_tool_name,
        fetch_env_tool_name=config.loop.fetch_env_tool_name,
        dataloader=dataloader,
        include_metadata_in_prompt=config.loop.include_metadata_in_prompt,
    )
    return loop, mcp_client


def build_configured_dataloader(
    config: AppConfig,
    *,
    static_images: dict[str, ImageInput] | None = None,
) -> DataLoader | None:
    """Build the configured DataLoader, letting explicit static images win."""
    dataloader = build_dataloader(config.dataloader, static_images=static_images or None)
    if dataloader is None and static_images:
        return StaticDataLoader(static_images)
    return dataloader
