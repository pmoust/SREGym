#!/usr/bin/env python3
"""Failure classes and tool-call attribution for one benchmark run, or two compared.

score_run.py answers how many attempts were graded correct. This answers a
different question: of the ones that were not, how many never produced a
reasoned answer at all, and where did the tool calls go.

The distinction matters because the two failure modes call for different work. An
attempt that gathered evidence and reached the wrong conclusion is a reasoning
problem. An attempt that gathered nothing and then submitted a report saying so
is a plumbing problem, and it scores identically to the first while telling you
nothing about the model.

Reads two artifacts per run, both written by the harness:

  brain_go_ALL_results.csv   one row per attempt, with the graded verdicts, the
                             submitted diagnosis text, and the tool and token
                             counts.
  tool-calls.json            one file per attempt, counted by the driver from
                             the agent's own stream rather than from Prometheus.

Zero-evidence attempts are identified structurally, not by the wording of the
report. The first version of this script matched a clause of brain-go's
zero-evidence text. brain-go reworded that text in c17cfe9db and the marker
matched nothing for the next seven days, while the script went on printing
"zero-evidence template 0" and folding any such attempt into "reasoned but
wrong" - a plumbing failure reported as a reasoning failure, in the direction
that reads as good news. Two signals replace it, neither of which depends on
what the report says:

  no evidence in the report   brain-go renders one bullet per evidence item
                              under an "## Evidence" heading and leaves the
                              heading out when there are none.
  no signal-worker tool call  in the diagnosis phase every call was issued by
                              the hypothesis-generation planner and none by a
                              signal worker, so no worker result could have
                              reached the report.

Both must hold, because either alone misclassifies attempts that are on file.
The report signal alone counts a correct summary-only report:
run-20260903-1259 has two, both graded correct. The tool signal alone counts an
attempt whose planner did the reading itself: the 2026-09-15 control and
treatment runs have one each, both carrying full evidence. On
run-20260908-0739, the run the class was derived from, the pair selects exactly
the 18 attempts the old marker selected.

What can still go stale is the "## Evidence" heading, which is brain-go's own
rendering. Renaming it makes every attempt read as zero-evidence, which fails
loudly rather than quietly: the count jumps to the whole run, and report() says
so when the class covers every attempt. An attempt with no tool-calls.json is
judged on its report alone, which over-counts rather than under-counts.

tool-calls.json is matched on its results path, not on its file name. The
harness writes it once, into a staging directory .runtime/<agent>/<opaque id>/,
and renames that whole directory onto results/.../<problem>/run_<n>/ when the
attempt finalizes (sregym/run_artifacts.py). An attempt that never finalizes
leaves its staging copy behind: the 2026-09-15 control archive holds 63
finalized attempts and 52 such leftovers. Requiring the results/.../run_N/
shape drops them on path shape alone. A pattern matching the file name anywhere
returns the right 63 today only because glob skips dot-directories and .runtime
is hidden - find(1) lists 115 files on the same tree - so it is right by
accident, and would inflate every share below the moment include_hidden is
passed or the staging root loses its dot.

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

# brain-go's node name for the hypothesis-generation planner's own tool calls.
PLANNER_NODE = "hypothesis_gen_tools"

# The submitted report's evidence section and the bullets in it. The lookahead
# stops at the next heading of any level, so a nested subsection stays inside
# the section it belongs to.
EVIDENCE_SECTION = re.compile(r"^#+ +Evidence[^\n]*$(.*?)(?=^#+ |\Z)", re.M | re.S)
BULLET = re.compile(r"^\s*[-*] ", re.M)


def _int(row: dict, key: str) -> int | None:
    raw = (row.get(key) or "").strip()

    return int(raw) if raw.lstrip("-").isdigit() else None


def _truthy(value: object) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes")


def row_key(row: dict) -> tuple[str, str]:
    """The (problem, attempt) identity a CSV row and a tool-calls path share."""
    return (row.get("problem_id") or "", str(row.get("attempt") or ""))


def load_attempts(run_dir: str) -> list[dict]:
    """Every attempt row in the run, at most one per (problem, attempt).

    The per-problem *_results.csv files carry the same verdicts but not the tool
    or token counts, so the aggregate is the one worth reading.

    A resumed run writes its finished attempts twice: main.py seeds the new run
    with the rows it read out of the previous CSV, then writes a fresh aggregate
    into a new timestamped directory while the old aggregate stays on disk, so
    the recursive glob reads those attempts from both. Later paths sort after
    earlier ones because the directory name is a timestamp, so keeping the last
    row for a key keeps the fresher copy. A row carrying no problem id or no
    attempt number has no identity to deduplicate on and is kept as it is.
    """
    rows: list[dict] = []
    seen: dict[tuple[str, str], int] = {}

    for path in sorted(glob.glob(f"{run_dir}/**/brain_go_ALL_results.csv", recursive=True)):
        with open(path, newline="") as handle:
            for row in csv.DictReader(handle):
                key = row_key(row)
                if not all(key):
                    rows.append(row)
                elif key in seen:
                    rows[seen[key]] = row
                else:
                    seen[key] = len(rows)
                    rows.append(row)

    return rows


def attempt_key(path: str) -> tuple[str, str] | None:
    """The (problem, attempt) a canonical tool-calls.json belongs to.

    The path ends .../brain_go/<problem>/run_<n>/tool-calls.json, which is the
    only place the per-attempt identity is recorded: the file itself holds
    counts and no identifier.
    """
    match = re.search(r"/([^/]+)/run_(\d+)/tool-calls\.json$", path)

    return (match.group(1), match.group(2)) if match else None


def load_tool_calls(run_dir: str) -> dict:
    """Tool calls by node, by phase, the run totals, and the raw per-attempt files.

    Matched on the results path rather than on the file name, for the reason in
    the module docstring: an unfinalized attempt leaves a second copy of the
    same file under a hidden staging directory.

    total and node_total count the same calls from the driver's two separate
    accumulators, per_phase and per_phase_by_node. A call that arrives with no
    node attached lands in the first and not the second, which is the one way
    the by-node shares can be wrong while the total is right, so both are
    returned and report() compares them.
    """
    by_node: collections.Counter = collections.Counter()
    by_phase_node: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    by_phase_tool: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    per_attempt: dict[tuple[str, str], dict] = {}
    files = 0
    total = 0
    node_total = 0

    for path in sorted(glob.glob(f"{run_dir}/**/results/**/run_*/tool-calls.json", recursive=True)):
        with open(path) as handle:
            data = json.load(handle)

        files += 1
        count = data.get("total") or 0
        count = sum(count.values()) if isinstance(count, dict) else count
        total += count

        nodes = data.get("by_node") or {}
        by_node.update(nodes)
        node_total += sum(nodes.values())

        for phase, counter in (data.get("by_phase_and_node") or {}).items():
            by_phase_node[phase].update(counter)

        for phase, counter in (data.get("by_phase_and_tool") or {}).items():
            by_phase_tool[phase].update(counter)

        key = attempt_key(path)
        if key:
            per_attempt[key] = data

    return {
        "by_node": by_node,
        "by_phase_node": dict(by_phase_node),
        "by_phase_tool": dict(by_phase_tool),
        "per_attempt": per_attempt,
        "files": files,
        "total": total,
        "node_total": node_total,
    }


def evidence_items(submission: str) -> int:
    """How many evidence items the submitted report lists.

    brain-go renders one bullet per item under an "## Evidence" heading and
    omits the heading when the report has none, so an absent section and an
    empty one both count zero.
    """
    section = EVIDENCE_SECTION.search(submission)

    return len(BULLET.findall(section.group(1))) if section else 0


def signal_worker_calls(tool_calls: dict) -> int:
    """Diagnosis-phase tool calls issued by a node other than the planner."""
    by_node = (tool_calls.get("by_phase_and_node") or {}).get("diagnosis") or {}

    return sum(count for node, count in by_node.items() if node != PLANNER_NODE)


def is_zero_evidence(row: dict, tool_calls: dict | None) -> bool:
    """Whether this attempt reported without evidence, on both available signals.

    See the module docstring for why it takes both and for what each one alone
    gets wrong. tool_calls is None when the attempt's tool-calls.json was not
    archived, and then the report is the only signal there is.
    """
    if evidence_items(row.get("Diagnosis.submission") or "") > 0:
        return False

    return tool_calls is None or signal_worker_calls(tool_calls) == 0


def classify(rows: list[dict], per_attempt: dict | None = None) -> dict:
    """Split the attempts into the three outcomes that need different work."""
    per_attempt = per_attempt or {}
    correct, boilerplate, wrong = [], [], []

    for row in rows:
        if _truthy(row.get("Diagnosis.success")):
            correct.append(row)
        elif is_zero_evidence(row, per_attempt.get(row_key(row))):
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

    calls = load_tool_calls(run_dir)
    summary = classify(rows, calls["per_attempt"])
    by_node = calls["by_node"]
    csv_total = sum(v for v in (_int(r, "tool_calls") for r in rows) if v is not None)

    summary["rows"] = rows
    summary["by_node"] = by_node
    summary["by_phase_node"] = calls["by_phase_node"]
    summary["by_phase_tool"] = calls["by_phase_tool"]
    summary["tool_files"] = calls["files"]
    summary["tool_total"] = calls["total"]
    summary["node_total"] = calls["node_total"]
    summary["csv_total"] = csv_total
    summary["planner_share"] = by_node.get(PLANNER_NODE, 0) / sum(by_node.values()) if by_node else None

    # Per attempt, not run-wide: whether the attempts that gave up on reporting
    # were also the ones that spent their calls in the planner is the question
    # the run-wide share cannot answer.
    template_keys = {row_key(r) for r in summary["boilerplate_rows"]}
    template_shares, reasoned_shares = [], []

    for key, data in calls["per_attempt"].items():
        nodes = data.get("by_node") or {}
        count = sum(nodes.values())
        if not count:
            continue
        share = nodes.get(PLANNER_NODE, 0) / count
        (template_shares if key in template_keys else reasoned_shares).append(share)

    summary["planner_share_template"] = statistics.median(template_shares) if template_shares else None
    summary["planner_share_reasoned"] = statistics.median(reasoned_shares) if reasoned_shares else None

    return summary


def reconciliation(summary: dict) -> tuple[list[str], bool]:
    """The tool-call checks, as the lines render() prints and a verdict.

    The two checks are not equally strong. main.py's _tool_calls copies the
    json file's own "total" into the csv's tool_calls column, so csv against
    json compares one number with itself: it catches a tool-calls.json this
    script never read or read twice, not a count that was wrong when written.

    by_node against total is a second count. The driver fills per_phase and
    per_phase_by_node from separate accumulators, so a call recorded without a
    node reaches the total and not the node table, and then every share below
    is computed over a short denominator while the total still looks right.

    A run with no tool-calls.json at all is unmeasured rather than broken,
    which is how main.py reads an absent file too: every agent but brain_go,
    and every archive older than the driver's counting, has none.
    """
    if not summary["tool_files"]:
        return ["  tool calls: not recorded (no tool-calls.json under this run)"], True

    files = (
        "ok"
        if summary["csv_total"] == summary["tool_total"]
        else f"MISMATCH (json {summary['tool_total']} vs csv {summary['csv_total']})"
    )
    nodes = (
        "ok"
        if summary["node_total"] == summary["tool_total"]
        else f"SHORT (by_node {summary['node_total']} of {summary['tool_total']}; shares below understate)"
    )

    return (
        [
            f"  tool-call files, csv vs json:       {files}",
            f"  node attribution, by_node vs total: {nodes}",
        ],
        files == "ok" and nodes == "ok",
    )


def render(name: str, summary: dict) -> str:
    """The report analyze() computed, as the text report() prints."""
    rows = summary["rows"]
    by_node = summary["by_node"]
    by_phase_node = summary["by_phase_node"]
    by_phase_tool = summary["by_phase_tool"]

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

    if summary["boilerplate"] and summary["boilerplate"] == summary["attempts"]:
        lines.append(
            "  WARNING: every attempt classified zero-evidence. That is what a renamed"
            "\n  '## Evidence' heading in brain-go's report looks like here, and it is more"
            "\n  likely than a run in which nothing was collected at all. Check the heading"
            "\n  against a submission before reading the split."
        )

    if summary["boilerplate_rows"]:
        lines.append(
            f"  template attempts, diagnosis-phase tool calls: "
            f"{spread(summary['boilerplate_rows'], 'tool_calls_diagnosis')}"
        )
    lines.append(f"  all attempts, diagnosis-phase tool calls:       {spread(rows, 'tool_calls_diagnosis')}")

    lines.extend(reconciliation(summary)[0])

    if by_node:
        lines.append("  tool calls by issuing node, all phases:")
        total = sum(by_node.values())
        for node, count in by_node.most_common():
            flag = "  <- planner" if node == PLANNER_NODE else ""
            lines.append(f"    {node:24s} {count:6d}  {100 * count / total:5.1f}%{flag}")
        lines.append(
            "    (which node issued the call, not what the call read: a planner-issued"
            "\n     get_logs counts under the planner. The tool table says what was read.)"
        )

    diagnosis = by_phase_node.get("diagnosis")
    if diagnosis:
        total = sum(diagnosis.values())
        share = 100 * diagnosis.get(PLANNER_NODE, 0) / total
        lines.append(f"  planner share of calls issued, diagnosis phase only: {share:.1f}%")

    diagnosis_tools = by_phase_tool.get("diagnosis")
    if diagnosis_tools:
        lines.append("  diagnosis-phase tool calls by tool:")
        total = sum(diagnosis_tools.values())
        for tool, count in diagnosis_tools.most_common():
            lines.append(f"    {tool:24s} {count:6d}  {100 * count / total:5.1f}%")

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
    summaries = [current]

    if args.baseline:
        base = report(os.path.basename(args.baseline.rstrip("/")), args.baseline)
        summaries.append(base)
        print(
            compare(
                os.path.basename(args.baseline.rstrip("/")),
                base,
                os.path.basename(args.run_dir.rstrip("/")),
                current,
            )
        )

    # Nonzero on a failed reconciliation. The report still prints every number,
    # because the failure says the shares are untrustworthy rather than absent,
    # but a caller scripting this must not read an unreconciled run as clean.
    return 0 if all(reconciliation(s)[1] for s in summaries) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
