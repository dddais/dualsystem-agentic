# dualsystem-robot-runtime

Robot-side HTTP runtime service for dualsystem-agentic deployments.

Install on the robot machine:

```bash
pip install -e ./robot_runtime
robot-runtime --port 8767
```

If this directory is moved into its own repository, install from that repository
root instead:

```bash
pip install -e .
robot-runtime --host 0.0.0.0 --port 8767
```

The runtime owns execution ids, monitor ids, observation endpoints, and robot
control endpoints. The agent communicates with it through the Dual-Franka MCP
adapter over HTTP.

The agent process does not import this package. It only needs the runtime URL,
for example `DUAL_FRANKA_RUNTIME_URL=http://ROBOT_MACHINE_IP:8767`, and matching
HTTP API contracts for `/executions`, `/monitors/status`, `/control/*`, and
`/observations/latest`.
