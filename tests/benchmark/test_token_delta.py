"""Per-attempt token accounting, as a delta between two counter scrapes.

brain-go reports LLM usage as cumulative Prometheus counters. The intended
attribution was by thread name (scrape_tokens.sh: "driver.py sets it to
cli:sregym-<problem_id>-<hex8>, so one scrape attributes tokens to problems"),
and that stopped holding when the driver started using the investigation's own
thread, cli:inv-<uuid>. Bracketing an attempt does not depend on a name.

Bracketing alone is not enough either: one brain-go process serves every slot
on a box and its counters are process-wide, so the window holds the other
slots' spend too. account_id is the label that separates them, one account per
slot, and thread_id still is not: it varies inside a single attempt.
"""

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location("token_delta", ROOT / "scripts" / "benchmark" / "token_delta.py")
token_delta = importlib.util.module_from_spec(_SPEC)
sys.modules["token_delta"] = token_delta
_SPEC.loader.exec_module(token_delta)


def _prom(tmp_path, name, lines):
    path = tmp_path / name
    path.write_text("# scrape: at 2026-09-03T07:13:40Z\n" + "\n".join(lines) + "\n")

    return path


def _sample(thread, token_type, value, node="unknown", account="2"):
    return (
        f'llm_tokens_total{{account_id="{account}",model="m",node="{node}",provider="bedrock",'
        f'thread_id="{thread}",token_type="{token_type}",user_id="unknown"}} {value}'
    )


def _calls(thread, value, node="unknown", account="2"):
    return (
        f'llm_calls_total{{account_id="{account}",model="m",node="{node}",provider="bedrock",'
        f'thread_id="{thread}",user_id="unknown"}} {value}'
    )


def _three_slots(tmp_path):
    """One box's scrape pair with three slots on it, one account each.

    Matches the label shape of run-20260908-0739-4fc0249e's
    shard-3/logs/sregym-tokens.prom, which carries account_id 2, 3 and 4 and
    15 distinct thread_id values. Account 2 spends 250 input over two threads
    and makes 3 calls; the file holds 1,150 input and 33 calls in total.
    """
    before = _prom(
        tmp_path,
        "b.prom",
        [
            _sample("cli:inv-aaa", "input", "100"),
            _sample("cli:inv-bbb", "input", "0"),
            _calls("cli:inv-aaa", "2"),
            _sample("cli:inv-ccc", "input", "500", account="3"),
            _calls("cli:inv-ccc", "10", account="3"),
            _sample("cli:inv-ddd", "input", "700", account="4"),
            _calls("cli:inv-ddd", "20", account="4"),
        ],
    )
    after = _prom(
        tmp_path,
        "a.prom",
        [
            _sample("cli:inv-aaa", "input", "300"),
            _sample("cli:inv-bbb", "input", "50"),
            _calls("cli:inv-aaa", "5"),
            _sample("cli:inv-ccc", "input", "900", account="3"),
            _calls("cli:inv-ccc", "20", account="3"),
            _sample("cli:inv-ddd", "input", "1200", account="4"),
            _calls("cli:inv-ddd", "40", account="4"),
        ],
    )

    return before, after


def test_the_delta_is_what_the_attempt_spent(tmp_path):
    before = _prom(tmp_path, "b.prom", [_sample("t1", "input", "100"), _sample("t1", "output", "50")])
    after = _prom(tmp_path, "a.prom", [_sample("t1", "input", "350"), _sample("t1", "output", "220")])

    assert token_delta.delta(before, after) == {
        "calls": 0,
        "input": 250,
        "output": 170,
        "cache_read": 0,
        "cache_write": 0,
    }


def test_scientific_notation_parses():
    """Prometheus renders large counters as 2.05674e+06, and these are large.

    A naive int() on that string raises, and a naive float()-then-truncate on
    the wrong side of the subtraction loses precision. The real scrape from
    run-20260903-0558 carried exactly this form.
    """
    assert True  # covered by the case below, kept as the reason


def test_a_cache_heavy_attempt_is_counted_in_full(tmp_path):
    """Most of a run's tokens are cache reads: 3.7M of 4.3M in the last smoke.

    Counting input+output alone would report 2% of what the attempt consumed.
    """
    before = _prom(tmp_path, "b.prom", [_sample("t1", "cache_read", "5000")])
    after = _prom(tmp_path, "a.prom", [_sample("t1", "cache_read", "2.05674e+06")])

    assert token_delta.delta(before, after)["cache_read"] == 2_051_740


def test_counts_are_summed_across_threads(tmp_path):
    """The delta brackets one attempt, so every increment inside it is that
    attempt's however many threads carried it. Summing means a mid-attempt
    thread change cannot lose counts."""
    before = _prom(tmp_path, "b.prom", [_sample("t1", "input", "10")])
    after = _prom(tmp_path, "a.prom", [_sample("t1", "input", "60"), _sample("t2", "input", "40")])

    assert token_delta.delta(before, after)["input"] == 90


