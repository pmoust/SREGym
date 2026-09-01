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


@pytest.mark.parametrize("value,external", [("1", True), ("true", True), ("", False), ("0", False)])
def test_flag_parsing(monkeypatch, value, external):
    monkeypatch.setenv("SREGYM_EXTERNAL_PORT_FORWARD", value)
    assert MCPServer._forward_is_external(_server(healthy=True)) is external
