"""Command-line entry point for running the agentic robot loop."""

from __future__ import annotations

import argparse
import json
import sys

from dualsystem_agentic.app import build_agentic_robot_loop_app, build_online_robot_app
from dualsystem_agentic.config import load_config
from dualsystem_agentic.io.image import parse_image_spec


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dualsystem-agentic")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run the agentic loop for a task")
    run_parser.add_argument("--config", required=True, help="Path to a YAML/JSON config file")
    run_parser.add_argument("--task", required=True, help="Long-horizon task instruction")
    run_parser.add_argument("--max-steps", type=int, default=None, help="Override loop.max_steps")
    run_parser.add_argument(
        "--image",
        action="append",
        default=[],
        metavar="key=path",
        help="Observation image(s) as key=path (repeatable)",
    )
    run_parser.set_defaults(func=_run)

    online_parser = subparsers.add_parser("online", help="Run the online interactive agent runtime")
    online_parser.add_argument("--config", required=True, help="Path to a YAML/JSON config file")
    online_parser.add_argument("--max-steps", type=int, default=None, help="Override loop.max_steps")
    online_parser.add_argument("--log-dir", default=None, help="Override logging.root_dir and enable logging")
    online_parser.add_argument("--no-log", action="store_true", help="Disable persistent run logging")
    online_parser.add_argument(
        "--image",
        action="append",
        default=[],
        metavar="key=path",
        help="Static observation image(s) as key=path (repeatable)",
    )
    online_parser.set_defaults(func=_online)

    args = parser.parse_args(argv)
    return args.func(args)


def _run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    images = dict(parse_image_spec(spec) for spec in args.image)

    loop, mcp_client = build_agentic_robot_loop_app(config, static_images=images)
    max_steps = args.max_steps if args.max_steps is not None else config.loop.max_steps
    try:
        results, _ = loop.run(args.task, max_steps=max_steps, images=images)
    finally:
        close = getattr(mcp_client, "close", None)
        if callable(close):
            close()

    for result in results:
        print(json.dumps(result.to_dict(), ensure_ascii=False))
    return 0


def _online(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    images = dict(parse_image_spec(spec) for spec in args.image)
    if args.no_log:
        config.logging.enabled = False
    if args.log_dir:
        config.logging.enabled = True
        config.logging.root_dir = args.log_dir

    app = build_online_robot_app(
        config,
        static_images=images,
        max_steps=args.max_steps if args.max_steps is not None else config.loop.max_steps,
    )
    app.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
