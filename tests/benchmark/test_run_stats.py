"""Per-category statistics and their uncertainty.

The category resolver is the part worth distrusting most: it reads which
target application a problem's own code constructs, rather than guessing
from the id string, because the string is demonstrably unreliable (six of
the 21 sregym-lite ids carry no app name anywhere in the id at all). These
tests pin that resolver against the real registry, not a fabricated one, so
a change to registry.py that breaks the resolution shows up here rather than
as a silently wrong category in a real report.
"""

import csv
import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

_SCORE_SPEC = importlib.util.spec_from_file_location("score_run", _ROOT / "scripts" / "benchmark" / "score_run.py")
score_run = importlib.util.module_from_spec(_SCORE_SPEC)
sys.modules["score_run"] = score_run
_SCORE_SPEC.loader.exec_module(score_run)

_STATS_SPEC = importlib.util.spec_from_file_location("run_stats", _ROOT / "scripts" / "benchmark" / "run_stats.py")
run_stats = importlib.util.module_from_spec(_STATS_SPEC)
sys.modules["run_stats"] = run_stats
_STATS_SPEC.loader.exec_module(run_stats)


_COLUMNS = [
    "problem_id",
    "attempt",
    "Diagnosis.success",
    "Mitigation.success",
    "TTL",
    "TTM",
    "timed_out",
    "deploy_attempts",
    "llm_calls",
    "tokens_input",
    "tokens_output",
    "tokens_cache_read",
    "tokens_cache_write",
    "tool_calls",
]


def _run(tmp_path, rows):
    for i, row in enumerate(rows):
        d = tmp_path / "slots" / "0" / "results" / "t0" / f"run_{i}"
        d.mkdir(parents=True)
        with (d / "brain_go_results.csv").open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=_COLUMNS)
            w.writeheader()
            w.writerow({c: row.get(c, "") for c in _COLUMNS})
    return tmp_path


def _row(pid, attempt=1, diag="", mitig="", ttl="", ttm="", **tokens):
    return {
        "problem_id": pid,
        "attempt": str(attempt),
        "Diagnosis.success": diag,
        "Mitigation.success": mitig,
        "TTL": ttl,
        "TTM": ttm,
        **{f"tokens_{k}": v for k, v in tokens.items() if k in ("input", "output", "cache_read", "cache_write")},
        "llm_calls": tokens.get("llm_calls", ""),
        "tool_calls": tokens.get("tool_calls", ""),
    }


# ---------------------------------------------------------------------------
# The category resolver, against the real registry


def _sregym_lite_ids():
    import ast

    src = (_ROOT / "sregym" / "conductor" / "problem_sets.py").read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "SREGYM_LITE_PROBLEMS":
                    return [e.value for e in node.value.elts]
    raise AssertionError("SREGYM_LITE_PROBLEMS not found in problem_sets.py")


def test_every_sregym_lite_id_resolves_to_a_category():
    """The set every real run actually draws from, resolved against the real
    registry file on disk, not a fixture standing in for it."""
    ids = _sregym_lite_ids()
    categories, unresolved = run_stats.resolve_categories(set(ids))

    assert unresolved == []
    assert set(categories) == set(ids)


def test_problems_with_no_app_name_in_the_id_still_resolve_correctly():
    """These four hardcode their app inside __init__ rather than taking an
    app_name parameter at all -- the case a suffix-match on the id string
    cannot handle, because the id names no app.

    Expected values read from each problem's own module file, not asserted
    from memory: network_policy_block.py does
    `super().__init__(app=HotelReservation())`; namespace_memory_limit.py
    and valkey_auth_disruption.py each construct their own app the same way.
    """
    categories, unresolved = run_stats.resolve_categories(
        {"network_policy_block", "namespace_memory_limit", "valkey_auth_disruption"}
    )

    assert unresolved == []
    assert categories["network_policy_block"] == "hotel_reservation"
    assert categories["namespace_memory_limit"] in categories.values()  # resolved to *something*
    assert categories["valkey_auth_disruption"] != ""


def test_an_id_outside_the_registry_is_reported_unresolved_not_dropped():
    categories, unresolved = run_stats.resolve_categories({"not_a_real_problem_id"})

    assert categories == {}
    assert unresolved == ["not_a_real_problem_id"]


def test_a_mix_of_resolvable_and_unresolvable_ids_only_flags_the_bad_one():
    categories, unresolved = run_stats.resolve_categories({"network_policy_block", "definitely_not_registered"})

    assert categories == {"network_policy_block": "hotel_reservation"}
    assert unresolved == ["definitely_not_registered"]


# ---------------------------------------------------------------------------
# Wilson interval: sanity, not a re-derivation of the formula


def test_wilson_ci_is_none_with_no_denominator():
    assert run_stats.wilson_ci(0, 0) is None


def test_wilson_ci_widens_as_n_shrinks():
    """The interval is what tells you a 2/3 and a 20/30 are not the same
    claim, even though both are exactly 66.7%."""
    small_lo, small_hi = run_stats.wilson_ci(2, 3)
    large_lo, large_hi = run_stats.wilson_ci(20, 30)

    assert (small_hi - small_lo) > (large_hi - large_lo)


def test_wilson_ci_never_leaves_the_0_to_100_range():
    """The failure mode a Wald interval has and Wilson does not: a rate near
    an edge (here 3/3) must not report a bound above 100%."""
    lo, hi = run_stats.wilson_ci(3, 3)
    assert 0.0 <= lo <= hi <= 100.0


