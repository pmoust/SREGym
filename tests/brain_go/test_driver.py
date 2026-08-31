"""Behaviour tests for the brain-go driver's turn loop.

The properties worth pinning are the two that differ deliberately from the brain
driver: a read timeout must not be retried, and the diagnosis phase must refuse
local tools.
"""

import subprocess
from unittest import mock

import pytest
import requests

from clients.brain_go import driver


def sse_response(*lines: str):
    """A stand-in for a streaming requests.Response."""
    resp = mock.MagicMock()
    resp.ok = True
    resp.iter_lines.return_value = iter(lines)
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda s, *a: False

    return resp


def test_read_timeout_is_not_retried():
    """The brain driver retries on requests.Timeout. This one must not.

    By the time a read timeout fires, graphexec has persisted an "input"
    checkpoint carrying the user message, plus a "loop" checkpoint per finished
    node. A retry rehydrates that partial state and appends the same message
    again, because it is built with no ID and AddMessages only dedupes non-empty
    IDs. The result is a half-finished run with a duplicated turn.
    """
    with mock.patch.object(driver.requests, "post") as post:
        post.side_effect = requests.exceptions.ReadTimeout("timed out")

        with pytest.raises(driver.TurnFailed) as exc:
            driver.run_turn("diagnose", "cli:t1", set())

    assert post.call_count == 1, "a read timeout must not be retried"
    assert "not retrying" in str(exc.value).lower()


def test_error_event_fails_the_turn():
    with mock.patch.object(driver.requests, "post") as post:
        post.return_value = sse_response("event: error", 'data: {"message":"boom"}', "")

        with pytest.raises(driver.TurnFailed, match="boom"):
            driver.run_turn("diagnose", "cli:t1", set())


def test_response_event_is_the_answer():
    with mock.patch.object(driver.requests, "post") as post:
        post.return_value = sse_response(
            "event: token",
            'data: {"accumulated":"partial"}',
            "",
            "event: response",
            'data: {"content":"the root cause is X"}',
            "",
            "event: done",
            "data: {}",
            "",
        )

        assert driver.run_turn("diagnose", "cli:t1", set()) == "the root cause is X"


def test_falls_back_to_accumulated_tokens_without_a_response_event():
    with mock.patch.object(driver.requests, "post") as post:
        post.return_value = sse_response(
            "event: token", 'data: {"accumulated":"streamed answer"}', "", "event: done", "data: {}", ""
        )

        assert driver.run_turn("diagnose", "cli:t1", set()) == "streamed answer"


def test_non_ok_response_fails_without_reading_the_stream():
    resp = mock.MagicMock()
    resp.ok = False
    resp.status_code = 401
    resp.text = "unauthorized"

    with (
        mock.patch.object(driver.requests, "post", return_value=resp),
        pytest.raises(driver.TurnFailed, match="401"),
    ):
        driver.run_turn("diagnose", "cli:t1", set())


def test_diagnosis_phase_refuses_local_tools():
    """Diagnosis is the measurement. Letting brain-go run kubectl there would
    read the injected fault straight off the cluster, bypassing the
    observability context the benchmark exists to evaluate."""
    posted = {}

    def fake_post(url, **kwargs):
        if url.endswith("/cli/tool-result"):
            posted.update(kwargs["json"])
        resp = mock.MagicMock()
        resp.ok = True

        return resp

    with (
        mock.patch.object(driver.requests, "post", side_effect=fake_post),
        mock.patch.object(driver.subprocess, "run") as run,
    ):
        driver.execute_local_tool(
            {"tool_call_id": "t1", "command": "kubectl get pods", "tool_type": "kubectl"},
            allowed_tools=set(),
        )

    run.assert_not_called()
    assert posted["exit_code"] == 1
    assert "not permitted" in posted["error"]


def test_mitigation_phase_runs_an_allowed_tool():
    posted = {}

    def fake_post(url, **kwargs):
        if url.endswith("/cli/tool-result"):
            posted.update(kwargs["json"])
        resp = mock.MagicMock()
        resp.ok = True

        return resp

    completed = subprocess.CompletedProcess(args="", returncode=0, stdout="pod/x patched", stderr="")

    with (
        mock.patch.object(driver.requests, "post", side_effect=fake_post),
        mock.patch.object(driver.subprocess, "run", return_value=completed),
    ):
        driver.execute_local_tool(
            {"tool_call_id": "t2", "command": "kubectl patch x", "tool_type": "kubectl"},
            allowed_tools={"kubectl", "helm"},
        )

    assert posted["exit_code"] == 0
    assert posted["result"] == "pod/x patched"
    assert posted["error"] is None


