"""Keyboard-driven execute/monitor/stop/reset loop, without a planner."""

from __future__ import annotations

import argparse
import math
import time
from string import Formatter
from uuid import uuid4
from enum import Enum
from typing import Callable

from dualsystem_agentic.core.types import JsonDict
from dualsystem_agentic.mcp.base import MCPToolClient
from dualsystem_agentic.config import SimpleLoopConfig


class SimplePhase(str, Enum):
    READY = "ready"
    EXECUTING = "executing"
    RECOVERING = "recovering"


class SimpleRobotLoop:
    """One operator, one robot, one execution at a time.

    The execute tool delegates monitor startup to Robot Runtime. Recovery uses
    stop/reset tool acknowledgements (operator or configured settling delay);
    it does not infer recovery completion from a GRM score.
    """

    def __init__(
        self,
        tool_client: MCPToolClient,
        *,
        namespace: str = "dual_franka",
        poll_interval_s: float = 1.0,
        execute_tool: str = "execute",
        monitor_tool: str = "monitor",
        stop_tool: str = "stop_task",
        recover_tool: str = "reset_task",
        settings: SimpleLoopConfig | None = None,
        read_input: Callable[[str], str] = input,
        write: Callable[[str], None] = print,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not math.isfinite(poll_interval_s) or poll_interval_s <= 0:
            raise ValueError("poll_interval_s must be finite and positive")
        self.tool_client = tool_client
        self.namespace = namespace
        self.poll_interval_s = poll_interval_s
        self.execute_tool = execute_tool
        self.monitor_tool = monitor_tool
        self.stop_tool = stop_tool
        self.recover_tool = recover_tool
        self.settings = settings or SimpleLoopConfig()
        fields = list(Formatter().parse(self.settings.instruction_template))
        if not any(field == "target" for _, field, _, _ in fields) or any(
            field is not None and (field != "target" or spec or conversion)
            for _, field, spec, conversion in fields
        ):
            raise ValueError("instruction_template must contain {target} and no other placeholders")
        for name in ("first_result_timeout_s", "result_timeout_s", "max_execution_s"):
            value = getattr(self.settings, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if not isinstance(self.settings.require_steering, bool):
            raise ValueError("require_steering must be boolean")
        self.last_target = (self.settings.default_target or "").strip()
        self.clock = clock
        self.read_input, self.write, self.sleep = read_input, write, sleep
        self.phase = SimplePhase.READY
        self.identity: JsonDict = {}

    def validate_tools(self) -> None:
        available = {
            row["name"] for row in self.tool_client.list_tools()
            if row.get("namespace") == self.namespace
        }
        required = {self.execute_tool, self.monitor_tool, self.stop_tool, self.recover_tool}
        missing = required - available
        if missing:
            raise ValueError(f"Missing tools in {self.namespace}: {', '.join(sorted(missing))}")

    def _call(self, name: str, arguments: JsonDict | None = None) -> JsonDict:
        result = self.tool_client.call_tool(name, arguments or {}, namespace=self.namespace)
        if not result.ok:
            raise RuntimeError(f"{name}: {result.error or 'tool call failed'}")
        data = result.data
        if any(data.get(key) is False for key in ("success", "ok", "executed", "stopped", "reset")):
            raise RuntimeError(f"{name}: {data.get('error') or data.get('message') or data}")
        if name in {self.stop_tool, self.recover_tool} and (
            data.get("error") or data.get("status") in {"failed", "error"}
        ):
            raise RuntimeError(f"{name}: {data.get('error') or data}")
        return data

    def _recover(self) -> None:
        self.phase = SimplePhase.RECOVERING
        self.write("[recovering] 中止任务并恢复")
        stopped = self._call(self.stop_tool, {"execution_id": self.identity["execution_id"]})
        if stopped.get("stopped") is not True:
            raise RuntimeError("stop tool did not acknowledge stop")
        if stopped.get("monitor_cleanup_error"):
            self.write(f"[warning] 机器人已停止，Monitor 清理失败: {stopped['monitor_cleanup_error']}")
        recovered = self._call(self.recover_tool)
        if recovered.get("reset") is not True:
            raise RuntimeError("recovery tool did not acknowledge completed reset")
        if recovered.get("completion_basis") == "command_and_delay":
            self.write(f"[recovering] 已等待归位 {recovered.get('wait_s', 0):g} 秒")
        self.identity = {}
        self.phase = SimplePhase.READY
        self.write("[ready] 中止与恢复完成")

    def run_cycle(self, target: str) -> None:
        """Fill the instruction template and run one target; always stop/reset.

        Execution/monitor errors recover and return to ready. Control errors
        propagate with phase=RECOVERING, preventing another execution.
        KeyboardInterrupt also runs recovery before propagating to the caller.
        """
        if self.phase is not SimplePhase.READY:
            raise RuntimeError("Cannot execute before recovery completes")
        target = target.strip() or self.last_target
        if not target:
            raise ValueError("Enter a target before reusing it")
        self.last_target = target
        subtask = self.settings.instruction_template.format(target=target)
        # Allocate before the network call, so cancellation works even if the
        # execute response is lost or startup is still waiting for reference.
        self.identity = {"execution_id": "exec-" + uuid4().hex}
        started = last_advance = self.clock()
        last_step = 0
        self.phase = SimplePhase.EXECUTING
        try:
            self.write(f"[executing] {subtask}")
            data = self._call(self.execute_tool, {
                **self.identity, "subtask": subtask, "subtask_index": 0, "target_queries": [target],
            })
            if data.get("execution_id") != self.identity["execution_id"] or not data.get("monitor_id"):
                raise RuntimeError("execute returned missing or mismatched execution/monitor IDs")
            self.identity["monitor_id"] = data["monitor_id"]
            # Manual startup can spend minutes waiting for the operator. Score
            # freshness budgets begin when execute has returned and activated
            # monitoring; the HTTP call has its own startup timeout.
            started = last_advance = self.clock()
            arguments = {**self.identity, "subtask": subtask, "subtask_index": 0}
            while True:
                status = str(data.get("status") or data.get("monitor_status") or "").strip().lower()
                self.write(f"[monitor] status={status or 'missing'} progress={data.get('progress')}")
                now = self.clock()
                if now - started >= self.settings.max_execution_s:
                    raise TimeoutError("execution time limit exceeded")
                step = int(data.get("poll_count") or 0)
                if step > last_step:
                    last_step, last_advance = step, now
                elif last_step and now - last_advance >= self.settings.result_timeout_s:
                    raise TimeoutError("no new monitor inference within result_timeout_s")
                if not last_step and now - started >= self.settings.first_result_timeout_s:
                    raise TimeoutError("first monitor inference timed out")
                result = data.get("result") or {}
                age = result.get("result_age_s")
                if age is not None and (not math.isfinite(float(age)) or (
                    step > 0 and float(age) > self.settings.result_timeout_s
                )):
                    raise TimeoutError("monitor result is stale or has an invalid age")
                if self.settings.require_steering and (step > 0 or status == "success"):
                    modes = result.get("modes") or {}
                    if not modes or any(
                        row.get("steering", {}).get("applied") is not True
                        or row.get("steering", {}).get("degraded") is not False
                        for row in modes.values()
                    ):
                        raise RuntimeError("monitor result has missing or degraded attention steering")
                if data.get("error") or status not in {"running", "progress"}:
                    break
                self.sleep(self.poll_interval_s)
                data = self._call(self.monitor_tool, arguments)
                for key, expected in self.identity.items():
                    if data.get(key) is not None and data[key] != expected:
                        raise RuntimeError(f"monitor returned a different {key}")
        except Exception as exc:
            self.write(f"[error] {exc}")
        finally:
            self._recover()

    def serve_forever(self) -> None:
        self.validate_tools()
        while True:
            try:
                target = self.read_input(f"[ready] 输入目标物体（回车复用 {self.last_target or '未设置'}，q 退出）> ").strip()
            except (EOFError, KeyboardInterrupt):
                return
            if target.lower() in {"q", "quit", "exit"}:
                return
            if not target and not self.last_target:
                self.write("[ready] 尚无上一次目标，请先输入目标物体")
                continue
            try:
                self.run_cycle(target)
            except KeyboardInterrupt:
                return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="examples/config.simple_loop.yaml")
    parser.add_argument("--namespace", default="dual_franka")
    parser.add_argument("--poll-interval", type=float, default=None, help="Seconds between monitor calls")
    parser.add_argument("--stop-tool", default="stop_task")
    parser.add_argument("--recover-tool", default="reset_task")
    args = parser.parse_args(argv)

    # Build only the existing MCP client. No VLM, executor, or camera loader.
    from dualsystem_agentic.config import build_mcp_client, load_config

    config = load_config(args.config)
    client = build_mcp_client(config.mcp)
    try:
        loop = SimpleRobotLoop(
            client,
            namespace=args.namespace,
            poll_interval_s=(args.poll_interval if args.poll_interval is not None
                             else config.loop.monitor_poll_interval_s),
            execute_tool=config.loop.execute_tool_name,
            monitor_tool=config.loop.monitor_tool_name,
            stop_tool=args.stop_tool,
            recover_tool=args.recover_tool,
            settings=config.simple_loop,
        )
        loop.serve_forever()
    except (ValueError, RuntimeError) as exc:
        print(f"[error] {exc}")
        return 1
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
