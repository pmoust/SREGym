#!/usr/bin/env python3
"""What a shape costs, before anything gets deployed.

    scripts/benchmark/cost_model.py --problem-count N --attempts N [--runs-per-instance N]

experiment.sh resolves --sregym-ref/--brain-go-ref and, separately,
--problems into a problem count via problem_sets.resolve_problems(). This
module takes that count plus --attempts and prints the shape, the expected
wall clock, the expected EC2 cost, and the statistical resolution the shape
buys -- all before `terraform apply` exists to ask the question of.

## Why setup is not one number

A slot either deploys clean or hits a deploy-retry cycle (measured, not
theorized: see the constants below), and one instance's own wall clock is
set by its slowest slot, not the average one, since slots on one instance run
in parallel. So instance-level setup time is the expected value of the
*maximum* of `runs_per_instance` independent draws from a two-point
{clean_minutes, retry_minutes} distribution at the measured retry
probability, not a weighted average of per-slot outcomes. `expected_max_of_n`
below computes this by enumerating the N+1 cases (how many of the N draws hit
retry), weighting each by its binomial probability, and using that case's max
value -- which collapses to retry_minutes for every case with at least one
retry, since retry_minutes > clean_minutes always holds here (asserted at
call time, not assumed).

## The shape this preview assumes

Boxes scale to give every problem its own slot: `instance_count =
ceil(problem_count / runs_per_instance)`, so depth is always 1 (one problem
per slot) and each slot runs exactly `attempts` attempts back to back. This
is the shape shown in the experiment-bench-plan.md worked example ("boxes =
ceil(problems / slots-per-box), depth 1, N attempts each"). It is also the
fastest-wall-clock shape for a given `runs_per_instance`; trading fewer,
deeper boxes for lower cost (what run.tfvars does deliberately, 4 boxes at
depth 2 rather than 7 at depth 1) is a manual override left to
`launch.sh`/the terraform wrapper (Day 4), not this preview.

Deviation from the spec text: Spec D's formula 2 reads
`setup_expected + ceil(problems/slots_per_box) x attempts x cycle_minutes`,
which would multiply the *box count* into wall clock as if it were a depth
term -- boxes run in parallel, so that overcounts by a factor of
instance_count for any run wider than one box. The plan's own worked dry-run
example (top of experiment-bench-plan.md, Phase 1) fixes depth at 1 and uses
`ceil(problems/slots-per-box)` only for box count, which is what this module
implements; the formula text is treated as a simplification that happens to
be exact only at instance_count == 1.

## Every constant below is cited, not guessed

Same convention as score_run.py's PAPER_STRATUS: a number here is either
measured, with the run that measured it, or explicitly unmeasured. No
constant is a placeholder pretending to be a measurement.
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VARIABLES_TF = REPO_ROOT / "terraform" / "variables.tf"

# ---------------------------------------------------------------------------
# Measured constants (Day 3 table, experiment-bench-plan.md)
# ---------------------------------------------------------------------------

# Midpoint of the observed clean-setup range: 9-21 min on run-20260902-0606
# (r7a.24xlarge, 4 of 9 slots) and 6 of 12 slots on run-20260903-1259
# (m8i.8xlarge). Same range on two box sizes 3x apart in CPU/memory.
SETUP_CLEAN_MINUTES = 15.0
SETUP_CLEAN_RANGE_MINUTES = (9.0, 21.0)

# Midpoint of the observed deploy-retry setup range: 56-68 min, 5 of 9 slots
# on run-20260902-0606 and 6 of 12 on run-20260903-1259.
SETUP_RETRY_MINUTES = 62.0
SETUP_RETRY_RANGE_MINUTES = (56.0, 68.0)

# ~50% per slot, consistent across both runs above and both box sizes. Read
# as external registry throttling (Docker Hub / ghcr.io), not local
# disk/CPU contention: a contention theory predicts the rate should improve
# on the 3x-smaller box's neighbors, and it does not. Named and documented
# because #47 (Phase 2) targets this rate directly; update it here when that
# lands rather than leaving a stale number quietly wrong.
RETRY_PROBABILITY = 0.5

# Median attempt cycle, run-20260902-0606, 51 deltas.
ATTEMPT_CYCLE_MINUTES = 24.8

# Proven concurrency width at m8i.8xlarge (run-20260903-1259, run.tvars);
# 7 lost a whole run to disk contention (run-20260903-0822). Matches
# terraform/variables.tf's runs_per_instance description.
RUNS_PER_INSTANCE_DEFAULT = 3

# AWS pricing API, us-east-2, captured alongside the Day 3 measurements.
# Tied to terraform/variables.tf's instance_type default; _assert_instance_type
# below fails loudly if that default ever moves without this rate following it.
EC2_INSTANCE_TYPE = "m8i.8xlarge"
EC2_HOURLY_RATE_USD = 1.6934

# Range across two measurements: 96.4M tokens / 60 attempts
# (run-20260902-0606) and 4.33M / 2 attempts (run-20260903-0558, post-fix).
# Midpoint used for the point estimate below; the range is printed alongside
# it rather than hidden behind one number.
TOKENS_PER_ATTEMPT_RANGE = (1.6e6, 2.2e6)
TOKENS_PER_ATTEMPT_MID = sum(TOKENS_PER_ATTEMPT_RANGE) / 2

# Bedrock's per-token rate for sonnet-4-5 is partner pricing and goes in a
# committed rates.yml once someone puts a real number there (open decision
# #3, experiment-bench-plan.md). It does not exist yet. Never guess it:
# score_run.py's own convention is to print what is known and say plainly
# what is not, rather than fabricate a number that would then get quoted.
RATES_FILE = REPO_ROOT / "terraform" / "rates.yml"

# ---------------------------------------------------------------------------
# McNemar / Connor (1987) sample-size relation, inverted to solve for the
# resolvable effect size at a fixed N rather than the more common N at a
# fixed effect size.
# ---------------------------------------------------------------------------

# 48% verdict flip between two runs of the same 21 problems
# (experiment-bench-plan.md's "constraint to design against"). This is the
# discordant-pair rate psi = p12 + p21 in Connor's notation: the fraction of
# paired problem-runs where the two conditions disagree at all, which is
# what a McNemar-style comparison actually has power against. An earlier,
# smaller pair of runs (sregym-findings.md) measured 43% (9/21) the same
# way; 48% is the more recent figure the plan cites and this follows it.
# Update this constant, not the formula, if a newer measurement supersedes it.
MCNEMAR_DISCORDANCE_RATE = 0.48

# Standard normal quantiles: two-sided alpha=0.05, and 80% power (one-sided
# beta=0.20). Fixed by convention, not measured; named so they read as what
# they are rather than as unexplained literals in the formula below.
Z_ALPHA_TWO_SIDED_05 = 1.959963985
Z_BETA_POWER_80 = 0.841621233


def _assert_instance_type() -> None:
    """Fail loudly if terraform/variables.tf's instance_type default moves
    without EC2_HOURLY_RATE_USD following it. Same pinning convention as
    ALL_PROBLEM_IDS being test-checked against the registry: a silent drift
    here would otherwise show up only as a wrong dollar figure."""
    text = VARIABLES_TF.read_text()
    match = re.search(r'variable\s+"instance_type"\s*\{.*?default\s*=\s*"([^"]+)"', text, re.DOTALL)
    if match is None:
        raise RuntimeError(f"could not find instance_type's default in {VARIABLES_TF}")
    if match.group(1) != EC2_INSTANCE_TYPE:
        raise RuntimeError(
            f"terraform/variables.tf's instance_type default is {match.group(1)!r}, "
            f"but cost_model.py's EC2_HOURLY_RATE_USD is priced for {EC2_INSTANCE_TYPE!r}. "
            "Update EC2_INSTANCE_TYPE/EC2_HOURLY_RATE_USD together with the terraform default."
        )


# ---------------------------------------------------------------------------
# The order statistic
# ---------------------------------------------------------------------------


def expected_max_of_n(n: int, p: float, clean: float, retry: float) -> float:
    """E[max(X_1, ..., X_n)], each X_i independently `clean` w.p. (1-p) and
    `retry` w.p. p.

    Enumerates the n+1 cases by how many of the n draws hit the retry
    branch, weights each by its binomial probability, and takes that case's
    max value: any retry among the n draws makes that case's outcome the
    retry value, since retry > clean always holds here (checked below, not
    assumed). This is the closed form the spec asks for, written as the
    explicit sum rather than the two-term simplification it collapses to
    (P(no retry)*clean + P(>=1 retry)*retry), so the enumeration this
    docstring describes is the code that runs, not just its algebra.
    """
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")
    if not 0.0 <= p <= 1.0:
        raise ValueError(f"p must be in [0, 1], got {p}")
    if retry <= clean:
        raise ValueError(f"retry ({retry}) must exceed clean ({clean}): the max-collapses-to-retry step requires it")

    expected = 0.0
    for k in range(n + 1):  # k = number of the n draws that hit the retry branch
        weight = math.comb(n, k) * (p**k) * ((1 - p) ** (n - k))
        outcome = retry if k >= 1 else clean
        expected += weight * outcome
    return expected


def expected_setup_minutes(runs_per_instance: int) -> float:
    """One instance's expected setup time: the expected max of
    `runs_per_instance` independent clean/retry draws at RETRY_PROBABILITY."""
    return expected_max_of_n(runs_per_instance, RETRY_PROBABILITY, SETUP_CLEAN_MINUTES, SETUP_RETRY_MINUTES)


# ---------------------------------------------------------------------------
# Shape, wall clock, cost
# ---------------------------------------------------------------------------


def instance_count_for(problem_count: int, runs_per_instance: int) -> int:
    """Boxes scaled to give every problem its own slot (depth 1). See the
    module docstring's "shape this preview assumes" for why."""
    if problem_count < 1:
        raise ValueError(f"problem_count must be >= 1, got {problem_count}")
    if runs_per_instance < 1:
        raise ValueError(f"runs_per_instance must be >= 1, got {runs_per_instance}")
    return math.ceil(problem_count / runs_per_instance)


