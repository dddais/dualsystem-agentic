#!/usr/bin/env python
"""Run a config-driven online agentic robot process.

This is the recommended script for robot deployment/debugging. It does not
hard-code a robot. Switch robots by switching the config:

    PYTHONPATH=src python examples/run_online_robot.py --config examples/config.mock.yaml
    PYTHONPATH=src python examples/run_online_robot.py --config examples/config.x2robot.yaml

The config controls VLM, MCP servers/tools, executor, dataloader, interaction,
logging, and loop limits.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable

from dualsystem_agentic.app import build_online_robot_app
from dualsystem_agentic.config import load_config
from dualsystem_agentic.core.types import AgenticStepResult
from dualsystem_agentic.io.image import parse_image_spec


class TaskListInteraction:
    """Use a finite task list while preserving normal step/status rendering."""

    def __init__(self, inner, tasks: Iterable[str]) -> None:
        self.inner = inner
        self._tasks = iter(tasks)

    def read_task(self) -> str | None:
        try:
            return next(self._tasks)
        except StopIteration:
            return None

    def show_startup(self) -> None:
        self.inner.show_startup()

    def show_task_started(self, task: str, session_id: str) -> None:
        self.inner.show_task_started(task, session_id)

    def show_step(self, result: AgenticStepResult) -> None:
        self.inner.show_step(result)

    def show_task_finished(self, summary) -> None:
        self.inner.show_task_finished(summary)

    def show_error(self, task: str, error: BaseException) -> None:
        self.inner.show_error(task, error)

    def show_shutdown(self) -> None:
        self.inner.show_shutdown()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run an online agentic robot from config.")
    parser.add_argument(
        "--config",
        default="examples/config.mock.yaml",
        help="YAML/JSON config path. Default uses the offline mock robot.",
    )
    parser.add_argument("--max-steps", type=int, default=None, help="Override loop.max_steps.")
    parser.add_argument("--log-dir", default=None, help="Enable logging and override logging.root_dir.")
    parser.add_argument("--no-log", action="store_true", help="Disable persistent run logging.")
    parser.add_argument(
        "--image",
        action="append",
        default=[],
        metavar="key=path",
        help="Static observation image(s), repeatable. Example: --image main=obs.jpg",
    )
    parser.add_argument(
        "--tasks",
        nargs="*",
        default=None,
        help="Optional non-interactive task list for debugging.",
    )
    parser.add_argument(
        "--print-components",
        action="store_true",
        help="Print constructed component classes before serving.",
    )
    args = parser.parse_args(argv)

    config = load_config(args.config)
    if args.no_log:
        config.logging.enabled = False
    if args.log_dir:
        config.logging.enabled = True
        config.logging.root_dir = args.log_dir

    images = dict(parse_image_spec(spec) for spec in args.image)

    interaction = None
    if args.tasks is not None:
        from dualsystem_agentic.config import build_interaction

        interaction = TaskListInteraction(build_interaction(config.interaction), args.tasks)

    app = build_online_robot_app(
        config,
        static_images=images,
        max_steps=args.max_steps,
        interaction=interaction,
    )

    if args.print_components:
        runtime = app.runtime
        print("config:", args.config)
        print("planner:", type(app.loop.planner).__name__)
        print("mcp_client:", type(app.mcp_client).__name__)
        print("executor:", type(app.loop.executor).__name__)
        print("dataloader:", type(app.dataloader).__name__ if app.dataloader else None)
        print("interaction:", type(runtime.interaction).__name__)
        print("logger:", type(runtime.logger).__name__)
        print("max_steps:", runtime.max_steps)
        print("monitor_poll_interval_s:", runtime.monitor_poll_interval_s)
        print("max_monitor_polls:", runtime.max_monitor_polls)

    app.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
