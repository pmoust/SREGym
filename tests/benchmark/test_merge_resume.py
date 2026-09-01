"""Merging a slot's prior result CSVs for main.py --resume.

The failure this guards against is silent. csv.DictReader does not raise on a
header mismatch and main.py wraps the resume block in `except Exception`, so a
bad merge shows up as a slot that re-runs work it already did, which looks
exactly like a normal run.
"""

import csv
import importlib.util
import os
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "merge_resume",
    Path(__file__).resolve().parents[2] / "scripts" / "benchmark" / "merge_resume.py",
)
merge_resume = importlib.util.module_from_spec(_SPEC)
sys.modules["merge_resume"] = merge_resume
_SPEC.loader.exec_module(merge_resume)


def _slot(tmp_path, *files):
    """A slot dir holding one results/<dir>/x_ALL_results.csv per entry, oldest first."""
    for i, text in enumerate(files):
        d = tmp_path / "results" / f"t{i}"
        d.mkdir(parents=True)
        path = d / "x_ALL_results.csv"
        path.write_text(text)
        # Explicit mtimes: the files are written within the same second, and
        # ordering is the whole basis of newest-wins.
        os.utime(path, (1000 + i, 1000 + i))
    return tmp_path


def _rows(path):
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def _header(path):
    with open(path, newline="") as fh:
        return next(csv.reader(fh))


def test_no_prior_results_returns_none(tmp_path):
    """The first boot of a slot. Must not invent an empty CSV: main.py would
    read it as zero completed problems either way, but an empty --resume file
    is indistinguishable from a merge that silently dropped everything."""
    assert merge_resume.merge(tmp_path) is None


def test_header_is_the_union_when_neither_file_is_a_superset(tmp_path):
    """The archived pair that motivates this: both 13 columns, incomparable.

    One carries agent_timeout_seconds and timed_out, the other Mitigation.success
    and TTM. Neither header can serve as the merged one.
    """
    a = "problem_id,attempt,agent_timeout_seconds,timed_out\np1,0,1800,False\n"
    b = "problem_id,attempt,Mitigation.success,TTM\np2,0,True,42\n"
    merged = merge_resume.merge(_slot(tmp_path, a, b))

    assert _header(merged) == [
        "Mitigation.success",
        "TTM",
        "agent_timeout_seconds",
        "attempt",
        "problem_id",
        "timed_out",
    ]
    rows = _rows(merged)
    assert {r["problem_id"] for r in rows} == {"p1", "p2"}
    # A column the source row did not have is empty, not the next column along.
    assert next(r for r in rows if r["problem_id"] == "p1")["TTM"] == ""
    assert next(r for r in rows if r["problem_id"] == "p2")["timed_out"] == ""


def test_concatenation_would_have_shifted_columns(tmp_path):
    """Pins the actual defect, by reproducing what the old script produced.

    Not a test of new code. It fails if someone concludes the widths are close
    enough to append, which is exactly the reasoning the old script carried.
    """
    a = "problem_id,attempt,Diagnosis.success\np1,0,True\n"
    b = "Diagnosis.success,Mitigation.success,attempt,problem_id\nTrue,True,0,p2\n"
    slot = _slot(tmp_path, a, b)

    naive = "".join([a, b.split("\n", 1)[1]])
    shifted = list(csv.DictReader(naive.splitlines(True)))
    assert shifted[1]["problem_id"] == "True"  # another column's value

    merged = _rows(merge_resume.merge(slot))
    assert {r["problem_id"] for r in merged} == {"p1", "p2"}


def test_duplicate_attempt_resolves_to_the_newest_file(tmp_path):
    """A restarted slot rewrites earlier problems, so files overlap.

    Keeping both would inflate main.py's per-problem row count and mark a
    multi-attempt problem complete after one attempt.
    """
    old = "problem_id,attempt,Diagnosis.success\np1,0,False\n"
    new = "problem_id,attempt,Diagnosis.success,TTM\np1,0,True,42\np2,0,True,9\n"
    rows = _rows(merge_resume.merge(_slot(tmp_path, old, new)))

    assert len(rows) == 2
    p1 = next(r for r in rows if r["problem_id"] == "p1")
    assert p1["Diagnosis.success"] == "True"
    assert p1["TTM"] == "42"


def test_same_problem_different_attempts_are_both_kept(tmp_path):
    """The dedupe key is (problem_id, attempt), not problem_id.

    Deduping on problem_id alone would collapse an 8-attempt problem to one row
    and re-run seven attempts that had already been paid for.
    """
    a = "problem_id,attempt,Diagnosis.success\np1,0,True\np1,1,False\n"
    b = "problem_id,attempt,Diagnosis.success\np1,2,True\n"
    rows = _rows(merge_resume.merge(_slot(tmp_path, a, b)))

    assert sorted(r["attempt"] for r in rows) == ["0", "1", "2"]


