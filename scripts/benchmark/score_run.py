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
import statistics
import sys
from pathlib import Path

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


def _rate(correct: int, total: int) -> float | None:
    return None if total == 0 else round(100.0 * correct / total, 1)


def score(rows: list[dict]) -> dict:
    attempts = len(rows)

    diag_graded = [r for r in rows if _val(r, "Diagnosis.success")]
    diag_ok = [r for r in diag_graded if _val(r, "Diagnosis.success") == "True"]
    mitig_graded = [r for r in rows if _val(r, "Mitigation.success")]
    mitig_ok = [r for r in mitig_graded if _val(r, "Mitigation.success") == "True"]

    # Both correct. Absent mitigation cannot be an end-to-end success either
    # way, so E2E has no graded/strict split on the numerator, only on the
    # denominator.
    e2e_ok = [r for r in rows if _val(r, "Diagnosis.success") == "True" and _val(r, "Mitigation.success") == "True"]
    both_graded = [r for r in rows if _val(r, "Diagnosis.success") and _val(r, "Mitigation.success")]

    censored = [r for r in rows if _val(r, "timed_out") == "True"]
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
    phase = [float(_val(r, "TTM")) - float(_val(r, "TTL")) for r in completed if _val(r, "TTM") and _val(r, "TTL")]

    def stats(xs: list[float]) -> dict | None:
        if not xs:
            return None

        return {
            "n": len(xs),
            "mean": round(statistics.mean(xs), 1),
            "median": round(statistics.median(xs), 1),
            "sd": round(statistics.stdev(xs), 1) if len(xs) > 1 else 0.0,
        }

    problems = {_val(r, "problem_id") for r in rows}

    return {
        "attempts": attempts,
        "problems": len(problems),
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
            "absent_all_censored": len(absent_mitig) == len(absent_and_censored),
        },
        "e2e": {
            "correct": len(e2e_ok),
            "strict_pct": _rate(len(e2e_ok), attempts),
            "graded_pct": _rate(len(e2e_ok), len(both_graded)),
            "graded": len(both_graded),
        },
        "censored": len(censored),
        "ttd_seconds": stats(ttl),
        "ttm_seconds": stats(ttm),
        "mitigation_phase_seconds": stats(phase),
    }


def tokens(run_dir: Path) -> dict | None:
    """Per-run token totals from the Prometheus scrape, if it was archived.

    brain-go labels llm_tokens_total by thread_id and the driver sets one per
    problem, so this is attributable. Counters are cumulative, so the last
    scrape before termination is the one that counts; scrape_tokens.sh writes
    exactly that.
    """
    totals: dict[str, float] = {}
    found = False
    for prom in sorted(run_dir.rglob("*tokens*.prom")):
        found = True
        for line in prom.read_text(errors="replace").splitlines():
            if not line.startswith("llm_tokens_total"):
                continue
            try:
                labels, value = line.rsplit(" ", 1)
                kind = labels.split('token_type="', 1)[1].split('"', 1)[0]
                totals[kind] = max(totals.get(kind, 0.0), float(value))
            except (IndexError, ValueError):
                continue

    if not found:
        return None

    return {k: int(v) for k, v in sorted(totals.items())}


def _pct(value: float | None) -> str:
    """A rate with no denominator is not zero. Say so rather than print 0.0%."""
    return "  n/a" if value is None else f"{value:>4}%"


def render(s: dict, tok: dict | None) -> str:
    out: list[str] = []
    d, m, e = s["diagnosis"], s["mitigation"], s["e2e"]
    out.append(f"{s['problems']} problems, {s['attempts']} attempts, {s['censored']} censored by the agent timeout")
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
    out.append("  Completed attempts only. A censored attempt's TTL sits on the cap, so")
    out.append("  averaging it in measures the timeout rather than the agent.")
    out.append("  TTM shares TTD's clock, so it includes TTD.")

    out.append("")
    if tok:
        total = sum(v for k, v in tok.items() if k in ("input", "output"))
        out.append(f"  Tokens             {total or sum(tok.values()):,}   {tok}")
    else:
        out.append("  Tokens             not archived for this run")

    out.append("")
    out.append("  For orientation, the paper's Stratus row (different agent, judge and")
    out.append(
        "  problem set, so not a target): "
        f"Diag {PAPER_STRATUS['diag']}%, Mitig {PAPER_STRATUS['mitig']}%, "
        f"E2E {PAPER_STRATUS['e2e']}%, TTD {PAPER_STRATUS['ttd']}s, TTM {PAPER_STRATUS['ttm']}s."
    )

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

    s = score(rows)
    tok = tokens(args.run_dir)
    if args.json:
        print(json.dumps({**s, "tokens": tok}, indent=2))
    else:
        print(render(s, tok))

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
