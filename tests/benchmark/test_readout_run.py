"""readout_run.py's own parsing: the inconclusive/Observed-signals split, the
shard-grouped aggregation for an unlabelled or signal-labelled RCI counter,
and the port-forward restart count.

score_run_test.py already covers score_run.py's own functions this file
imports (load(), _composite(), _log_roots(), _token_files()); these tests are
for the logic readout_run.py adds on top.
"""

import csv
import importlib.util
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parents[2] / "scripts" / "benchmark"

_SCORE_SPEC = importlib.util.spec_from_file_location("score_run", _HERE / "score_run.py")
score_run = importlib.util.module_from_spec(_SCORE_SPEC)
sys.modules["score_run"] = score_run
_SCORE_SPEC.loader.exec_module(score_run)

_READOUT_SPEC = importlib.util.spec_from_file_location("readout_run", _HERE / "readout_run.py")
readout_run = importlib.util.module_from_spec(_READOUT_SPEC)
sys.modules["readout_run"] = readout_run
_READOUT_SPEC.loader.exec_module(readout_run)

_COLUMNS = ["problem_id", "attempt", "Diagnosis.submission", "Diagnosis.composite_score"]

_INCONCLUSIVE = "Investigation inconclusive: none of the evaluated hypotheses could be confirmed. Nothing panned out."
_OBSERVED = "\n\n## Observed signals\n\n- something noted but unexplained\n"


def _row(pid, attempt=1, submission="", composite=""):
    return {
        "problem_id": pid,
        "attempt": str(attempt),
        "Diagnosis.submission": submission,
        "Diagnosis.composite_score": composite,
    }


def _archive(tmp_path, rows, run_id="run-x"):
    """A results tree under sregym-results/<run-id>, the shape score_run.load()
    reads and the shape score_run._log_roots() derives a sregym-run-logs
    sibling from.
    """
    results = tmp_path / "sregym-results" / run_id
    for i, row in enumerate(rows):
        d = results / "shard-0" / "slots" / "0" / "results" / "t0" / f"run_{i}"
        d.mkdir(parents=True)
        with (d / "brain_go_results.csv").open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=_COLUMNS)
            w.writeheader()
            w.writerow({c: row.get(c, "") for c in _COLUMNS})

    return results


def _logs_dir(results_dir, shard="shard-0"):
    logs = results_dir.parent.parent / "sregym-run-logs" / results_dir.name / shard
    logs.mkdir(parents=True, exist_ok=True)
    return logs


# --- inconclusive / Observed signals / judge composite (items 1, 2, 6) ---


def test_inconclusive_count_matches_the_exact_needle():
    """A report that merely mentions "inconclusive" elsewhere does not count;
    only the fixed lead sentence the harness's own submission text carries
    does."""
    rows = [
        _row("a", submission=_INCONCLUSIVE, composite="0.0"),
        _row("b", submission="Confirmed: the root cause is a bad rollout.", composite="1.0"),
        _row("c", submission="The investigation ran into trouble and stalled."),
    ]
    out = readout_run.inconclusive(rows)

    assert out["attempts"] == 3
    assert out["inconclusive"] == 1


def test_observed_signals_is_counted_within_the_inconclusive_subset_only():
    """An Observed signals section on a non-inconclusive report (should not
    happen, brain-go only renders the block on the inconclusive path) must
    not inflate the numerator against the wrong denominator."""
    rows = [
        _row("a", submission=_INCONCLUSIVE + _OBSERVED, composite="0.1"),
        _row("b", submission=_INCONCLUSIVE, composite="0.2"),
        _row("c", submission="Confirmed." + _OBSERVED, composite="1.0"),
    ]
    out = readout_run.inconclusive(rows)

    assert out["inconclusive"] == 2
    assert out["observed_signals"] == 1


def test_judge_mean_composite_is_restricted_to_the_inconclusive_subset():
    rows = [
        _row("a", submission=_INCONCLUSIVE, composite="0.4"),
        _row("b", submission=_INCONCLUSIVE, composite="0.6"),
        _row("c", submission="Confirmed.", composite="1.0"),
    ]
    out = readout_run.inconclusive(rows)

    assert out["judge_scored"] == 2
    assert out["composite_mean"] == 0.5


