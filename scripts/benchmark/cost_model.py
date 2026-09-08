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

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
VARIABLES_TF = REPO_ROOT / "terraform" / "variables.tf"

# ---------------------------------------------------------------------------
# Measured constants (Day 3 table, experiment-bench-plan.md)
# ---------------------------------------------------------------------------

# Observed clean-setup range, 9-21 min, unchanged by a third measurement:
# run-20260902-0606 (r7a.24xlarge, 4 of 9 slots), run-20260903-1259
# (m8i.8xlarge, 6 of 12 slots), and run-20260908-0739-4fc0249e (m8i.8xlarge,
# all 21 slots -- every deploy this run was clean, see RETRY_PROBABILITY).
# Same range across three box sizes.
#
# The point value below no longer is that range's midpoint. It is
# run-20260908-0739-4fc0249e's own median: each slot's first result-row mtime
# minus the run's recorded launch (2026-09-08T07:39:23 UTC), less that same
# attempt's own TTM (the agent-clock component, read from the row, so what is
# left is deploy-plus-fault-injection time). n=10 of the run's 12 slots; the
# other 2 are excluded because their result-row mtimes were not monotonic in
# attempt order (run_2 finishing before run_1), consistent with a mid-run
# harness resume rewriting a subset of rows rather than a real timing
# sample. Cross-checked against a fault-injection creationTimestamp found
# in one slot's archived agent log, which the same computation lands within
# two minutes of. Median 673.8s = 11.2 min; still inside the 9-21 range
# above, just no longer read off that range's midpoint.
SETUP_CLEAN_MINUTES = 11.2
SETUP_CLEAN_RANGE_MINUTES = (9.0, 21.0)

# Midpoint of the observed deploy-retry setup range: 56-68 min, 5 of 9 slots
# on run-20260902-0606 and 6 of 12 on run-20260903-1259. Not updated by
# run-20260908-0739-4fc0249e: that run had zero retried deploys (see
# RETRY_PROBABILITY), so it contributes no new retry-duration sample. This
# pair is pre-#56-fix and unconfirmed since; update it once a post-fix run
# actually hits the retry branch.
SETUP_RETRY_MINUTES = 62.0
SETUP_RETRY_RANGE_MINUTES = (56.0, 68.0)

# Was ~50% per slot across run-20260902-0606 and run-20260903-1259 (both
# pre-#56-fix; see the retry-storm fix on the deploy-retry path). Superseded:
# run-20260908-0739-4fc0249e (sha cd28fbd1, post-fix) recorded deploy_attempts
# == 1 on every one of its 21 slot-deploys, i.e. zero observed retries. Zero
# events in 21 trials does not license asserting the true rate is now exactly
# zero, so this uses the rule-of-three upper bound for zero observed events at
# 95% confidence instead: 3/n = 3/21 = 0.14. Update again once a post-fix run
# actually observes a retry, which will pin the rate rather than bound it.
RETRY_PROBABILITY = 0.14

# Was the median wall-clock gap between successive attempt completions,
# run-20260902-0606, 51 deltas. Superseded by the same measurement on
# run-20260908-0739-4fc0249e: 38 consecutive same-problem attempt-completion
# deltas (result-row mtimes), across the 19 of 21 problems whose rows landed
# in monotonic attempt order (see SETUP_CLEAN_MINUTES for the 2 excluded and
# why). Median 501s = 8.4 min, well under half the old figure -- consistent
# with the same speedups that moved SETUP_CLEAN_MINUTES and
# RETRY_PROBABILITY down on this run.
ATTEMPT_CYCLE_MINUTES = 8.4

# Proven concurrency width at m8i.8xlarge (run-20260903-1259, run.tvars);
# 7 lost a whole run to disk contention (run-20260903-0822). Matches
# terraform/variables.tf's runs_per_instance description.
RUNS_PER_INSTANCE_DEFAULT = 3

# AWS pricing API, us-east-2, captured alongside the Day 3 measurements.
# Tied to terraform/variables.tf's instance_type default; _assert_instance_type
# below fails loudly if that default ever moves without this rate following it.
EC2_INSTANCE_TYPE = "m8i.8xlarge"
EC2_HOURLY_RATE_USD = 1.6934

