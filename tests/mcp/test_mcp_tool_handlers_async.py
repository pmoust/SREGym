"""exec_kubectl_cmd_safely and the observability tool handlers must not block
the event loop that also serves /healthz (sregym#95).

FastMCP calls a sync tool `def` inline: FunctionTool.run in
fastmcp/tools/tool.py calls type_adapter.validate_python(arguments), which
invokes the tool function directly and only awaits the return value if that
value is itself awaitable. mcp_server/sregym_mcp_server.py starts uvicorn
with no `workers=`, one worker, one event loop, and /healthz is a plain
Route on the same Starlette app as the tool mounts. A slow kubectl or HTTP
call therefore froze every other request on that worker for its whole
duration -- including /healthz -- and the port-forward supervisor in
scripts/benchmark/run_shard.sh restarts the forward once /healthz misses its
probe window (mcp_probe_code, --max-time 3, every
PF_PROBE_INTERVAL_SECONDS=15). exec_kubectl_cmd_safely alone was 45-52% of
all tool calls in the last two benchmark runs.

These tests drive the real FastMCP dispatch path: fastmcp.Client talking to
the server over FastMCPTransport, which runs the FastMCP server's own
_mcp_server.run() as a task on the *same* event loop as the test (see
fastmcp/client/transports.py, FastMCPTransport.connect_session) -- the same
relationship uvicorn has to mcp_server/sregym_mcp_server.py. A concurrent
probe task stands in for /healthz: no I/O, so on a free event loop it gets
scheduled roughly every PROBE_INTERVAL_SECONDS. Reverting either tool
function below to a plain `def` that calls its blocking dependency directly
(no anyio.to_thread.run_sync) starves that probe for the whole call: a
throwaway FastMCP server built the same way, with a sync `def` tool wrapping
a 1s time.sleep, measured 2 probes landing during the call, against 20 with
the fix. MIN_PROBES_DURING_CALL sits comfortably between the two.
"""

import asyncio
import time
from unittest import mock

import anyio
from fastmcp import Client

import pytest

from mcp_server import jaeger_server, kubectl_mcp_tools, loki_server, prometheus_server, submit_server

SLOW_CALL_SECONDS = 1.0
PROBE_INTERVAL_SECONDS = 0.05
MIN_PROBES_DURING_CALL = 8


async def _run_and_probe(client, tool_name, arguments):
    """Call `tool_name` through `client` while a /healthz-shaped probe loop
    runs concurrently on the same event loop.

    Returns (result_text, call_elapsed_seconds, probes_during_call).
    """
    events = []

    async def run_call():
        events.append(("call_start", time.monotonic()))
        result = await client.call_tool(tool_name, arguments)
        events.append(("call_end", time.monotonic()))
        events.append(("result", result[0].text))

    async def probe():
        # No I/O, same as healthz() in mcp_server/sregym_mcp_server.py.
        while True:
            events.append(("probe", time.monotonic()))
            await anyio.sleep(PROBE_INTERVAL_SECONDS)

    async with anyio.create_task_group() as tg:
        tg.start_soon(run_call)
        tg.start_soon(probe)
        await anyio.sleep(SLOW_CALL_SECONDS + 0.3)
        tg.cancel_scope.cancel()

    start = next(t for label, t in events if label == "call_start")
    end = next(t for label, t in events if label == "call_end")
    result_text = next(v for label, v in events if label == "result")
    probes_during_call = [t for label, t in events if label == "probe" and start < t < end]
    return result_text, end - start, probes_during_call


