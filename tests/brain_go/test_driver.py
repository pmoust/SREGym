"""Behaviour tests for the brain-go driver's turn loop.

The properties worth pinning are the two that differ deliberately from the brain
driver: a read timeout must not be retried, and the diagnosis phase must refuse
local tools.
"""

import datetime
import json
import re
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


# brain-go's own regexes, transcribed from internal/chat/lifecycle/nlp.go
# (explicitTimePattern, reFromTo, reISODate, timeToken). Transcribed rather than
# approximated because the whole point of the window sentence is that brain-go
# parses it, and every constraint on its phrasing comes from these three.
#
# reFromTo uses `$` in Go without (?m), which matches end of text only. Python's
# `$` also matches before a trailing newline, which would hide exactly the bug
# these tests exist to catch, so `\Z` is used here instead.
_TIME_TOKEN = (
    r"(?:\d{1,2}:\d{2}(?::\d{2})?(?:\s*[ap]m)?|\d{1,2}\s*[ap]m"
    r"|\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2})?|\d{1,2}[/\-]\d{1,2}(?:[/\-]\d{2,4})?)"
)
_EXPLICIT_TIME = re.compile(r"(?i)\bbetween\s+" + _TIME_TOKEN + r"\s+and\s+" + _TIME_TOKEN)
_FROM_TO = re.compile(r"(?i)\b(?:from|between)\s+(.+?)\s+(?:to|and)\s+(.+)\Z")
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2}))?\b")

_INJECTED_AT = datetime.datetime(2026, 9, 2, 6, 27, 8, tzinfo=datetime.UTC)


def _rendered(template, window):
    return template.format(
        app_name="astronomy-shop",
        namespace="astronomy-shop",
        descriptions="demo",
        window=window,
    ).rstrip()


@pytest.mark.parametrize("template", ["DIAGNOSIS_PROMPT", "MITIGATION_PROMPT"])
def test_brain_go_can_parse_the_window_out_of_the_prompt(monkeypatch, template):
    """The sentence is only worth sending if brain-go's own parser takes it.

    Three ways it silently does not, all of them ending in brain-go using a
    flat one-hour lookback instead: the explicit-time gate does not fire, the
    from/to match fails because something follows the window, or an anchor
    does not parse. Each leaves no error anywhere.
    """
    monkeypatch.setenv("BRAIN_INCIDENT_WINDOW_SECONDS", "2400")
    prompt = _rendered(getattr(driver, template), driver.incident_window(_INJECTED_AT.timestamp()))

    assert _EXPLICIT_TIME.search(prompt), "hasExplicitTimeReference would not fire"
    match = _FROM_TO.search(prompt)
    assert match, "reFromTo needs the window last in the message, with nothing after it"

    bounds = [_ISO_DATE.search(g) for g in (match.group(1), match.group(2))]
    assert all(bounds), "an anchor did not parse as an ISO timestamp"
    # 06:27:08 rounds up to 06:28, so the window never includes pre-fault time.
    assert [b.groups() for b in bounds] == [
        ("2026", "09", "02", "06", "28"),
        ("2026", "09", "02", "07", "08"),
    ]


def test_the_window_ends_one_window_budget_after_the_fault(monkeypatch):
    """brain-go freezes a thread's window on turn 1, so the end has to cover
    the whole investigation rather than the moment the prompt was built.

    Measured from the rounded start, not from the fault, so the window is a
    full budget long rather than a budget minus the rounding.
    """
    monkeypatch.setenv("BRAIN_INCIDENT_WINDOW_SECONDS", "600")
    window = driver.incident_window(_INJECTED_AT.timestamp())

    assert "between 2026-09-02T06:28 and 2026-09-02T06:38" in window


def test_the_window_is_independent_of_the_agent_timeout(monkeypatch):
    """AGENT_TIMEOUT_SECONDS bounds the harness wall clock; it must not also
    size the window, or a cap raised to avoid censoring an attempt (see
    run.tfvars) silently pushes the baseline before the cluster existed.
    """
    monkeypatch.setenv("AGENT_TIMEOUT_SECONDS", "2400")
    monkeypatch.delenv("BRAIN_INCIDENT_WINDOW_SECONDS", raising=False)
    window = driver.incident_window(_INJECTED_AT.timestamp())

    # The default width (8 minutes), not 2400s worth of window.
    assert "between 2026-09-02T06:28 and 2026-09-02T06:36" in window


