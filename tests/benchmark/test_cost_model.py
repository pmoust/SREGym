"""The order-statistic setup estimate and the resolution preview.

The part most likely to be gotten wrong under time pressure is
expected_max_of_n: it is tempting to average the per-slot outcomes instead
of taking their max, which understates a box's own wall clock (the box
finishes when its slowest slot does, not when the average one does). These
tests prove the closed form against a brute-force enumeration independent of
the implementation, not just against itself, plus the three boundary
properties the spec calls out by name: p=0 collapses to clean_minutes
regardless of N, p=1 collapses to retry_minutes regardless of N, and the
expectation strictly increases with N at any fixed p in (0, 1).
"""

import importlib.util
import itertools
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "cost_model",
    Path(__file__).resolve().parents[2] / "scripts" / "benchmark" / "cost_model.py",
)
cost_model = importlib.util.module_from_spec(_SPEC)
sys.modules["cost_model"] = cost_model
_SPEC.loader.exec_module(cost_model)


# ---------------------------------------------------------------------------
# expected_max_of_n
# ---------------------------------------------------------------------------


def test_p_zero_is_always_clean_regardless_of_n():
    """No retry ever happens, so the max of N clean draws is just clean."""
    for n in (1, 2, 3, 7, 12):
        assert cost_model.expected_max_of_n(n, p=0.0, clean=15.0, retry=62.0) == pytest.approx(15.0)


def test_p_one_is_always_retry_regardless_of_n():
    """Every slot always retries, so the max of N retry draws is just retry."""
    for n in (1, 2, 3, 7, 12):
        assert cost_model.expected_max_of_n(n, p=1.0, clean=15.0, retry=62.0) == pytest.approx(62.0)


def _brute_force_expected_max(n, p, clean, retry):
    """Independent of cost_model's own algebra: literally enumerate every one
    of the 2**n equally-unlikely-in-general (but here p=0.5, so equally
    likely) clean/retry combinations, take each combination's max, and
    average. This is the "direct enumeration of all 8 equally-likely
    combinations" the spec asks the closed form to be checked against.
    """
    total = 0.0
    combos = list(itertools.product([clean, retry], repeat=n))
    for combo in combos:
        weight = 1.0
        for draw in combo:
            weight *= p if draw == retry else (1 - p)
        total += weight * max(combo)
    return total


def test_p_half_n_three_matches_brute_force_enumeration():
    """The spec's own example: p=0.5, N=3, 8 equally-likely combinations."""
    clean, retry = 15.0, 62.0
    brute = _brute_force_expected_max(3, 0.5, clean, retry)
    closed_form = cost_model.expected_max_of_n(3, p=0.5, clean=clean, retry=retry)
    assert closed_form == pytest.approx(brute)
    # And the brute-force answer has a known shape: P(all 3 clean) = 1/8, so
    # E[max] = 7/8 * retry + 1/8 * clean.
    assert brute == pytest.approx(0.125 * clean + 0.875 * retry)


@pytest.mark.parametrize("n_pair", [(1, 2), (2, 3), (3, 7), (7, 12)])
def test_expected_max_strictly_increases_with_n_at_fixed_p(n_pair):
    """More slots on a box means a higher chance at least one retries, so the
    box's expected setup should get worse, not better, as N grows. This is
    the whole point of the order-statistic model over a plain average."""
    smaller, larger = n_pair
    p = 0.5
    e_smaller = cost_model.expected_max_of_n(smaller, p=p, clean=15.0, retry=62.0)
    e_larger = cost_model.expected_max_of_n(larger, p=p, clean=15.0, retry=62.0)
    assert e_larger > e_smaller


def test_expected_max_matches_two_term_simplification():
    """Any retry among N draws makes that case's max the retry value, so the
    n+1-term sum should collapse exactly to
    P(all clean)*clean + P(>=1 retry)*retry. Cross-checks the enumeration
    against the algebraic shortcut it is claimed to equal."""
    n, p, clean, retry = 5, 0.3, 15.0, 62.0
    all_clean = (1 - p) ** n
    expected = all_clean * clean + (1 - all_clean) * retry
    assert cost_model.expected_max_of_n(n, p=p, clean=clean, retry=retry) == pytest.approx(expected)


