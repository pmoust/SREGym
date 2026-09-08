#!/usr/bin/env python3
"""Compute a run's headline metrics from its archived result CSVs.

    scripts/benchmark/score_run.py <run-dir> [--json]

Written because the repo ships nothing that does this. Every run so far has had
its numbers recomputed by hand, and the recomputation is where the mistakes
happened: censored attempts quoted next to a paper figure, a mitigation rate
stated without saying which denominator it used, and timing metrics compared
against a baseline measured a different way.

The six metrics the SREGym paper's Table 3 reports are Diag %, Mitig %, E2E %,
TTD, TTM and tokens. This prints all six, and prints the denominator with each
one, because that is the part that gets dropped.

## Absent is not false

`Mitigation.success` is missing on some attempts. Missing does not mean the
agent failed to mitigate; it means the harness killed the agent before the
mitigation oracle ran, so there is no verdict to record.

In run-20260831-1752 the coupling is exact: 18 attempts have no mitigation
verdict, 18 attempts have `timed_out=True`, and they are the same 18. Nothing
lacks a verdict for any other reason. So counting absent as failure does not
understate the agent slightly, it reports a harness timeout as a reasoning
error.

This script therefore refuses to pick one denominator for you. It prints the
strict rate (absent counted as failure, comparable with a paper row that did
the same) and the graded rate (absent excluded, which measures the agent), and
it prints the censored count so the gap between them is never a surprise. The
repo's own `visualizer/queries.py` maps an empty string to False, which is the
strict reading applied silently; that is the thing this output exists to
prevent.

## TTD and TTM

`TTL` in the CSVs is the paper's TTD. The name is inherited from AIOpsLab's
detect/localize/mitigate taxonomy, and the judge's own first dimension is
"D1 Fault Localization". There is no TTD column.

Both TTL and TTM are measured from one clock, reset after deployment and fault
injection, so neither includes cluster setup and TTM is cumulative: it includes
TTL rather than timing the mitigation phase alone. The mitigation phase on its
own is TTM - TTL, reported separately here.

Timings are reported over completed attempts only. A censored attempt's TTL
sits on the agent timeout and averaging it in measures the cap, not the agent.
"""

import argparse
import csv
import json
import os
import statistics
import sys
from pathlib import Path

# cost_model.py is a sibling script, not an installed package; putting its own
# directory on sys.path first makes `import cost_model` resolve the same way
# whether this file is run directly (python's own sys.path[0] already does
# this) or loaded some other way, e.g. the test suite's
# importlib.util.spec_from_file_location.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import cost_model  # noqa: E402

# The paper's Stratus / Sonnet-4.6 row, no noise, for orientation only. Not a
# target: a different agent, a different judge, and 90 problems x 3 rather than
# 21 x 3, so the populations differ. Printed so a number is never read in
# isolation, never to claim parity.
PAPER_STRATUS = {"diag": 61.5, "mitig": 78.5, "e2e": 54.8, "ttd": 114.0, "ttm": 771.0}


def _val(row: dict, key: str) -> str:
    return (row.get(key) or "").strip()


def load(run_dir: Path) -> list[dict]:
    """Every per-attempt row under run_dir.

    Matches only files inside a run_N directory. The per-problem rollup and the
    slot's _ALL_ file are siblings of those, and counting them multiplies the
    row count by about 2.5 (see the progress-output fix in #41).
    """
    rows: list[dict] = []
    for path in sorted(run_dir.rglob("*/run_*/*_results.csv")):
        with path.open(newline="", encoding="utf-8", errors="replace") as fh:
            rows.extend(csv.DictReader(fh))

    return [r for r in rows if _val(r, "problem_id")]


def load_deploy_failed(run_dir: Path) -> list[dict]:
    """deploy_failed marker rows: a deploy that exhausted every retry before
    an attempt ever ran.

    main.py writes the marker only into the slot's _ALL_ rollup, never into a
    run_N directory, because no run happened for that attempt. load()'s glob
    excludes the rollups on purpose, to avoid double-counting the completed
    attempts they also carry (#41); widening that glob to see the marker would
    reintroduce exactly that. Reading the rollups here for deploy_failed rows
    only, and nothing else in them, does not.
    """
    rows: list[dict] = []
    for path in sorted(run_dir.rglob("*_ALL_results.csv")):
        with path.open(newline="", encoding="utf-8", errors="replace") as fh:
            rows.extend(
                row for row in csv.DictReader(fh) if _val(row, "problem_id") and _val(row, "deploy_failed") == "True"
            )

    return rows