def test_local_tool_timeout_is_reported_not_raised():
    """A hung command must not abandon a live stream."""
    posted = {}

    def fake_post(url, **kwargs):
        if url.endswith("/cli/tool-result"):
            posted.update(kwargs["json"])
        resp = mock.MagicMock()
        resp.ok = True

        return resp

    with (
        mock.patch.object(driver.requests, "post", side_effect=fake_post),
        mock.patch.object(driver.subprocess, "run", side_effect=subprocess.TimeoutExpired("cmd", 120)),
    ):
        driver.execute_local_tool(
            {"tool_call_id": "t3", "command": "sleep 999", "tool_type": "kubectl"},
            allowed_tools={"kubectl"},
        )

    assert posted["exit_code"] == 124
    assert "timed out" in posted["error"]


def test_tool_result_post_failure_does_not_kill_the_turn():
    """The model can proceed without the result; raising here would abandon the stream."""
    with (
        mock.patch.object(driver.requests, "post", side_effect=requests.ConnectionError("refused")),
        mock.patch.object(driver.subprocess, "run") as run,
    ):
        driver.execute_local_tool({"tool_call_id": "t4", "command": "x", "tool_type": "nope"}, allowed_tools=set())

    run.assert_not_called()


def test_default_diagnosis_tools_are_empty():
    """Diagnosis executes no local tools for brain-go; mitigation executes kubectl.

    This is about the driver's own shell-out, not about cluster access. brain-go
    reaches the cluster over its sregym-kubectl MCP connection regardless of
    this set, so an empty set here is not a graph-only measurement (#26).
    """
    assert set() == driver.DIAGNOSIS_LOCAL_TOOLS
    assert "kubectl" in driver.MITIGATION_LOCAL_TOOLS


def test_run_label_uses_the_variables_the_bootstrap_exports(monkeypatch):
    """The label carries run, shard and slot when the fleet supplies them.

    The first version of this read three variables nothing in the repo wrote,
    so it silently took the random fallback on every run and the identity it
    advertised was never present. This test is what makes that visible.
    """
    monkeypatch.setenv("SREGYM_RUN_ID", "run-20260901-1000-abcd")
    monkeypatch.setenv("SREGYM_SHARD_INDEX", "2")
    monkeypatch.setenv("SREGYM_SLOT", "1")

    assert driver._run_label() == "run-20260901-1000-abcd 2 1"


def test_run_label_falls_back_when_unset(monkeypatch):
    """A local run has none of them and still gets something distinguishing."""
    for key in ("SREGYM_RUN_ID", "SREGYM_SHARD_INDEX", "SREGYM_SLOT"):
        monkeypatch.delenv(key, raising=False)

    label = driver._run_label()
    assert len(label) == 8
    assert label.isalnum()


def test_confirm_persisted_logs_but_does_not_raise_on_an_empty_report(monkeypatch):
    """An empty report is ambiguous, so it must not cost the problem.

    It means either the session and the API key resolve to different tenants,
    or the turn legitimately produced no structured report. Raising would
    punish the second case for the first, and an earlier version did exactly
    that before the diagnosis had even been submitted.
    """
    monkeypatch.setattr(driver, "BRAIN_ADMIN_API_URL", "http://127.0.0.1:8000")
    monkeypatch.setattr(driver, "BRAIN_SESSION", "session_x")
    monkeypatch.setattr(
        driver.requests,
        "get",
        lambda *a, **k: mock.Mock(ok=True, json=lambda: {"data": {"state": "IN_PROGRESS"}}),
    )

    error = mock.Mock()
    monkeypatch.setattr(driver.logger, "error", error)

    driver.confirm_persisted("uuid-1")

    error.assert_called_once()
    assert "NOT PERSISTED" in error.call_args[0][0]


def test_confirm_persisted_raises_when_the_investigation_cannot_be_read(monkeypatch):
    """Unreadable is unambiguous: the credential or the base url is wrong."""
    monkeypatch.setattr(driver, "BRAIN_ADMIN_API_URL", "http://127.0.0.1:8000")
    monkeypatch.setattr(driver, "BRAIN_SESSION", "session_x")
    monkeypatch.setattr(
        driver.requests,
        "get",
        lambda *a, **k: mock.Mock(ok=False, status_code=403, text="forbidden"),
    )

    with pytest.raises(RuntimeError, match="could not be read back"):
        driver.confirm_persisted("uuid-1")


def test_confirm_persisted_is_a_noop_without_credentials(monkeypatch):
    """A local run against a hand-started stack must not be blocked by this."""
    monkeypatch.setattr(driver, "BRAIN_ADMIN_API_URL", "")
    monkeypatch.setattr(driver, "BRAIN_SESSION", "")
    monkeypatch.setattr(driver.requests, "get", mock.Mock(side_effect=AssertionError("must not call out")))

    driver.confirm_persisted("uuid-1")
