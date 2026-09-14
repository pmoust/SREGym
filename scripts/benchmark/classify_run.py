#!/usr/bin/env python3
"""Failure classes and tool-call attribution for one benchmark run, or two compared.

score_run.py answers how many attempts were graded correct. This answers a
different question: of the ones that were not, how many never produced a
reasoned answer at all, and where did the tool calls go.

The distinction matters because the two failure modes call for different work. An
attempt that gathered evidence and reached the wrong conclusion is a reasoning
problem. An attempt that gathered evidence and then submitted a template saying
no evidence was collected is a plumbing problem, and it scores identically to
the first while telling you nothing about the model.

Reads two artifacts per run, both written by the harness:

  brain_go_ALL_results.csv   one row per attempt, with the graded verdicts, the
                             submitted diagnosis text, and the tool and token
                             counts.
  tool-calls.json            one file per attempt, counted by the driver from
                             the agent's own stream rather than from Prometheus.

The harness writes tool-calls.json twice: once under the attempt's own results
directory, keyed by problem and attempt, and once under .runtime/brain_go, keyed
by an opaque per-investigation hash. Only the first is read here, matched by its
path: the pattern requires a results/.../run_N/ shape, which the .runtime copy
never has, so it is excluded on path shape alone, regardless of glob's
hidden-directory handling. A pattern that matched the file name anywhere, with
no such path constraint, would count 115 files for 63 attempts and inflate
every share below it, and it would be the one relying on an accident instead:
right only while the runtime directory stays hidden and include_hidden is
never passed.

That provenance is why the by-node figures here are safe to compare across runs
while token figures are not. Each box runs three slots against one brain-go
process whose Prometheus counters are process-wide, so a per-node token figure
scraped before the per-slot account filter landed sums three concurrent slots.
The driver counts its own attempt's calls, so it was never affected.
"""

from __future__ import annotations

import argparse
import collections
import csv
import glob
import json
import os
import re
import statistics
import sys

# The zero-evidence diagnosis template, matched on the clause that carries its
# claim. Every instance in run-20260908-0739 was byte-identical, so a substring
# is enough and is more durable than pinning the whole text: the surrounding
# wording has already changed once.
ZERO_EVIDENCE_MARK = "could not produce a diagnosis because no observ"

# brain-go's node name for the hypothesis-generation planner's own tool calls.
PLANNER_NODE = "hypothesis_gen_tools"


def _int(row: dict, key: str) -> int | None:
    raw = (row.get(key) or "").strip()

    return int(raw) if raw.lstrip("-").isdigit() else None


def _truthy(value: object) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes")


def load_attempts(run_dir: str) -> list[dict]:
    """Every attempt row in the run, from the per-problem aggregate CSVs.

    The per-problem *_results.csv files carry the same verdicts but not the tool
    or token counts, so the aggregate is the one worth reading.
    """
    rows: list[dict] = []

    for path in sorted(glob.glob(f"{run_dir}/**/brain_go_ALL_results.csv", recursive=True)):
        with open(path, newline="") as handle:
            rows.extend(csv.DictReader(handle))

    return rows


def attempt_key(path: str) -> tuple[str, str] | None:
    """The (problem, attempt) a canonical tool-calls.json belongs to.

    The path ends .../brain_go/<problem>/run_<n>/tool-calls.json, which is the
    only place the per-attempt identity is recorded: the file itself holds
    counts and no identifier.
    """
    match = re.search(r"/([^/]+)/run_(\d+)/tool-calls\.json$", path)

    return (match.group(1), match.group(2)) if match else None


def load_tool_calls(run_dir: str) -> tuple[collections.Counter, dict[str, collections.Counter], int, dict]:
    """Tool calls by node, by phase and node, the run total, and per attempt.

    Matched on the results path rather than on the file name, for the reason in
    the module docstring: the same file exists a second time under a hidden
    runtime directory and counting both doubles most of the run.
    """
    by_node: collections.Counter = collections.Counter()
    by_phase_node: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    per_attempt: dict[tuple[str, str], dict] = {}
    total = 0

    for path in sorted(glob.glob(f"{run_dir}/**/results/**/run_*/tool-calls.json", recursive=True)):
        with open(path) as handle:
            data = json.load(handle)

        count = data.get("total") or 0
        count = sum(count.values()) if isinstance(count, dict) else count
        total += count
        by_node.update(data.get("by_node") or {})

        for phase, counter in (data.get("by_phase_and_node") or {}).items():
            by_phase_node[phase].update(counter)

        key = attempt_key(path)
        if key:
            per_attempt[key] = data

    return by_node, dict(by_phase_node), total, per_attempt


