"""Tests for the dual_franka HTTP deployment adapter."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from dualsystem_agentic.io.dataloader import HTTPDataLoader


def _load_dual_franka_server_module():
    try:
        import mcp  # noqa: F401
    except ImportError:
        pytest.skip("mcp SDK is not installed")
    path = Path(__file__).resolve().parents[1] / "mcp_server" / "dual_franka_mcp_server" / "server.py"
    spec = importlib.util.spec_from_file_location("dual_franka_mcp_server_for_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dual_franka_monitor_status_normalization():
    server = _load_dual_franka_server_module()

    assert server._derive_monitor_status({"state": "executing"}) == "running"
    assert server._derive_monitor_status({"task_status": "completed"}) == "success"
    assert server._derive_monitor_status({"error": "collision"}) == "failed"
    assert server._derive_monitor_status({"current_step": 2, "total_steps": 5}) == "running"
    assert server._derive_monitor_status({"current_step": 5, "total_steps": 5}) == "success"


def test_dual_franka_execute_payload_allows_bridge_specific_overrides():
    server = _load_dual_franka_server_module()

    payload = server._build_execute_payload(
        {
            "subtask": "pick up the cube",
            "task": "clean the table",
            "left_arm": "stabilize tray",
            "payload": {"priority": "high", "prompt": "custom prompt"},
        }
    )

    assert payload["subtask"] == "pick up the cube"
    assert payload["instruction"] == "pick up the cube"
    assert payload["task"] == "clean the table"
    assert payload["left_arm"] == "stabilize tray"
    assert payload["priority"] == "high"
    assert payload["prompt"] == "custom prompt"


def test_http_dataloader_accepts_wrapped_bridge_image_response():
    dataloader = HTTPDataLoader(url="http://unused", image_key="concatenated_image", label="main")
    frame = dataloader._parse_response(
        {
            "success": True,
            "data": {
                "concatenated_image": "x" * 120,
                "timestamp": 123.0,
            },
        }
    )

    assert frame is not None
    assert frame.images["main"].data == "x" * 120
    assert frame.timestamp == 123.0
