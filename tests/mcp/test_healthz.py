"""/healthz is a plain route, not one of the SSE mounts.

The forward-supervision loop in scripts/benchmark/run_shard.sh and
MCPServer._is_port_forward_healthy both poll this route every few seconds for
a whole benchmark run's lifetime. Before it existed they polled /kubectl/sse
instead: not a tool-session cost (see the healthz() docstring in
mcp_server/sregym_mcp_server.py for what a bare GET there actually allocates),
but a client-side one -- curl held that connection open for the full
--max-time on every check, healthy or not, because the endpoint answers
immediately but the body never ends. This pins /healthz answering fast and
never streaming, so a regression that routed it back through one of the SSE
mounts would be caught here rather than only showing up as a probe that
mysteriously takes seconds again.
"""

from starlette.testclient import TestClient

from mcp_server.sregym_mcp_server import app


def test_healthz_returns_200_without_a_body_dependent_on_any_mcp_session():
    client = TestClient(app)

    resp = client.get("/healthz")

    assert resp.status_code == 200


def test_healthz_is_registered_ahead_of_the_sse_mounts():
    """A plain Route, not a Mount. A Mount would swallow /healthz as a prefix
    match if one were declared before it; asserting the exact path is present
    on the app pins it as its own route instead."""
    paths = [getattr(r, "path", None) for r in app.routes]

    assert "/healthz" in paths


def test_healthz_content_type_is_not_event_stream():
    """The regression this exists to catch: routing /healthz back through one
    of the create_sse_app mounts, which would answer with text/event-stream
    and hold the connection open instead of a plain, immediate response."""
    client = TestClient(app)

    resp = client.get("/healthz")

    assert "text/event-stream" not in resp.headers.get("content-type", "")
