"""Tests for the dual_franka HTTP deployment adapter."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from dualsystem_agentic.io.dataloader import HTTPDataLoader


def _load_module(relative_path: str, name: str):
    path = Path(__file__).resolve().parents[1] / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_dual_franka_server_module():
    try:
        import mcp  # noqa: F401
    except ImportError:
        pytest.skip("mcp SDK is not installed")
    return _load_module(
        "mcp_server/dual_franka_mcp_server/server.py",
        "dual_franka_mcp_server_for_test",
    )


def _load_dual_franka_bridge_module():
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError:
        pytest.skip("fastapi/uvicorn is not installed")
    return _load_module(
        "mcp_server/dual_franka_mcp_server/dual_franka_bridge.py",
        "dual_franka_bridge_for_test",
    )


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


def test_dual_franka_bridge_reads_local_camera_files(tmp_path):
    bridge = _load_dual_franka_bridge_module()
    bridge.image_dir = tmp_path
    for filename in bridge.CAMERA_FILES.values():
        (tmp_path / filename).write_bytes(b"fake-jpeg")

    images, missing = bridge._read_camera_images()

    assert missing == []
    assert set(images) == {"cam_high", "cam_left_wrist", "cam_right_wrist"}


def test_dual_franka_bridge_writes_subtask_atomically(tmp_path):
    bridge = _load_dual_franka_bridge_module()
    bridge.subtask_file = tmp_path / "subtask.txt"

    bridge._write_subtask_file("pick up the cube")
    bridge._write_subtask_file("place the cube")

    assert bridge.subtask_file.read_text(encoding="utf-8") == "place the cube\n"


def test_dual_franka_bridge_reads_monitor_result_json_and_text(tmp_path):
    bridge = _load_dual_franka_bridge_module()
    bridge.monitor_result_file = tmp_path / "monitor_result.txt"

    bridge.monitor_result_file.write_text('{"status": "success", "score": 1.0}', encoding="utf-8")
    assert bridge._read_monitor_result()["status"] == "success"

    bridge.monitor_result_file.write_text("failed", encoding="utf-8")
    assert bridge._read_monitor_result()["status"] == "failed"