def test_the_window_bounds_carry_no_seconds_and_no_zulu_suffix(monkeypatch):
    """Both break the parse outright, and neither is obvious to a later editor.

    brain-go's token pattern ends at the minute, so ":08" does not match, and a
    trailing "Z" stops both the gate and the anchor from matching at all.

    Scoped to the two bounds rather than every timestamp in the sentence: the
    prose deliberately carries the exact instant with seconds, which is for the
    model rather than for the parser, and sits before the word "between" where
    it cannot reach the match.
    """
    monkeypatch.setenv("BRAIN_INCIDENT_WINDOW_SECONDS", "2400")
    window = driver.incident_window(_INJECTED_AT.timestamp())
    match = re.search(r"between (\S+) and (\S+)", window)

    assert match, "no window bounds in the sentence"
    for bound in match.groups():
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", bound), f"unparseable bound: {bound}"


@pytest.mark.parametrize("unknown", [None, 0, 0.0])
def test_an_unknown_incident_start_sends_no_window_rather_than_a_guess(unknown):
    """No sentence leaves brain-go on its own default, which is the behaviour
    before this existed. A wrong window would read as deliberate."""
    assert driver.incident_window(unknown) == ""

    prompt = _rendered(driver.DIAGNOSIS_PROMPT, "")
    assert "between" not in prompt
    assert not prompt.endswith("\n"), "an absent window must not leave trailing blank lines"


@pytest.mark.parametrize(
    ("injected", "expected_start"),
    [
        ((2026, 9, 2, 20, 35, 38), "2026-09-02T20:36"),  # the real failure
        ((2026, 9, 2, 20, 20, 24), "2026-09-02T20:21"),
        ((2026, 9, 2, 20, 35, 1), "2026-09-02T20:36"),
        ((2026, 9, 2, 20, 35, 59), "2026-09-02T20:36"),
        ((2026, 9, 2, 20, 35, 0), "2026-09-02T20:35"),  # already on the minute
    ],
)
def test_the_window_never_starts_before_the_fault(monkeypatch, injected, expected_start):
    """Rounding down is what broke run-20260902-2008.

    The fault landed at 20:35:38, the window went out as 20:35, and all seven
    findings the agent cited fell between 20:35:13 and 20:35:28: inside the
    window it was given, and before the fault. It diagnosed otel-collector
    shutdown churn and scored 0.0 on a problem that scored 100 the run before.

    brain-go's token pattern has no seconds field, so minute precision is the
    only option and the only choice is which way to round.
    """
    monkeypatch.setenv("BRAIN_INCIDENT_WINDOW_SECONDS", "1800")
    moment = datetime.datetime(*injected, tzinfo=datetime.UTC)
    window = driver.incident_window(moment.timestamp())

    match = re.search(r"between (\S+) and (\S+)", window)
    assert match, window
    assert match.group(1) == expected_start

    started = datetime.datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M").replace(tzinfo=datetime.UTC)
    assert started >= moment, "the window must not hand the agent any pre-fault time"
    assert (started - moment).total_seconds() < 60, "and must not skip a whole minute of the incident"


def test_the_prose_carries_the_exact_instant_the_window_cannot(monkeypatch):
    """The window is minute-rounded for brain-go's parser; the model reading
    the prose has no such limit, and the true instant is better for the tool
    calls it makes itself."""
    monkeypatch.setenv("BRAIN_INCIDENT_WINDOW_SECONDS", "1800")
    window = driver.incident_window(datetime.datetime(2026, 9, 2, 20, 35, 38, tzinfo=datetime.UTC).timestamp())

    assert "introduced at 2026-09-02T20:35:38 UTC" in window
    # And an ISO stamp before the word "between" must not disturb the parse,
    # since reFromTo matches from "between" onward.
    assert _FROM_TO.search(window.strip())


def test_confirm_persisted_keeps_the_report(monkeypatch, tmp_path):
    """The report is the only per-hypothesis record a run leaves behind.

    Before this it was fetched, checked for emptiness and dropped, so every
    verdict, evidence list and posterior an attempt produced died with the slot
    and no offline scoring was possible.
    """
    monkeypatch.setattr(driver, "BRAIN_ADMIN_API_URL", "http://127.0.0.1:8000")
    monkeypatch.setattr(driver, "BRAIN_SESSION", "session_x")
    report = {"hypotheses_tested": [{"id": "h1", "status": "confirmed"}]}
    monkeypatch.setattr(
        driver.requests,
        "get",
        lambda *a, **k: mock.Mock(ok=True, json=lambda: {"data": {"rca_response": report}}),
    )
    monkeypatch.setenv("AGENT_LOGS_DIR", str(tmp_path))

    driver.confirm_persisted("uuid-1")

    assert json.loads((tmp_path / "rca_response.json").read_text()) == report