def classify(rows: list[dict]) -> dict:
    """Split the attempts into the three outcomes that need different work."""
    correct, boilerplate, wrong = [], [], []

    for row in rows:
        if _truthy(row.get("Diagnosis.success")):
            correct.append(row)
        elif ZERO_EVIDENCE_MARK in (row.get("Diagnosis.submission") or ""):
            boilerplate.append(row)
        else:
            wrong.append(row)

    reasoned = correct + wrong

    return {
        "attempts": len(rows),
        "correct": len(correct),
        "boilerplate": len(boilerplate),
        "wrong": len(wrong),
        "mitigated": sum(1 for r in rows if _truthy(r.get("Mitigation.success"))),
        # A boilerplate attempt that mitigated the fault is the sharpest case:
        # the agent fixed the incident and then reported it had no evidence.
        "boilerplate_mitigated": sum(1 for r in boilerplate if _truthy(r.get("Mitigation.success"))),
        "diagnosis_rate": len(correct) / len(rows) if rows else None,
        # The rate among attempts that actually answered. Not a better number,
        # a different one: it is what the model scores when the plumbing does
        # not eat the answer, and the gap between the two is the cost of the
        # plumbing.
        "diagnosis_rate_reasoned": len(correct) / len(reasoned) if reasoned else None,
        "boilerplate_rows": boilerplate,
    }


def spread(rows: list[dict], key: str) -> str:
    values = sorted(v for v in (_int(r, key) for r in rows) if v is not None)

    if not values:
        return "n/a"

    return f"min {values[0]} / med {int(statistics.median(values))} / max {values[-1]}"


def analyze(run_dir: str) -> dict | None:
    """Every number report() prints, computed with no output of its own.

    None when run_dir has no brain_go_ALL_results.csv at all, so the caller
    can say so and exit; every other case returns a summary carrying both the
    numbers already in classify()'s dict and the tool-call figures report()
    renders, so render() only formats and report() only prints.
    """
    rows = load_attempts(run_dir)

    if not rows:
        return None

    summary = classify(rows)
    by_node, by_phase_node, tool_total, per_attempt = load_tool_calls(run_dir)
    csv_total = sum(v for v in (_int(r, "tool_calls") for r in rows) if v is not None)

    summary["rows"] = rows
    summary["by_node"] = by_node
    summary["by_phase_node"] = by_phase_node
    summary["tool_total"] = tool_total
    summary["csv_total"] = csv_total
    summary["planner_share"] = by_node.get(PLANNER_NODE, 0) / sum(by_node.values()) if by_node else None

    # Per attempt, not run-wide: whether the attempts that gave up on reporting
    # were also the ones that spent their calls in the planner is the question
    # the run-wide share cannot answer.
    template_keys = {(r.get("problem_id", ""), str(r.get("attempt", ""))) for r in summary["boilerplate_rows"]}
    template_shares, reasoned_shares = [], []

    for key, data in per_attempt.items():
        nodes = data.get("by_node") or {}
        calls = sum(nodes.values())
        if not calls:
            continue
        share = nodes.get(PLANNER_NODE, 0) / calls
        (template_shares if key in template_keys else reasoned_shares).append(share)

    summary["planner_share_template"] = statistics.median(template_shares) if template_shares else None
    summary["planner_share_reasoned"] = statistics.median(reasoned_shares) if reasoned_shares else None

    return summary


