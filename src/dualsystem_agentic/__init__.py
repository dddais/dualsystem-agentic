"""Agentic robot Dual-System framework: VLM planner + MCP tools + VLA executor."""

from __future__ import annotations

from dualsystem_agentic.app import OnlineRobotApp, build_online_robot_app
from dualsystem_agentic.core.loop import AgenticRobotLoop
from dualsystem_agentic.core.parser import parse_agentic_planner_output
from dualsystem_agentic.core.prompts import build_agentic_prompt
from dualsystem_agentic.core.types import (
    ActiveExecution,
    AgenticEvent,
    AgenticPhase,
    AgenticPlannerInput,
    AgenticPlannerOutput,
    AgenticSessionState,
    AgenticStepResult,
    ExecutorInput,
    ExecutorOutput,
    ImageInput,
    MonitorStatus,
    ToolCall,
    ToolResult,
)
from dualsystem_agentic.executor.base import ExecutorClient
from dualsystem_agentic.interaction import (
    ConsoleInteractionLayer,
    InteractionLayer,
    OnlineTaskSummary,
    TuiInteractionLayer,
)
from dualsystem_agentic.mcp.base import MCPToolClient
from dualsystem_agentic.mcp.fake import FakeMCPToolClient
from dualsystem_agentic.run_logger import JsonlRunLogger, NullRunLogger, RunLogger
from dualsystem_agentic.runtime import OnlineAgentRuntime
from dualsystem_agentic.vlm.base import CallablePlanner, VLMPlanner

__all__ = [
    "ActiveExecution",
    "AgenticEvent",
    "AgenticPhase",
    "AgenticRobotLoop",
    "OnlineRobotApp",
    "AgenticPlannerInput",
    "AgenticPlannerOutput",
    "AgenticSessionState",
    "AgenticStepResult",
    "ExecutorClient",
    "ExecutorInput",
    "ExecutorOutput",
    "InteractionLayer",
    "ConsoleInteractionLayer",
    "TuiInteractionLayer",
    "OnlineTaskSummary",
    "ImageInput",
    "MonitorStatus",
    "MCPToolClient",
    "FakeMCPToolClient",
    "OnlineAgentRuntime",
    "RunLogger",
    "JsonlRunLogger",
    "NullRunLogger",
    "ToolCall",
    "ToolResult",
    "VLMPlanner",
    "CallablePlanner",
    "build_online_robot_app",
    "build_agentic_prompt",
    "parse_agentic_planner_output",
]
