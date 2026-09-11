"""Browser check: save once, Start three rounds, edit next instruction, refresh, stale tab."""
from concurrent.futures import ThreadPoolExecutor
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "robot_runtime")]

from playwright.sync_api import sync_playwright, expect
from dualsystem_agentic.config import SimpleLoopConfig
from dualsystem_agentic.simple_loop import SimpleRobotLoop
from dualsystem_agentic.web_input import WebTargetInput
from robot_runtime.api.app import create_app
from robot_runtime.adapters.dual_franka.monitor_provider import LocalMemoryMonitorProvider
from test_manual_bridge_lifecycle import RuntimeTools
from test_saved_instruction import setup
from validate_manual_dashboard import serve


class UnavailableCamera:
    def latest(self):
        raise FileNotFoundError("simulated camera unavailable")


def main():
    runtime, _, scheduler, _ = setup(LocalMemoryMonitorProvider(auto_success_after_polls=1))
    runtime.camera_provider = UnavailableCamera()
    loop = SimpleRobotLoop(RuntimeTools(runtime), settings=SimpleLoopConfig(require_steering=False, recover_tool="recover_task"),
                          poll_interval_s=.03, write=lambda _: None)
    with serve(create_app(runtime)) as url, ThreadPoolExecutor() as pool, sync_playwright() as playwright:
        source = WebTargetInput(url, "pick {target}", allow_full_instruction=True)
        cancelled = threading.Event()
        original_request = source._request
        def request(method, path, payload=None):
            if cancelled.is_set() and method != "DELETE":
                raise RuntimeError("browser validation cancelled")
            return original_request(method, path, payload)
        source._request = request
        def cycles():
            for _ in range(3):
                loop.run_cycle(source("ready"))
        running = pool.submit(cycles)
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width":1440, "height":1000})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            page.goto(url + "/manual")
            page.wait_for_function("isReady()")
            page.locator("#mode-instruction").click()
            page.locator("#full-instruction").fill("pick cup")
            page.locator("#target-queries").fill("cup")
            expect(page.locator("#bridge-autonomous")).to_be_disabled()
            page.locator("#submit-target").click()
            expect(page.locator("#saved-instruction")).to_have_text("pick cup")
            expect(page.locator("#bridge-autonomous")).to_be_enabled()
            assert not runtime._executions and not runtime._monitors
            for index, instruction in enumerate(("pick cup", "pick cup", "pick carrot")):
                expect(page.locator("#bridge-autonomous")).to_be_enabled()
                if index == 1:
                    page.reload()
                    expect(page.locator("#saved-instruction")).to_have_text("pick cup")
                    expect(page.locator("#full-instruction")).to_have_value("pick cup")
                    expect(page.locator("#bridge-autonomous")).to_be_enabled()
                    # Drafts survive polling, and are distinct from the saved task.
                    page.locator("#full-instruction").fill("unsaved draft")
                    expect(page.locator("#bridge-autonomous")).to_be_disabled()
                    page.wait_for_timeout(600)
                    expect(page.locator("#full-instruction")).to_have_value("unsaved draft")
                    expect(page.locator("#saved-instruction")).to_have_text("pick cup")
                    page.locator("#discard-instruction").click()
                    expect(page.locator("#full-instruction")).to_have_value("pick cup")
                page.locator("#bridge-autonomous").click()
                expect(page.locator("#bridge-home")).to_be_enabled(timeout=10000)
                assert len(runtime._executions) == index + 1
                assert scheduler.state["prompt"] == instruction
                if index == 1:
                    other = browser.new_page()
                    other.goto(url + "/manual")
                    expect(other.locator("#full-instruction")).to_have_value("pick cup")
                    other.locator("#full-instruction").fill("stale other tab draft")
                    page.locator("#full-instruction").fill("pick carrot")
                    page.locator("#target-queries").fill("carrot")
                    page.locator("#submit-target").click()
                    expect(page.locator("#saved-instruction")).to_have_text("pick carrot")
                    assert scheduler.state["prompt"] == "pick cup"
                    assert runtime._requests[runtime._latest_execution_id].subtask == "pick cup"
                    other.locator("#submit-target").click()
                    expect(other.locator("#target-error")).to_contain_text("another page")
                    other.close()
                page.locator("#bridge-home").click()
                if index < 2:
                    page.wait_for_function("isReady()")
            running.result(5)
            assert [r.subtask for r in runtime._requests.values()] == ["pick cup", "pick cup", "pick carrot"]
            assert len(runtime._monitors) == 3
            page.reload()
            expect(page.locator("#saved-instruction")).to_have_text("pick carrot")
            expect(page.locator("#full-instruction")).to_have_value("pick carrot")
            page.evaluate("window.scrollTo(0,0)")
            page.screenshot(path="/tmp/manual-saved-instruction.png", full_page=True)
            assert not errors, errors
            print("PASS: save once → Start × 3 → fresh monitors; next-round edits, draft retention, reload, stale-tab conflict")
        finally:
            cancelled.set()
            browser.close()
            # Release a simulated gate if an assertion failed.
            if not running.done():
                runtime.close()


if __name__ == "__main__":
    main()