def test_a_counter_that_went_backwards_reads_as_zero_not_negative(tmp_path):
    """A counter resets when the process restarts, and the pre-restart total is
    unrecoverable. A negative would corrupt any sum built from these rows."""
    before = _prom(tmp_path, "b.prom", [_sample("t1", "input", "5000")])
    after = _prom(tmp_path, "a.prom", [_sample("t1", "input", "12")])

    assert token_delta.delta(before, after)["input"] == 0


def test_llm_calls_are_counted_too(tmp_path):
    """Scraped all along and never reported. It answers "was this attempt one
    long turn or ninety short ones", which the token totals cannot."""
    before = _prom(tmp_path, "b.prom", ['llm_calls_total{thread_id="t1"} 10'])
    after = _prom(tmp_path, "a.prom", ['llm_calls_total{thread_id="t1"} 98'])

    assert token_delta.delta(before, after)["calls"] == 88


def test_a_missing_scrape_reads_as_no_spend_rather_than_raising(tmp_path):
    """Pricing a run must never be able to fail one, so every path degrades."""
    after = _prom(tmp_path, "a.prom", [_sample("t1", "input", "500")])

    assert token_delta.delta(tmp_path / "absent.prom", after)["input"] == 500
    assert token_delta.delta(after, tmp_path / "absent.prom")["input"] == 0


def test_unparseable_lines_are_skipped(tmp_path):
    """The scrape file carries comment headers and, on a bad scrape, prose."""
    before = _prom(tmp_path, "b.prom", ["# scrape: tworker unreachable", _sample("t1", "input", "1")])
    after = _prom(
        tmp_path, "a.prom", ["garbage", 'llm_tokens_total{token_type="input"} nan', _sample("t1", "input", "11")]
    )

    assert token_delta.delta(before, after)["input"] == 10


def test_delta_by_node_keeps_nodes_apart_unlike_delta(tmp_path):
    """delta() sums across node into one total; delta_by_node() is the same
    data with node kept apart instead of folded in -- the per-node question
    brain-go#2687 added the label to answer."""
    before = _prom(
        tmp_path,
        "b.prom",
        [_sample("t1", "input", "0", node="hypothesis_gen_tools"), _sample("t1", "input", "0", node="diagnosis")],
    )
    after = _prom(
        tmp_path,
        "a.prom",
        [_sample("t1", "input", "70", node="hypothesis_gen_tools"), _sample("t1", "input", "30", node="diagnosis")],
    )

    assert token_delta.delta(before, after)["input"] == 100
    by_node = token_delta.delta_by_node(before, after)
    assert by_node["hypothesis_gen_tools"]["input"] == 70
    assert by_node["diagnosis"]["input"] == 30


def test_delta_by_node_sums_across_thread_the_same_way_delta_does(tmp_path):
    """Node is the one label kept apart; every other label, thread included,
    still collapses into one figure per node."""
    before = _prom(tmp_path, "b.prom", [_sample("t1", "input", "10", node="diagnosis")])
    after = _prom(
        tmp_path,
        "a.prom",
        [_sample("t1", "input", "60", node="diagnosis"), _sample("t2", "input", "40", node="diagnosis")],
    )

    assert token_delta.delta_by_node(before, after)["diagnosis"]["input"] == 90


def test_delta_by_node_floors_each_node_independently(tmp_path):
    """One node's counter resetting must not corrupt another node's figure --
    the same never-negative floor delta() applies to the total, applied here
    per node."""
    before = _prom(
        tmp_path,
        "b.prom",
        [_sample("t1", "input", "5000", node="diagnosis"), _sample("t1", "input", "10", node="mitigation")],
    )
    after = _prom(
        tmp_path,
        "a.prom",
        [_sample("t1", "input", "12", node="diagnosis"), _sample("t1", "input", "60", node="mitigation")],
    )

    by_node = token_delta.delta_by_node(before, after)
    assert by_node["diagnosis"]["input"] == 0  # went backwards, floored
    assert by_node["mitigation"]["input"] == 50  # unaffected by diagnosis's reset


def test_a_node_absent_from_a_scrape_defaults_to_unknown(tmp_path):
    """A scrape taken before the node label existed, or a sample some other
    caller wrote without it, must not raise or get silently dropped -- same
    fallback brain-go itself uses for an unattributed call."""
    before = _prom(tmp_path, "b.prom", ['llm_tokens_total{token_type="input"} 5'])
    after = _prom(tmp_path, "a.prom", ['llm_tokens_total{token_type="input"} 25'])

    assert token_delta.delta_by_node(before, after)["unknown"]["input"] == 20