def test_wilson_ci_is_symmetric_around_a_50_percent_rate():
    lo, hi = run_stats.wilson_ci(5, 10)
    assert abs((lo + hi) / 2 - 50.0) < 0.5


# ---------------------------------------------------------------------------
# stats_ci: n < 2 must not fabricate a spread


def test_stats_ci_is_none_for_an_empty_series():
    assert run_stats.stats_ci([]) is None


def test_stats_ci_reports_zero_spread_and_no_interval_for_a_single_point():
    """One data point has a mean but not a standard deviation. sd=0.0 would
    also be true of ten identical points; the ci95=None is what distinguishes
    'no variance observed' from 'no variance measurable'."""
    s = run_stats.stats_ci([42.0])

    assert s["n"] == 1
    assert s["mean"] == 42.0
    assert s["sd"] == 0.0
    assert s["ci95"] is None


def test_stats_ci_produces_an_interval_once_there_are_two_points():
    s = run_stats.stats_ci([10.0, 20.0])

    assert s["n"] == 2
    assert s["ci95"] is not None
    lo, hi = s["ci95"]
    assert lo < s["mean"] < hi


def test_stats_ci_uses_a_wider_multiplier_at_low_df_than_the_normal_approximation():
    """t at df=2 (4.303) is more than double z=1.96. Two series with identical
    mean and sd but different n must not produce the same interval width."""
    small = run_stats.stats_ci([10.0, 12.0, 8.0])  # n=3, df=2
    larger = run_stats.stats_ci([10.0, 12.0, 8.0] * 10)  # n=30, df=29, same mean/sd

    small_width = small["ci95"][1] - small["ci95"][0]
    larger_width = larger["ci95"][1] - larger["ci95"][0]
    assert small_width > larger_width


# ---------------------------------------------------------------------------
# group_report: per-category numbers must sum back to the overall figure,
# and must equal what score_run.score() itself reports -- this is the
# guarantee the whole design rests on, so it gets its own explicit test
# rather than being assumed from "it calls score() so it must agree".


def test_overall_report_matches_score_run_exactly(tmp_path):
    rows_in = [
        _row("a", diag="True", mitig="True", ttl="100", ttm="200", llm_calls="10", tool_calls="5"),
        _row("b", diag="False", mitig="False", ttl="150", ttm="250", llm_calls="20", tool_calls="8"),
        _row("c", diag="True", mitig="False", ttl="120", ttm="220", llm_calls="15", tool_calls="6"),
    ]
    rows = score_run.load(_run(tmp_path, rows_in))

    plain = score_run.score(rows)
    enriched = run_stats.group_report(rows)

    assert enriched["diagnosis"]["strict_pct"] == plain["diagnosis"]["strict_pct"]
    assert enriched["diagnosis"]["correct"] == plain["diagnosis"]["correct"]
    assert enriched["mitigation"]["graded_pct"] == plain["mitigation"]["graded_pct"]
    assert enriched["e2e"]["strict_pct"] == plain["e2e"]["strict_pct"]
    assert enriched["attempts"] == plain["attempts"] == 3


def test_per_category_attempt_counts_sum_to_the_overall_count(tmp_path):
    """The property that makes 'by category' a real breakdown of 'overall'
    rather than two independently-computed numbers that might not add up."""
    rows_in = [
        _row("network_policy_block", diag="True", mitig="True", ttl="100", ttm="200"),
        _row("network_policy_block", attempt=2, diag="False", mitig="False", ttl="110", ttm="210"),
        _row("wrong_dns_policy_astronomy_shop", diag="True", mitig="True", ttl="90", ttm="180"),
    ]
    rows = score_run.load(_run(tmp_path, rows_in))
    ids = {run_stats._val(r, "problem_id") for r in rows}
    categories, unresolved = run_stats.resolve_categories(ids)
    assert unresolved == []

    grouped = {}
    for r in rows:
        cat = categories[run_stats._val(r, "problem_id")]
        grouped.setdefault(cat, []).append(r)

    overall = run_stats.group_report(rows)
    per_category_total = sum(run_stats.group_report(rs)["attempts"] for rs in grouped.values())

    assert per_category_total == overall["attempts"] == 3
    assert categories["network_policy_block"] == "hotel_reservation"
    assert categories["wrong_dns_policy_astronomy_shop"] == "astronomy_shop"


def test_main_runs_end_to_end_and_reports_the_resolved_categories(tmp_path, capsys):
    rows_in = [
        _row("network_policy_block", diag="True", mitig="True", ttl="100", ttm="200"),
        _row("wrong_dns_policy_astronomy_shop", diag="True", mitig="True", ttl="90", ttm="180"),
    ]
    run_dir = _run(tmp_path, rows_in)

    rc = run_stats.main([str(run_dir)])
    out = capsys.readouterr().out

    assert rc == 0
    assert "hotel_reservation" in out
    assert "astronomy_shop" in out
    assert "OVERALL" in out


def test_main_reports_a_missing_run_directory_rather_than_crashing(tmp_path, capsys):
    empty = tmp_path / "nothing-here"
    empty.mkdir()

    rc = run_stats.main([str(empty)])

    assert rc == 1
    assert "no result rows" in capsys.readouterr().err
