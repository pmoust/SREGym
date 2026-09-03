"""Counting the agent's tool calls, and getting them off the box.

The tool_start events already arrived on the SSE stream and were only logged.
Recovering them meant parsing a column-formatted console log in which two
thirds of tool names wrap onto a continuation line, so a per-name grep
undercounted by however many of that tool's calls happened to wrap: 193 of 286
wrapped on run-20260903-0558 (sregym#67).
"""

import collections
import json
from pathlib import Path
from unittest import mock

from clients.brain_go import driver

ROOT = Path(__file__).resolve().parents[2]


def sse(*lines: str):
    resp = mock.MagicMock()
    resp.ok = True
    resp.iter_lines.return_value = iter(lines)
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda s, *a: False

    return resp


def _turn(counter, *events):
    with mock.patch.object(driver.requests, "post", return_value=sse(*events)):
        driver.run_turn("m", "t", set(), counter)


def test_every_tool_start_is_counted_by_name():
    counter = collections.Counter()
    _turn(
        counter,
        "event: tool_start",
        'data: {"tool": "get_logs"}',
        "",
        "event: tool_end",
        'data: {"tool": "get_logs"}',
        "",
        "event: tool_start",
        'data: {"tool": "get_logs"}',
        "",
        "event: tool_start",
        'data: {"tool": "exec_kubectl_cmd_safely"}',
        "",
        "event: response",
        'data: {"content": "done"}',
        "",
    )

    assert counter == {"get_logs": 2, "exec_kubectl_cmd_safely": 1}


def test_a_tool_start_with_no_name_is_counted_not_dropped():
    """Dropping it would undercount silently, which is the bug being fixed."""
    counter = collections.Counter()
    _turn(counter, "event: tool_start", "data: {}", "", "event: response", 'data: {"content": "x"}', "")

    assert counter == {"unknown": 1}


def test_counting_is_optional_so_other_callers_are_unaffected():
    """run_turn is called without a counter in tests and by any other caller."""
    with mock.patch.object(driver.requests, "post", return_value=sse("event: response", 'data: {"content": "x"}', "")):
        assert driver.run_turn("m", "t", set()) == "x"


def test_the_sidecar_lands_in_the_artifact_directory(tmp_path, monkeypatch):
    """AGENT_LOGS_DIR is the run's artifact dir, exported by main.py around the
    agent launch, so this lands beside the attempt it belongs to."""
    monkeypatch.setenv("AGENT_LOGS_DIR", str(tmp_path))
    driver.write_tool_calls(
        {
            "diagnosis": collections.Counter({"get_logs": 83, "exec_kubectl_cmd_safely": 75}),
            "mitigation": collections.Counter({"exec_kubectl_cmd_safely": 61, "get_logs": 59}),
        }
    )
    payload = json.loads((tmp_path / "tool-calls.json").read_text())

    assert payload["total"] == 83 + 75 + 61 + 59
    assert payload["by_phase"] == {"diagnosis": 158, "mitigation": 120}
    # Per tool across phases, and per tool within each phase: the balance
    # inverted between them in the reference run and that is the interesting bit.
    assert payload["by_tool"]["exec_kubectl_cmd_safely"] == 136
    assert payload["by_phase_and_tool"]["mitigation"]["get_logs"] == 59


def test_no_artifact_directory_is_not_an_error(tmp_path, monkeypatch):
    """A local run has none. Bookkeeping must not fail a run."""
    monkeypatch.delenv("AGENT_LOGS_DIR", raising=False)
    driver.write_tool_calls({"diagnosis": collections.Counter({"get_logs": 1})})

    assert not list(tmp_path.iterdir())


def test_a_directory_that_is_actually_a_file_is_not_an_error(tmp_path, monkeypatch):
    """The realistic failure: something else already owns that path."""
    blocked = tmp_path / "logs"
    blocked.write_text("not a directory")
    monkeypatch.setenv("AGENT_LOGS_DIR", str(blocked))

    driver.write_tool_calls({"diagnosis": collections.Counter({"get_logs": 1})})


def test_the_counts_are_recorded_on_every_exit_path():
    """Including the two early returns: a failed diagnosis turn, and a
    conductor that goes straight to done. Those are exactly the attempts whose
    tool use is worth seeing, so a happy-path-only write would miss them."""
    source = (ROOT / "clients" / "brain_go" / "driver.py").read_text()
    main_body = source.split("def main() -> int:", 1)[1]

    assert "finally:\n        write_tool_calls(tool_calls)" in main_body, (
        "write_tool_calls must run from a finally, or an early return records nothing"
    )
    # And it must be the only call, so the happy path cannot double-count.
    assert main_body.count("write_tool_calls(") == 1
