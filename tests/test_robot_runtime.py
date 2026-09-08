"""Tests for the robot runtime service."""

from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path

import pytest

from robot_runtime.adapters.dual_franka.camera_provider import (
    CAMERA_FILES,
    DualFrankaLocalFileCameraProvider,
)
from robot_runtime.adapters.dual_franka.monitor_provider import LocalMemoryMonitorProvider
from robot_runtime.adapters.dual_franka.robot_driver import PlaceholderDualFrankaRobotDriver
from robot_runtime.core.runtime import RobotRuntime


def _runtime(tmp_path: Path, *, auto_success_after_polls: int = 0) -> RobotRuntime:
    for filename in CAMERA_FILES.values():
        (tmp_path / filename).write_bytes(b"fake-jpeg")
    return RobotRuntime(
        robot_type="dual_franka",
        robot_driver=PlaceholderDualFrankaRobotDriver(),
        camera_provider=DualFrankaLocalFileCameraProvider(tmp_path),
        monitor_provider=LocalMemoryMonitorProvider(auto_success_after_polls=auto_success_after_polls),
        safety={"max_execution_s": 300},
    )


def _load_api_module():
    try:
        import fastapi  # noqa: F401
        import yaml  # noqa: F401
    except ImportError:
        pytest.skip("fastapi/yaml is not installed")
    path = Path(__file__).resolve().parents[1] / "robot_runtime/robot_runtime/api/app.py"
    spec = importlib.util.spec_from_file_location("robot_runtime_api_for_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runtime_execution_returns_stable_execution_and_monitor_ids(tmp_path):
    runtime = _runtime(tmp_path)

    execution = runtime.create_execution(
        {
            "subtask": "pick up the cube",
            "task": "clean the table",
            "subtask_index": 2,
            "metadata": {"operator": "test"},
        }
    )

    assert execution.execution_id.startswith("exec-")
    assert execution.monitor_id.startswith("mon-")
    assert execution.status == "running"
    assert execution.subtask == "pick up the cube"
    assert execution.subtask_index == 2
    assert execution.driver_result["placeholder"] is True


def test_runtime_monitor_status_rejects_mismatched_ids(tmp_path):
    runtime = _runtime(tmp_path)
    first = runtime.create_execution({"subtask": "pick up the cube"})
    runtime.stop({"execution_id": first.execution_id})
    second = runtime.create_execution({"subtask": "place the cube"})

    with pytest.raises(ValueError):
        runtime.monitor_status(
            {
                "monitor_id": first.monitor_id,
                "execution_id": second.execution_id,
            }
        )


def test_runtime_monitor_status_can_auto_succeed_for_smoke_tests(tmp_path):
    runtime = _runtime(tmp_path, auto_success_after_polls=2)
    execution = runtime.create_execution({"subtask": "pick up the cube"})

    first = runtime.monitor_status({"monitor_id": execution.monitor_id})
    second = runtime.monitor_status({"monitor_id": execution.monitor_id})

    assert first.status == "running"
    assert second.status == "success"
    assert second.progress == 1.0


def test_runtime_monitor_status_provider_error_returns_failed_state(tmp_path):
    class FailingStatusMonitorProvider(LocalMemoryMonitorProvider):
        def status(self, monitor):
            raise RuntimeError("remote monitor is unavailable")

    for filename in CAMERA_FILES.values():
        (tmp_path / filename).write_bytes(b"fake-jpeg")
    runtime = RobotRuntime(
        robot_type="dual_franka",
        robot_driver=PlaceholderDualFrankaRobotDriver(),
        camera_provider=DualFrankaLocalFileCameraProvider(tmp_path),
        monitor_provider=FailingStatusMonitorProvider(),
    )
    execution = runtime.create_execution({"subtask": "pick up the cube"})

    monitor = runtime.monitor_status(
        {
            "execution_id": execution.execution_id,
            "monitor_id": execution.monitor_id,
        }
    )

    assert monitor.status == "failed"
    assert monitor.message == "monitor provider status failed"
    assert monitor.error == "remote monitor is unavailable"
    assert runtime.latest_execution_dict()["status"] == "failed"


def test_dual_franka_camera_provider_returns_observation_payload(tmp_path):
    for filename in CAMERA_FILES.values():
        (tmp_path / filename).write_bytes(b"fake-jpeg")
    provider = DualFrankaLocalFileCameraProvider(tmp_path)

    frame = provider.latest()
    payload = frame.to_dict()

    assert payload["concatenated_image"] == base64.b64encode(b"fake-jpeg").decode("ascii")
    assert set(provider.camera_names()) == {"cam_high", "cam_left_wrist", "cam_right_wrist"}
    assert payload["frame_id"].startswith("frame-")
    assert payload["mime_types"]["cam_high"] == "image/jpeg"


def test_dual_franka_camera_provider_returns_binary_camera_image(tmp_path):
    for filename in CAMERA_FILES.values():
        (tmp_path / filename).write_bytes(b"fake-jpeg")
    provider = DualFrankaLocalFileCameraProvider(tmp_path)

    image = provider.latest_image("cam_left_wrist")
    concatenated = provider.latest_image("concatenated_image")

    assert image.camera == "cam_left_wrist"
    assert image.data == b"fake-jpeg"
    assert image.mime_type == "image/jpeg"
    assert concatenated.camera == "cam_high"


def test_dual_franka_camera_provider_reports_missing_images(tmp_path):
    provider = DualFrankaLocalFileCameraProvider(tmp_path)

    with pytest.raises(FileNotFoundError):
        provider.latest()

    health = provider.health()
    assert health["available_cameras"] == []
    assert len(health["missing_cameras"]) == 3


def test_runtime_api_wraps_observations_and_monitor_status(tmp_path):
    api = _load_api_module()
    runtime = _runtime(tmp_path, auto_success_after_polls=1)
    app = api.create_app(runtime)

    from fastapi.testclient import TestClient

    client = TestClient(app)

    execute_response = client.post("/executions", json={"subtask": "pick cup", "subtask_index": 0})
    execute_payload = execute_response.json()["data"]
    assert execute_response.status_code == 200
    assert execute_payload["execution_id"].startswith("exec-")
    assert execute_payload["monitor_id"].startswith("mon-")

    monitor_response = client.post(
        "/monitors/status",
        json={
            "execution_id": execute_payload["execution_id"],
            "monitor_id": execute_payload["monitor_id"],
        },
    )
    assert monitor_response.status_code == 200
    assert monitor_response.json()["data"]["status"] == "success"

    observation_response = client.get("/observations/latest")
    observation_payload = observation_response.json()["data"]
    assert observation_response.status_code == 200
    assert observation_payload["concatenated_image"]

    metadata_response = client.get("/observations/latest/metadata")
    metadata_payload = metadata_response.json()["data"]
    assert metadata_response.status_code == 200
    assert metadata_payload["binary_endpoints"]["cam_high"] == "/observations/latest/cam_high.jpg"

    image_response = client.get("/observations/latest/cam_high.jpg")
    assert image_response.status_code == 200
    assert image_response.content == b"fake-jpeg"
    assert image_response.headers["x-camera"] == "cam_high"


def test_runtime_api_missing_observation_returns_503(tmp_path):
    api = _load_api_module()
    runtime = RobotRuntime(
        robot_type="dual_franka",
        robot_driver=PlaceholderDualFrankaRobotDriver(),
        camera_provider=DualFrankaLocalFileCameraProvider(tmp_path),
        monitor_provider=LocalMemoryMonitorProvider(),
    )
    app = api.create_app(runtime)

    from fastapi.testclient import TestClient

    response = TestClient(app).get("/observations/latest")

    assert response.status_code == 503
    assert response.json()["success"] is False


def test_runtime_api_config_builds_dual_franka_runtime(tmp_path):
    api = _load_api_module()
    config_path = tmp_path / "runtime.yaml"
    config_path.write_text(
        json.dumps(
            {
                "robot": {"type": "dual_franka", "driver": "placeholder"},
                "camera": {"provider": "local_files", "image_dir": str(tmp_path)},
                "monitor": {"provider": "local_memory", "auto_success_after_polls": 1},
            }
        ),
        encoding="utf-8",
    )

    runtime = api.build_runtime_from_config(api.load_runtime_config(config_path))

    assert runtime.robot_type == "dual_franka"
    assert runtime.health()["monitor"]["auto_success_after_polls"] == 1