def test_an_unscored_inconclusive_attempt_is_excluded_not_zeroed():
    """No composite cell is not a composite of 0.0 -- score_run.py already
    draws this distinction for the run at large; the inconclusive subset
    must not lose it."""
    rows = [
        _row("a", submission=_INCONCLUSIVE, composite=""),
        _row("b", submission=_INCONCLUSIVE, composite="0.8"),
    ]
    out = readout_run.inconclusive(rows)

    assert out["judge_scored"] == 1
    assert out["composite_mean"] == 0.8


def test_no_scored_inconclusive_attempt_reports_none_not_zero():
    rows = [_row("a", submission=_INCONCLUSIVE, composite="")]
    out = readout_run.inconclusive(rows)

    assert out["judge_scored"] == 0
    assert out["composite_mean"] is None


# --- shard-grouped aggregation (items 4 and 5's breaker-trip half) ---


def test_breaker_trips_sum_the_max_of_each_shard(tmp_path):
    """The case this aggregation exists for: two boxes, each scraped more
    than once, an unlabelled series shared by every process that registers
    it. Within a shard the counter only grows, so max is exact; across
    shards it is two different processes, so those maxima add."""
    results = _archive(tmp_path, [_row("a")])
    shard0 = _logs_dir(results, "shard-0")
    shard1 = _logs_dir(results, "shard-1")
    (shard0 / "sregym-tokens.prom").write_text("custom_mcp_breaker_trips_total 0\ncustom_mcp_breaker_trips_total 2\n")
    (shard1 / "sregym-tokens.prom").write_text("custom_mcp_breaker_trips_total 3\n")

    totals = readout_run.grouped_totals(results, "custom_mcp_breaker_trips_total")

    assert totals[""] == 5.0  # max(0, 2) + 3, not 0 + 2 + 3


def test_an_earlier_per_attempt_scrape_does_not_inflate_its_own_shards_total(tmp_path):
    """A per-attempt tokens-before/after.prom in the results tree reads the
    same box's counter earlier in the run. Folding it into the same shard
    group must not add to what the shard's own final scrape already caught."""
    results = _archive(tmp_path, [_row("a")])
    shard0 = _logs_dir(results, "shard-0")
    (shard0 / "sregym-tokens.prom").write_text("custom_mcp_breaker_trips_total 2\n")
    attempt_dir = results / "shard-0" / "slots" / "0" / "results" / "t0" / "run_0"
    (attempt_dir / "tokens-before.prom").write_text("custom_mcp_breaker_trips_total 0\n")
    (attempt_dir / "tokens-after.prom").write_text("custom_mcp_breaker_trips_total 1\n")

    totals = readout_run.grouped_totals(results, "custom_mcp_breaker_trips_total")

    assert totals[""] == 2.0  # the shard's own high-water mark, not 2 + 1


def test_a_metric_absent_from_every_file_returns_empty():
    """{}, not {"": 0.0}: the counter not existing in this run's build reads
    differently from it existing and firing zero times."""
    assert readout_run.grouped_totals(Path("/nonexistent-for-this-test"), "rci_signal_agent_dispatched_total") == {}


def test_signal_labelled_counter_is_grouped_per_signal(tmp_path):
    results = _archive(tmp_path, [_row("a")])
    shard0 = _logs_dir(results, "shard-0")
    (shard0 / "sregym-tokens.prom").write_text(
        'rci_signal_agent_dispatched_total{signal="metrics"} 3\nrci_signal_agent_dispatched_total{signal="logs"} 1\n'
    )

    rci = readout_run.new_rci_counters(results)

    assert rci["signal_agent_dispatched"] == {"logs": 1, "metrics": 3}


def test_a_metric_name_that_is_a_prefix_of_another_is_not_conflated():
    """rci_hypotheses_generated_count must not be read as a sample of
    rci_hypotheses_generated, and vice versa."""
    text = 'rci_hypotheses_generated_count 5\nrci_hypotheses_generated_bucket{le="1"} 2\n'
    samples = list(readout_run._parse_prom_lines(text, "rci_hypotheses_generated"))

    assert samples == []


# --- port-forward restarts (item 5's other half) ---


def test_restart_count_is_the_number_of_restart_events(tmp_path):
    results = _archive(tmp_path, [_row("a")])
    logs_root = results.parent.parent / "sregym-run-logs" / results.name
    slot0 = logs_root / "shard-0" / "slots" / "0"
    slot0.mkdir(parents=True)
    (slot0 / "mcp_pf_restarts.jsonl").write_text(
        '{"event":"restart","old_pid":"1","new_pid":"2"}\n{"event":"restart","old_pid":"2","new_pid":"3"}\n'
    )
    slot1 = logs_root / "shard-1" / "slots" / "2"
    slot1.mkdir(parents=True)
    (slot1 / "mcp_pf_restarts.jsonl").write_text('{"event":"restart","old_pid":"9","new_pid":"10"}\n')

    out = readout_run.restarts(results)

    assert out["restart"] == 3
    assert out["files"] == 2


