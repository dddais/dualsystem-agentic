"""Tests for config-driven online robot app builders."""

from __future__ import annotations

import json

from dualsystem_agentic.app import build_configured_dataloader, build_online_robot_app
from dualsystem_agentic.config import AppConfig
from dualsystem_agentic.core.types import ImageInput
from dualsystem_agentic.io.dataloader import StaticDataLoader


def _scripted_config() -> AppConfig:
    return AppConfig.from_dict(
        {
            "vlm": {
                "provider": "scripted",
                "script": [{"task_complete": True}],
            },
            "executor": {"provider": "noop"},
            "mcp": {"provider": "fake"},
            "dataloader": {"provider": "none"},
            "interaction": {"provider": "console"},
            "logging": {"enabled": False},
        }
    )


def test_build_online_robot_app_wires_components_from_config():
    app = build_online_robot_app(_scripted_config(), max_steps=2)

    assert type(app.loop.planner).__name__ == "ScriptedVLMPlanner"
    assert type(app.mcp_client).__name__ == "FakeMCPToolClient"
    assert type(app.loop.executor).__name__ == "NoopExecutorClient"
    assert app.dataloader is None
    assert app.runtime.max_steps == 2


def test_built_online_robot_app_can_run_one_scripted_task():
    class OneTaskInteraction:
        def __init__(self) -> None:
            self._tasks = iter(["finish", None])
            self.summaries = []

        def read_task(self):
            return next(self._tasks)

        def show_startup(self) -> None:
            pass

        def show_task_started(self, task: str, session_id: str) -> None:
            pass

        def show_step(self, result) -> None:
            assert json.loads(result.planner_output.raw_output)["task_complete"] is True

        def show_task_finished(self, summary) -> None:
            self.summaries.append(summary)

        def show_error(self, task: str, error: BaseException) -> None:
            raise AssertionError(error)

        def show_shutdown(self) -> None:
            pass

    interaction = OneTaskInteraction()
    app = build_online_robot_app(_scripted_config(), interaction=interaction)

    summaries = app.serve_forever()

    assert summaries[0].task_complete is True
    assert interaction.summaries[0].stop_reason == "task_complete"


def test_static_images_override_none_dataloader_config():
    config = _scripted_config()
    images = {"main": ImageInput(type="base64", data="abc", mime_type="image/jpeg")}

    dataloader = build_configured_dataloader(config, static_images=images)

    assert isinstance(dataloader, StaticDataLoader)
    assert dataloader.capture().images == images
