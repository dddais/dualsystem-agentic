"""CLI regression tests for one-shot and online commands."""

from __future__ import annotations

import json

from dualsystem_agentic import FakeMCPToolClient
from dualsystem_agentic.cli import main
from dualsystem_agentic.core.types import AgenticStepResult
from dualsystem_agentic.interaction import OnlineTaskSummary


class ScriptedInteraction:
    def __init__(self, tasks: list[str | None]) -> None:
        self._tasks = iter(tasks)
        self.summaries: list[OnlineTaskSummary] = []
        self.started: list[tuple[str, str]] = []
        self.shutdown_count = 0

    def read_task(self) -> str | None:
        return next(self._tasks)

    def show_startup(self) -> None:
        pass

    def show_task_started(self, task: str, session_id: str) -> None:
        self.started.append((task, session_id))

    def show_step(self, result) -> None:
        pass

    def show_task_finished(self, summary: OnlineTaskSummary) -> None:
        self.summaries.append(summary)

    def show_error(self, task: str, error: BaseException) -> None:
        raise AssertionError(f"unexpected online task error for {task}: {error}")

    def show_shutdown(self) -> None:
        self.shutdown_count += 1


class ClosableFakeMCP(FakeMCPToolClient):
    def __init__(self) -> None:
        super().__init__()
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _config_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """
vlm:
  provider: openai_compatible
  model: dummy
executor:
  provider: noop
mcp:
  provider: fake
loop:
  max_steps: 3
dataloader:
  provider: none
interaction:
  provider: console
logging:
  enabled: false
""".strip(),
        encoding="utf-8",
    )
    return path


def test_run_command_keeps_one_shot_json_output(monkeypatch, tmp_path, capsys):
    class Loop:
        def run(self, task, *, max_steps, images=None, metadata=None):
            from dualsystem_agentic.core.types import AgenticPlannerInput, AgenticPlannerOutput

            result = AgenticStepResult(
                task=task,
                step_index=0,
                planner_input=AgenticPlannerInput(task=task),
                planner_output=AgenticPlannerOutput(raw_output='{"task_complete": true}', task_complete=True),
                task_complete=True,
            )
            return [result], None

    monkeypatch.setattr(
        "dualsystem_agentic.cli.build_agentic_robot_loop_app",
        lambda _config, *, static_images=None: (Loop(), ClosableFakeMCP()),
    )

    exit_code = main(["run", "--config", str(_config_file(tmp_path)), "--task", "finish now"])

    assert exit_code == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    result = json.loads(lines[0])
    assert result["task"] == "finish now"
    assert result["task_complete"] is True


def test_online_command_runs_task_and_closes_mcp(monkeypatch, tmp_path):
    captured = {}

    class App:
        def serve_forever(self):
            captured["served"] = True

    def build_app(config, *, static_images=None, max_steps=None, interaction=None, logger=None):
        captured["config"] = config
        captured["static_images"] = static_images
        captured["max_steps"] = max_steps
        captured["interaction"] = interaction
        captured["logger"] = logger
        return App()

    monkeypatch.setattr("dualsystem_agentic.cli.build_online_robot_app", build_app)

    exit_code = main(["online", "--config", str(_config_file(tmp_path)), "--max-steps", "2"])

    assert exit_code == 0
    assert captured["served"] is True
    assert captured["max_steps"] == 2
    assert captured["static_images"] == {}


def test_online_command_accepts_static_images(monkeypatch, tmp_path):
    image_path = tmp_path / "obs.jpg"
    image_path.write_bytes(b"fake jpeg")
    captured = {}

    class App:
        def serve_forever(self):
            return []

    def build_app(config, *, static_images=None, max_steps=None, interaction=None, logger=None):
        captured["static_images"] = static_images
        return App()

    monkeypatch.setattr("dualsystem_agentic.cli.build_online_robot_app", build_app)

    exit_code = main(
        [
            "online",
            "--config",
            str(_config_file(tmp_path)),
            "--image",
            f"main={image_path}",
        ]
    )

    assert exit_code == 0
    assert list(captured["static_images"]) == ["main"]