def test_a_malformed_restart_line_costs_itself_not_the_count(tmp_path):
    results = _archive(tmp_path, [_row("a")])
    logs_root = results.parent.parent / "sregym-run-logs" / results.name
    slot0 = logs_root / "shard-0" / "slots" / "0"
    slot0.mkdir(parents=True)
    # Three shapes of bad line, not one. "not json" fails to parse, but 42
    # and [1] parse fine and hand back an int and a list, neither of which
    # has .get: catching only JSONDecodeError let those two through to an
    # AttributeError that took the whole count down.
    (slot0 / "mcp_pf_restarts.jsonl").write_text(
        '{"event":"restart","old_pid":"1","new_pid":"2"}\n'
        "not json\n"
        "42\n"
        "[1]\n"
        "null\n"
        '{"event":"restart","old_pid":"2","new_pid":"3"}\n'
    )

    out = readout_run.restarts(results)

    assert out["restart"] == 2


def test_no_restart_file_is_none_not_zero():
    """Not archived and archived-with-zero-restarts must read differently.
    archive_run.sh syncs mcp_pf_restarts.jsonl now, but an archive taken
    before that include was added does not have it, and a real run with real
    restarts must not read as zero."""
    assert readout_run.restarts(Path("/nonexistent-for-this-test")) is None


def test_a_labelled_downgrade_counter_reads_as_absent_not_a_crash(tmp_path):
    """rci_symptom_improved_downgrade_total is unlabelled today. If it ever
    gains a label, the bare series this reads disappears, and that must
    report as absent rather than take the readout down with a KeyError."""
    results = _archive(tmp_path, [_row("a")])
    logs_root = results.parent.parent / "sregym-run-logs" / results.name
    shard = logs_root / "shard-0"
    shard.mkdir(parents=True)
    (shard / "sregym-tokens.prom").write_text(
        'rci_symptom_improved_downgrade_total{reason="x"} 3\n'
    )

    out = readout_run.new_rci_counters(results)

    assert out["symptom_improved_downgrade"] == 0


def test_the_same_restart_file_reached_by_two_roots_counts_once(tmp_path):
    """_log_roots() can return the same directory twice under different
    unresolved paths (a symlinked temp root is the common case); dedup must
    be on the resolved path, not the string form, or every restart in that
    file is counted twice."""
    real = tmp_path / "real"
    real.mkdir()
    results = _archive(real, [_row("a")])
    logs_root = results.parent.parent / "sregym-run-logs" / results.name
    slot0 = logs_root / "shard-0" / "slots" / "0"
    slot0.mkdir(parents=True)
    (slot0 / "mcp_pf_restarts.jsonl").write_text('{"event":"restart","old_pid":"1","new_pid":"2"}\n')

    alias = tmp_path / "alias"
    alias.symlink_to(real)
    results_via_alias = alias / "sregym-results" / results.name

    out = readout_run.restarts(results_via_alias)

    assert out["restart"] == 1
    assert out["files"] == 1


# --- resolve_run_dir ---


def test_an_existing_directory_is_used_directly(tmp_path):
    d = tmp_path / "somewhere"
    d.mkdir()
    assert readout_run.resolve_run_dir(str(d)) == d


# --- end to end ---


def test_main_reproduces_the_documented_counts_end_to_end(tmp_path, capsys):
    rows = [_row(f"p{i}", submission=_INCONCLUSIVE, composite="0.1") for i in range(2)]
    rows += [_row(f"p{i}", submission="Confirmed.", composite="1.0") for i in range(3)]
    results = _archive(tmp_path, rows)
    shard0 = _logs_dir(results, "shard-0")
    (shard0 / "sregym-tokens.prom").write_text("custom_mcp_breaker_trips_total 1\n")

    rc = readout_run.main(["readout_run.py", str(results), "--json"])
    out = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert out["inconclusive"]["inconclusive"] == 2
    assert out["inconclusive"]["attempts"] == 5
    assert out["contamination"]["breaker_trips"] == 1