# Point estimate, not a range, and this comment says why: the previous two
# measurements (96.4M/60 attempts on run-20260902-0606, 4.33M/2 attempts on
# run-20260903-0558) were both used as a range because neither alone covered
# enough attempts to trust. run-20260908-0739-4fc0249e is 63 attempts, the
# whole paired shape this file's own resolvable_effect_pp treats as a real
# sample, and its total is verified two independent ways: the sum of these
# four per-type figures and score_run.py's own tokens() on the archived
# scrape agree to the token. Per-type, not blended, for the same reason
# rates.yml is per-type: cache_read alone is 88% of this run's tokens and
# prices at 1/50th of output, so a single blended figure would misprice any
# run whose mix differs from this one's.
#
# Do not compute this figure from the result CSVs' own tokens_* columns: they
# are inflated roughly 2.41x by concurrent-slot contamination (see
# token_delta.py's module docstring). These four are run-20260908-0739-4fc0249e's
# whole-run totals -- input 1,451,168, output 2,220,044, cache_write
# 20,143,259, cache_read 176,035,499 -- divided by its 63 attempts, and the
# whole-run figure is the one this bug does not touch.
#
# One clean run is one data point. Update this, not just widen it into a
# range, once a second run gives a second point to compare against.
TOKENS_PER_ATTEMPT_BY_TYPE = {
    "input": 1_451_168 / 63,
    "output": 2_220_044 / 63,
    "cache_write": 20_143_259 / 63,
    "cache_read": 176_035_499 / 63,
}
TOKENS_PER_ATTEMPT_TOTAL = sum(TOKENS_PER_ATTEMPT_BY_TYPE.values())

# The committed rates, once someone puts real numbers in rates.yml (open
# decision #3, experiment-bench-plan.md, closed by run-20260908-0739-4fc0249e's
# measurement). Four token types, not a blended one, because they differ by
# up to 50x: see rates.yml's own header for why and for brain-go's
# CachePointTypeDefault tying cache_write to the 5-minute rate specifically.
RATES_FILE = REPO_ROOT / "terraform" / "rates.yml"

# ---------------------------------------------------------------------------
# McNemar / Connor (1987) sample-size relation, inverted to solve for the
# resolvable effect size at a fixed N rather than the more common N at a
# fixed effect size.
# ---------------------------------------------------------------------------

# 31.7% verdict flip within run-20260908-0739-4fc0249e's own 63 attempt pairs
# (20 of 63 disagreed on Diagnosis.success). This is the discordant-pair rate
# psi = p12 + p21 in Connor's notation: the fraction of paired problem-runs
# where the two conditions disagree at all, which is what a McNemar-style
# comparison actually has power against.
#
# Better measurement than the 48% it replaces, not just a newer one: 48% came
# from comparing two separate runs (experiment-bench-plan.md's "constraint to
# design against"), which mixes in whatever drifted between those runs'
# environments on top of the model's own flakiness. 31.7% is within one run,
# one fleet, one code version (sha cd28fbd1), so environmental drift cannot be
# part of it.
#
# Collected with a known evidence-collection bug present (brain-go#2486: 29%
# of this run's attempts collected zero evidence), so this may move again
# once that lands. Update this constant, not the formula, if a newer
# measurement supersedes it.
MCNEMAR_DISCORDANCE_RATE = 0.317

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


def _agent_model_default() -> str:
    """terraform/variables.tf's agent_model default, for a preview that has no
    run manifest to read an actual model out of (score_run.py's job -- see
    that file's cost_summary()). Not asserted against rates.yml the way
    _assert_instance_type asserts EC2_HOURLY_RATE_USD: an unpriced default is
    normal (the default itself is stale, see rates.yml's own note on it) and
    must read as "unknown", not raise.
    """
    text = VARIABLES_TF.read_text()
    match = re.search(r'variable\s+"agent_model"\s*\{.*?default\s*=\s*"([^"]+)"', text, re.DOTALL)
    if match is None:
        raise RuntimeError(f"could not find agent_model's default in {VARIABLES_TF}")
    return match.group(1)


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