def coverage(rows: list[dict], deploy_failed_rows: list[dict], manifest: dict[str, str] | None) -> dict | None:
    """Attempts on record against the run's own declared population.

    The manifest's "problems" field is the fully resolved list
    write_run_manifest.sh recorded this run as intending to cover -- after
    PROBLEMS_OVERRIDE narrowing and named-set union, not a re-derivation from
    PROBLEM_SETS[problem_set], which would overcount whenever a run narrowed
    its selection.

    None when there is nothing to compare against: no manifest archived, or
    one missing the fields needed to compute an expected population. That is
    "unknown", not "clean" -- the caller decides how to say so.
    """
    if not manifest:
        return None

    problems = manifest.get("problems", "").split()
    try:
        n_attempts = int(manifest.get("n_attempts", ""))
    except ValueError:
        return None
    if not problems or n_attempts <= 0:
        return None

    expected = {(pid, attempt) for pid in problems for attempt in range(1, n_attempts + 1)}
    found = {(_val(r, "problem_id"), _int(_val(r, "attempt"))) for r in rows}
    failed = {(_val(r, "problem_id"), _int(_val(r, "attempt"))) for r in deploy_failed_rows}

    return {
        "expected": len(expected),
        "found": len(expected & found),
        "deploy_failed": len(expected & failed),
        "missing": sorted(expected - found - failed),
    }


def _int(raw: str) -> int:
    """A CSV cell as an int, with anything unparseable reading as 0."""
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return 0


# The four token types brain-go reports, all input-side except output. Both
# providers report cache reads and writes OUTSIDE the input count, so these sum
# to real consumption without double counting. There is no cached output:
# output tokens are generated every time.
_TOKEN_COLUMNS = ("tokens_input", "tokens_output", "tokens_cache_read", "tokens_cache_write")


def _per_problem_tokens(rows: list[dict]) -> list[dict]:
    """Per-attempt token spend, from the row rather than from a scrape.

    Recorded by main.py as a delta bracketing each attempt (sregym#67). Before
    that column existed a run had one total and no attribution, because the
    counters are labelled by thread and the driver's thread is the
    investigation's cli:inv-<uuid>, which carries no problem id.

    WARNING: every tokens_*/llm_calls figure this returns is inflated, by
    about 2.41x measured on run-20260908-0739-4fc0249e. brain-go runs one
    shared stack per box across every slot, and its Prometheus counters are
    process-wide, so token_delta.py's bracketing of one attempt also captures
    whatever every other slot on the same box spent concurrently. tool_calls
    is not affected -- the driver counts those off its own SSE stream, not a
    shared counter. See token_delta.py's module docstring for how the 2.41x
    was measured, and use tokens()'s whole-run total below for a trustworthy
    figure instead of summing this table.

    Rows with no token columns are skipped rather than reported as zero, so an
    older archive reads as unmeasured instead of free.
    """
    out = []
    for row in rows:
        if not any(_val(row, column) for column in (*_TOKEN_COLUMNS, "tool_calls")):
            continue

        counts = {column.removeprefix("tokens_"): _int(_val(row, column)) for column in _TOKEN_COLUMNS}
        out.append(
            {
                "problem_id": _val(row, "problem_id"),
                "attempt": _val(row, "attempt"),
                "llm_calls": _int(_val(row, "llm_calls")),
                "tool_calls": _int(_val(row, "tool_calls")),
                **counts,
                "total": sum(counts.values()),
            }
        )

    return out


def _rate(correct: int, total: int) -> float | None:
    return None if total == 0 else round(100.0 * correct / total, 1)