def wall_clock_minutes(attempts: int, runs_per_instance: int) -> float:
    """Expected wall clock for one instance: its expected setup, once, plus
    `attempts` attempt cycles run back to back in each of its slots (depth 1,
    so a slot never works more than one problem)."""
    if attempts < 1:
        raise ValueError(f"attempts must be >= 1, got {attempts}")
    return expected_setup_minutes(runs_per_instance) + attempts * ATTEMPT_CYCLE_MINUTES


def ec2_cost_usd(instance_count: int, wall_clock_min: float) -> float:
    return instance_count * EC2_HOURLY_RATE_USD * (wall_clock_min / 60.0)


def _bedrock_rate_per_million_tokens() -> float | None:
    """The committed rate, if one has ever been put in terraform/rates.yml.
    Never guessed: matches score_run.py's refusal to pick a denominator for
    you. No YAML dependency is added for this: the file does not exist yet
    (open decision #3), and the day it does, whoever adds a real number can
    also wire up whatever format they choose to read it back."""
    return None


def bedrock_estimate(paired_attempts: int) -> tuple[float, float, float | None]:
    """(low, high, cost_usd) token estimate for `paired_attempts` attempts.
    cost_usd is None when no rate is configured -- the caller must print that
    as "unknown", not omit it or guess."""
    low = paired_attempts * TOKENS_PER_ATTEMPT_RANGE[0]
    high = paired_attempts * TOKENS_PER_ATTEMPT_RANGE[1]
    rate = _bedrock_rate_per_million_tokens()
    if rate is None:
        return (low, high, None)
    mid = paired_attempts * TOKENS_PER_ATTEMPT_MID
    return (low, high, mid / 1e6 * rate)


