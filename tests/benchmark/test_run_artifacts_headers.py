"""Header stability across a run's per-attempt result CSVs (#39).

finalize_and_publish() writes each attempt's row against whatever fieldnames
main.py's driver had accumulated by that point in the run, so a file
published early in a run is narrower than one published once a later
attempt's oracle has contributed a key of its own. Across the 63 per-attempt
files of one real run this produced nine distinct header shapes, 13 to 19
columns wide, and 11 files with no TTM column at all.

stabilize_run_csv_headers() reconciles every already-published per-attempt
CSV under a run directory to one shared header, called once the run's
attempts are all in.

Fixture headers are written pre-sorted (main.py's own `sorted(...)` order),
matching what finalize_and_publish() actually produces, so a test failure
means a real content difference rather than an incidental column reorder.
"""

import csv
from pathlib import Path

from sregym.run_artifacts import stabilize_run_csv_headers


def _publish(root: Path, agent: str, problem_id: str, attempt: int, text: str) -> Path:
    """A per-attempt CSV at the path finalize_and_publish() actually uses."""
    run_dir = root / agent / problem_id / f"run_{attempt}"
    run_dir.mkdir(parents=True)
    path = run_dir / f"{problem_id}_results.csv"
    path.write_text(text)
    return path


def _header(path: Path) -> list[str]:
    with path.open(newline="") as fh:
        return next(csv.reader(fh))


def _rows(path: Path) -> list[dict]:
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def test_header_stays_identical_across_files_even_when_only_some_rows_produce_a_key(tmp_path):
    """The exact mechanism: one problem's oracle contributes a key another
    attempt's never does, so an early file's header should not stay narrower
    than a later file's."""
    early = _publish(tmp_path, "agent", "p1", 0, "TTL,attempt,problem_id\n12.5,0,p1\n")
    later = _publish(
        tmp_path,
        "agent",
        "p2",
        0,
        "Mitigation.internalTrafficPolicy,TTL,attempt,problem_id\nLocal,9.0,0,p2\n",
    )

    rewritten = stabilize_run_csv_headers(tmp_path)

    assert set(rewritten) == {early}  # later already had the full header
    assert _header(early) == _header(later)
    assert _header(early) == ["Mitigation.internalTrafficPolicy", "TTL", "attempt", "problem_id"]


def test_genuinely_absent_value_is_empty_distinct_from_written_false(tmp_path):
    """Mitigation.success missing (the harness killed the agent before the
    oracle ran) must not read the same as Mitigation.success recorded False
    (the oracle ran and failed)."""
    no_verdict = _publish(tmp_path, "agent", "p1", 0, "attempt,problem_id,timed_out\n0,p1,True\n")
    failed = _publish(tmp_path, "agent", "p2", 0, "Mitigation.success,attempt,problem_id\nFalse,0,p2\n")

    stabilize_run_csv_headers(tmp_path)

    no_verdict_row = _rows(no_verdict)[0]
    failed_row = _rows(failed)[0]
    assert no_verdict_row["Mitigation.success"] == ""  # never ran
    assert failed_row["Mitigation.success"] == "False"  # ran, failed
    assert no_verdict_row["Mitigation.success"] != failed_row["Mitigation.success"]


def test_full_union_lands_in_every_file(tmp_path):
    """Every file ends up with the same header: the union of every column
    that appeared anywhere under the run directory."""
    a = _publish(tmp_path, "agent", "p1", 0, "Diagnosis.success,attempt,problem_id\nTrue,0,p1\n")
    b = _publish(tmp_path, "agent", "p1", 1, "TTM,attempt,problem_id\n42,1,p1\n")
    c = _publish(
        tmp_path,
        "agent",
        "p2",
        0,
        "agent_timeout_seconds,attempt,problem_id,timed_out\n1800,0,p2,True\n",
    )

    stabilize_run_csv_headers(tmp_path)

    expected = ["Diagnosis.success", "TTM", "agent_timeout_seconds", "attempt", "problem_id", "timed_out"]
    for path in (a, b, c):
        assert _header(path) == expected

    # And no data was lost or shifted in the rewrite.
    assert _rows(a)[0]["Diagnosis.success"] == "True"
    assert _rows(a)[0]["TTM"] == ""
    assert _rows(b)[0]["TTM"] == "42"
    assert _rows(c)[0]["timed_out"] == "True"


def test_already_uniform_files_are_left_untouched(tmp_path):
    """A run where every attempt happened to produce the same keys should not
    be rewritten at all."""
    a = _publish(tmp_path, "agent", "p1", 0, "attempt,problem_id\n0,p1\n")
    b = _publish(tmp_path, "agent", "p2", 0, "attempt,problem_id\n0,p2\n")

    assert stabilize_run_csv_headers(tmp_path) == []
    assert _header(a) == ["attempt", "problem_id"]
    assert _header(b) == ["attempt", "problem_id"]


def test_fewer_than_two_files_is_a_no_op(tmp_path):
    """Nothing to reconcile with only one (or zero) published files."""
    _publish(tmp_path, "agent", "p1", 0, "attempt,problem_id\n0,p1\n")

    assert stabilize_run_csv_headers(tmp_path) == []
    assert stabilize_run_csv_headers(tmp_path / "nothing-here") == []


def test_only_the_files_whose_header_actually_changed_are_reported(tmp_path):
    """A caller logging progress should not be told about files that were
    already correct."""
    already_full = _publish(tmp_path, "agent", "p1", 0, "Mitigation.success,attempt,problem_id\nTrue,0,p1\n")
    narrower = _publish(tmp_path, "agent", "p2", 0, "attempt,problem_id\n0,p2\n")

    rewritten = stabilize_run_csv_headers(tmp_path)

    assert rewritten == [narrower]
    assert _header(already_full) == ["Mitigation.success", "attempt", "problem_id"]