def test_delta_by_node_is_reachable_from_the_cli(tmp_path):
    """The same CLI entry point delta() is exposed through, so main.py can
    shell out to it exactly the same way."""
    before = _prom(tmp_path, "b.prom", [_sample("t1", "input", "0", node="diagnosis")])
    after = _prom(tmp_path, "a.prom", [_sample("t1", "input", "15", node="diagnosis")])

    assert token_delta.main(["token_delta.py", "delta-by-node", str(before), str(after)]) == 0


def test_only_the_requested_account_is_counted(tmp_path):
    """Three slots share one brain-go process and its process-wide counters,
    one account each, so an attempt's own figure is one account's.

    The account's spend is spread over two threads and both are counted, which
    is why the filter cannot be thread_id: one attempt carried 15 of them on
    the run this fixture copies.
    """
    before, after = _three_slots(tmp_path)

    assert token_delta.delta(before, after, "2") == {
        "calls": 3,
        "input": 250,
        "output": 0,
        "cache_read": 0,
        "cache_write": 0,
    }


def test_without_an_account_every_slot_on_the_box_is_summed(tmp_path):
    """The single-slot case, and what the per-attempt columns used to report
    for every box: 1,150 input against the one attempt's own 250."""
    before, after = _three_slots(tmp_path)

    assert token_delta.delta(before, after)["input"] == 1_150
    assert token_delta.delta(before, after)["calls"] == 33


def test_the_per_account_deltas_add_up_to_the_unfiltered_one(tmp_path):
    """The filter partitions the samples, so nothing is double counted and
    nothing is dropped. Holds on run-20260908-0739-4fc0249e's shard-3 scrape
    too: 273 + 106 + 263 calls per account against 642 unfiltered."""
    before, after = _three_slots(tmp_path)
    per_account = [token_delta.delta(before, after, account) for account in ("2", "3", "4")]
    unfiltered = token_delta.delta(before, after)

    assert {key: sum(d[key] for d in per_account) for key in unfiltered} == unfiltered


def test_a_sample_with_no_account_label_is_left_out_of_a_filtered_delta(tmp_path):
    """Nothing says an unlabelled sample is this account's, and attributing
    another slot's spend is worse than missing a scrape's own."""
    before = _prom(tmp_path, "b.prom", [_sample("t1", "input", "100"), 'llm_tokens_total{token_type="input"} 5'])
    after = _prom(tmp_path, "a.prom", [_sample("t1", "input", "150"), 'llm_tokens_total{token_type="input"} 55'])

    assert token_delta.delta(before, after, "2")["input"] == 50
    assert token_delta.delta(before, after)["input"] == 100


def test_delta_by_node_counts_one_account_on_a_shared_node(tmp_path):
    """Node does not separate slots: every slot's calls land on the same node
    labels, so token-usage-by-node.json needs the account filter as much as
    the flat total does."""
    before = _prom(
        tmp_path,
        "b.prom",
        [_sample("t1", "input", "0", node="diagnosis"), _sample("t9", "input", "0", node="diagnosis", account="3")],
    )
    after = _prom(
        tmp_path,
        "a.prom",
        [_sample("t1", "input", "30", node="diagnosis"), _sample("t9", "input", "900", node="diagnosis", account="3")],
    )

    assert token_delta.delta_by_node(before, after, "2")["diagnosis"]["input"] == 30
    assert token_delta.delta_by_node(before, after)["diagnosis"]["input"] == 930


def test_a_node_only_another_account_touched_is_absent_from_a_filtered_delta(tmp_path):
    """A node this attempt never used must not appear in its breakdown at
    all, rather than appearing as a row of zeros."""
    before = _prom(tmp_path, "b.prom", [_sample("t9", "input", "0", node="changes", account="3")])
    after = _prom(tmp_path, "a.prom", [_sample("t9", "input", "900", node="changes", account="3")])

    assert token_delta.delta_by_node(before, after, "2") == {}


def test_the_account_reaches_delta_through_the_cli(tmp_path, capsys):
    """main.py shells out rather than importing, so the flag has to survive
    the CLI as well as the function call."""
    before, after = _three_slots(tmp_path)

    assert token_delta.main(["token_delta.py", "delta", str(before), str(after), "--account", "2"]) == 0
    assert json.loads(capsys.readouterr().out)["input"] == 250

    assert token_delta.main(["token_delta.py", "delta-by-node", str(before), str(after), "--account", "2"]) == 0
    assert json.loads(capsys.readouterr().out)["unknown"]["input"] == 250


def test_snapshot_refuses_an_account(tmp_path):
    """One scrape carries every account the process serves, so an --account
    there would filter nothing. Refused with the usage message rather than
    accepted and ignored."""
    assert token_delta.main(["token_delta.py", "snapshot", str(tmp_path / "out.prom"), "--account", "2"]) == 2
    assert not (tmp_path / "out.prom").exists()
