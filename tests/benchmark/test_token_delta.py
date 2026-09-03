"""Per-attempt token accounting, as a delta between two counter scrapes.

brain-go reports LLM usage as cumulative Prometheus counters. The intended
attribution was by thread name (scrape_tokens.sh: "driver.py sets it to
cli:sregym-<problem_id>-<hex8>, so one scrape attributes tokens to problems"),
and that stopped holding when the driver started using the investigation's own
thread, cli:inv-<uuid>. Bracketing an attempt does not depend on a name.
"""

import importlib.util
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


def _sample(thread, token_type, value):
    return (
        f'llm_tokens_total{{account_id="2",model="m",provider="bedrock",'
        f'thread_id="{thread}",token_type="{token_type}",user_id="unknown"}} {value}'
    )


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
