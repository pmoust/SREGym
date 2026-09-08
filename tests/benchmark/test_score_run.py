"""Scoring a run.

The point of these is the absent-vs-false distinction. Every other number here
is arithmetic; that one is a judgement that changes the headline by 15 points,
and the repo's own visualizer applies the strict reading silently.
"""

import csv
import importlib.util
import os
import sys
from pathlib import Path

import pytest

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
    "agent_timeout_seconds",
    "deploy_attempts",
    "llm_calls",
    "tokens_input",
    "tokens_output",
    "tokens_cache_read",
    "tokens_cache_write",
    "tool_calls",
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


def _row(pid, attempt=1, diag="", mitig="", ttl="", ttm="", timed_out="", deploy_attempts="", tokens=None):
    tokens = tokens or {}
    return {
        "problem_id": pid,
        "attempt": str(attempt),
        "Diagnosis.success": diag,
        "Mitigation.success": mitig,
        "TTL": ttl,
        "TTM": ttm,
        "timed_out": timed_out,
        "deploy_attempts": deploy_attempts,
        "llm_calls": tokens.get("llm_calls", ""),
        "tokens_input": tokens.get("input", ""),
        "tokens_output": tokens.get("output", ""),
        "tokens_cache_read": tokens.get("cache_read", ""),
        "tokens_cache_write": tokens.get("cache_write", ""),
        "tool_calls": tokens.get("tool_calls", ""),
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
    archive = (
        Path(os.environ.get("SREGYM_ARCHIVE_DIR", Path.home() / "nofire-repos" / "brain-go" / "sregym-results"))
        / "run-20260829-1325-a4d231c1"
    )
    if not archive.is_dir():
        # skip, not return. A bare return reports as passed, so this test was
        # green on every machine that does not have the archive, including CI,
        # while asserting nothing. Review found it dead everywhere but one
        # laptop, which is worse than not having it.
        pytest.skip(f"archive not present at {archive}; set SREGYM_ARCHIVE_DIR to run this")

    s = score_run.score(score_run.load(archive))
    assert s["attempts"] == 21
    assert s["diagnosis"]["strict_pct"] == 47.6
    assert s["mitigation"]["strict_pct"] == 52.4
    assert s["e2e"]["strict_pct"] == 47.6


def _prom(tmp_path, lines):
    p = tmp_path / "sregym-tokens.prom"
    p.write_text("\n".join(lines) + "\n")
    return tmp_path


def _tok(kind, value, thread, port="9091", node=None):
    node_label = f',node="{node}"' if node else ""

    return (
        f'llm_tokens_total{{provider="bedrock",model="m",token_type="{kind}",'
        f'account_id="1",user_id="1",thread_id="{thread}",job="{port}"{node_label}}} {value}'
    )


def test_tokens_sum_across_threads_rather_than_keeping_the_busiest(tmp_path):
    """The bug that made this metric silently ~35x low.

    llm_tokens_total is labelled by thread_id and the driver sets one per
    problem, so a run has one counter series per problem. Taking max() across
    series keeps the busiest problem and discards every other one.
    """
    d = _prom(
        tmp_path,
        [
            _tok("output", 500, "cli:sregym-a-1"),
            _tok("output", 3000, "cli:sregym-b-2"),
            _tok("input", 100, "cli:sregym-a-1"),
            _tok("input", 200, "cli:sregym-b-2"),
        ],
    )
    t = score_run.tokens(d)

    assert t["output"] == 3500, "kept one thread instead of summing"
    assert t["input"] == 300
    assert t["series"] == 4


def test_tokens_take_the_maximum_within_one_series(tmp_path):
    """A counter is cumulative, so repeated scrapes of the same series must not
    be added together."""
    d = _prom(
        tmp_path,
        [
            _tok("output", 100, "cli:sregym-a-1"),
            _tok("output", 400, "cli:sregym-a-1"),  # a later scrape of the same series
        ],
    )

    assert score_run.tokens(d)["output"] == 400


def test_tokens_keep_both_processes(tmp_path):
    """chat and tworker are separate processes exporting the same thread, and
    both contributed. Collapsing them loses one process's work."""
    d = _prom(
        tmp_path,
        [
            _tok("output", 700, "cli:sregym-a-1", port="9091"),
            _tok("output", 250, "cli:sregym-a-1", port="9092"),
        ],
    )

    assert score_run.tokens(d)["output"] == 950


def test_tokens_are_none_when_nothing_was_archived(tmp_path):
    assert score_run.tokens(tmp_path) is None


def test_tokens_are_broken_out_by_node(tmp_path):
    """brain-go#2687: which RCI node or signal spent them, at the whole-run
    level -- the same question token_delta.py's delta_by_node() answers per
    attempt."""
    d = _prom(
        tmp_path,
        [
            _tok("input", 100, "cli:inv-1", node="hypothesis_gen_tools"),
            _tok("output", 40, "cli:inv-1", node="hypothesis_gen_tools"),
            _tok("input", 30, "cli:inv-2", node="diagnosis"),
        ],
    )
    t = score_run.tokens(d)

    assert t["by_node"]["hypothesis_gen_tools"] == {"input": 100, "output": 40}
    assert t["by_node"]["diagnosis"] == {"input": 30}
    # And the flat totals are unaffected by adding the breakdown alongside them.
    assert t["input"] == 130
    assert t["output"] == 40


def test_a_series_with_no_node_label_reads_as_unknown(tmp_path):
    """A scrape taken before the node label existed, or a sample without one,
    must not raise or vanish from the breakdown."""
    d = _prom(tmp_path, [_tok("input", 50, "cli:inv-1")])

    assert score_run.tokens(d)["by_node"]["unknown"] == {"input": 50}


def test_by_node_does_not_leak_into_the_flat_counts_rendered(tmp_path):
    """render() iterates tok's keys to print flat totals; by_node must be
    excluded from that loop the same way series already is, or it tries to
    format a dict as a token count and breaks."""
    d = _prom(tmp_path, [_tok("input", 10, "cli:inv-1", node="diagnosis")])
    rendered = score_run.render(score_run.score([]), score_run.tokens(d))

    assert "By node" in rendered
    assert "diagnosis" in rendered


def test_an_ungraded_diagnosis_is_excluded_from_the_graded_rate(tmp_path):
    """Diagnosis gets the same strict/graded treatment as mitigation and had no
    test, so a mutation collapsing its denominator to the attempt count passed
    the whole suite."""
    rows = [
        _row("a", diag="True", mitig="True", ttl="10", ttm="20"),
        _row("b", mitig="False", ttl="10", ttm="20"),  # no diagnosis verdict
    ]
    s = score_run.score(score_run.load(_run(tmp_path, rows)))

    assert s["diagnosis"]["strict_pct"] == 50.0
    assert s["diagnosis"]["graded_pct"] == 100.0
    assert s["diagnosis"]["ungraded"] == 1


def test_a_verdict_that_is_neither_true_nor_false_is_reported(tmp_path):
    """It used to be counted as a graded failure, moving the rate silently."""
    rows = [_row("a", diag="True", mitig="MAYBE", ttl="10", ttm="20")]
    s = score_run.score(score_run.load(_run(tmp_path, rows)))

    assert s["odd_verdicts"] == ["MAYBE"]


def test_an_unparseable_timing_does_not_take_the_report_down(tmp_path):
    """Every other field degrades quietly; this one used to raise."""
    rows = [
        _row("a", diag="True", mitig="True", ttl="not_a_number", ttm="20"),
        _row("b", diag="True", mitig="True", ttl="10", ttm="30"),
    ]
    s = score_run.score(score_run.load(_run(tmp_path, rows)))

    assert s["mitigation_phase_seconds"]["n"] == 1
    assert s["ttd_seconds"]["n"] == 1


def test_censored_attempts_are_imputed_at_the_cap_for_the_paper_comparison(tmp_path):
    """The paper gives a timed-out run the cap and includes it. Excluding them
    answers a different question, so both are reported."""
    rows = [
        _row("a", diag="True", mitig="True", ttl="100", ttm="200"),
        dict(_row("b", diag="False", timed_out="True", ttl="1800"), agent_timeout_seconds="1800"),
    ]
    s = score_run.score(score_run.load(_run(tmp_path, rows)))

    assert s["ttd_seconds"]["n"] == 1 and s["ttd_seconds"]["mean"] == 100.0
    assert s["ttd_seconds_cap_imputed"]["n"] == 2
    assert s["ttd_seconds_cap_imputed"]["mean"] == 950.0
    assert s["timeout_cap_seconds"] == 1800.0


def test_the_censored_flag_is_not_true_when_nothing_is_absent(tmp_path):
    """0 of 0 absent verdicts being censored is not a fact about the run."""
    rows = [_row("a", diag="True", mitig="True", ttl="10", ttm="20")]
    s = score_run.score(score_run.load(_run(tmp_path, rows)))

    assert s["mitigation"]["absent_all_censored"] is None


def test_the_scrape_is_found_in_the_sibling_log_tree(tmp_path):
    """archive_run.sh splits a run into two sibling trees.

    Results go to sregym-results/<run-id> and logs to
    sregym-run-logs/<run-id>. The scrape is a log, so passing the results
    directory, which is the directory a person passes, found nothing. The smoke
    run on 2026-09-01 archived 8 real series and was reported as having none,
    for the one metric that had never been measured at all.
    """
    results = tmp_path / "sregym-results" / "run-x"
    logs = tmp_path / "sregym-run-logs" / "run-x" / "shard-0" / "logs"
    results.mkdir(parents=True)
    logs.mkdir(parents=True)
    (logs / "sregym-tokens.prom").write_text(_tok("output", 4242, "cli:inv-a") + "\n")

    t = score_run.tokens(results)

    assert t is not None, "did not look in the sibling log tree"
    assert t["output"] == 4242


def test_the_same_scrape_read_twice_counts_once(tmp_path):
    """Both roots are searched, so one file can be reached by two paths.

    That is harmless by construction rather than by a dedupe: a series is keyed
    by its whole label set and collapsed with max(), so re-reading the same
    line adds nothing. This pins that, because the obvious alternative, summing
    every line, would double the run's tokens.
    """
    results = tmp_path / "sregym-results" / "run-x"
    logs = tmp_path / "sregym-run-logs" / "run-x"
    results.mkdir(parents=True)
    logs.mkdir(parents=True)
    line = _tok("output", 100, "cli:inv-a") + "\n"
    (results / "sregym-tokens.prom").write_text(line)
    (logs / "sregym-tokens.prom").write_text(line)

    t = score_run.tokens(results)

    assert t["output"] == 100, "the same series was added twice"
    assert t["series"] == 1


def test_the_cap_sentence_reads_when_nothing_was_censored(tmp_path):
    """It printed "the timeout (Nones)" when there was no cap to name."""
    rows = [_row("a", diag="True", mitig="True", ttl="10", ttm="20")]
    s = score_run.score(score_run.load(_run(tmp_path, rows)))

    assert "Nones" not in score_run.render(s, None)


def test_the_token_line_does_not_hide_the_cache_columns(tmp_path):
    """Reporting input+output alone hid a cache_read four times larger than
    every other column combined, which is most of what the run consumed."""
    results = tmp_path / "sregym-results" / "run-x"
    results.mkdir(parents=True)
    (results / "sregym-tokens.prom").write_text(
        "\n".join(
            [
                _tok("input", 100, "cli:inv-a"),
                _tok("output", 200, "cli:inv-a"),
                _tok("cache_read", 9000, "cli:inv-a"),
            ]
        )
        + "\n"
    )
    rows = [_row("a", diag="True", mitig="True", ttl="10", ttm="20")]
    text = score_run.render(score_run.score(score_run.load(_run(tmp_path / "slot", rows))), score_run.tokens(results))

    assert "9,300" in text, "headline excluded the cache columns"
    assert "cache_read" in text


def _tokens_dict(input_=0, output=0, cache_read=0, cache_write=0, by_node=None):
    d = {"input": input_, "output": output, "cache_read": cache_read, "cache_write": cache_write, "series": 1}
    d["by_node"] = by_node or {}
    return d


def test_cost_summary_is_unknown_without_a_model():
    """No model, so nothing to look a rate up by -- unknown, not zero."""
    cost = score_run.cost_summary(_tokens_dict(input_=100), model_id=None)
    assert cost == {"model": None, "total_usd": None, "by_node_usd": {}}


def test_cost_summary_is_unknown_for_an_unpriced_model():
    """A real-looking model id, just not one anyone has priced."""
    cost = score_run.cost_summary(_tokens_dict(input_=100), model_id="not-a-priced-model")
    assert cost["total_usd"] is None


def test_cost_summary_prices_the_real_committed_rate():
    """terraform/rates.yml is real now, not a future decision. Prices
    run-20260908-0739-4fc0249e's own whole-run totals at
    us.anthropic.claude-sonnet-4-6's committed rate: $166.00 by hand (see
    cost_model.py's TOKENS_PER_ATTEMPT_BY_TYPE comment for the per-type
    arithmetic)."""
    tok = _tokens_dict(input_=1_451_168, output=2_220_044, cache_write=20_143_259, cache_read=176_035_499)
    cost = score_run.cost_summary(tok, model_id="us.anthropic.claude-sonnet-4-6")
    assert cost["total_usd"] == pytest.approx(166.00, abs=0.01)


def test_cost_summary_prices_each_node_independently():
    tok = _tokens_dict(
        input_=1000,
        by_node={
            "diagnosis": {"input": 400},
            "hypothesis_gen_agent": {"input": 600},
        },
    )
    cost = score_run.cost_summary(tok, model_id="us.anthropic.claude-sonnet-4-6")
    assert cost["by_node_usd"]["diagnosis"] == pytest.approx(400 / 1e6 * 3.00)
    assert cost["by_node_usd"]["hypothesis_gen_agent"] == pytest.approx(600 / 1e6 * 3.00)


def test_render_shows_the_cost_beside_the_tokens_when_a_rate_is_configured(tmp_path):
    results = tmp_path / "sregym-results" / "run-x"
    results.mkdir(parents=True)
    (results / "sregym-tokens.prom").write_text(_tok("input", 1_000_000, "cli:inv-a", node="diagnosis") + "\n")
    tok = score_run.tokens(results)
    text = score_run.render(score_run.score([]), tok, model_id="us.anthropic.claude-sonnet-4-6")

    assert "$3.00" in text, "1M input tokens at $3.00/M should print as $3.00"
    assert "judge" in text, "must say the judge's cost is not included"


def test_render_says_plainly_when_no_model_is_recorded(tmp_path):
    results = tmp_path / "sregym-results" / "run-x"
    results.mkdir(parents=True)
    (results / "sregym-tokens.prom").write_text(_tok("input", 100, "cli:inv-a") + "\n")
    tok = score_run.tokens(results)
    text = score_run.render(score_run.score([]), tok, model_id=None)

    assert "unknown: no manifest model recorded" in text


def test_render_says_plainly_when_the_model_has_no_rate(tmp_path):
    results = tmp_path / "sregym-results" / "run-x"
    results.mkdir(parents=True)
    (results / "sregym-tokens.prom").write_text(_tok("input", 100, "cli:inv-a") + "\n")
    tok = score_run.tokens(results)
    text = score_run.render(score_run.score([]), tok, model_id="not-a-priced-model")

    assert "unknown: no rate configured for 'not-a-priced-model'" in text


def test_a_symlinked_results_directory_still_finds_the_sibling_tree(tmp_path):
    """resolve() dereferences before the name check.

    Review found that a symlinked sregym-results directory resolves to a name
    without "sregym-results" in it, so the sibling derivation was skipped and
    the scrape went unfound: the same "not archived" failure the derivation
    exists to remove, reached a different way.
    """
    real = tmp_path / "archive-2026" / "run-x"
    real.mkdir(parents=True)
    logs = tmp_path / "sregym-run-logs" / "run-x"
    logs.mkdir(parents=True)
    (logs / "sregym-tokens.prom").write_text(_tok("output", 777, "cli:inv-a") + "\n")
    (tmp_path / "sregym-results").symlink_to(tmp_path / "archive-2026")

    t = score_run.tokens(tmp_path / "sregym-results" / "run-x")

    assert t is not None, "the symlinked path defeated the derivation"
    assert t["output"] == 777


def test_the_deepest_results_directory_is_the_one_used(tmp_path):
    """A path nested under a backup of the same name must derive the sibling
    next to the run, not next to the backup."""
    run = tmp_path / "sregym-results" / "backup" / "sregym-results" / "run-x"
    run.mkdir(parents=True)
    logs = tmp_path / "sregym-results" / "backup" / "sregym-run-logs" / "run-x"
    logs.mkdir(parents=True)
    (logs / "sregym-tokens.prom").write_text(_tok("output", 555, "cli:inv-a") + "\n")

    t = score_run.tokens(run)

    assert t is not None, "derived the sibling from the wrong occurrence"
    assert t["output"] == 555


def test_the_token_dir_override_adds_rather_than_replaces(tmp_path, monkeypatch):
    """Documented as an override and implemented as an addition. Adding is the
    safer of the two, so the docstring was corrected to match: setting it
    cannot silently hide a scrape the default derivation already found."""
    results = tmp_path / "sregym-results" / "run-x"
    elsewhere = tmp_path / "somewhere-else"
    results.mkdir(parents=True)
    elsewhere.mkdir()
    (results / "sregym-tokens.prom").write_text(_tok("output", 10, "cli:inv-a") + "\n")
    (elsewhere / "sregym-tokens.prom").write_text(_tok("output", 20, "cli:inv-b") + "\n")

    monkeypatch.setenv("SREGYM_TOKEN_DIR", str(elsewhere))
    t = score_run.tokens(results)

    assert t["output"] == 30, "the override replaced instead of adding"
    assert t["series"] == 2


def test_wasted_deploys_are_counted_and_not_folded_into_any_rate(tmp_path):
    """A retry costs 35 to 40 minutes and moves no score, so it needs saying.

    One attempt deployed once, one needed two deploys, one exhausted all three.
    That is three wasted deploys across two of the three attempts, and the
    diagnosis rate is untouched by any of it.
    """
    run = _run(
        tmp_path,
        [
            _row("p1", diag="True", mitig="True", deploy_attempts="1"),
            _row("p2", diag="True", mitig="True", deploy_attempts="2"),
            _row("p3", diag="False", mitig="False", deploy_attempts="3"),
        ],
    )
    s = score_run.score(score_run.load(run))

    assert s["wasted_deploys"] == 3
    assert s["retried_attempts"] == 2
    assert s["deploy_attempts_known"] == 3
    # The point of reporting it separately: no rate moves.
    assert s["diagnosis"]["correct"] == 2
    assert s["attempts"] == 3

    assert "3 deploy(s) produced no attempt, across 2 of 3 attempts" in score_run.render(s, None)


def test_a_run_with_no_deploy_column_reads_as_unknown_not_as_zero(tmp_path):
    """Every run archived before this column existed has to say so.

    Reading a blank cell as zero would let an old run claim it wasted no
    deploys, which is the same mistake as counting an absent verdict as a
    failure.
    """
    run = _run(tmp_path, [_row("p1", diag="True", mitig="True")])
    s = score_run.score(score_run.load(run))

    assert s["deploy_attempts_known"] == 0
    assert s["wasted_deploys"] == 0
    out = score_run.render(s, None)
    assert "deploy retries: not recorded in this run's rows" in out
    assert "produced no attempt" not in out


def test_a_clean_run_says_nothing_about_deploys(tmp_path):
    """No line at all when every attempt deployed once. Silence is the signal."""
    run = _run(tmp_path, [_row("p1", diag="True", mitig="True", deploy_attempts="1")])
    out = score_run.render(score_run.score(score_run.load(run)), None)

    assert "deploy retries" not in out


def test_tokens_are_reported_per_attempt(tmp_path):
    """A run total cannot say which problem was expensive.

    The counters are labelled by thread, and since #28 the driver's thread is
    the investigation's cli:inv-<uuid>, which carries no problem id, so a
    scrape alone attributes nothing. main.py brackets each attempt and records
    the delta on the row instead (sregym#67).
    """
    run = _run(
        tmp_path,
        [
            _row(
                "p1",
                diag="True",
                mitig="True",
                tokens={
                    "llm_calls": "88",
                    "input": "17000",
                    "output": "25000",
                    "cache_read": "2056740",
                    "cache_write": "326711",
                },
            ),
            _row(
                "p2",
                diag="False",
                mitig="False",
                tokens={
                    "llm_calls": "80",
                    "input": "16501",
                    "output": "24923",
                    "cache_read": "1668552",
                    "cache_write": "192990",
                },
            ),
        ],
    )
    s = score_run.score(score_run.load(run))
    per = {row["problem_id"]: row for row in s["per_problem_tokens"]}

    assert per["p1"]["input"] == 17000
    assert per["p1"]["cache_read"] == 2056740
    assert per["p1"]["llm_calls"] == 88
    # The four sum to real consumption: both providers report cache reads and
    # writes outside the input count, so nothing here double counts.
    assert per["p1"]["total"] == 17000 + 25000 + 2056740 + 326711

    out = score_run.render(s, None)
    assert "Per attempt, from the result rows" in out
    assert "2,056,740" in out
    assert "There is no cached output" not in out, "that note belongs with the scrape totals"


def test_an_attempt_with_no_token_columns_is_omitted_not_zeroed(tmp_path):
    """Every archive before this column existed has none.

    Reporting them as zero would read as a free attempt, which is the same
    mistake as counting an absent verdict as a failure.
    """
    run = _run(tmp_path, [_row("p1", diag="True", mitig="True")])
    s = score_run.score(score_run.load(run))

    assert s["per_problem_tokens"] == []
    assert "Per attempt" not in score_run.render(s, None)


def test_a_measured_attempt_that_spent_nothing_still_appears(tmp_path):
    """Zero and unmeasured are different, and the calls column tells them apart."""
    run = _run(tmp_path, [_row("p1", diag="True", tokens={"llm_calls": "0", "input": "0"})])
    s = score_run.score(score_run.load(run))

    assert len(s["per_problem_tokens"]) == 1
    assert s["per_problem_tokens"][0]["total"] == 0


def test_tool_calls_are_reported_beside_llm_calls(tmp_path):
    """Two different questions. 88 model calls and 286 tool calls describe very
    different attempts, and neither number implies the other."""
    run = _run(
        tmp_path,
        [_row("p1", diag="True", tokens={"llm_calls": "88", "tool_calls": "286", "input": "17000"})],
    )
    s = score_run.score(score_run.load(run))
    row = s["per_problem_tokens"][0]

    assert row["llm_calls"] == 88
    assert row["tool_calls"] == 286

    out = score_run.render(s, None)
    assert "llm is model calls, tool is tool invocations" in out
    assert "286" in out


def test_an_attempt_with_only_tool_calls_still_appears(tmp_path):
    """An agent that records tool calls but no tokens is still measured.

    The row is included when any of these columns is present, so a future
    agent supplying one and not the other is not silently dropped.
    """
    run = _run(tmp_path, [_row("p1", diag="True", tokens={"tool_calls": "12"})])
    s = score_run.score(score_run.load(run))

    assert len(s["per_problem_tokens"]) == 1
    assert s["per_problem_tokens"][0]["tool_calls"] == 12
    assert s["per_problem_tokens"][0]["total"] == 0


# --- deploy_failed markers and expected-count coverage (#51) ----------------
#
# A run that loses a problem to an exhausted deploy retry produces no row a
# normal load() sees at all: the marker lives only in the slot's _ALL_
# rollup, which the run_N glob excludes on purpose (#41). Nothing above this
# point notices such a loss unless something reads that rollup specifically
# and compares the result against a population the run declared for itself.


def _all_rollup(tmp_path, slot, timestamp, rows):
    """A slot's _ALL_ rollup file: the sibling load()'s glob excludes."""
    d = tmp_path / "slots" / str(slot) / "results" / timestamp
    d.mkdir(parents=True, exist_ok=True)
    fieldnames = sorted({key for row in rows for key in row})
    with (d / "brain_go_ALL_results.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    return tmp_path


def _deploy_failed_row(pid, attempt, deploy_attempts=3):
    return {
        "problem_id": pid,
        "attempt": str(attempt),
        "deploy_failed": "True",
        "deploy_attempts": str(deploy_attempts),
    }


def _manifest(tmp_path, problems, n_attempts, **extra):
    """A run manifest the way write_run_manifest.sh writes one, minus the
    fields this file never reads."""
    fields = {
        "run_id": "run-x",
        "shard_index": "0",
        "shard_count": "1",
        "problem_set": "sregym-lite",
        "problems": " ".join(problems),
        "n_attempts": str(n_attempts),
    }
    fields.update(extra)
    d = tmp_path / "logs"
    d.mkdir(parents=True, exist_ok=True)
    text = "\n".join(f"{k}={v}" for k, v in fields.items()) + "\n"
    (d / "sregym-run-manifest-i-0123456789abcdef0.txt").write_text(text)
    return tmp_path


def test_deploy_failed_rows_are_read_from_the_all_rollup_not_the_run_glob(tmp_path):
    """The marker is real and archived, but only in the file load() skips."""
    _all_rollup(tmp_path, 1, "0902_0616", [_deploy_failed_row("p1", 1)])

    assert score_run.load(tmp_path) == []
    failed = score_run.load_deploy_failed(tmp_path)
    assert len(failed) == 1
    assert failed[0]["problem_id"] == "p1"


def test_a_completed_row_in_the_all_rollup_is_not_read_as_deploy_failed(tmp_path):
    """The rollup also carries every completed attempt for that problem;
    reading it for deploy_failed rows must not pick those up too, or a
    completed attempt would be double-counted against #41's fix."""
    _all_rollup(
        tmp_path,
        1,
        "0902_0616",
        [
            {"problem_id": "p1", "attempt": "1", "deploy_failed": ""},
            _deploy_failed_row("p2", 1),
        ],
    )

    failed = score_run.load_deploy_failed(tmp_path)
    assert [r["problem_id"] for r in failed] == ["p2"]


def test_deploy_failed_is_reported_as_its_own_category_not_folded_into_a_rate(tmp_path):
    """Neither a graded failure nor an absent verdict: nothing ran."""
    rows = [_row("a", diag="True", mitig="True", ttl="1", ttm="2")]
    run = _run(tmp_path, rows)
    _all_rollup(tmp_path, 0, "t0", [_deploy_failed_row("b", 1)])

    s = score_run.score(score_run.load(run), score_run.load_deploy_failed(run))

    assert s["deploy_failed"]["count"] == 1
    assert s["deploy_failed"]["attempts"] == [("b", 1)]
    # The rate that does exist is untouched by it.
    assert s["diagnosis"]["correct"] == 1
    assert s["attempts"] == 1

    out = score_run.render(s, None)
    assert "deploy exhausted: 1 attempt(s)" in out
    assert "b attempt 1" in out


def test_coverage_is_none_without_a_manifest(tmp_path):
    """No manifest archived means no expected count to check against --
    unknown, not clean."""
    run = _run(tmp_path, [_row("a", diag="True", mitig="True")])
    assert score_run.read_manifest(run) is None
    assert score_run.coverage(score_run.load(run), [], None) is None


def test_read_manifest_parses_key_value_lines(tmp_path):
    _manifest(tmp_path, ["a", "b", "c"], 3)
    manifest = score_run.read_manifest(tmp_path)

    assert manifest is not None
    assert manifest["problems"] == "a b c"
    assert manifest["n_attempts"] == "3"


def test_a_problem_that_lost_every_attempt_is_named_as_missing(tmp_path):
    """The scenario the issue describes: 'c' produced no row anywhere, not
    even a deploy_failed marker, and the run manifest is what says it should
    have."""
    _manifest(tmp_path, ["a", "b", "c"], 1)
    run = _run(
        tmp_path,
        [
            _row("a", diag="True", mitig="True", ttl="1", ttm="2"),
            _row("b", diag="True", mitig="True", ttl="1", ttm="2"),
        ],
    )

    manifest = score_run.read_manifest(run)
    rows = score_run.load(run)
    cov = score_run.coverage(rows, score_run.load_deploy_failed(run), manifest)

    assert cov == {"expected": 3, "found": 2, "deploy_failed": 0, "missing": [("c", 1)]}


def test_a_deploy_failed_marker_counts_as_accounted_for_not_missing(tmp_path):
    """A named, visible loss is not the same failure this coverage check
    exists to catch; only a silent one is."""
    _manifest(tmp_path, ["a", "b"], 1)
    run = _run(tmp_path, [_row("a", diag="True", mitig="True", ttl="1", ttm="2")])
    _all_rollup(run, 0, "t0", [_deploy_failed_row("b", 1)])

    cov = score_run.coverage(score_run.load(run), score_run.load_deploy_failed(run), score_run.read_manifest(run))

    assert cov["missing"] == []
    assert cov["deploy_failed"] == 1
    assert cov["found"] == 1


def test_a_full_run_reports_zero_missing(tmp_path):
    _manifest(tmp_path, ["a", "b"], 1)
    run = _run(
        tmp_path,
        [
            _row("a", diag="True", mitig="True", ttl="1", ttm="2"),
            _row("b", diag="True", mitig="True", ttl="1", ttm="2"),
        ],
    )

    cov = score_run.coverage(score_run.load(run), score_run.load_deploy_failed(run), score_run.read_manifest(run))

    assert cov == {"expected": 2, "found": 2, "deploy_failed": 0, "missing": []}


def test_render_prints_the_missing_pairs_when_a_manifest_is_given(tmp_path):
    _manifest(tmp_path, ["a", "b", "c"], 1)
    run = _run(tmp_path, [_row("a", diag="True", mitig="True", ttl="1", ttm="2")])

    rows = score_run.load(run)
    deploy_failed_rows = score_run.load_deploy_failed(run)
    cov = score_run.coverage(rows, deploy_failed_rows, score_run.read_manifest(run))
    out = score_run.render(score_run.score(rows, deploy_failed_rows), None, cov)

    assert "coverage: 1/3 expected attempts on record" in out
    assert "b attempt 1" in out
    assert "c attempt 1" in out


def test_render_omits_coverage_when_no_manifest_was_given(tmp_path):
    """render()'s existing callers pass no cov argument at all; adding
    coverage reporting must not change their output."""
    rows = [_row("a", diag="True", mitig="True", ttl="1", ttm="2")]
    out = score_run.render(score_run.score(score_run.load(_run(tmp_path, rows))), None)

    assert "coverage" not in out


def test_main_exits_nonzero_and_names_what_is_missing(tmp_path, capsys):
    """The end-to-end path: a lost problem must fail the run loudly rather
    than being read as a clean 20/21."""
    _manifest(tmp_path, ["a", "b"], 1)
    _run(tmp_path, [_row("a", diag="True", mitig="True", ttl="1", ttm="2")])

    rc = score_run.main(["score_run.py", str(tmp_path)])
    out = capsys.readouterr()

    assert rc == 1
    assert "b attempt 1" in out.out
    assert "1 expected attempt(s) never landed" in out.err


def test_main_exits_zero_when_every_expected_attempt_is_on_record(tmp_path, capsys):
    _manifest(tmp_path, ["a"], 1)
    _run(tmp_path, [_row("a", diag="True", mitig="True", ttl="1", ttm="2")])

    rc = score_run.main(["score_run.py", str(tmp_path)])

    assert rc == 0


def test_main_warns_but_does_not_fail_when_no_manifest_is_archived(tmp_path, capsys):
    """An archive predating the manifest, or one from a layout this file does
    not derive, should not read as a failure -- there is nothing to check
    against, which is different from a check that failed."""
    _run(tmp_path, [_row("a", diag="True", mitig="True", ttl="1", ttm="2")])

    rc = score_run.main(["score_run.py", str(tmp_path)])
    err = capsys.readouterr().err

    assert rc == 0
    assert "no run manifest found" in err


def test_main_warns_when_a_manifest_exists_but_has_no_usable_coverage_fields(tmp_path, capsys):
    """A manifest is archived (so 'no manifest found' would be a wrong and
    misleading message) but its problems field is empty -- exactly what
    write_run_manifest.sh itself writes on a PROBLEM_SET resolution
    failure, per its own comment. coverage() correctly returns None here,
    same as the no-manifest case, but a caller checking only `manifest is
    None` would stay silent: a report indistinguishable from a fully
    covered run, on a run this file could not actually check at all."""
    _manifest(tmp_path, [], 3)
    _run(tmp_path, [_row("a", diag="True", mitig="True", ttl="1", ttm="2")])

    rc = score_run.main(["score_run.py", str(tmp_path)])
    err = capsys.readouterr().err

    assert rc == 0
    assert err, "a manifest with no usable problems/n_attempts must still warn, not stay silent"
    assert "missing a usable problems/n_attempts" in err