# ---------------------------------------------------------------------------
# Resolution: what effect size the shape can detect
# ---------------------------------------------------------------------------


def resolvable_effect_pp(
    paired_attempts: int,
    discordance: float = MCNEMAR_DISCORDANCE_RATE,
    z_alpha: float = Z_ALPHA_TWO_SIDED_05,
    z_beta: float = Z_BETA_POWER_80,
) -> float | None:
    """The smallest verdict-flip-rate difference (in percentage points, 0-100)
    that `paired_attempts` paired problem-runs can detect at 80% power,
    alpha 0.05 two-sided, given the discordant-pair rate `discordance`.

    This is Connor's (1987) McNemar sample-size relation,

        n = [z_alpha*sqrt(psi) + z_beta*sqrt(psi - delta^2)]^2 / delta^2

    solved for delta instead of n. Squaring both sides of
    sqrt(n)*delta - z_alpha*sqrt(psi) = z_beta*sqrt(psi - delta^2) turns it
    into a quadratic in delta:

        (n + z_beta^2)*delta^2 - 2*sqrt(n)*z_alpha*sqrt(psi)*delta
            + psi*(z_alpha^2 - z_beta^2) = 0

    Reference point this reproduces: at psi=0.48 (MCNEMAR_DISCORDANCE_RATE)
    and n=63, this returns ~24pp, matching "63 paired problem-runs resolves
    about 23pp at 80% power" (experiment-bench-plan.md, sregym-findings.md)
    to within the rounding the source text itself uses ("about").

    Returns None when no bounded effect (0 < delta <= 1) is resolvable at
    this N -- true for very small N, where the expected number of discordant
    pairs (n * psi) is too small for any test to reach 80% power.
    """
    if paired_attempts < 1:
        raise ValueError(f"paired_attempts must be >= 1, got {paired_attempts}")
    if not 0.0 < discordance <= 1.0:
        raise ValueError(f"discordance must be in (0, 1], got {discordance}")

    n = paired_attempts
    a = n + z_beta**2
    b = -2 * math.sqrt(n) * z_alpha * math.sqrt(discordance)
    c = discordance * (z_alpha**2 - z_beta**2)

    discriminant = b * b - 4 * a * c
    if discriminant < 0:
        return None

    delta = (-b + math.sqrt(discriminant)) / (2 * a)
    if not 0.0 < delta <= 1.0:
        return None
    return delta * 100.0


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_preview(problem_count: int, attempts: int, runs_per_instance: int = RUNS_PER_INSTANCE_DEFAULT) -> str:
    _assert_instance_type()

    instances = instance_count_for(problem_count, runs_per_instance)
    setup = expected_setup_minutes(runs_per_instance)
    wall_min = wall_clock_minutes(attempts, runs_per_instance)
    wall_hr = wall_min / 60.0
    cost = ec2_cost_usd(instances, wall_min)
    paired = problem_count * attempts
    pp = resolvable_effect_pp(paired)
    tok_low, tok_high, tok_cost = bedrock_estimate(paired)

    lines = []
    lines.append(
        f"experiment: shape        instance_count = ceil({problem_count}/{runs_per_instance}) = {instances}, "
        f"depth 1, {attempts} attempt(s) each (runs_per_instance: {runs_per_instance})"
    )
    lines.append(
        f"experiment: resolution   {paired} paired attempts, resolves "
        + (f"~{pp:.0f}pp at 80% power" if pp is not None else "no bounded effect at 80% power (too few attempts)")
    )
    lines.append(
        f"experiment: wall clock   ~{wall_hr:.1f} h  "
        f"(setup ~{setup:.0f} min expected [{SETUP_CLEAN_RANGE_MINUTES[0]:.0f}-{SETUP_RETRY_RANGE_MINUTES[1]:.0f} min "
        f"range, {RETRY_PROBABILITY:.0%} per-slot retry chance], then {attempts} x {ATTEMPT_CYCLE_MINUTES:.1f} min)"
    )
    lines.append(
        f"experiment: ec2          ~${cost:.2f}  "
        f"({instances} instance(s) @ ${EC2_HOURLY_RATE_USD}/h x {wall_hr:.1f}h, {EC2_INSTANCE_TYPE}, us-east-2)"
    )
    if tok_cost is None:
        lines.append(
            f"experiment: bedrock      tokens ~{tok_low / 1e6:.1f}-{tok_high / 1e6:.1f}M "
            f"({paired} attempts x ~{TOKENS_PER_ATTEMPT_RANGE[0] / 1e6:.1f}-{TOKENS_PER_ATTEMPT_RANGE[1] / 1e6:.1f}M), "
            f"dollar cost unknown (no rate configured; see {RATES_FILE.relative_to(REPO_ROOT)})"
        )
    else:
        lines.append(f"experiment: bedrock      tokens ~{tok_low / 1e6:.1f}-{tok_high / 1e6:.1f}M, ~${tok_cost:.2f}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--problem-count", type=int, required=True)
    parser.add_argument("--attempts", type=int, required=True)
    parser.add_argument("--runs-per-instance", type=int, default=RUNS_PER_INSTANCE_DEFAULT)
    args = parser.parse_args(argv)

    try:
        print(render_preview(args.problem_count, args.attempts, args.runs_per_instance))
    except ValueError as exc:
        print(f"cost_model: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
