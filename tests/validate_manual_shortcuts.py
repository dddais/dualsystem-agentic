"""Real-browser shortcut regression checks with HTTP Runtime and fake hardware.

Run from the repository root: python tests/validate_manual_shortcuts.py
Requires Playwright/Chromium; no robot, GPU or model weights.
"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "robot_runtime")]

import httpx
from playwright.sync_api import sync_playwright, expect

from robot_runtime.adapters.manual_bridge.robot_driver import ManualBridgeRobotDriver
from robot_runtime.adapters.manual.robot_driver import ManualRobotDriver
from robot_runtime.adapters.dual_franka.monitor_provider import LocalMemoryMonitorProvider
from robot_runtime.api.app import create_app
from robot_runtime.core.runtime import RobotRuntime
from test_bridge_adapters import Scheduler, bridge
from validate_manual_dashboard import serve


class KeyboardScheduler(Scheduler):
    offline = False

    def __init__(self):
        super().__init__(takeover=True)
        self.state.update(scheduler="Keyboard test", modes=["idle", "teleop", "autonomous"],
                          recording=False, person="", phase=0, has_phase=True,
                          digit_mode="phase", phase_locked=False, latency_step=1,
                          gripper_map={"scale": 1, "offset": 0})
        self.state["actions"] += ["toggle_recording", "set_person", "step", "adjust_latency",
                                  "set_phase", "toggle_phase_lock", "toggle_digit_mode",
                                  "press_digit", "set_gripper_map"]

    def call(self, request):
        if self.offline:
            raise OSError("simulated disconnect")
        name, args = request.get("name"), request.get("args", {})
        if name == "toggle_recording":
            self.state["recording"] = not self.state["recording"]
        elif name == "toggle_digit_mode":
            self.state["digit_mode"] = "prompt" if self.state["digit_mode"] == "phase" else "phase"
        elif name == "toggle_phase_lock":
            self.state["phase_locked"] = not self.state["phase_locked"]
        elif name == "set_phase":
            self.state["phase"] = args["phase"]
        elif name == "adjust_latency":
            self.state["latency_step"] += args["delta"]
        elif name == "set_mode" and args["mode"] == "autonomous":
            self.state["single_step"] = False
        return super().call(request)


def check_plain_manual(browser, pool):
    runtime = RobotRuntime(robot_type="test", robot_driver=ManualRobotDriver(operator_timeout_s=10),
                           camera_provider=None, monitor_provider=LocalMemoryMonitorProvider())
    page = browser.new_page()
    posts = []
    page.on("request", lambda request: posts.append(request.url) if request.method == "POST" else None)
    page.route("**/observations/latest/metadata", lambda route: route.fulfill(
        status=503, json={"success":False, "message":"No camera in keyboard test"}))
    try:
        with serve(create_app(runtime)) as url:
            page.goto(url + "/manual")
            for label, operation in [("已开始", lambda: runtime.create_execution({"subtask":"pick cup"})),
                                     ("已停止", runtime.stop), ("已归位", runtime.reset)]:
                future = pool.submit(operation)
                expect(page.locator("#ack")).to_have_text(label)
                page.locator("h1").click()
                count = len(posts)
                for key in ["r", "s", "h", "p", "1", "Enter"]:
                    page.keyboard.press(key)
                page.wait_for_timeout(100)
                assert len(posts) == count
                page.keyboard.press("Space")
                expect(page.locator("#ack")).to_be_hidden()
                future.result(3)
            assert len(posts) == 3 and all(url.endswith("/manual/ack") for url in posts)
    finally:
        page.close()
        runtime.close()


def main():
    backend, scheduler, _ = bridge(KeyboardScheduler(), start_delay_s=.4)
    driver = ManualBridgeRobotDriver(operator_timeout_s=20, bridge_driver=backend, control_client=scheduler)
    runtime = RobotRuntime(robot_type="test", robot_driver=driver, camera_provider=None,
                           monitor_provider=LocalMemoryMonitorProvider())
    try:
        with serve(create_app(runtime)) as url, sync_playwright() as playwright, ThreadPoolExecutor() as pool:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width":1440, "height":1000})
            page.route("**/observations/latest/metadata", lambda route: route.fulfill(
                status=503, json={"success":False, "message":"No camera in keyboard test"}))
            posts, errors = [], []
            page.on("request", lambda request: posts.append(request.post_data_json) if request.method == "POST" else None)
            page.on("pageerror", lambda error: errors.append(str(error)))
            with httpx.Client(base_url=url) as client:
                assert client.post("/manual/input/open", json={"request_id":"keyboard", "instruction_template":"pick {target}",
                    "allow_full_instruction":True, "instruction_templates":{}}).status_code == 200
                page.goto(url + "/manual")
                expect(page.locator("#bridge-record")).to_be_enabled()

                def press(key):
                    page.locator("h1").click()  # Leave editable fields / native button focus.
                    page.keyboard.press(key)

                def command(key, name, args=None, *, focused=False):
                    with page.expect_response(lambda response: response.request.method == "POST"
                                              and response.url.endswith("/manual/bridge/action")) as response:
                        page.keyboard.press(key) if focused else press(key)
                    assert response.value.status == 200, response.value.text()
                    assert posts[-1] == {"name":name, "args":args or {}}
                    expect(page.locator("#bridge-record")).to_be_enabled()  # Includes refreshed Scheduler state.

                def no_commands(keys, *, focused=False):
                    count = len(posts)
                    for key in keys:
                        page.keyboard.press(key) if focused else press(key)
                    page.wait_for_timeout(100)
                    assert len(posts) == count, posts[count:]

                command("r", "toggle_recording")
                assert scheduler.state["recording"]
                command("R", "toggle_recording")
                assert not scheduler.state["recording"]
                # Holding R cannot toggle repeatedly, including after the first response.
                page.locator("h1").click()
                with page.expect_response("**/manual/bridge/action"):
                    page.keyboard.down("r")
                expect(page.locator("#bridge-record")).to_have_text("停止录制（录制中）")
                count = len(posts)
                page.keyboard.down("r")
                page.wait_for_timeout(100)
                page.keyboard.up("r")
                assert len(posts) == count

                for selector in ["#target", "#bridge-person", "#bridge-scale", "#bridge-phase", "#bridge-prompt"]:
                    page.locator(selector).focus()
                    no_commands(["r", "s", "1", "p", "h"], focused=True)
                page.locator("#mode-instruction").click()
                for selector in ["#full-instruction", "#target-queries"]:
                    page.locator(selector).focus()
                    no_commands(["r", "Enter", "Space", "s", "1"], focused=True)
                page.evaluate("document.body.insertAdjacentHTML('beforeend', '<div id=editable contenteditable=true><span>edit</span></div>')")
                page.locator("#editable").focus()
                no_commands(["r", "s", "Enter", "1"], focused=True)
                page.locator("#editable").evaluate("el => el.remove()")
                page.locator("h1").click()
                count = len(posts)
                for flags in [{"ctrlKey":True}, {"altKey":True}, {"metaKey":True},
                              {"isComposing":True}, {"keyCode":229}, {"repeat":True}]:
                    page.dispatch_event("body", "keydown", {"key":"r", **flags})
                page.wait_for_timeout(100)
                assert len(posts) == count

                command("l", "toggle_phase_lock")
                command("[", "adjust_latency", {"delta":-1})
                command("]", "adjust_latency", {"delta":1})
                command("3", "set_phase", {"phase":3})
                command("p", "toggle_digit_mode")
                expect(page.locator("#bridge-digit-status")).to_contain_text("Prompt")
                command("1", "set_prompt", {"index":1})
                no_commands(["9", "h", "s", "Enter", "i", "t", "a", "Space"])
                command("p", "toggle_digit_mode")

                scheduler.state["actions"].remove("toggle_recording")
                expect(page.locator("#bridge-record")).to_be_hidden()
                no_commands(["r"])
                scheduler.state["actions"].append("toggle_recording")
                expect(page.locator("#bridge-record")).to_be_enabled()
                scheduler.offline = True
                expect(page.locator("#bridge-connection")).to_have_text("Scheduler 未连接")
                no_commands(["r", "p", "3"])
                scheduler.offline = False
                expect(page.locator("#bridge-record")).to_be_enabled()

                start = pool.submit(runtime.create_execution, {"subtask":"pick cup"})
                expect(page.locator("#ack")).to_have_text("启动 VLA")
                no_commands(["r", "p", "3", "h", "s", "a"])
                with page.expect_response("**/manual/action"):
                    press("Space")
                expect(page.locator("#ack")).to_be_disabled()
                no_commands(["Space", "h", "r"])
                expect(page.locator("#bridge-record")).to_be_enabled()
                assert start.result(3).driver_result["executed"]
                for key, mode in [("i","idle"), ("t","teleop"), ("a","autonomous")]:
                    command(key, "set_mode", {"mode":mode})
                command("s", "toggle_single_step")
                expect(page.locator("#bridge-step")).to_be_enabled()
                command("Enter", "step")
                # Enter on a focused button activates only that button, never also step.
                page.locator("#bridge-record").focus()
                count = len(posts)
                command("Enter", "toggle_recording", focused=True)
                assert len(posts) == count + 1
                # The native Enter path must also reject key-repeat after the
                # request has completed and the button becomes enabled again.
                with page.expect_response("**/manual/bridge/action"):
                    page.keyboard.down("Enter")
                expect(page.locator("#bridge-record")).to_be_enabled()
                count = len(posts)
                page.keyboard.down("Enter")
                page.wait_for_timeout(100)
                page.keyboard.up("Enter")
                assert len(posts) == count
                command("s", "toggle_single_step")
                no_commands(["Enter", "h"])
                command("2", "set_phase", {"phase":2})
                command("p", "toggle_digit_mode")
                no_commands(["0", "1"])
                assert scheduler.state["prompt"] == "pick cup"
                # The backend independently rejects prompt changes and raw digit dispatch.
                for name, args in [("set_prompt", {"index":0}), ("press_digit", {"digit":0})]:
                    assert client.post("/manual/bridge/action", json={"name":name, "args":args}).status_code == 409
                page.locator(".shortcut-help summary").click()
                page.evaluate("window.scrollTo(0,0)")
                page.screenshot(path="/tmp/manual-keyboard-shortcuts.png", full_page=True, animations="disabled")
                page.set_viewport_size({"width":390, "height":844})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

                stop = pool.submit(runtime.stop)
                expect(page.locator("#ack")).to_have_text("停止 VLA")
                press("Space")
                expect(page.locator("#ack")).to_be_hidden()
                assert stop.result(3)["stopped"]
                reset = pool.submit(runtime.reset)
                expect(page.locator("#ack")).to_have_text("执行归位")
                press("h")
                expect(page.locator("#ack")).to_be_hidden()
                assert reset.result(3)["reset"]
                assert [call.get("name") for call in scheduler.calls].count("homing") == 1
                assert not errors, errors
            check_plain_manual(browser, pool)
            browser.close()
        print("PASS: keyboard mappings, typing/IME/modifiers/repeat guards, native focus, capability/disconnect/lifecycle gates, prompt lock and mobile layout")
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
