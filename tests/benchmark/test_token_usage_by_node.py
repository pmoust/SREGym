"""main.py's per-attempt node breakdown artifact (brain-go#2687).

token_delta.py's delta_by_node() answers "which node spent what" for one
attempt; these two helpers are how main.py reaches it and gets the answer
onto disk beside the attempt, the same way write_tool_calls
(clients/brain_go/driver.py) already does for tool calls.

Only these two are tested directly, the same reasoning
test_main_deploy_exhausted.py gives for _deploy_exhausted_problems: the row-
construction code around them drives a live Conductor this suite has no
cluster to run against, but _token_delta_by_node shells out to the real
token_delta.py CLI against real fixture files and needs no cluster to do
that, and _write_token_usage_by_node is pure file I/O.
"""

import importlib.util
import json
import sys
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "main_under_test_token_usage",
    Path(__file__).resolve().parents[2] / "main.py",
)
main_mod = importlib.util.module_from_spec(_SPEC)
sys.modules["main_under_test_token_usage"] = main_mod
_SPEC.loader.exec_module(main_mod)


def _prom(tmp_path, name, lines):
    path = tmp_path / name
    path.write_text("# scrape: at 2026-09-07T00:00:00Z\n" + "\n".join(lines) + "\n")

    return path


def _sample(node, token_type, value):
    return (
        f'llm_tokens_total{{account_id="2",model="m",node="{node}",provider="bedrock",'
        f'thread_id="t1",token_type="{token_type}",user_id="unknown"}} {value}'
    )


def test_token_delta_by_node_reaches_the_real_cli(tmp_path):
    """Shells out to the real token_delta.py, the same way _token_delta()
    (the flat total, already wired) does."""
    before = _prom(tmp_path, "before.prom", [_sample("diagnosis", "input", "0")])
    after = _prom(tmp_path, "after.prom", [_sample("diagnosis", "input", "42")])

    assert main_mod._token_delta_by_node(before, after) == {
        "diagnosis": {"calls": 0, "input": 42, "output": 0, "cache_read": 0, "cache_write": 0}
    }


def test_token_delta_by_node_degrades_to_empty_not_an_exception(tmp_path):
    """Pricing a run must never be able to fail it -- same contract as the
    flat _token_delta()."""
    missing = tmp_path / "does-not-exist.prom"

    assert main_mod._token_delta_by_node(missing, missing) == {}


def test_write_token_usage_by_node_lands_beside_the_attempt(tmp_path):
    main_mod._write_token_usage_by_node(
        tmp_path, {"diagnosis": {"input": 42, "calls": 3}, "hypothesis_gen_tools": {"input": 7, "calls": 1}}
    )
    payload = json.loads((tmp_path / "token-usage-by-node.json").read_text())

    assert payload == {"diagnosis": {"input": 42, "calls": 3}, "hypothesis_gen_tools": {"input": 7, "calls": 1}}


def test_an_empty_breakdown_writes_nothing(tmp_path):
    """Absent means unmeasured, not a file full of zeros -- same convention
    write_tool_calls follows for an unset AGENT_LOGS_DIR."""
    main_mod._write_token_usage_by_node(tmp_path, {})

    assert not list(tmp_path.iterdir())


def test_a_write_failure_is_not_fatal(tmp_path, capsys):
    """A bookkeeping failure must not cost a run its result."""
    blocked = tmp_path / "active"
    blocked.write_text("not a directory")

    main_mod._write_token_usage_by_node(blocked, {"diagnosis": {"input": 1}})  # must not raise
