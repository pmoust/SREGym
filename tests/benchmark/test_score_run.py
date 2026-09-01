"""Scoring a run.

The point of these is the absent-vs-false distinction. Every other number here
is arithmetic; that one is a judgement that changes the headline by 15 points,
and the repo's own visualizer applies the strict reading silently.
"""

import csv
import importlib.util
import sys
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "score_run",
    Path(__file__).resolve().parents[2] / "scripts" / "benchmark" / "score_run.py",
)
score_run = importlib.util.module_from_spec(_SPEC)
sys.modules["score_run"] = score_run
_SPEC.loader.exec_module(score_run)

_COLUMNS = [
    "problem_id",
    "attempt",
    "Diagnosis.success",
    "Mitigation.success",
    "TTL",
    "TTM",
    "timed_out",
]


def _run(tmp_path, rows):
    """An archived run laid out the way the harness writes one."""
    for i, row in enumerate(rows):
        d = tmp_path / "slots" / "0" / "results" / "t0" / f"run_{i}"
        d.mkdir(parents=True)
        with (d / "brain_go_results.csv").open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=_COLUMNS)
            w.writeheader()
            w.writerow({c: row.get(c, "") for c in _COLUMNS})

    return tmp_path


def _row(pid, attempt=1, diag="", mitig="", ttl="", ttm="", timed_out=""):
    return {
        "problem_id": pid,
        "attempt": str(attempt),
        "Diagnosis.success": diag,
        "Mitigation.success": mitig,
        "TTL": ttl,
        "TTM": ttm,
        "timed_out": timed_out,
    }


def test_absent_mitigation_is_not_counted_as_a_failure_in_the_graded_rate(tmp_path):
    """The whole reason this script exists.

    One correct, one wrong, one never graded because the agent was killed. The
    strict rate calls that a 1-in-3 agent; the graded rate calls it 1-in-2. Both
    are printed, neither is chosen here.
    """
    rows = [
        _row("a", diag="True", mitig="True", ttl="100", ttm="200"),
        _row("b", diag="True", mitig="False", ttl="100", ttm="200"),
        _row("c", diag="False", timed_out="True", ttl="1800"),
    ]
    s = score_run.score(score_run.load(_run(tmp_path, rows)))

    assert s["mitigation"]["strict_pct"] == 33.3
    assert s["mitigation"]["graded_pct"] == 50.0
    assert s["mitigation"]["absent"] == 1
    assert s["mitigation"]["absent_all_censored"] is True


def test_an_absent_verdict_with_no_timeout_is_flagged_as_unexplained(tmp_path):
    """A run where mitigation simply did not run looks identical in the numbers
    to one where it failed. The coupling check is what tells them apart."""
    rows = [
        _row("a", diag="True", mitig="True", ttl="100", ttm="200"),
        _row("b", diag="True", ttl="100"),  # no verdict, no timeout
    ]
    s = score_run.score(score_run.load(_run(tmp_path, rows)))

    assert s["mitigation"]["absent"] == 1
    assert s["mitigation"]["absent_all_censored"] is False


def test_a_rate_with_no_denominator_is_none_not_zero(tmp_path):
    """0/0 is unknown. Reporting it as 0.0% claims the agent failed at
    something it was never asked to do, and one archived run is exactly this."""
    rows = [_row("a", diag="False", ttl="100")]
    s = score_run.score(score_run.load(_run(tmp_path, rows)))

    assert s["mitigation"]["graded_pct"] is None
    assert s["mitigation"]["strict_pct"] == 0.0
    assert "n/a" in score_run.render(s, None)


def test_e2e_requires_both_verdicts(tmp_path):
    """Diagnosis right and mitigation absent is not an end-to-end success."""
    rows = [
        _row("a", diag="True", mitig="True", ttl="100", ttm="200"),
        _row("b", diag="True", timed_out="True", ttl="1800"),
    ]
    s = score_run.score(score_run.load(_run(tmp_path, rows)))

    assert s["e2e"]["correct"] == 1
    assert s["diagnosis"]["correct"] == 2


def test_timings_exclude_censored_attempts(tmp_path):
    """A censored attempt's TTL is the cap. Averaging it in measures the cap."""
    rows = [
        _row("a", diag="True", mitig="True", ttl="100", ttm="300"),
        _row("b", diag="True", mitig="True", ttl="200", ttm="400"),
        _row("c", diag="False", timed_out="True", ttl="1800"),
    ]
    s = score_run.score(score_run.load(_run(tmp_path, rows)))

    assert s["ttd_seconds"]["n"] == 2
    assert s["ttd_seconds"]["mean"] == 150.0
    # TTM is cumulative, so the mitigation phase is the difference.
    assert s["mitigation_phase_seconds"]["mean"] == 200.0


def test_rollup_and_all_results_files_are_not_counted(tmp_path):
    """Only files under a run_N directory are per-attempt rows. Counting the
    siblings inflates the attempt count by about 2.5x, which is the same defect
    the progress output had."""
    d = tmp_path / "slots" / "0" / "results" / "t0"
    (d / "run_0").mkdir(parents=True)
    for path in (
        d / "run_0" / "brain_go_results.csv",  # counted
        d / "a_brain_go_results.csv",  # per-problem rollup, not counted
        d / "brain_go_ALL_results.csv",  # slot rollup, not counted
    ):
        with path.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=_COLUMNS)
            w.writeheader()
            w.writerow({c: "" for c in _COLUMNS} | {"problem_id": "a", "attempt": "1"})

    assert len(score_run.load(tmp_path)) == 1


def test_rows_without_a_problem_id_are_dropped(tmp_path):
    """A ragged or half-written row cannot be attributed to a problem."""
    rows = [_row("a", diag="True", mitig="True", ttl="1", ttm="2"), _row("", diag="True")]
    assert len(score_run.load(_run(tmp_path, rows))) == 1


def test_the_archived_run_reproduces_its_published_figures():
    """Guards the scorer against the numbers already quoted elsewhere.

    run-20260829-1325 is the only on-protocol run of the first pair and was
    reported as Diag 47.6%, Mitig 52.4%, E2E 47.6%. If this file ever disagrees,
    one of the two is wrong and it matters which.
    """
    archive = Path("/Users/nofire/nofire-repos/brain-go/sregym-results/run-20260829-1325-a4d231c1")
    if not archive.is_dir():
        return  # the archive is not part of this repo

    s = score_run.score(score_run.load(archive))
    assert s["attempts"] == 21
    assert s["diagnosis"]["strict_pct"] == 47.6
    assert s["mitigation"]["strict_pct"] == 52.4
    assert s["e2e"]["strict_pct"] == 47.6
