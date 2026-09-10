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
from fastapi.responses import HTMLResponse, JSONResponse, Response
from contextlib import asynccontextmanager
from starlette.concurrency import run_in_threadpool

from robot_runtime.adapters.dual_franka.camera_provider import DualFrankaLocalFileCameraProvider
from robot_runtime.adapters.dual_franka.monitor_provider import (
    LocalMemoryMonitorProvider,
    RemoteHTTPMonitorProvider,
)
from robot_runtime.adapters.dual_franka.robot_driver import PlaceholderDualFrankaRobotDriver
from robot_runtime.adapters.manual.robot_driver import ManualRobotDriver
from robot_runtime.adapters.manual_bridge.robot_driver import ManualBridgeRobotDriver
from robot_runtime.adapters.manual.target_input import ManualTargetInput
from robot_runtime.adapters.robot_bridge.camera_provider import RobotBridgeCameraProvider
from robot_runtime.adapters.robot_bridge.robot_driver import RobotBridgeRobotDriver
from robot_runtime.core.runtime import RobotRuntime


def create_app(runtime: RobotRuntime) -> FastAPI:
    target_input = ManualTargetInput()

    @asynccontextmanager
    async def lifespan(app):
        yield
        await run_in_threadpool(runtime.close)

    # Sync routes run in Starlette's worker pool, leaving the event loop free
    # for camera and stop requests while remote monitor I/O is pending.
    app = FastAPI(title="Robot Runtime API", lifespan=lifespan)

    @app.get("/health")
    def health():
        return _ok(runtime.health())

    @app.get("/capabilities")
    def capabilities():
        return _ok(runtime.capabilities())

    @app.get("/environment")
    def environment():
        return _ok(runtime.environment())

    @app.get("/observations/latest")
    def observations_latest():
        try:
            return _ok(runtime.latest_observation().to_dict())
        except FileNotFoundError as exc:
            return _fail(str(exc), status=503)

    @app.get("/observations/latest/metadata")
    def observations_latest_metadata():
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
        endpoints = getattr(runtime.camera_provider, "binary_endpoints", None)
        if endpoints is not None:
            metadata["binary_endpoints"] = endpoints(frame.frame_id)
        return _ok(metadata)

    @app.get("/observations/latest/{camera}.jpg")
    def observations_latest_camera_jpeg(camera: str):
        try:
            image = runtime.latest_observation_image(camera)
        except KeyError as exc:
            return _fail(str(exc), status=404)
        except FileNotFoundError as exc:
            return _fail(str(exc), status=503)
        return _image_response(image)

    @app.get("/observations/frames/{frame_id}/{camera}.jpg")
    def observations_snapshot(frame_id: str, camera: str):
        try:
            return _image_response(runtime.latest_observation_image(camera, frame_id))
        except KeyError as exc:
            return _fail(str(exc), status=404)
        except FileNotFoundError as exc:
            return _fail(str(exc), status=503)

    @app.get("/manual", response_class=HTMLResponse)
    def manual_page():
        if not isinstance(runtime.robot_driver, ManualRobotDriver):
            return _fail("manual adapter is not enabled", status=404)
        page = Path(__file__).resolve().parents[1] / "adapters/manual/operator.html"
        return HTMLResponse(page.read_text(encoding="utf-8"))

    @app.get("/manual/status")
    def manual_status():
        if not isinstance(runtime.robot_driver, ManualRobotDriver):
            return _fail("manual adapter is not enabled", status=404)
        return _ok({**runtime.robot_driver.status(), **runtime.manual_snapshot(),
                    "input": target_input.status()})

    @app.get("/manual/app.js")
    def manual_script():
        if not isinstance(runtime.robot_driver, ManualRobotDriver):
            return _fail("manual adapter is not enabled", status=404)
        script = Path(__file__).resolve().parents[1] / "adapters/manual/operator.js"
        return Response(script.read_text(encoding="utf-8"), media_type="text/javascript",
                        headers={"Cache-Control": "no-cache"})

    def check_input_ready():
        if not isinstance(runtime.robot_driver, ManualRobotDriver):
            raise ValueError("manual adapter is not enabled")
        state = runtime.manual_snapshot()
        if (state["active_execution_id"] or state["resetting"] or state["estop_latched"]
                or runtime.robot_driver.status()["pending"]):
            raise ValueError("finish stopping and resetting before entering another target")

    @app.post("/manual/input/open")
    def manual_input_open(body: dict[str, Any]):
        try:
            check_input_ready()
            return _ok(target_input.open(body.get("request_id"), body.get("instruction_template"),
                                         body.get("last_target", "")))
        except ValueError as exc:
            return _fail(str(exc), status=409)

    @app.get("/manual/input/{request_id}")
    def manual_input_poll(request_id: str):
        try:
            check_input_ready()
            return _ok(target_input.poll(request_id))
        except ValueError as exc:
            return _fail(str(exc), status=409)

    @app.delete("/manual/input/{request_id}")
    def manual_input_close(request_id: str):
        target_input.close(request_id)
        return _ok({"closed": True})

    @app.post("/manual/target")
    def manual_target(body: dict[str, Any]):
        try:
            check_input_ready()
            return _ok(target_input.submit(body.get("request_id"), body.get("target")))
        except ValueError as exc:
            return _fail(str(exc), status=409)

    @app.get("/manual/monitor/frames/{frame_set_id}/{camera}.png")
    def manual_monitor_frame(frame_set_id: str, camera: str):
        if not isinstance(runtime.robot_driver, ManualRobotDriver):
            return _fail("manual adapter is not enabled", status=404)
        fetch = getattr(runtime.monitor_provider, "frame_image", None)
        if fetch is None:
            return _fail("monitor does not provide inference images", status=404)
        try:
            return Response(fetch(frame_set_id, camera), media_type="image/png",
                            headers={"Cache-Control": "private, max-age=60"})
        except ValueError as exc:
            return _fail(str(exc), status=400)
        except Exception as exc:
            return _fail(str(exc), status=503)

    @app.post("/manual/ack")
    def manual_ack(body: dict[str, Any]):
        if not isinstance(runtime.robot_driver, ManualRobotDriver):
            return _fail("manual adapter is not enabled", status=404)
        request_id = body.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            return _fail("request_id is required", status=400)
        try:
            return _ok(runtime.robot_driver.acknowledge(request_id))
        except ValueError as exc:
            return _fail(str(exc), status=409)

    @app.post("/manual/action")
    def manual_action(body: dict[str, Any]):
        if not isinstance(runtime.robot_driver, ManualBridgeRobotDriver):
            return _fail("manual_bridge adapter is not enabled", status=404)
        request_id = body.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            return _fail("request_id is required", status=400)
        try:
            return _ok(runtime.robot_driver.request_action(request_id))
        except ValueError as exc:
            return _fail(str(exc), status=409)

    @app.get("/manual/bridge/status")
    def manual_bridge_status():
        if not isinstance(runtime.robot_driver, ManualBridgeRobotDriver):
            return _fail("manual_bridge adapter is not enabled", status=404)
        try:
            return _ok({**runtime.robot_driver.scheduler_status(),
                        "allowed_actions": runtime.dashboard_allowed_actions()})
        except Exception as exc:
            return _fail(str(exc), status=503)

    @app.post("/manual/bridge/action")
    def manual_bridge_action(body: dict[str, Any]):
        if not isinstance(runtime.robot_driver, ManualBridgeRobotDriver):
            return _fail("manual_bridge adapter is not enabled", status=404)
        if not isinstance(body.get("name"), str) or not isinstance(body.get("args", {}), dict):
            return _fail("name must be a string and args an object", status=400)
        try:
            return _ok(runtime.dashboard_action(body["name"], body.get("args", {})))
        except ValueError as exc:
            return _fail(str(exc), status=409)
        except Exception as exc:
            return _fail(str(exc), status=503)

    @app.get("/manual/bridge/log")
    def manual_bridge_log(target: str = "scheduler", lines: int = 200):
        if not isinstance(runtime.robot_driver, ManualBridgeRobotDriver):
            return _fail("manual_bridge adapter is not enabled", status=404)
        try:
            return _ok(runtime.robot_driver.scheduler_log(target, lines))
        except ValueError as exc:
            return _fail(str(exc), status=400)
        except Exception as exc:
            return _fail(str(exc), status=503)

    @app.post("/executions")
    def executions(body: dict[str, Any]):
        try:
            execution = runtime.create_execution(body)
        except ValueError as exc:
            return _fail(str(exc), status=400)
        except Exception as exc:
            return _fail(str(exc), status=500)
        return _ok({"executed": execution.status != "failed", **execution.to_dict()})

    @app.post("/monitors/status")
    def monitors_status(body: dict[str, Any]):
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
    def control_stop(body: dict[str, Any] | None = Body(default=None)):
        return _ok(runtime.stop(body or {}))

    @app.post("/control/reset")
    def control_reset():
        return _ok(runtime.reset())

    @app.post("/control/emergency_stop")
    def control_emergency_stop():
        return _ok(runtime.emergency_stop())

    @app.get("/")
    def root():
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
                    "GET  /observations/frames/{frame_id}/{camera}.jpg",
                    "GET  /manual",
                    "GET  /manual/status",
                    "POST /manual/ack",
                    "POST /manual/action",
                    "GET  /manual/bridge/status",
                    "POST /manual/bridge/action",
                    "GET  /manual/bridge/log",
                    "POST /manual/input/open",
                    "GET  /manual/input/{request_id}",
                    "DELETE /manual/input/{request_id}",
                    "POST /manual/target",
                    "GET  /manual/monitor/frames/{frame_set_id}/{camera}.png",
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
    if robot_type not in {"dual_franka", "x1pro", "manual", "robot_bridge"}:
        raise ValueError(f"unsupported robot.type: {robot_type}")
    if driver_name == "placeholder":
        robot_driver = PlaceholderDualFrankaRobotDriver()
    elif driver_name == "manual":
        robot_driver = ManualRobotDriver(
            operator_timeout_s=robot_config.get("operator_timeout_s", 300.0))
    elif driver_name in {"robot_bridge", "manual_bridge"}:
        driver_class = ManualBridgeRobotDriver if driver_name == "manual_bridge" else RobotBridgeRobotDriver
        operator_config = {"operator_timeout_s": robot_config.get("operator_timeout_s", 300.0)} if driver_name == "manual_bridge" else {}
        robot_driver = driver_class(
            **operator_config,
            scheduler_url=robot_config.get("scheduler_url", "ws://127.0.0.1:8088"),
            robot_url=robot_config.get("robot_url", "ws://127.0.0.1:9946"),
            timeout_s=robot_config.get("timeout_s", 5.0),
            prompt_map=robot_config.get("prompt_map"),
            stop_delay_s=robot_config.get("stop_delay_s", 1.0),
            reset_delay_s=robot_config.get("reset_delay_s", 8.0),
            start_delay_s=robot_config.get("start_delay_s", 0.5))
    else:
        raise ValueError(f"unsupported robot.driver: {driver_name}")

    camera_provider_name = str(camera_config.get("provider") or "local_files")
    if camera_provider_name == "local_files":
        camera_provider = DualFrankaLocalFileCameraProvider(
            image_dir=camera_config.get("image_dir") or "/tmp/img")
    elif camera_provider_name == "robot_bridge":
        camera_provider = RobotBridgeCameraProvider(
            robot_url=camera_config.get("robot_url", robot_config.get("robot_url", "ws://127.0.0.1:9946")),
            timeout_s=camera_config.get("timeout_s", 5.0),
            cache_s=camera_config.get("cache_s", 0.5),
            snapshot_ttl_s=camera_config.get("snapshot_ttl_s", 30.0),
            max_snapshots=camera_config.get("max_snapshots", 32),
            max_obs_lag_s=camera_config.get("max_obs_lag_s", 3.0),
            quality=camera_config.get("quality", 90), size=camera_config.get("size"))
    else:
        raise ValueError(f"unsupported camera.provider: {camera_provider_name}")

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
            activate_path=str(monitor_config.get("activate_path") or "/monitors/activate"),
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


def _image_response(image) -> Response:
    headers = {"X-Frame-Id": image.frame_id, "X-Camera": image.camera,
               "Cache-Control": "no-store"}
    if image.timestamp is not None:
        headers["X-Timestamp"] = str(image.timestamp)
    return Response(content=image.data, media_type=image.mime_type, headers=headers)


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