def test_rejects_retry_not_exceeding_clean():
    with pytest.raises(ValueError):
        cost_model.expected_max_of_n(3, p=0.5, clean=62.0, retry=15.0)


def test_rejects_n_below_one():
    with pytest.raises(ValueError):
        cost_model.expected_max_of_n(0, p=0.5, clean=15.0, retry=62.0)


def test_rejects_p_outside_unit_interval():
    with pytest.raises(ValueError):
        cost_model.expected_max_of_n(3, p=1.5, clean=15.0, retry=62.0)


# ---------------------------------------------------------------------------
# Shape and wall clock
# ---------------------------------------------------------------------------


def test_instance_count_gives_every_problem_its_own_slot():
    # 21 problems at 3 slots per instance is exactly 7, no partial instance.
    assert cost_model.instance_count_for(21, 3) == 7
    # 22 problems needs an 8th instance for the one leftover.
    assert cost_model.instance_count_for(22, 3) == 8


def test_wall_clock_adds_setup_once_and_scales_with_attempts():
    runs_per_instance = 3
    setup = cost_model.expected_setup_minutes(runs_per_instance)
    one_attempt = cost_model.wall_clock_minutes(1, runs_per_instance)
    three_attempts = cost_model.wall_clock_minutes(3, runs_per_instance)
    assert one_attempt == pytest.approx(setup + cost_model.ATTEMPT_CYCLE_MINUTES)
    assert three_attempts == pytest.approx(setup + 3 * cost_model.ATTEMPT_CYCLE_MINUTES)


def test_ec2_cost_scales_with_instance_count_and_wall_clock():
    cost_one = cost_model.ec2_cost_usd(1, 60.0)
    cost_two = cost_model.ec2_cost_usd(2, 60.0)
    assert cost_one == pytest.approx(cost_model.EC2_HOURLY_RATE_USD)
    assert cost_two == pytest.approx(2 * cost_model.EC2_HOURLY_RATE_USD)


def test_instance_type_pinned_against_terraform_default():
    """If terraform/variables.tf's instance_type default ever moves without
    EC2_HOURLY_RATE_USD following it, the dollar figure silently prices the
    wrong box. This is the same pinning convention ALL_PROBLEM_IDS uses
    against the registry."""
    cost_model._assert_instance_type()  # must not raise


# ---------------------------------------------------------------------------
# Bedrock estimate
# ---------------------------------------------------------------------------


def test_bedrock_cost_is_unknown_without_a_model():
    """No model given, so there is nothing to look a rate up by. The token
    estimate must still print; the dollar cost must not be guessed."""
    estimate = cost_model.bedrock_estimate(63)
    assert estimate["total"] == pytest.approx(63 * cost_model.TOKENS_PER_ATTEMPT_TOTAL)
    assert estimate["input"] == pytest.approx(63 * cost_model.TOKENS_PER_ATTEMPT_BY_TYPE["input"])
    assert estimate["cost_usd"] is None


def test_bedrock_cost_is_unknown_for_a_model_with_no_rates_entry():
    """A real model, just not one anyone has priced yet -- still "unknown",
    not a guess."""
    estimate = cost_model.bedrock_estimate(63, model_id="not-a-priced-model")
    assert estimate["cost_usd"] is None


def test_bedrock_rates_for_model_strips_the_bedrock_prefix():
    """main.py's --judge-model needs the "bedrock/" LiteLLM prefix; brain-go's
    own agent_model does not. Both must resolve to the same rates.yml row."""
    with_prefix = cost_model.bedrock_rates_for_model("bedrock/us.anthropic.claude-sonnet-4-6")
    without_prefix = cost_model.bedrock_rates_for_model("us.anthropic.claude-sonnet-4-6")
    assert with_prefix == without_prefix
    assert with_prefix is not None


