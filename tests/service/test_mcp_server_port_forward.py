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

    The 15 "absent or stale" lines that diagnosed run-20260831-1752 came from
    this probe.
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