def score(rows: list[dict], deploy_failed_rows: list[dict] | None = None) -> dict:
    attempts = len(rows)
    deploy_failed_rows = deploy_failed_rows or []

    diag_graded = [r for r in rows if _val(r, "Diagnosis.success")]
    diag_ok = [r for r in diag_graded if _val(r, "Diagnosis.success") == "True"]
    mitig_graded = [r for r in rows if _val(r, "Mitigation.success")]
    mitig_ok = [r for r in mitig_graded if _val(r, "Mitigation.success") == "True"]
    # Anything neither True nor False was counted as a graded failure, silently
    # moving the rate. Collected so the reader is told rather than left guessing.
    odd_verdicts = sorted(
        {
            _val(r, key)
            for r in rows
            for key in ("Diagnosis.success", "Mitigation.success")
            if _val(r, key) not in ("", "True", "False")
        }
    )

    # Both correct. Absent mitigation cannot be an end-to-end success either
    # way, so E2E has no graded/strict split on the numerator, only on the
    # denominator.
    e2e_ok = [r for r in rows if _val(r, "Diagnosis.success") == "True" and _val(r, "Mitigation.success") == "True"]
    both_graded = [r for r in rows if _val(r, "Diagnosis.success") and _val(r, "Mitigation.success")]

    censored = [r for r in rows if _val(r, "timed_out") == "True"]

    # Deploys that produced no attempt. start_problem waits 600s on pod
    # readiness, and a retry tears the problem down and redeploys it, so one
    # retry costs 35 to 40 minutes of the run's wall clock (#53). Reported
    # because it does not touch any rate: a run that wasted a dozen deploys
    # scores exactly like one that wasted none and finishes an hour later.
    #
    # Rows written before deploy_attempts existed have no column, which reads
    # as 0 wasted rather than as unknown. Counting the rows that carry it is
    # what tells the two apart.
    retried = [r for r in rows if _int(_val(r, "deploy_attempts")) > 1]
    wasted_deploys = sum(_int(_val(r, "deploy_attempts")) - 1 for r in retried)
    deploy_attempts_known = len([r for r in rows if _val(r, "deploy_attempts")])

    absent_mitig = [r for r in rows if not _val(r, "Mitigation.success")]
    # The claim in this file's docstring, checked against the data in hand
    # rather than asserted from one run.
    absent_and_censored = [r for r in absent_mitig if _val(r, "timed_out") == "True"]

    def floats(key: str, only: list[dict]) -> list[float]:
        out = []
        for r in only:
            try:
                out.append(float(_val(r, key)))
            except ValueError:
                continue

        return out

    completed = [r for r in rows if _val(r, "timed_out") != "True"]
    ttl = floats("TTL", completed)
    ttm = floats("TTM", completed)
    # Parsed defensively, the way floats() above already is. This called
    # float() directly, so one unparseable TTL took the whole run's report
    # down with a traceback while every other field degraded quietly. The
    # CSVs already carry nine header shapes (#39), so a malformed numeric
    # field is not far-fetched.
    phase = []
    for r in completed:
        try:
            phase.append(float(_val(r, "TTM")) - float(_val(r, "TTL")))
        except ValueError:
            continue

    def stats(xs: list[float]) -> dict | None:
        if not xs:
            return None

        return {
            "n": len(xs),
            "mean": round(statistics.mean(xs), 1),
            "median": round(statistics.median(xs), 1),
            "sd": round(statistics.stdev(xs), 1) if len(xs) > 1 else 0.0,
        }

    # The paper imputes the cap for a timed-out run and includes it, so failure
    # cost lands in the mean instead of being selected out. Excluding censored
    # attempts answers a different question, "how long does the agent take when
    # it finishes", and both are worth having: the first is what a Table 3
    # figure can be compared against, the second is what describes the agent.
    # Reporting only one of them silently picks a convention, which is the thing
    # this script exists not to do.
    cap = None
    for r in rows:
        try:
            cap = max(cap or 0.0, float(_val(r, "agent_timeout_seconds")))
        except ValueError:
            continue
    if cap is None and censored:
        # No column, so fall back to the longest censored attempt, which sits on
        # the cap by definition.
        observed = [x for x in (floats("TTL", censored) or []) if x]
        cap = max(observed) if observed else None

    ttl_capped = floats("TTL", completed) + [cap] * len(censored) if cap else []
    ttm_capped = floats("TTM", completed) + [cap] * len(censored) if cap else []

    problems = {_val(r, "problem_id") for r in rows}

    # A deploy_failed row is neither a graded failure nor an absent verdict:
    # nothing ran, so there is no result to grade and no oracle that was
    # skipped. This file's own absent-is-not-false reasoning for mitigation
    # applies here too and did not yet cover it (#51): counting it as either
    # would understate or overstate the agent for something the harness, not
    # the agent, gave up on.
    deploy_failed = sorted(
        (_val(r, "problem_id"), _int(_val(r, "attempt"))) for r in deploy_failed_rows if _val(r, "problem_id")
    )

    return {
        "attempts": attempts,
        "problems": len(problems),
        "deploy_failed": {"count": len(deploy_failed), "attempts": deploy_failed},
        "diagnosis": {
            "correct": len(diag_ok),
            "graded": len(diag_graded),
            "strict_pct": _rate(len(diag_ok), attempts),
            "graded_pct": _rate(len(diag_ok), len(diag_graded)),
            "ungraded": attempts - len(diag_graded),
        },
        "mitigation": {
            "correct": len(mitig_ok),
            "graded": len(mitig_graded),
            "strict_pct": _rate(len(mitig_ok), attempts),
            "graded_pct": _rate(len(mitig_ok), len(mitig_graded)),
            "absent": len(absent_mitig),
            "absent_all_censored": (None if not absent_mitig else len(absent_mitig) == len(absent_and_censored)),
        },
        "e2e": {
            "correct": len(e2e_ok),
            "strict_pct": _rate(len(e2e_ok), attempts),
            "graded_pct": _rate(len(e2e_ok), len(both_graded)),
            "graded": len(both_graded),
        },
        "censored": len(censored),
        "per_problem_tokens": _per_problem_tokens(rows),
        "wasted_deploys": wasted_deploys,
        "retried_attempts": len(retried),
        "deploy_attempts_known": deploy_attempts_known,
        "odd_verdicts": odd_verdicts,
        "ttd_seconds": stats(ttl),
        "ttm_seconds": stats(ttm),
        "ttd_seconds_cap_imputed": stats(ttl_capped),
        "ttm_seconds_cap_imputed": stats(ttm_capped),
        "timeout_cap_seconds": cap,
        "mitigation_phase_seconds": stats(phase),
    }


