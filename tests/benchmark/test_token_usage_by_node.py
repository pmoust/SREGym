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


def _sample(node, token_type, value, account="2"):
    return (
        f'llm_tokens_total{{account_id="{account}",model="m",node="{node}",provider="bedrock",'
        f'thread_id="t1",token_type="{token_type}",user_id="unknown"}} {value}'
    )


class _Resp:
    """The two things _brain_account_id reads off a requests response."""

    def __init__(self, ok, payload, status_code=200):
        self.ok = ok
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


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


def test_the_account_reaches_the_cli_and_narrows_the_delta(tmp_path):
    """The slots on a box share one brain-go process and its counters, so a
    per-attempt breakdown that does not pass its own account also reports the
    neighbours' spend."""
    before = _prom(
        tmp_path,
        "before.prom",
        [_sample("diagnosis", "input", "0"), _sample("diagnosis", "input", "0", account="3")],
    )
    after = _prom(
        tmp_path,
        "after.prom",
        [_sample("diagnosis", "input", "42"), _sample("diagnosis", "input", "900", account="3")],
    )

    assert main_mod._token_delta_by_node(before, after, "2")["diagnosis"]["input"] == 42
    assert main_mod._token_delta(before, after, "2")["input"] == 42
    assert main_mod._token_delta(before, after)["input"] == 942


def test_no_account_id_passes_no_flag():
    """token_delta.py reads an empty account as "every account", and relying on
    that rather than stating it is how a typo becomes a silent zero."""
    assert main_mod._account_args("") == []
    assert main_mod._account_args("3") == ["--account", "3"]


def test_the_account_id_comes_from_the_slot_session(monkeypatch):
    """seed_slot.sh mints a tenant per slot and exports the session that owns
    it; GET /api/users/me is what turns that session into an account id."""
    seen = {}

    def fake_get(url, cookies=None, timeout=None):
        seen["url"] = url
        seen["cookies"] = cookies

        return _Resp(True, {"user": {"account_id": 4}, "is_app_installed": False})

    monkeypatch.setattr(main_mod.requests, "get", fake_get)
    monkeypatch.setenv("BRAIN_ADMIN_API_URL", "http://127.0.0.1:8000/")
    monkeypatch.setenv("BRAIN_SESSION", "session_abc")
    main_mod._brain_account_id.cache_clear()

    assert main_mod._brain_account_id() == "4"
    assert seen["url"] == "http://127.0.0.1:8000/api/users/me"
    assert seen["cookies"] == {"session": "session_abc"}

    main_mod._brain_account_id.cache_clear()


def test_a_run_with_no_brain_go_tenant_asks_nothing(monkeypatch):
    """Every agent but brain_go runs without one, and so does a local run
    against a hand-started stack."""

    def fail(*args, **kwargs):
        raise AssertionError("must not call the api service without a session")

    monkeypatch.setattr(main_mod.requests, "get", fail)
    monkeypatch.delenv("BRAIN_ADMIN_API_URL", raising=False)
    monkeypatch.delenv("BRAIN_SESSION", raising=False)
    main_mod._brain_account_id.cache_clear()

    assert main_mod._brain_account_id() == ""

    main_mod._brain_account_id.cache_clear()