def render(name: str, summary: dict) -> str:
    """The report analyze() computed, as the text report() prints."""
    rows = summary["rows"]
    by_node = summary["by_node"]
    by_phase_node = summary["by_phase_node"]

    lines = [f"\n=== {name} ==="]
    lines.append(f"  attempts                {summary['attempts']}")
    lines.append(f"  diagnosed correctly     {summary['correct']}  ({_pct(summary['diagnosis_rate'])})")
    lines.append(f"  reasoned but wrong      {summary['wrong']}")
    lines.append(f"  zero-evidence template  {summary['boilerplate']}")
    lines.append(f"  mitigated               {summary['mitigated']}")
    lines.append(
        f"  template AND mitigated  {summary['boilerplate_mitigated']}   (fixed the fault, then reported no evidence)"
    )
    lines.append(f"  diagnosis rate, reasoned attempts only  {_pct(summary['diagnosis_rate_reasoned'])}")

    if summary["boilerplate_rows"]:
        lines.append(
            f"  template attempts, diagnosis-phase tool calls: "
            f"{spread(summary['boilerplate_rows'], 'tool_calls_diagnosis')}"
        )
    lines.append(f"  all attempts, diagnosis-phase tool calls:       {spread(rows, 'tool_calls_diagnosis')}")

    # A mismatch means some attempt's tool-calls.json is missing or double
    # counted, which would silently skew every share above it.
    status = (
        "ok"
        if summary["csv_total"] == summary["tool_total"]
        else f"MISMATCH (json {summary['tool_total']} vs csv {summary['csv_total']})"
    )
    lines.append(f"  tool-call reconciliation: {status}")

    if by_node:
        lines.append("  tool calls by node:")
        total = sum(by_node.values())
        for node, count in by_node.most_common():
            flag = "  <- planner" if node == PLANNER_NODE else ""
            lines.append(f"    {node:24s} {count:6d}  {100 * count / total:5.1f}%{flag}")

    diagnosis = by_phase_node.get("diagnosis")
    if diagnosis:
        total = sum(diagnosis.values())
        share = 100 * diagnosis.get(PLANNER_NODE, 0) / total
        lines.append(f"  planner share, diagnosis phase only: {share:.1f}%")

    if summary["planner_share_template"] is not None and summary["planner_share_reasoned"] is not None:
        lines.append(
            f"  planner share per attempt: template {_pct(summary['planner_share_template'])}"
            f"  vs reasoned {_pct(summary['planner_share_reasoned'])}   (medians)"
        )

    return "\n".join(lines)


def report(name: str, run_dir: str) -> dict:
    summary = analyze(run_dir)

    if summary is None:
        print(f"{name}: no brain_go_ALL_results.csv under {run_dir}", file=sys.stderr)
        sys.exit(1)

    print(render(name, summary))

    return summary


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:.1f}%"


def _delta(before: float | None, after: float | None, unit: str = "pp") -> str:
    if before is None or after is None:
        return "n/a"

    return f"{100 * (after - before):+.1f}{unit}"


def compare(a_name: str, a: dict, b_name: str, b: dict) -> str:
    lines = [f"\n=== {a_name}  ->  {b_name} ==="]
    lines.append(f"  attempts                {a['attempts']:>6d}  {b['attempts']:>6d}")
    lines.append(
        f"  diagnosed correctly     {a['correct']:>6d}  {b['correct']:>6d}"
        f"   {_delta(a['diagnosis_rate'], b['diagnosis_rate'])}"
    )
    lines.append(
        f"  zero-evidence template  {a['boilerplate']:>6d}  {b['boilerplate']:>6d}"
        f"   {b['boilerplate'] - a['boilerplate']:+d}"
    )
    lines.append(
        f"  template AND mitigated  {a['boilerplate_mitigated']:>6d}  {b['boilerplate_mitigated']:>6d}"
        f"   {b['boilerplate_mitigated'] - a['boilerplate_mitigated']:+d}"
    )
    lines.append(
        f"  mitigated               {a['mitigated']:>6d}  {b['mitigated']:>6d}   {b['mitigated'] - a['mitigated']:+d}"
    )
    lines.append(
        f"  planner tool-call share {_pct(a['planner_share']):>6s}  {_pct(b['planner_share']):>6s}"
        f"   {_delta(a['planner_share'], b['planner_share'])}"
    )
    lines.append(
        "\n  The template count is the number to read first. It is a count, not an"
        "\n  estimate, so it carries meaning at 63 attempts where a score difference"
        "\n  of a few points does not."
    )

    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run_dir", help="a run directory, or its parent")
    parser.add_argument("baseline", nargs="?", help="an earlier run to compare against")
    args = parser.parse_args(argv[1:])

    current = report(os.path.basename(args.run_dir.rstrip("/")), args.run_dir)

    if args.baseline:
        base = report(os.path.basename(args.baseline.rstrip("/")), args.baseline)
        print(
            compare(
                os.path.basename(args.baseline.rstrip("/")),
                base,
                os.path.basename(args.run_dir.rstrip("/")),
                current,
            )
        )

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
