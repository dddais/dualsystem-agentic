"""Cross-repository HTTP/MCP contract check with synthetic images and models.

Uses the actual loop, MCP stdio, Robot Runtime API, GRM monitor lifecycle,
grounding client and SAM3 HTTP server. Neural generation/detection and physical
actions are doubles: this does not validate model quality or real hardware.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delivery-root", type=Path, default=Path(__file__).resolve().parents[2] / "Robo-Dopamine-delivery")
    parser.add_argument("--agent-python", required=True, help="Python executable with the MCP SDK installed")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path[:0] = [str(root / "src"), str(root / "robot_runtime"), str(args.delivery_root)]
    from PIL import Image
    import uvicorn
    from monitor_runtime.grm_backend import GRMMonitorBackend
    from monitor_runtime.service import create_app as monitor_app
    from grm_runtime.grounding import GroundingClient
    from sam3_runtime.service import make_server
    from robot_runtime.api.app import create_app as robot_app
    from robot_runtime.core.runtime import RobotRuntime
    from robot_runtime.adapters.dual_franka.camera_provider import CAMERA_FILES, DualFrankaLocalFileCameraProvider
    from robot_runtime.adapters.dual_franka.monitor_provider import RemoteHTTPMonitorProvider
    from robot_runtime.adapters.dual_franka.robot_driver import PlaceholderDualFrankaRobotDriver

    detected, samples_seen, actions = [], [], []
    with ExitStack() as stack:
        temp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        camera_dir = temp / "camera"
        camera_dir.mkdir()

        def reserve():
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            stack.callback(sock.close)
            return sock, f"http://127.0.0.1:{sock.getsockname()[1]}"

        runtime_socket, runtime_url = reserve()
        monitor_socket, monitor_url = reserve()

        class Detector:
            fingerprint = "synthetic-detector-contract-test"
            def detect(self, image, queries):
                detected.append(queries)
                return [{"bbox": [1, 1, 20, 20], "score": 0.99, "query": queries[0]}]

        sam = make_server("127.0.0.1", 0, Detector())
        sam_thread = threading.Thread(target=sam.serve_forever, daemon=True)
        sam_thread.start()
        def close_sam():
            sam.shutdown()
            sam.server_close()
            sam_thread.join(2)
        stack.callback(close_sam)
        grounder = GroundingClient(f"http://127.0.0.1:{sam.server_port}", timeout_s=2)

        class Model:
            def inference_batch(self, samples):
                outputs = []
                for sample in samples:
                    samples_seen.append(sample)
                    grounding = grounder.detect(sample["image"][5], sample["target_queries"])
                    assert grounding["selected"] is not None
                    outputs.append({**sample, "valid": True, "pred": "<score>+70%</score>",
                        "steering": {"applied": True, "degraded": False,
                                     "grounding": {"after_cam_high": grounding}}})
                return outputs

        steering = temp / "steering.yaml"
        steering.write_text(json.dumps({"enabled": True,
            "profile_path": str(args.delivery_root / "assets/steering/grm_8b_profile.json")}))
        backend = GRMMonitorBackend(model=Model(), runtime_url=runtime_url,
            goal_image=str(args.delivery_root / "examples/blank_goal.png"),
            steering_config=str(steering), inference_engine="hf", interval=0.1,
            active_modes=["forward", "incremental"], success_stable_steps=1,
            output_root=str(temp / "monitor"))
        stack.callback(backend.close)

        class Driver(PlaceholderDualFrankaRobotDriver):
            def execute(self, request, execution):
                assert backend.status({"monitor_id": execution.monitor_id}).result["warming_up"] is False
                assert backend.status({"monitor_id": execution.monitor_id}).poll_count == 0
                assert backend.status({"monitor_id": execution.monitor_id}).result["inference_enabled"] is False
                assert request.target_queries == ["purple mug"]
                assert request.subtask == "把 purple mug 放到 yellow plate"
                actions.append("execute")
                return super().execute(request, execution)
            def stop(self, execution_id=None):
                actions.append("stop")
                return super().stop(execution_id)
            def reset(self):
                actions.append("reset")
                return super().reset()

        runtime = RobotRuntime(robot_type="dual_franka", robot_driver=Driver(),
            camera_provider=DualFrankaLocalFileCameraProvider(camera_dir),
            monitor_provider=RemoteHTTPMonitorProvider(url=monitor_url, timeout=3),
            safety={"monitor_ready_timeout_s": 3, "max_execution_s": 15})
        stack.callback(runtime.close)

        def write_frame(index):
            buf = io.BytesIO()
            Image.new("RGB", (64, 64), (index % 255, 30, 40)).save(buf, format="JPEG")
            stamp = time.time()
            for filename in CAMERA_FILES.values():
                path = camera_dir / filename
                pending = path.with_suffix(".tmp")
                pending.write_bytes(buf.getvalue())
                os.utime(pending, (stamp, stamp))
                pending.replace(path)
        write_frame(0)
        done = threading.Event()
        def camera_writer():
            index = 1
            while not done.wait(0.05):
                write_frame(index)
                index += 1
        writer = threading.Thread(target=camera_writer, daemon=True)
        writer.start()
        def close_writer():
            done.set()
            writer.join(2)
        stack.callback(close_writer)

        def serve(app, sock):
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
            thread.start()
            def close_server():
                server.should_exit = True
                thread.join(5)
                assert not thread.is_alive(), "API did not shut down"
            stack.callback(close_server)
            deadline = time.monotonic() + 5
            while not server.started:
                if time.monotonic() > deadline:
                    raise TimeoutError("API startup timeout")
                time.sleep(0.01)
        serve(monitor_app(backend), monitor_socket)
        serve(robot_app(runtime), runtime_socket)

        config = temp / "client.yaml"
        config.write_text(json.dumps({
            "mcp": {"provider": "sdk", "servers": [{"namespace": "dual_franka", "transport": "stdio",
                "command": str(Path(args.agent_python).resolve()),
                "args": [str(root / "mcp_server/dual_franka_mcp_server/server.py")],
                "env": {"DUAL_FRANKA_RUNTIME_URL": runtime_url, "DUAL_FRANKA_ENABLE_RESET": "true",
                        "DUAL_FRANKA_TIMEOUT_S": "10", "DUAL_FRANKA_UNKNOWN_STATUS": "failed"}}]},
            "loop": {"monitor_poll_interval_s": 0.05},
            "simple_loop": {"instruction_template": "把 {target} 放到 yellow plate",
                            "first_result_timeout_s": 5, "result_timeout_s": 5,
                            "max_execution_s": 10, "require_steering": True},
        }))
        result = subprocess.run([args.agent_python, str(root / "examples/run_simple_robot.py"), "--config", str(config)],
            input="purple mug\n\nq\n", text=True, capture_output=True, cwd=root,
            env={**os.environ, "PYTHONPATH": str(root / "src")}, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "[error]" not in result.stdout, result.stdout
        assert actions == ["execute", "stop", "reset"] * 2, actions
        assert len(samples_seen) >= 4
        assert detected and all(queries == ["purple mug"] for queries in detected)
        assert all(s["target_queries"] == ["purple mug"] for s in samples_seen)
        assert result.stdout.count("[ready] 中止与恢复完成") == 2
        print(result.stdout)
        print(json.dumps({"cycles": 2, "sam3_queries": detected,
                          "mode_samples": len(samples_seen), "actions": actions,
                          "neural_models": "synthetic doubles", "driver": "placeholder"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
