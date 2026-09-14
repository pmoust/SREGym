"""Port-forward ownership and health checking.

These pin the three behaviours a benchmark run depends on and that nothing else
covers. All of them are control flow over three seams, so none needs a cluster.
"""

from unittest import mock

import pytest

from sregym.service import mcp_server
from sregym.service.mcp_server import MCPServer


def _server(healthy, running=True):
    """An MCPServer with the cluster and the network stubbed out."""
    s = MCPServer.__new__(MCPServer)
    s.namespace = "sregym"
    s.service_name = "mcp-server"
    s.port = 9954
    s.port_forward_process = None
    s.kubectl = mock.Mock()
    s._ensure_rbac = mock.Mock()
    s._is_running = mock.Mock(return_value=running)
    s._is_port_forward_healthy = mock.Mock(return_value=healthy)
    s.start_port_forward = mock.Mock()
    s._kill_stale_port_forward = mock.Mock()
    return s


def test_deploy_restarts_a_dead_forward_when_it_owns_one(monkeypatch):
    """Unset flag keeps the standalone behaviour, which upstream relies on."""
    monkeypatch.delenv("SREGYM_EXTERNAL_PORT_FORWARD", raising=False)
    s = _server(healthy=False)

    s.deploy()

    s._ensure_rbac.assert_called_once()
    s.start_port_forward.assert_called_once()


def test_deploy_never_touches_a_forward_it_does_not_own(monkeypatch):
    """The defect this exists for: deploy() ran per problem and replaced the
    forward the bootstrap owns for the whole slot."""
    monkeypatch.setenv("SREGYM_EXTERNAL_PORT_FORWARD", "1")
    s = _server(healthy=False)

    s.deploy()

    s._ensure_rbac.assert_called_once()  # still does its real work
    s.start_port_forward.assert_not_called()


def test_deploy_still_probes_when_the_forward_is_external(monkeypatch):
    """Not acting is the point; not looking would delete the only signal.

    The "absent or stale" lines that diagnosed run-20260831-1752 came from this
    probe.

    Weaker than it looks, and left in deliberately. Reverting the ownership
    guard does not fail this test, because the already-running branch probes
    anyway. What it does catch is someone making the external branch skip the
    probe to save a call. test_deploy_never_touches_a_forward_it_does_not_own is
    the one that guards the guard.
    """
    monkeypatch.setenv("SREGYM_EXTERNAL_PORT_FORWARD", "1")
    s = _server(healthy=False)

    s.deploy()

    s._is_port_forward_healthy.assert_called()


def test_start_port_forward_leaves_a_serving_forward_alone():
    """It used to kill first and ask later, so a healthy forward could be torn
    down and, if the three retries then failed, replaced by nothing."""
    s = _server(healthy=True)

    MCPServer.start_port_forward(s)

    s._kill_stale_port_forward.assert_not_called()


def test_start_port_forward_waits_for_traffic_before_accepting(monkeypatch):
    """A forward that binds but needs a moment to serve must not be destroyed.

    poll() is None said "healthy" the instant the process existed. Checking
    once three seconds in would tear down a forward that was about to work.
    """
    monkeypatch.setattr(mcp_server.time, "sleep", lambda _: None)
    s = _server(healthy=False)
    s._is_port_forward_healthy = mock.Mock(side_effect=[False, False, True])
    proc = mock.Mock()
    proc.poll.return_value = None  # alive throughout
    monkeypatch.setattr(mcp_server.subprocess, "Popen", mock.Mock(return_value=proc))
    s.is_port_in_use = mock.Mock(return_value=False)
    s.stop_port_forward = mock.Mock()

    MCPServer.start_port_forward(s)

    assert s._is_port_forward_healthy.call_count == 3
    s.stop_port_forward.assert_not_called()


def test_fresh_deploy_starts_a_forward_when_it_owns_one(monkeypatch):
    """The deployment-absent path, which no other test reaches."""
    monkeypatch.delenv("SREGYM_EXTERNAL_PORT_FORWARD", raising=False)
    s = _server(healthy=False, running=False)

    s.deploy()

    s.kubectl.exec_command.assert_called_once()  # the kustomize apply still runs
    s.kubectl.wait_for_ready.assert_called_once()
    s.start_port_forward.assert_called_once()


def test_fresh_deploy_starts_no_forward_when_it_does_not(monkeypatch):
    """The guard that makes deploy_mcp.py's teardown honest.

    Without it this path calls start_port_forward unconditionally -- no health
    gate at all -- which is the one place deploy() really can kill a healthy
    forward. Deleting the guard used to leave every test in this file passing.
    """
    monkeypatch.setenv("SREGYM_EXTERNAL_PORT_FORWARD", "1")
    s = _server(healthy=False, running=False)

    s.deploy()

    s.kubectl.exec_command.assert_called_once()  # still deploys
    s.kubectl.wait_for_ready.assert_called_once()
    s.start_port_forward.assert_not_called()


