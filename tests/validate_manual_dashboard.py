"""Browser smoke test with real HTTP/MCP/loop and simulated cameras/GRM.

Run from the repository root, with robot-bridge on PYTHONPATH:
  python tests/validate_manual_dashboard.py --monitor-repo ../Robo-Dopamine-delivery
Requires playwright + its Chromium browser. No hardware, model weights or GPU.
"""

import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "robot_runtime")]


@contextmanager
def serve(app):
    import uvicorn
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("test HTTP server failed to start")
            time.sleep(.01)
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        server.should_exit = True
        thread.join(8)
        sock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--monitor-repo", default=str(ROOT.parent / "Robo-Dopamine-delivery"))
    parser.add_argument("--screenshot", default="/tmp/manual-dashboard.png")
    parser.add_argument("--driver", choices=["manual", "manual_bridge"], default="manual")
    parser.add_argument("--input-mode", choices=["template", "instruction"], default="template")
    parser.add_argument("--template", default="default")
    args = parser.parse_args()
    delivery = Path(args.monitor_repo).resolve()
    sys.path.insert(0, str(delivery))

    import cv2
    import numpy as np
    import httpx
    from PIL import Image
    from playwright.sync_api import sync_playwright, expect
    from monitor_runtime.grm_backend import GRMMonitorBackend
    from monitor_runtime.service import create_app as monitor_app
    from robot_runtime.adapters.manual.robot_driver import ManualRobotDriver
    from robot_runtime.adapters.dual_franka.monitor_provider import LocalMemoryMonitorProvider, RemoteHTTPMonitorProvider
    from robot_runtime.adapters.robot_bridge.camera_provider import RobotBridgeCameraProvider, CAMERA_VIEWS
    from robot_runtime.api.app import create_app
    from robot_runtime.core.runtime import RobotRuntime
    from dualsystem_agentic.config import load_config

    settings = load_config(ROOT / "examples/config.simple_loop.manual.yaml").simple_loop
    templates = {"default": settings.instruction_template, **settings.instruction_templates}
    instruction = ("把 carrot 放到 {box} 旁。\n然后松开夹爪。" if args.input_mode == "instruction"
                   else templates[args.template].format(target="carrot"))

    class SimulatedCamera:
        count = 0
        def call(self, request):
            self.count += 1
            images = {}
            for i, view in enumerate(CAMERA_VIEWS.values()):
                image = np.full((240, 320, 3), (65+i*15, 80+i*15, 90+i*15), np.uint8)
                cv2.rectangle(image, (70,50), (250,190), (45,130,225), -1)
                cv2.putText(image, f"SIMULATED VIEW {i+1} / {self.count}", (12,225),
                            cv2.FONT_HERSHEY_SIMPLEX, .45, (230,230,230), 1)
                images[view] = [cv2.imencode(".jpg", image)[1].tobytes()]
            return {"status": "ok", "obs": {"images": images, "obs_lag_ms": 5}}
        def close(self):
            pass

    class SimulatedGRM:
        def inference_batch(self, samples):
            time.sleep(.2)
            results = []
            for sample in samples:
                assert sample["task"] == instruction
                assert sample["target_queries"] == ["carrot"]
                with Image.open(sample["image"][5]) as image:
                    size = list(image.size)
                results.append({**sample, "pred": "<score>+20%</score>", "valid": True,
                    "steering": {"enabled": True, "applied": True, "degraded": False,
                        "grounding": {"after_cam_high": {"selection_status": "ok", "image_size": size,
                            "selected": {"bbox": [70,50,250,190], "query": "carrot", "score": .97}}}}})
            return results

    integrated = args.driver == "manual_bridge"
    if integrated:
        from robot_runtime.adapters.manual_bridge.robot_driver import ManualBridgeRobotDriver
        from test_bridge_adapters import bridge
        backend, scheduler, robot = bridge(prompt_map={instruction: "pick carrot"})
        scheduler.state.update(scheduler="SimulatedOpenPiScheduler", iteration=0,
                               latency_step=0, move_steps=2, recording=False, person="")
        scheduler.state["actions"] += ["toggle_recording", "set_person", "step", "adjust_latency", "set_prompt_text"]
        original_call = scheduler.call
        def scheduler_call(request):
            name = request.get("name")
            if name == "toggle_recording":
                scheduler.state["recording"] = not scheduler.state["recording"]
            elif name == "set_person":
                scheduler.state["person"] = request["args"]["person"]
            elif name == "adjust_latency":
                scheduler.state["latency_step"] += request["args"]["delta"]
            elif name == "set_prompt_text":
                scheduler.state["prompt"] = request["args"]["prompt"]
            return original_call(request)
        scheduler.call = scheduler_call
        driver = ManualBridgeRobotDriver(operator_timeout_s=20, bridge_driver=backend, control_client=scheduler)
    else:
        driver = ManualRobotDriver(operator_timeout_s=20)
    runtime = RobotRuntime(robot_type="x1pro", robot_driver=driver,
        camera_provider=RobotBridgeCameraProvider(client=SimulatedCamera(), cache_s=.05),
        monitor_provider=LocalMemoryMonitorProvider(), safety={"max_execution_s":30})
    with tempfile.TemporaryDirectory() as directory, serve(create_app(runtime)) as runtime_url:
        backend = GRMMonitorBackend(runtime_url=runtime_url, goal_image=str(delivery / "examples/blank_goal.png"),
            inference_engine="hf", steering_config=str(delivery / "configs/steering.yaml"),
            model=SimulatedGRM(), output_root=directory, active_modes=["forward", "incremental"],
            interval=.15, success_threshold=.5, success_stable_steps=2, success_max_drift=.15)
        with serve(monitor_app(backend)) as monitor_url, tempfile.TemporaryFile(mode="w+") as log:
            runtime.monitor_provider = RemoteHTTPMonitorProvider(url=monitor_url, timeout=3)
            env = {**os.environ, "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
                   "PYTHONPATH": str(ROOT / "src") + os.pathsep + os.environ.get("PYTHONPATH", ""),
                   "DUAL_FRANKA_RUNTIME_URL": runtime_url, "PYTHONUNBUFFERED": "1"}
            process = subprocess.Popen([sys.executable, "examples/run_simple_robot.py", "--config",
                "examples/config.simple_loop.manual.yaml", "--input-source", "web", "--poll-interval", ".2"],
                cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    page = browser.new_page(viewport={"width":1440, "height":1000})
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(runtime_url + "/manual")
                    expect(page.locator("#submit-target")).to_be_enabled(timeout=10000)
                    expect(page.locator(".viewport.loaded")).to_have_count(3)
                    if integrated:
                        expect(page.locator("#bridge-connection")).to_have_text("Scheduler 已连接")
                        expect(page.locator("#bridge-single-step")).to_be_disabled()
                        page.locator("#bridge-record").click()
                        expect(page.locator("#bridge-record")).to_have_text("停止录制（录制中）")
                        page.locator("#bridge-person").fill("browser-test")
                        page.locator("#bridge-set-person").click()
                    original_prompt = httpx.get(runtime_url + "/manual/status").json()["data"]["input"]["request_id"]
                    if args.input_mode == "instruction":
                        page.locator("#mode-instruction").click()
                        page.locator("#full-instruction").fill(instruction)
                        page.locator("#target-queries").fill("carrot")
                    else:
                        page.locator("#instruction-template").select_option(args.template)
                        page.locator("#target").fill("carrot")
                    expect(page.locator("#task-preview")).to_have_text(instruction)
                    page.screenshot(path=args.screenshot.replace(".png", "-input.png"), full_page=True)
                    page.locator("#submit-target").click()
                    expect(page.locator("#ack")).to_have_text("启动 VLA" if integrated else "已开始", timeout=10000)
                    expect(page.locator("#submit-target")).to_be_disabled()
                    assert httpx.get(runtime_url + "/manual/status").json()["data"]["execution"]["subtask"] == instruction
                    page.locator("#ack").click()
                    page.locator("#view-grm").click()
                    expect(page.locator("#note-cam_high")).to_contain_text("97.0%", timeout=10000)
                    expect(page.locator("#note-cam_left_wrist")).to_contain_text("未对这一视角")
                    pixel = "Array.from(document.getElementById('cam_high').getContext('2d').getImageData(70,90,1,1).data).slice(0,3).join(',')"
                    page.wait_for_function(f"{pixel} === '52,237,181'")
                    page.locator("#show-bbox").uncheck()
                    page.wait_for_function(f"{pixel} !== '52,237,181'")
                    page.locator("#show-bbox").check()
                    page.wait_for_function(f"{pixel} === '52,237,181'")
                    expect(page.locator("#ack")).to_have_text("停止 VLA" if integrated else "已停止", timeout=15000)
                    page.locator("#ack").click()
                    expect(page.locator("#ack")).to_have_text("执行归位" if integrated else "已归位")
                    page.locator("#ack").click()
                    expect(page.locator("#submit-target")).to_be_enabled()
                    expect(page.locator("#score-status")).to_have_text("任务成功")
                    expect(page.locator(".viewport.loaded")).to_have_count(3)
                    stale = httpx.post(runtime_url + "/manual/target", json={"request_id":original_prompt, "target":"cup"})
                    assert stale.status_code == 409 and len(runtime._executions) == 1
                    result = httpx.get(runtime_url + "/manual/status").json()["data"]["monitor"]["result"]
                    for camera in CAMERA_VIEWS:
                        frozen = httpx.get(f"{runtime_url}/manual/monitor/frames/{result['preview']['frame_set_id']}/{camera}.png")
                        assert frozen.content == Path(result["frames"][camera]).read_bytes()
                    # A missing view must not leave a mixed/partially updated score triplet.
                    route_pattern = "**/manual/monitor/frames/**/cam_left_wrist.png"
                    page.route(route_pattern, lambda route: route.fulfill(status=503, body="unavailable"))
                    page.locator("#show-bbox").uncheck()
                    expect(page.locator("#camera-error")).to_contain_text("503")
                    expect(page.locator(".viewport.loaded")).to_have_count(0)
                    expect(page.locator("#progress")).to_have_text("—")
                    page.unroute(route_pattern)
                    page.locator("#show-bbox").check()
                    expect(page.locator(".viewport.loaded")).to_have_count(3)
                    expect(page.locator("#score-status")).to_have_text("任务成功")
                    page.locator("#view-live").click()
                    expect(page.locator("#note-cam_high")).to_contain_text("实时原图")
                    page.locator("#view-grm").click()
                    expect(page.locator("#note-cam_high")).to_contain_text("97.0%")
                    page.screenshot(path=args.screenshot, full_page=True)
                    page.set_viewport_size({"width":390, "height":844})
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    ready_prompt = httpx.get(runtime_url + "/manual/status").json()["data"]["input"]["request_id"]
                    page.reload()
                    expect(page.locator("#submit-target")).to_be_enabled()
                    assert httpx.get(runtime_url + "/manual/status").json()["data"]["input"]["request_id"] == ready_prompt
                    assert not errors, errors
                    if integrated:
                        assert scheduler.state["prompt"] == instruction
                        assert not any(c.get("name") == "set_prompt" for c in scheduler.calls)
                        assert [c.get("name") for c in scheduler.calls].count("homing") == 1
                        assert scheduler.state["single_step"] and scheduler.state["person"] == "browser-test"
                        assert len(robot.calls) == 4
                    browser.close()
                process.send_signal(signal.SIGINT)
                assert process.wait(8) == 0
                print(f"PASS: web target → MCP loop → {args.driver} start → score/bbox → stop → reset → ready; screenshot {args.screenshot}")
            finally:
                driver.close()
                if process.poll() is None:
                    process.send_signal(signal.SIGINT)
                    try:
                        process.wait(8)
                    except subprocess.TimeoutExpired:
                        process.kill(); process.wait()
                if process.returncode:
                    log.seek(0); print(log.read()[-6000:])


if __name__ == "__main__":
    main()
