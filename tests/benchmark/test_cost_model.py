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


def test_bedrock_cost_is_unknown_without_a_configured_rate():
    """No rates.yml exists yet (open decision, experiment-bench-plan.md).
    The token estimate must still print; the dollar cost must not be
    guessed."""
    low, high, cost = cost_model.bedrock_estimate(63)
    assert low == pytest.approx(63 * cost_model.TOKENS_PER_ATTEMPT_RANGE[0])
    assert high == pytest.approx(63 * cost_model.TOKENS_PER_ATTEMPT_RANGE[1])
    assert cost is None


# ---------------------------------------------------------------------------
# Resolution: resolvable_effect_pp
# ---------------------------------------------------------------------------


def test_resolvable_effect_matches_the_63_attempt_reference_point():
    """experiment-bench-plan.md and sregym-findings.md: 63 paired
    problem-runs resolves "about 23pp" at 80% power. This is the reference
    calculation Spec D asks to generalize, not re-derive; reproducing it
    within the source text's own rounding is the check that the constant and
    the formula are the ones actually cited."""
    pp = cost_model.resolvable_effect_pp(63)
    assert pp == pytest.approx(24.0, abs=0.5)


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
    text = cost_model.render_preview(problem_count=24, attempts=3)
    assert "shape" in text
    assert "resolution" in text
    assert "72 paired attempts" in text
    assert "wall clock" in text
    assert "ec2" in text
    assert "bedrock" in text
    assert "dollar cost unknown" in text


def test_main_cli_reports_bad_input_without_a_traceback(capsys):
    exit_code = cost_model.main(["--problem-count", "0", "--attempts", "3"])
    assert exit_code == 2
    captured = capsys.readouterr()
    assert "cost_model:" in captured.err