def test_free_text_with_commas_and_newlines_survives(tmp_path):
    """Diagnosis.reasoning is judge prose. Line-based merging splits it."""
    reasoning = "The pod failed, then restarted.\nSecond line, with a comma."
    a = f'problem_id,attempt,Diagnosis.reasoning\np1,0,"{reasoning}"\n'
    rows = _rows(merge_resume.merge(_slot(tmp_path, a)))

    assert len(rows) == 1
    assert rows[0]["Diagnosis.reasoning"] == reasoning


def test_unkeyed_rows_are_dropped_not_counted(tmp_path):
    """A row with no problem_id cannot be deduped.

    Dropping it costs one re-run. Keeping it could add a phantom row to some
    problem's attempt count and skip work that was never done.
    """
    a = "problem_id,attempt,Diagnosis.success\n,0,True\np1,0,True\n"
    rows = _rows(merge_resume.merge(_slot(tmp_path, a)))

    assert [r["problem_id"] for r in rows] == ["p1"]


def test_ragged_row_does_not_leak_a_restkey_column(tmp_path):
    """DictReader parks surplus fields under None. Writing that back out would
    put a literal "None" column in the merged header."""
    a = "problem_id,attempt\np1,0,surplus\n"
    merged = merge_resume.merge(_slot(tmp_path, a))

    assert "None" not in _header(merged)
    assert _header(merged) == ["attempt", "problem_id"]


@pytest.mark.parametrize("n_attempts,expected_complete", [(1, True), (2, False)])
def test_merged_output_drives_main_py_completion_the_way_it_should(tmp_path, n_attempts, expected_complete):
    """The end the merge exists for: main.py:266 counts rows per problem_id and
    calls a problem done at >= n_attempts. Reproduced here so a change to the
    merge that breaks that count fails in this file rather than on an instance."""
    from collections import Counter

    old = "problem_id,attempt,Diagnosis.success\np1,0,False\n"
    new = "problem_id,attempt,Diagnosis.success\np1,0,True\n"
    rows = _rows(merge_resume.merge(_slot(tmp_path, old, new)))

    counts = Counter(r["problem_id"] for r in rows)
    complete = {pid for pid, c in counts.items() if c >= n_attempts}
    assert ("p1" in complete) is expected_complete


def test_a_non_utf8_byte_does_not_lose_the_whole_shard(tmp_path):
    """The judge's prose has carried non-UTF-8 bytes before.

    Strict decoding raised out of the process. The call site is
    `resume_csv=$(merge_resume.py ... || true)`, which keeps stdout and drops
    the exit code, so the visible result was an empty resume and a slot that
    re-ran everything it had already finished.
    """
    d = tmp_path / "results" / "t0"
    d.mkdir(parents=True)
    (d / "x_ALL_results.csv").write_bytes(b'problem_id,attempt,Diagnosis.reasoning\np1,1,"caf\xe9 \xff failed"\n')

    rows = _rows(merge_resume.merge(tmp_path))

    assert [r["problem_id"] for r in rows] == ["p1"]
    assert rows[0]["attempt"] == "1"  # still keyed correctly


def test_one_unreadable_file_does_not_discard_the_others(tmp_path):
    """Aborting on the first bad file throws away every later file's problems."""
    good = "problem_id,attempt,Diagnosis.success\np1,1,True\n"
    _slot(tmp_path, good, "problem_id,attempt\np2,1\n")
    bad = tmp_path / "results" / "t1" / "x_ALL_results.csv"
    bad.chmod(0o000)
    try:
        rows = _rows(merge_resume.merge(tmp_path))
    finally:
        bad.chmod(0o644)

    assert [r["problem_id"] for r in rows] == ["p1"]  # the readable one survives


def test_returns_none_when_nothing_is_recoverable(tmp_path):
    """An empty resume CSV is worse than none.

    main.py would load it, count zero completed problems, and log a successful
    resume. Returning None makes the caller omit --resume, which is the honest
    description of what can be offered.
    """
    _slot(tmp_path, "problem_id,attempt\np1,1\n")
    bad = tmp_path / "results" / "t0" / "x_ALL_results.csv"
    bad.chmod(0o000)
    try:
        assert merge_resume.merge(tmp_path) is None
    finally:
        bad.chmod(0o644)


def test_a_repeated_header_row_is_not_kept_as_data(tmp_path):
    """DictReader hands a mid-file header line back as an ordinary row."""
    a = "problem_id,attempt,x\np1,1,a\nproblem_id,attempt,x\np2,1,b\n"
    rows = _rows(merge_resume.merge(_slot(tmp_path, a)))

    assert sorted(r["problem_id"] for r in rows) == ["p1", "p2"]
    assert "problem_id" not in [r["problem_id"] for r in rows]