def test_an_unreadable_account_id_degrades_to_no_filter(monkeypatch):
    """Pricing must never fail a run, so a refused or malformed answer reads
    as "sum every account" -- which is what the columns did before."""
    monkeypatch.setenv("BRAIN_ADMIN_API_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("BRAIN_SESSION", "session_abc")

    for response in (
        _Resp(False, {}, status_code=401),
        _Resp(True, {"user": {}}),
        _Resp(True, {}),
        _Resp(True, []),
    ):
        monkeypatch.setattr(main_mod.requests, "get", lambda *a, resp=response, **k: resp)
        main_mod._brain_account_id.cache_clear()

        assert main_mod._brain_account_id() == ""

    def raise_timeout(*args, **kwargs):
        raise main_mod.requests.Timeout("too slow")

    monkeypatch.setattr(main_mod.requests, "get", raise_timeout)
    main_mod._brain_account_id.cache_clear()

    assert main_mod._brain_account_id() == ""

    main_mod._brain_account_id.cache_clear()


def test_the_account_id_is_read_once_per_process(monkeypatch):
    """It sits on the per-attempt path, and the tenant is seeded before the
    run starts and cannot change under it."""
    calls = []

    def fake_get(*args, **kwargs):
        calls.append(1)

        return _Resp(True, {"user": {"account_id": 2}})

    monkeypatch.setattr(main_mod.requests, "get", fake_get)
    monkeypatch.setenv("BRAIN_ADMIN_API_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("BRAIN_SESSION", "session_abc")
    main_mod._brain_account_id.cache_clear()

    assert [main_mod._brain_account_id(), main_mod._brain_account_id()] == ["2", "2"]
    assert len(calls) == 1

    main_mod._brain_account_id.cache_clear()


def test_the_row_records_which_account_narrowed_it(tmp_path, monkeypatch):
    """The fallback to summing every account is otherwise invisible in the
    archive: its warning goes to the slot log, and the lookup is cached for the
    life of the process, so one failed lookup at startup reverts that slot's
    whole run while its neighbours stay narrowed. The column is what lets a
    reader of the archived CSV tell the two apart."""
    before = _prom(tmp_path, "b.prom", [_sample("t1", "input", "0", account="7")])
    after = _prom(tmp_path, "a.prom", [_sample("t1", "input", "50", account="7")])

    monkeypatch.setattr(main_mod, "_brain_account_id", lambda: "7")

    snapshot: dict[str, object] = {}
    main_mod._record_token_columns(snapshot, before, after, tmp_path)

    assert snapshot["tokens_account_id"] == "7"
    assert snapshot["tokens_input"] == 50


def test_an_unnarrowed_row_says_so_rather_than_looking_attributed(tmp_path, monkeypatch):
    """An empty id is the signal that the columns beside it count every slot on
    the box. Leaving the column out, or filling it in anyway, would make an
    inflated row indistinguishable from an attributed one."""
    before = _prom(tmp_path, "b.prom", [_sample("t1", "input", "0", account="7")])
    after = _prom(
        tmp_path,
        "a.prom",
        [_sample("t1", "input", "50", account="7"), _sample("t2", "input", "900", account="9")],
    )

    monkeypatch.setattr(main_mod, "_brain_account_id", lambda: "")

    snapshot: dict[str, object] = {}
    main_mod._record_token_columns(snapshot, before, after, tmp_path)

    assert snapshot["tokens_account_id"] == ""
    # The neighbour's 900 is in the figure, which is exactly what the empty
    # column warns about.
    assert snapshot["tokens_input"] == 950


def test_the_helper_also_writes_the_node_breakdown(tmp_path, monkeypatch):
    """The row columns and the node file are written by the same helper, and two
    reviews found this path under-tested in turn. Removing the write left the
    whole suite green; so did leaving the write but handing it no account, which
    silently reverts the file to every slot's spend while the row stays correct.

    A second account is what makes the difference visible: with one account a
    filtered and an unfiltered breakdown are identical, so the fixture cannot
    tell them apart. The neighbour's 900 below is the whole point."""
    before = _prom(
        tmp_path,
        "b.prom",
        [_sample("diagnosis", "input", "0", account="7"), _sample("diagnosis", "input", "0", account="9")],
    )
    after = _prom(
        tmp_path,
        "a.prom",
        [_sample("diagnosis", "input", "40", account="7"), _sample("diagnosis", "input", "900", account="9")],
    )

    monkeypatch.setattr(main_mod, "_brain_account_id", lambda: "7")

    snapshot: dict[str, object] = {}
    main_mod._record_token_columns(snapshot, before, after, tmp_path)

    written = tmp_path / "token-usage-by-node.json"
    assert written.exists(), "the helper must write the node breakdown, not only the row columns"
    # 40, not 940: the file has to carry the same account the row was narrowed to.
    assert json.loads(written.read_text())["diagnosis"]["input"] == 40
    assert snapshot["tokens_input"] == 40
