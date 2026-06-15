#!/usr/bin/env python3
"""HTTP API for the robot runtime service."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi import Body, FastAPI
from fastapi.responses import JSONResponse, Response

from robot_runtime.adapters.dual_franka.camera_provider import DualFrankaLocalFileCameraProvider
from robot_runtime.adapters.dual_franka.monitor_provider import (
    LocalMemoryMonitorProvider,
    RemoteHTTPMonitorProvider,
)
from robot_runtime.adapters.dual_franka.robot_driver import PlaceholderDualFrankaRobotDriver
from robot_runtime.core.runtime import RobotRuntime


def create_app(runtime: RobotRuntime) -> FastAPI:
    app = FastAPI(title="Robot Runtime API")

    @app.get("/health")
    async def health():
        return _ok(runtime.health())

    @app.get("/capabilities")
    async def capabilities():
        return _ok(runtime.capabilities())

    @app.get("/environment")
    async def environment():
        return _ok(runtime.environment())

    @app.get("/observations/latest")
    async def observations_latest():
        try:
            return _ok(runtime.latest_observation().to_dict())
        except FileNotFoundError as exc:
            return _fail(str(exc), status=503)

    @app.get("/observations/latest/metadata")
    async def observations_latest_metadata():
        try:
            frame = runtime.latest_observation()
        except FileNotFoundError as exc:
            return _fail(str(exc), status=503)
        metadata = frame.metadata_dict()
        metadata["binary_endpoints"] = {
            camera: f"/observations/latest/{camera}.jpg"
            for camera in metadata["cameras"]
        }
        metadata["binary_endpoints"]["concatenated_image"] = "/observations/latest/concatenated_image.jpg"
        return _ok(metadata)

    @app.get("/observations/latest/{camera}.jpg")
    async def observations_latest_camera_jpeg(camera: str):
        try:
            image = runtime.latest_observation_image(camera)
        except KeyError as exc:
            return _fail(str(exc), status=404)
        except FileNotFoundError as exc:
            return _fail(str(exc), status=503)
        return Response(
            content=image.data,
            media_type=image.mime_type,
            headers={
                "X-Frame-Id": image.frame_id,
                "X-Camera": image.camera,
                "X-Timestamp": str(image.timestamp),
            },
        )

    @app.post("/executions")
    async def executions(body: dict[str, Any]):
        try:
            execution = runtime.create_execution(body)
        except ValueError as exc:
            return _fail(str(exc), status=400)
        except Exception as exc:
            return _fail(str(exc), status=500)
        return _ok({"executed": execution.status != "failed", **execution.to_dict()})

    @app.post("/monitors/status")
    async def monitors_status(body: dict[str, Any]):
        try:
            monitor = runtime.monitor_status(body)
        except KeyError as exc:
            return _fail(str(exc), status=404)
        except ValueError as exc:
            return _fail(str(exc), status=409)
        except Exception as exc:
            return _fail(str(exc), status=500)
        return _ok(monitor.to_dict())

    @app.post("/control/stop")
    async def control_stop(body: dict[str, Any] | None = Body(default=None)):
        return _ok(runtime.stop(body or {}))

    @app.post("/control/reset")
    async def control_reset():
        return _ok(runtime.reset())

    @app.post("/control/emergency_stop")
    async def control_emergency_stop():
        return _ok(runtime.emergency_stop())

    @app.get("/")
    async def root():
        return JSONResponse(
            {
                "service": "robot_runtime",
                "status": "running",
                "endpoints": [
                    "GET  /health",
                    "GET  /capabilities",
                    "GET  /environment",
                    "GET  /observations/latest",
                    "GET  /observations/latest/metadata",
                    "GET  /observations/latest/{camera}.jpg",
                    "POST /executions",
                    "POST /monitors/status",
                    "POST /control/stop",
                    "POST /control/reset",
                    "POST /control/emergency_stop",
                ],
            }
        )

    return app


def build_runtime_from_config(config: dict[str, Any]) -> RobotRuntime:
    robot_config = dict(config.get("robot") or {})
    camera_config = dict(config.get("camera") or {})
    monitor_config = dict(config.get("monitor") or {})
    safety_config = dict(config.get("safety") or {})

    robot_type = str(robot_config.get("type") or "dual_franka")
    driver_name = str(robot_config.get("driver") or "placeholder")
    if robot_type != "dual_franka":
        raise ValueError(f"unsupported robot.type: {robot_type}")
    if driver_name != "placeholder":
        raise ValueError(f"unsupported dual_franka driver: {driver_name}")
    robot_driver = PlaceholderDualFrankaRobotDriver()

    camera_provider_name = str(camera_config.get("provider") or "local_files")
    if camera_provider_name != "local_files":
        raise ValueError(f"unsupported camera.provider: {camera_provider_name}")
    camera_provider = DualFrankaLocalFileCameraProvider(
        image_dir=camera_config.get("image_dir") or "/tmp/img"
    )

    monitor_provider_name = str(monitor_config.get("provider") or "local_memory")
    if monitor_provider_name in {"local_memory", "local_grm"}:
        monitor_provider = LocalMemoryMonitorProvider(
            default_status=str(monitor_config.get("default_status") or "running"),
            auto_success_after_polls=int(monitor_config.get("auto_success_after_polls") or 0),
        )
    elif monitor_provider_name == "remote_http":
        monitor_provider = RemoteHTTPMonitorProvider(
            url=str(monitor_config["url"]),
            timeout=float(monitor_config.get("timeout") or 30.0),
            start_path=str(monitor_config.get("start_path") or "/monitors/start"),
            status_path=str(monitor_config.get("status_path") or "/monitors/status"),
            stop_path=str(monitor_config.get("stop_path") or "/monitors/stop"),
        )
    else:
        raise ValueError(f"unsupported monitor.provider: {monitor_provider_name}")

    return RobotRuntime(
        robot_type=robot_type,
        robot_driver=robot_driver,
        camera_provider=camera_provider,
        monitor_provider=monitor_provider,
        safety=safety_config,
    )


def load_runtime_config(path: str | Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError as exc:
        raise ImportError("PyYAML is required to load robot runtime YAML configs") from exc

    return yaml.safe_load(Path(path).expanduser().read_text(encoding="utf-8")) or {}


def _ok(data: object = None, message: str = "ok") -> JSONResponse:
    return JSONResponse({"success": True, "data": data, "message": message})


def _fail(message: str, *, status: int) -> JSONResponse:
    return JSONResponse({"success": False, "data": None, "message": message}, status_code=status)


def _default_config_path() -> str:
    return str(Path(__file__).resolve().parents[1] / "configs" / "dual_franka.runtime.yaml")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Robot Runtime API server")
    parser.add_argument("--config", default=_default_config_path())
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8767)
    args = parser.parse_args(argv)

    runtime = build_runtime_from_config(load_runtime_config(args.config))

    import uvicorn

    uvicorn.run(create_app(runtime), host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