def _log_roots(run_dir: Path, env_override: str | None = None) -> list[Path]:
    """Every directory that might hold this run's logs, results tree excluded.

    archive_run.sh splits a run in two: results under sregym-results/<run-id>
    and logs under sregym-run-logs/<run-id>, siblings of each other. A caller
    passing the results directory, which is the directory a person passes,
    finds nothing in the log tree unless it is derived. The smoke run on
    2026-09-01 archived 8 real token-scrape series and was reported as having
    none, for exactly this reason.

    env_override adds a third root, for an archive laid out some other way. It
    adds rather than replaces: a file found by the default derivation still
    counts, so setting it cannot silently hide data that was already found.
    """
    roots = [run_dir]

    if env_override:
        roots.append(Path(env_override))

    # The sibling log tree, derived rather than assumed: only added when the
    # path really does contain the results directory name.
    #
    # Both the given path and the resolved one, because resolve() dereferences
    # symlinks first. Review found that a symlinked sregym-results directory
    # resolves to a name without "sregym-results" in it, so the derivation was
    # skipped and the target file went unfound: the same "not archived"
    # failure this function exists to remove, reached a different way.
    for candidate in (run_dir, run_dir.resolve()):
        parts = list(candidate.parts)
        if "sregym-results" not in parts:
            continue
        # rindex, not index: the run directory is the deepest occurrence, so a
        # path nested under a backup directory of the same name still derives
        # the sibling next to the run rather than next to the backup.
        last = len(parts) - 1 - parts[::-1].index("sregym-results")
        parts[last] = "sregym-run-logs"
        roots.append(Path(*parts))

    return [root for root in roots if root.is_dir()]


def _token_files(run_dir: Path) -> list[Path]:
    """Every token scrape belonging to this run, in both trees it can land in.

    No file-level dedupe. Reading the same scrape twice is harmless: a series
    is keyed by its whole label set and collapsed with max(), so a repeat of
    the same line contributes nothing. An earlier version resolved paths into
    a seen-set for this, which no mutation could reach, because the max was
    already doing the work.
    """
    files: list[Path] = []
    for root in _log_roots(run_dir, os.environ.get("SREGYM_TOKEN_DIR")):
        files.extend(root.rglob("*tokens*.prom"))

    return files