# The four types rates.yml prices per model. Same four token_delta.py and
# score_run.py account for; named again here rather than imported from
# either, since neither is a natural home for a pricing-shaped constant.
PRICED_TOKEN_TYPES = ("input", "output", "cache_write", "cache_read")


def bedrock_rates_for_model(model_id: str) -> dict[str, float] | None:
    """This model's {input, output, cache_write, cache_read} rate, USD per
    million tokens, from terraform/rates.yml. None when the file does not
    exist, does not parse, or has no entry for `model_id` -- never guessed,
    same convention score_run.py already uses for a missing denominator.

    `model_id` is matched after stripping a leading "bedrock/": main.py's
    --judge-model needs that LiteLLM prefix and brain-go's own
    bedrockruntime.Converse call (what agent_model feeds) does not, so the
    same id can arrive either way depending which role is being priced.
    """
    if not RATES_FILE.is_file():
        return None
    try:
        data = yaml.safe_load(RATES_FILE.read_text())
    except yaml.YAMLError:
        return None

    models = (data or {}).get("models")
    if not isinstance(models, dict):
        return None
    rates = models.get(model_id.removeprefix("bedrock/"))
    if not isinstance(rates, dict):
        return None

    try:
        return {kind: float(rates[kind]) for kind in PRICED_TOKEN_TYPES}
    except (KeyError, TypeError, ValueError):
        return None


def token_cost(totals: dict[str, float], model_id: str | None) -> float | None:
    """Dollar cost of a {input, output, cache_write, cache_read, ...} token
    totals dict, at `model_id`'s rate in terraform/rates.yml. Extra keys in
    `totals` (score_run.py's tokens() also carries "series"/"by_node"/"total")
    are ignored: only the four priced kinds are summed. None when `model_id`
    is not given or has no configured rate -- unknown, never guessed, the
    same convention every caller of this module already follows.
    """
    if not model_id:
        return None
    rates = bedrock_rates_for_model(model_id)
    if rates is None:
        return None
    return sum(totals.get(kind, 0) / 1e6 * rate for kind, rate in rates.items())


def bedrock_estimate(paired_attempts: int, model_id: str | None = None) -> dict[str, float | None]:
    """Token and dollar estimate for `paired_attempts` attempts, priced at
    TOKENS_PER_ATTEMPT_BY_TYPE's measured per-type rate.

    Returns each type's token count, "total" tokens, and "cost_usd" --
    None when `model_id` is not given or has no configured rate, which the
    caller must print as "unknown", not omit or guess.
    """
    if paired_attempts < 1:
        raise ValueError(f"paired_attempts must be >= 1, got {paired_attempts}")

    tokens: dict[str, float | None] = {
        kind: paired_attempts * per_attempt for kind, per_attempt in TOKENS_PER_ATTEMPT_BY_TYPE.items()
    }
    tokens["total"] = paired_attempts * TOKENS_PER_ATTEMPT_TOTAL
    tokens["cost_usd"] = token_cost(tokens, model_id)
    return tokens


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

    Reference point this reproduces: at psi=0.317 (MCNEMAR_DISCORDANCE_RATE,
    measured on run-20260908-0739-4fc0249e) and n=63, this returns ~19.5pp.
    The formula itself is unchanged; only the constant it defaults to moved,
    from the 0.48 measured across two separate runs to this run's own 0.317
    (see MCNEMAR_DISCORDANCE_RATE's comment for why that is the better
    measurement, not just the newer one).

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
    agent_model = _agent_model_default()
    estimate = bedrock_estimate(paired, agent_model)
    tok_total, tok_cost = estimate["total"], estimate["cost_usd"]

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
            f"experiment: bedrock      tokens ~{tok_total / 1e6:.1f}M "
            f"({paired} attempts x ~{TOKENS_PER_ATTEMPT_TOTAL / 1e6:.2f}M), "
            f"dollar cost unknown (no rate configured for {agent_model!r}; "
            f"see {RATES_FILE.relative_to(REPO_ROOT)})"
        )
    else:
        lines.append(f"experiment: bedrock      tokens ~{tok_total / 1e6:.1f}M, ~${tok_cost:.2f} ({agent_model})")
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