def test_bedrock_rates_for_model_reads_the_real_committed_rates_file():
    """rates.yml is a real, committed file now, not a future decision. This
    pins the agent model's rate this repo actually priced, so a typo or a
    reformat in the yaml is caught here rather than only in a $ figure being
    silently wrong."""
    rates = cost_model.bedrock_rates_for_model("us.anthropic.claude-sonnet-4-6")
    assert rates == {"input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30}


def test_bedrock_estimate_reproduces_the_measured_run_cost():
    """Acceptance test: run-20260908-0739-4fc0249e's own real token counts,
    priced at us.anthropic.claude-sonnet-4-6's committed rate, computed by
    hand: input 1,451,168 @ $3.00/M = $4.35; output 2,220,044 @ $15.00/M =
    $33.30; cache_write 20,143,259 @ $3.75/M = $75.54; cache_read 176,035,499
    @ $0.30/M = $52.81. Total $166.00 (agent-only; the judge runs via LiteLLM
    on the host, not through brain-go, so its tokens are not in these
    counters at all)."""
    estimate = cost_model.bedrock_estimate(63, model_id="us.anthropic.claude-sonnet-4-6")
    assert estimate["input"] == pytest.approx(1_451_168)
    assert estimate["output"] == pytest.approx(2_220_044)
    assert estimate["cache_write"] == pytest.approx(20_143_259)
    assert estimate["cache_read"] == pytest.approx(176_035_499)
    assert estimate["cost_usd"] == pytest.approx(166.00, abs=0.01)


# ---------------------------------------------------------------------------
# Resolution: resolvable_effect_pp
# ---------------------------------------------------------------------------


def test_resolvable_effect_matches_the_63_attempt_reference_point():
    """At MCNEMAR_DISCORDANCE_RATE's current value (0.317, measured on
    run-20260908-0739-4fc0249e) and n=63, resolvable_effect_pp's own
    docstring cites ~19.5pp as its reference point. Reproducing it here
    catches the constant and the formula drifting apart, the same way the
    old 0.48-based ~24pp reference did before this constant moved."""
    pp = cost_model.resolvable_effect_pp(63)
    assert pp == pytest.approx(19.5, abs=0.5)


def test_resolvable_effect_decreases_as_attempts_grow():
    """More paired attempts should resolve a smaller effect, not a larger
    one -- the mirror image of the setup order statistic's monotonicity."""
    smaller = cost_model.resolvable_effect_pp(30)
    larger = cost_model.resolvable_effect_pp(300)
    assert smaller is not None and larger is not None
    assert larger < smaller


def test_resolvable_effect_none_when_too_few_attempts():
    """At very low N the expected discordant-pair count (N * psi) is too
    small for any bounded effect to reach 80% power; this must be reported
    as "not resolvable", not as a delta over 100% or a crash."""
    assert cost_model.resolvable_effect_pp(1) is None


def test_resolvable_effect_rejects_bad_inputs():
    with pytest.raises(ValueError):
        cost_model.resolvable_effect_pp(0)
    with pytest.raises(ValueError):
        cost_model.resolvable_effect_pp(63, discordance=0.0)
    with pytest.raises(ValueError):
        cost_model.resolvable_effect_pp(63, discordance=1.5)


# ---------------------------------------------------------------------------
# render_preview: end to end, no crash, every line present
# ---------------------------------------------------------------------------


def test_render_preview_covers_every_line():
    """terraform/variables.tf's agent_model default (Sonnet 4.5) now has a
    rates.yml entry (sregym#149), so the preview prints a real dollar figure
    rather than "unknown". bedrock_estimate's own unknown-model path is
    covered directly by test_bedrock_cost_is_unknown_for_a_model_with_no_rates_entry
    above; this test only has to prove every section of the preview renders."""
    text = cost_model.render_preview(problem_count=24, attempts=3)
    assert "shape" in text
    assert "resolution" in text
    assert "72 paired attempts" in text
    assert "wall clock" in text
    assert "ec2" in text
    assert "bedrock" in text
    assert "~$" in text.split("bedrock")[-1], "expected a priced dollar figure, not 'dollar cost unknown'"


def test_main_cli_reports_bad_input_without_a_traceback(capsys):
    exit_code = cost_model.main(["--problem-count", "0", "--attempts", "3"])
    assert exit_code == 2
    captured = capsys.readouterr()
    assert "cost_model:" in captured.err