def _manifest_files(run_dir: Path) -> list[Path]:
    """Every archived run manifest, in the log tree write_run_manifest.sh's
    output lands in (see _log_roots). One per shard, since each instance
    writes and ships its own; they agree on everything but shard_index."""
    files: list[Path] = []
    for root in _log_roots(run_dir):
        files.extend(root.rglob("sregym-run-manifest-*.txt"))

    return files


def read_manifest(run_dir: Path) -> dict[str, str] | None:
    """This run's own recorded configuration, or None if none was archived.

    write_run_manifest.sh writes one KEY=VALUE per line; a line with no "="
    (the free-form BRAIN_ENV_EXTRA block can be blank) is skipped rather than
    raising. None rather than {} when nothing is found, so a caller can tell
    "no manifest archived" apart from "a manifest with nothing useful in it"
    without checking key membership twice.
    """
    files = _manifest_files(run_dir)
    if not files:
        return None

    # Any one: every shard's manifest resolves the same full problem list and
    # n_attempts, because write_run_manifest.sh derives them the same way
    # run_shard.sh does, before either shards by index. Sorted for a
    # deterministic pick across runs.
    text = sorted(files)[0].read_text(encoding="utf-8", errors="replace")
    return dict(line.split("=", 1) for line in text.strip("\n").split("\n") if "=" in line)


def tokens(run_dir: Path) -> dict | None:
    """Per-run token totals from the Prometheus scrape, if it was archived.

    A Prometheus counter is identified by its whole label set, and
    llm_tokens_total carries thread_id, which the driver sets per problem. So a
    run has one series per (problem, token_type), not one series per
    token_type.

    That means the aggregation is: take the maximum within each series, because
    a counter is cumulative and scrape_tokens.sh may write several scrapes, then
    sum across series, because different problems consumed different tokens.

    Doing max() across series instead keeps the single busiest problem and
    discards every other one. Review measured that at roughly 35x low on a
    realistic 63-thread scrape, printed in the same format as a correct number,
    for the one Table 3 metric that has never been measured at all. A silently
    low figure is worse than the "not archived" line this returns without data.

    Also breaks the same totals out by node (brain-go#2687: which RCI node or
    signal issued the call), under "by_node" -- this is the whole-run answer
    to the question token_delta.py's delta_by_node() answers per attempt.
    node is one more label on the same series, not a new dimension to scrape,
    so it costs nothing extra to keep alongside token_type.
    """
    # Keyed by the full label set, so two processes scraping the same run (chat
    # on 9091 and tworker on 9092) contribute their own series rather than one
    # overwriting the other.
    series: dict[str, float] = {}
    kinds: dict[str, str] = {}
    nodes: dict[str, str] = {}
    found = False

    for prom in sorted(_token_files(run_dir)):
        found = True
        for line in prom.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line.startswith("llm_tokens_total{") or " " not in line:
                continue
            labels, _, raw = line.rpartition(" ")
            try:
                value = float(raw)
            except ValueError:
                continue
            if 'token_type="' not in labels:
                continue
            kind = labels.split('token_type="', 1)[1].split('"', 1)[0]
            node = labels.split('node="', 1)[1].split('"', 1)[0] if 'node="' in labels else "unknown"
            series[labels] = max(series.get(labels, 0.0), value)
            kinds[labels] = kind
            nodes[labels] = node

    if not found:
        return None

    totals: dict[str, float] = {}
    by_node: dict[str, dict[str, float]] = {}
    for labels, value in series.items():
        totals[kinds[labels]] = totals.get(kinds[labels], 0.0) + value
        node_totals = by_node.setdefault(nodes[labels], {})
        node_totals[kinds[labels]] = node_totals.get(kinds[labels], 0.0) + value

    out = {k: int(v) for k, v in sorted(totals.items())}
    out["series"] = len(series)
    out["by_node"] = {
        node: {k: int(v) for k, v in sorted(node_totals.items())} for node, node_totals in sorted(by_node.items())
    }

    return out