def _fake_clock(monkeypatch):
    """A clock that only advances when the code under test sleeps.

    start_port_forward polls `while time.time() < deadline`, so stubbing sleep
    alone turns a 20s deadline into a 20s busy-wait per attempt: 60 real
    seconds for the three-attempt path. Driving time from sleep keeps the
    deadline logic exercised and the test instant.
    """
    now = [1000.0]

    def sleep(seconds):
        now[0] += seconds

    monkeypatch.setattr(mcp_server.time, "sleep", sleep)
    monkeypatch.setattr(mcp_server.time, "time", lambda: now[0])
    return now


def test_start_port_forward_gives_up_when_the_forward_never_serves(monkeypatch):
    """A forward that binds and never answers is torn down, not accepted.

    Accepting one is what made every later tool call pay a full timeout against
    a socket that connects and never replies.
    """
    _fake_clock(monkeypatch)
    s = _server(healthy=False)
    proc = mock.Mock()
    proc.poll.return_value = None  # alive throughout, just never serving
    monkeypatch.setattr(mcp_server.subprocess, "Popen", mock.Mock(return_value=proc))
    s.is_port_in_use = mock.Mock(return_value=False)
    s.stop_port_forward = mock.Mock()

    MCPServer.start_port_forward(s)

    assert s.stop_port_forward.call_count == 3  # one per attempt, then give up
    assert s.port_forward_process is None  # nothing left for a caller to trust


def test_start_port_forward_stops_waiting_when_the_process_dies(monkeypatch):
    """Process death ends the wait early instead of burning the deadline."""
    _fake_clock(monkeypatch)
    s = _server(healthy=False)
    proc = mock.Mock()
    proc.poll.return_value = 1  # exited immediately
    monkeypatch.setattr(mcp_server.subprocess, "Popen", mock.Mock(return_value=proc))
    s.is_port_in_use = mock.Mock(return_value=False)
    s.stop_port_forward = mock.Mock()

    MCPServer.start_port_forward(s)

    # Only the early-return probe. Once poll() reports an exit there is nothing
    # left to probe, so the deadline is never waited out.
    assert s._is_port_forward_healthy.call_count == 1


def test_is_port_forward_healthy_probes_healthz_not_a_real_session():
    """/healthz, not one of the SSE mounts.

    A GET against /kubectl/sse (the endpoint this probed before) is not
    expensive for the tool-session reason it looks like -- see the healthz()
    docstring in mcp_server/sregym_mcp_server.py for what a bare GET there
    actually costs. It is expensive because the endpoint answers 200
    immediately but never ends the body, so a caller using --max-time pays
    the whole timeout on every check, healthy or not. Probing that every 15s
    for a whole slot's lifetime adds that cost every cycle. This pins the
    probe at the cheap, non-streaming route instead.
    """
    calls = []

    class FakeResponse:
        status_code = 200

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse()

    s = _server(healthy=True)
    s.port = 9954
    with mock.patch.object(mcp_server.requests, "get", fake_get):
        result = MCPServer._is_port_forward_healthy(s)

    assert result is True
    assert calls == [("http://127.0.0.1:9954/healthz", {"timeout": 5})]


@pytest.mark.parametrize("status", [200, 404, 503])
def test_is_port_forward_healthy_true_on_any_answer(status):
    """Any status proves the forward carried the request to the app.

    404 is the case this exists for, and is what the deployed container
    actually returns. deploy() applies the kustomization with `kubectl apply
    -k`, and its `images:` transform rewrites deployment.yaml's `sregym` to
    ghcr.io/sregym/sregym-mcp, which imagePullPolicy: Always pulls fresh from
    GHCR on every pod start. That published image was built 2026-09-06 and has
    no /healthz route, which entered this repo on 2026-09-11 alongside this
    probe (#94). Requiring 200 made this permanently False and aborted all 12
    slots of run-20260914-0912.

    503 is in here for the same reason rather than as a separate case: a
    caller acts on False by restarting `kubectl port-forward`, which cannot
    change a status the app chose to send.
    """

    class FakeResponse:
        status_code = status

    s = _server(healthy=True)
    with mock.patch.object(mcp_server.requests, "get", mock.Mock(return_value=FakeResponse())):
        assert MCPServer._is_port_forward_healthy(s) is True


def test_is_port_forward_healthy_false_on_request_error():
    s = _server(healthy=True)
    with mock.patch.object(mcp_server.requests, "get", mock.Mock(side_effect=OSError("refused"))):
        assert MCPServer._is_port_forward_healthy(s) is False


@pytest.mark.parametrize(
    "value,external",
    [
        ("1", True),
        ("true", True),
        ("yes", True),
        ("TRUE", True),
        (" 1 ", True),
        ("", False),
        ("0", False),
        ("false", False),
        ("no", False),
    ],
)
def test_flag_parsing(monkeypatch, value, external):
    monkeypatch.setenv("SREGYM_EXTERNAL_PORT_FORWARD", value)
    assert MCPServer._forward_is_external(_server(healthy=True)) is external