def test_exec_kubectl_cmd_safely_does_not_starve_healthz(monkeypatch):
    """The tool that mattered most: 45-52% of all calls in the last two
    benchmark runs, per sregym#95."""
    tool = mock.Mock()
    tool.action_stack = []
    tool.cmd_runner.exec_kubectl_cmd_safely = mock.Mock(
        side_effect=lambda cmd: (time.sleep(SLOW_CALL_SECONDS), "pods listed")[1]
    )
    monkeypatch.setattr(kubectl_mcp_tools, "extract_session_id", lambda ctx: "session-1")
    monkeypatch.setattr(kubectl_mcp_tools, "get_tools", lambda ssid: tool)

    async def scenario():
        async with Client(kubectl_mcp_tools.kubectl_mcp) as client:
            return await _run_and_probe(client, "exec_kubectl_cmd_safely", {"cmd": "kubectl get pods"})

    result_text, call_elapsed, probes_during_call = asyncio.run(scenario())

    # The full result comes back. Offloading to a thread must not truncate
    # or race the return value.
    assert result_text == "pods listed"
    assert call_elapsed >= SLOW_CALL_SECONDS
    assert len(probes_during_call) >= MIN_PROBES_DURING_CALL


# Every tool that reaches a backend, one case each. Naming one of them as a
# stand-in for the others does not hold: each server wraps its own handler,
# so a handler reverted to a blocking `def` in one file is invisible to a
# test that only calls another file's. The parameters are (module, tool
# name, arguments, expected result text).
OBSERVABILITY_TOOLS = [
    (prometheus_server, "get_metrics", {"query": "up"}, {"data": "up 1"}),
    (prometheus_server, "get_alerts", {}, {"data": {"alerts": []}}),
    (loki_server, "get_labels", {}, {"data": ["app", "namespace"]}),
    (loki_server, "get_label_values", {"label": "app"}, {"data": ["frontend"]}),
    (jaeger_server, "get_services", {}, {"data": ["frontend"]}),
    (jaeger_server, "get_operations", {"service": "frontend"}, {"data": ["GET /"]}),
    (
        jaeger_server,
        "get_dependency_graph",
        {"last_n_minutes": 30},
        {"data": [{"parent": "frontend", "child": "cart"}]},
    ),
]


@pytest.mark.parametrize(
    ("module", "tool_name", "arguments", "payload"),
    OBSERVABILITY_TOOLS,
    ids=[f"{m.__name__.split('.')[-1]}.{n}" for m, n, _, _ in OBSERVABILITY_TOOLS],
)
def test_observability_tools_do_not_starve_healthz(monkeypatch, module, tool_name, arguments, payload):
    """Prometheus, Loki and Jaeger share exec_kubectl_cmd_safely's shape: a
    sync tool `def` around a blocking `requests` call."""
    response = mock.Mock()
    response.status_code = 200
    response.json.return_value = payload

    def slow_make_request(self, method, url, **kwargs):
        time.sleep(SLOW_CALL_SECONDS)
        return response

    monkeypatch.setattr(module.ObservabilityClient, "make_request", slow_make_request)

    async def scenario():
        async with Client(module.mcp) as client:
            return await _run_and_probe(client, tool_name, arguments)

    _, call_elapsed, probes_during_call = asyncio.run(scenario())

    assert call_elapsed >= SLOW_CALL_SECONDS
    assert len(probes_during_call) >= MIN_PROBES_DURING_CALL


def test_submit_does_not_starve_healthz(monkeypatch):
    """submit is the one handler whose blocking call has no timeout at all,
    so a hung submission server would hold the loop indefinitely."""
    response = mock.Mock()
    response.status_code = 200
    response.text = "accepted"

    def slow_post(*args, **kwargs):
        time.sleep(SLOW_CALL_SECONDS)
        return response

    monkeypatch.setattr(submit_server.requests, "post", slow_post)

    async def scenario():
        async with Client(submit_server.mcp) as client:
            return await _run_and_probe(client, "submit", {"ans": "the root cause"})

    _, call_elapsed, probes_during_call = asyncio.run(scenario())

    assert call_elapsed >= SLOW_CALL_SECONDS
    assert len(probes_during_call) >= MIN_PROBES_DURING_CALL