def cost_summary(tok: dict | None, model_id: str | None) -> dict:
    """Dollar cost of a run's tokens(), total and by node, at `model_id`'s
    rate in terraform/rates.yml (cost_model.py's own convention: never
    guessed, so total_usd/by_node_usd read None rather than a wrong number
    when the model is unknown or unpriced).

    `model_id` is expected to be the run's own manifest agent_model, since
    tokens() is entirely brain-go's Prometheus counters and the judge model
    runs via LiteLLM on the host, never through brain-go -- so there is
    nothing here to attribute to the judge in the first place, priced or not.
    """
    by_node = (tok or {}).get("by_node") or {}
    counts = {k: v for k, v in (tok or {}).items() if k not in ("series", "by_node")}

    return {
        "model": model_id,
        "total_usd": cost_model.token_cost(counts, model_id) if tok else None,
        "by_node_usd": {node: cost_model.token_cost(node_counts, model_id) for node, node_counts in by_node.items()},
    }


def _pct(value: float | None) -> str:
    """A rate with no denominator is not zero. Say so rather than print 0.0%."""
    return "  n/a" if value is None else f"{value:>4}%"


def render(s: dict, tok: dict | None, cov: dict | None = None, model_id: str | None = None) -> str:
    out: list[str] = []
    d, m, e = s["diagnosis"], s["mitigation"], s["e2e"]
    out.append(f"{s['problems']} problems, {s['attempts']} attempts, {s['censored']} censored by the agent timeout")
    if s["deploy_attempts_known"] == 0:
        out.append("  deploy retries: not recorded in this run's rows")
    elif s["wasted_deploys"]:
        out.append(
            f"  deploy retries: {s['wasted_deploys']} deploy(s) produced no attempt, "
            f"across {s['retried_attempts']} of {s['deploy_attempts_known']} attempts"
        )
    dep_failed = s.get("deploy_failed") or {"count": 0, "attempts": []}
    if dep_failed["count"]:
        out.append(
            f"  deploy exhausted: {dep_failed['count']} attempt(s) never ran at all, "
            "not a graded failure and not an absent verdict:"
        )
        for pid, attempt in dep_failed["attempts"]:
            out.append(f"    {pid} attempt {attempt}")

    if cov is not None:
        out.append(
            f"  coverage: {cov['found']}/{cov['expected']} expected attempts on record"
            + (f", {cov['deploy_failed']} more exhausted their deploy" if cov["deploy_failed"] else "")
        )
        if cov["missing"]:
            out.append(f"  {len(cov['missing'])} expected attempt(s) missing with no record at all:")
            for pid, attempt in cov["missing"]:
                out.append(f"    {pid} attempt {attempt}")
    out.append("")
    out.append("                 strict          graded          note")
    out.append(
        f"  Diagnosis      {_pct(d['strict_pct'])}  {d['correct']}/{s['attempts']}"
        f"    {_pct(d['graded_pct'])}  {d['correct']}/{d['graded']}"
        f"    {d['ungraded']} attempt(s) never graded"
    )
    out.append(
        f"  Mitigation     {_pct(m['strict_pct'])}  {m['correct']}/{s['attempts']}"
        f"    {_pct(m['graded_pct'])}  {m['correct']}/{m['graded']}"
        f"    {m['absent']} absent" + (", all censored" if m["absent_all_censored"] and m["absent"] else "")
    )
    out.append(
        f"  End to end     {_pct(e['strict_pct'])}  {e['correct']}/{s['attempts']}"
        f"    {_pct(e['graded_pct'])}  {e['correct']}/{e['graded']}"
    )
    out.append("")
    out.append("  strict counts an absent verdict as a failure; graded excludes it.")
    if m["absent"] and m["absent_all_censored"]:
        out.append("  Every absent mitigation verdict here is an attempt the harness killed")
        out.append("  before the oracle ran, so strict reports those timeouts as reasoning")
        out.append("  errors. Say which column you are quoting.")
    elif m["graded"] == 0:
        out.append("  No attempt in this run produced a mitigation verdict at all, and none")
        out.append("  was censored, so the stage did not run rather than failing. Nothing in")
        out.append("  the results says why: check the run manifest's stages line, and whether")
        out.append("  a tasklist.yml was present. Do not report a mitigation or E2E figure")
        out.append("  for this run.")
    elif m["absent"]:
        out.append("  Some absent verdicts are not explained by a timeout, so neither column")
        out.append("  is clean. Find out why before quoting either.")

    out.append("")
    for label, key in (
        ("TTD", "ttd_seconds"),
        ("TTM (cumulative)", "ttm_seconds"),
        ("Mitigation phase", "mitigation_phase_seconds"),
    ):
        st = s[key]
        if st is None:
            out.append(f"  {label:<18} no completed attempts to measure")
            continue
        out.append(
            f"  {label:<18} mean {st['mean']:>7.1f}s   median {st['median']:>7.1f}s   sd {st['sd']:>6.1f}   n={st['n']}"
        )
    if s.get("timeout_cap_seconds") and s["censored"]:
        out.append("")
        for label, key in (
            ("TTD cap-imputed", "ttd_seconds_cap_imputed"),
            ("TTM cap-imputed", "ttm_seconds_cap_imputed"),
        ):
            st = s[key]
            if st is None:
                continue
            out.append(
                f"  {label:<18} mean {st['mean']:>7.1f}s   median {st['median']:>7.1f}s"
                f"   sd {st['sd']:>6.1f}   n={st['n']}"
            )
    out.append("")
    out.append("  The first pair covers completed attempts only, which describes the agent")
    out.append("  when it finishes. The cap-imputed pair gives a censored attempt the")
    cap = s.get("timeout_cap_seconds")
    cap_text = f"{cap:.0f}s" if cap else "the agent timeout"
    out.append(f"  timeout ({cap_text}) and includes it, which is what the paper")
    out.append("  does, so it is the pair to compare against a Table 3 figure. Quoting the")
    out.append("  completed-only mean beside a paper number compares two different")
    out.append("  statistics.")
    out.append("  TTM shares TTD's clock, so it includes TTD.")

    out.append("")
    if tok:
        counts = {k: v for k, v in tok.items() if k not in ("series", "by_node")}
        fresh = counts.get("input", 0) + counts.get("output", 0)
        cached = counts.get("cache_read", 0) + counts.get("cache_write", 0)
        cost = cost_summary(tok, model_id)
        out.append(f"  Tokens             {fresh + cached:,} across {tok.get('series', 0)} thread series")
        for kind in sorted(counts):
            out.append(f"    {kind:<16} {counts[kind]:>12,}")
        if cost["total_usd"] is not None:
            out.append(f"    {'cost':<16} {'$' + format(cost['total_usd'], ',.2f'):>12}")
            out.append(f"  at {model_id}'s rate (terraform/rates.yml). Agent tokens only: the judge")
            out.append("  runs via LiteLLM on the host, never through brain-go, so its cost is")
            out.append("  not in these counters at all and is not included above.")
        elif model_id is None:
            out.append("    cost               unknown: no manifest model recorded for this run")
        else:
            out.append(f"    cost               unknown: no rate configured for {model_id!r} in terraform/rates.yml")
        if cached:
            # Reporting input+output alone hid a cache_read four times larger
            # than every other column combined, which is most of what the run
            # actually consumed and is priced differently.
            out.append("  input and output are the fresh tokens; cache_read and cache_write")
            out.append("  are billed at different rates, so quote which total you mean.")
            out.append("  There is no cached output: output tokens are generated every time.")

        by_node = tok.get("by_node") or {}
        if by_node:
            out.append("")
            out.append("  By node (which RCI node or signal spent them):")
            for node in sorted(by_node):
                node_counts = by_node[node]
                node_fresh = node_counts.get("input", 0) + node_counts.get("output", 0)
                node_cached = node_counts.get("cache_read", 0) + node_counts.get("cache_write", 0)
                node_cost = cost["by_node_usd"].get(node)
                cost_suffix = f"   ${node_cost:,.2f}" if node_cost is not None else ""
                out.append(f"    {node:<24} {node_fresh + node_cached:>12,}{cost_suffix}")

    else:
        out.append("  Tokens             not archived for this run")
        out.append("  scrape_tokens.sh writes sregym-tokens.prom into the log tree; check")
        out.append("  that sync_logs ran, or point SREGYM_TOKEN_DIR at it.")

    per_problem = s.get("per_problem_tokens") or []
    if per_problem:
        out.append("")
        out.append("  Per attempt, from the result rows rather than the scrape:")
        out.append(
            f"    {'problem':<40} {'att':>3} {'llm':>5} {'tool':>5}"
            f" {'input':>9} {'output':>8} {'cache_rd':>10} {'cache_wr':>9}"
        )
        for row in sorted(per_problem, key=lambda r: (r["problem_id"], r["attempt"])):
            out.append(
                f"    {row['problem_id'][:40]:<40} {row['attempt']:>3}"
                f" {row['llm_calls']:>5,} {row['tool_calls']:>5,}"
                f" {row['input']:>9,} {row['output']:>8,} {row['cache_read']:>10,} {row['cache_write']:>9,}"
            )
        out.append("")
        out.append("  llm is model calls, tool is tool invocations; they answer different")
        out.append("  questions and neither implies the other.")
        out.append("  A row of zeros means the counters did not move, not that they were")
        out.append("  unmeasured: an attempt recording none of these is omitted here.")
        out.append("")
        out.append("  WARNING: llm and every token column above (not tool) are inflated by")
        out.append("  about 2.41x: brain-go's counters are shared by every slot on the same")
        out.append("  box, and this table brackets by wall-clock time, not by slot, so a")
        out.append("  concurrent slot's spend leaks into these rows. The Tokens total above,")
        out.append("  from the whole-run scrape, is not affected and is the number to trust.")

    out.append("")
    out.append(
        "  For orientation, the paper's Stratus row: "
        f"Diag {PAPER_STRATUS['diag']}%, Mitig {PAPER_STRATUS['mitig']}%, "
        f"E2E {PAPER_STRATUS['e2e']}%, TTD {PAPER_STRATUS['ttd']}s, TTM {PAPER_STRATUS['ttm']}s."
    )
    out.append("  Not a target and not like-for-like: a different agent, a different judge,")
    out.append("  and 90 problems x 3 rather than 21 x 3. Compare its timings against the")
    out.append("  cap-imputed pair above rather than the completed-only pair, because that")
    out.append("  is the convention it used.")

    return "\n".join(out)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", type=Path, help="an archived run directory")
    ap.add_argument("--json", action="store_true", help="emit the numbers as JSON")
    args = ap.parse_args(argv[1:])

    if not args.run_dir.is_dir():
        print(f"score_run: not a directory: {args.run_dir}", file=sys.stderr)
        return 2

    rows = load(args.run_dir)
    if not rows:
        print(f"score_run: no per-attempt result rows under {args.run_dir}", file=sys.stderr)
        print("score_run: expected files matching */run_N/*_results.csv", file=sys.stderr)
        return 1

    deploy_failed_rows = load_deploy_failed(args.run_dir)
    manifest = read_manifest(args.run_dir)
    cov = coverage(rows, deploy_failed_rows, manifest)

    s = score(rows, deploy_failed_rows)
    tok = tokens(args.run_dir)
    # The run's own recorded agent model, not an assumption: tokens() is
    # entirely brain-go's counters, and brain-go only ever answers with
    # agent_model (the judge runs via LiteLLM on the host, never through
    # brain-go, so there is no judge rate to select here either).
    model_id = (manifest or {}).get("agent_model") or None
    cost = cost_summary(tok, model_id)
    if args.json:
        print(json.dumps({**s, "coverage": cov, "tokens": tok, "tokens_cost": cost}, indent=2))
    else:
        print(render(s, tok, cov, model_id))
        if cov is None:
            if manifest is None:
                print(
                    "\nscore_run: no run manifest found under this run's log tree; cannot tell "
                    "whether every expected problem and attempt made it into this report",
                    file=sys.stderr,
                )
            else:
                print(
                    "\nscore_run: run manifest found but missing a usable problems/n_attempts "
                    "field; cannot tell whether every expected problem and attempt made it "
                    "into this report",
                    file=sys.stderr,
                )

    if cov is not None and cov["missing"]:
        print(
            f"score_run: {len(cov['missing'])} expected attempt(s) never landed "
            f"({cov['found']}/{cov['expected']}); see the missing list above",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