def test_an_empty_report_writes_no_artifact(monkeypatch, tmp_path):
    """Absent must read as unmeasured, not as an empty result."""
    monkeypatch.setattr(driver, "BRAIN_ADMIN_API_URL", "http://127.0.0.1:8000")
    monkeypatch.setattr(driver, "BRAIN_SESSION", "session_x")
    monkeypatch.setattr(
        driver.requests,
        "get",
        lambda *a, **k: mock.Mock(ok=True, json=lambda: {"data": {"state": "IN_PROGRESS"}}),
    )
    monkeypatch.setattr(driver.logger, "error", mock.Mock())
    monkeypatch.setenv("AGENT_LOGS_DIR", str(tmp_path))

    driver.confirm_persisted("uuid-1")

    assert not (tmp_path / "rca_response.json").exists()


def test_write_artifact_survives_an_unwritable_target(monkeypatch, tmp_path):
    """A bookkeeping failure must never cost a run its numbers."""
    blocked = tmp_path / "file-not-a-dir"
    blocked.write_text("x")
    monkeypatch.setenv("AGENT_LOGS_DIR", str(blocked))
    warning = mock.Mock()
    monkeypatch.setattr(driver.logger, "warning", warning)

    driver.write_artifact("rca_response.json", {"a": 1})

    warning.assert_called_once()


def test_parent_workflow_id_is_read_off_the_start_event():
    """The chat turn's id format is brain-go's business.

    Rebuilding it here from the thread id would drift the first time brain-go
    changes it, so it is read from the child's own start event instead.
    """
    history = {
        "events": [
            {
                "eventId": "1",
                "workflowExecutionStartedEventAttributes": {
                    "parentWorkflowExecution": {"workflowId": "acct:3|cli:inv-abc", "runId": "r1"},
                },
            },
        ],
    }

    assert driver._parent_workflow_id(history) == "acct:3|cli:inv-abc"


@pytest.mark.parametrize(
    "history",
    [None, {}, {"events": []}, {"events": [{"eventId": "1"}]}],
    ids=["none", "empty", "no-events", "orphan"],
)
def test_parent_workflow_id_yields_empty_on_an_unexpected_shape(history):
    """An orphan child exports its own history and stops, rather than raising."""
    assert driver._parent_workflow_id(history) == ""


def test_histories_are_not_exported_without_an_investigation(monkeypatch):
    """A turn that never opened an investigation has no history to export."""
    show = mock.Mock()
    monkeypatch.setattr(driver, "_temporal_show", show)

    driver.write_temporal_histories("")

    show.assert_not_called()


def test_the_child_history_is_exported_under_brain_gos_own_id(monkeypatch, tmp_path):
    """The RCI child's id is rci- plus the investigation uuid."""
    monkeypatch.setenv("AGENT_LOGS_DIR", str(tmp_path))
    child = {
        "events": [
            {
                "eventId": "1",
                "workflowExecutionStartedEventAttributes": {
                    "parentWorkflowExecution": {"workflowId": "parent-1"},
                },
            },
        ],
    }
    asked = []

    def show(workflow_id):
        asked.append(workflow_id)

        return child if workflow_id.startswith("rci-") else {"events": []}

    monkeypatch.setattr(driver, "_temporal_show", show)

    driver.write_temporal_histories("uuid-1")

    assert asked == ["rci-uuid-1", "parent-1"]
    assert (tmp_path / "rci_history.json").exists()
    assert (tmp_path / "turn_history.json").exists()


def test_a_missing_temporal_cli_records_nothing_and_does_not_raise(monkeypatch, tmp_path):
    """A slot without the CLI is a miss, not a failure."""
    monkeypatch.setenv("AGENT_LOGS_DIR", str(tmp_path))
    monkeypatch.setattr(
        driver.subprocess,
        "run",
        mock.Mock(side_effect=FileNotFoundError("temporal")),
    )

    driver.write_temporal_histories("uuid-1")

    assert not (tmp_path / "rci_history.json").exists()


def test_a_workflow_past_its_retention_is_an_expected_miss(monkeypatch, tmp_path):
    """An aged-out workflow is the expected miss, so it must not warn."""
    monkeypatch.setenv("AGENT_LOGS_DIR", str(tmp_path))
    monkeypatch.setattr(
        driver.subprocess,
        "run",
        mock.Mock(return_value=mock.Mock(returncode=1, stdout="", stderr="workflow not found")),
    )
    warning = mock.Mock()
    monkeypatch.setattr(driver.logger, "warning", warning)

    assert driver._temporal_show("rci-uuid-1") is None
    warning.assert_not_called()
