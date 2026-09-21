#!/usr/bin/env python3
"""The six-item RCI readout for one archived run.

    scripts/benchmark/readout_run.py <run-dir-or-run-id> [--json]

A scored run's readout wants six things beyond what score_run.py and
classify_run.py already print:

  1. Inconclusive count: attempts whose Diagnosis.submission contains
     "Investigation inconclusive: none of the evaluated hypotheses could be
     confirmed".
  2. Of those, how many carry an "## Observed signals" section (brain-go's
     block for an inconclusive report that banked an observation).
  3. Replan rate: attempts that ran a second hypothesis-generation round, and
     its token cost.
  4. rci_signal_agent_dispatched_total by signal, and
     rci_symptom_improved_downgrade_total.
  5. custom_mcp_breaker_trips_total and the port-forward restart count.
  6. Judge mean composite restricted to the inconclusive subset.

This extends score_run.py rather than restating it: item counting reuses
score_run.load() for the per-attempt rows and score_run._composite() for the
judge score, and item 5's file discovery reuses score_run._log_roots() and
score_run._token_files(), the same sregym-results/sregym-run-logs split
score_run.py already resolves.

## Why the breaker-trip aggregation is not score_run.tokens()'s aggregation

score_run.tokens() dedupes llm_tokens_total by its full label set and takes
the max per set, because thread_id makes each series unique to one attempt:
two scrapes of the same attempt are the same series, and max is exact.

custom_mcp_breaker_trips_total and the rci_ counters carry no per-attempt
label (rci_*_total's only label, when it has one, is "signal", which is
shared by the whole run, not by one attempt). The same literal series text
is exposed by every brain-go process that registers it, so deduping across
the whole run by label text alone would collapse two different shards' two
different processes into one and silently drop a shard's trips. What is
actually shared is the process, which run-20260917-ctl3's own layout answers
with: one shard is one box, and main.py's own comment on tokens_account_id
says a box's brain-go serves every slot on it from one process with
process-wide counters. So the aggregation here is max-within-shard, because
one shard's counter is one cumulative series across everything scraped from
that box, then sum-across-shards, because that is where one physical process
ends and the next begins. Checked against run-20260917-ctl3: summing the
four shards' own maxima (2, 0, 1, 3) gives 6, and every per-attempt
tokens-before/after.prom sampled from shard-0 tops out at the same 2 its own
shard-level sregym-tokens.prom carries, so folding those files into the same
shard group changes nothing -- they are earlier readings of the counter the
shard-level file already caught at its highest.

One case this undercounts and does not detect: a brain-go that restarts
mid-run resets its counters to zero, so a shard holds two segments rather
than one cumulative series, and max takes the larger segment instead of
their sum. Checked on run-20260917-ctl3, where it does not arise: each
shard's sregym-brain-go.log carries exactly one tworker start. A run whose
brain-go did restart would need the segments summed, which means reading
them in scrape order rather than taking a maximum.

## Item 3, replan rate

internal/chat/rci/metrics.go (brain-go) has no counter for a replan round.
internal/chat/rci/aggregator.go's applyReplan increments State.ReplanCount
in memory (internal/chat/state/rci.go), but nothing writes ReplanCount to a
Prometheus series, a log line, or the stored investigation record; grep for
it outside internal/chat/rci and internal/chat/state turns up nothing. So
this script does not compute item 3: there is no counter to read, and the
one candidate proxy -- the hypothesis-generation node's per-attempt call
count in token-usage-by-node.json -- cannot tell a replan round from that
node's own ordinary tool-calling loop (nodeGenAgent/nodeGenTools in
internal/chat/rci/agent.go), so a raised call count is not evidence of a
replan specifically. Rendered output says this plainly instead of guessing
a number.

## Item 4, the new RCI counters

rci_signal_agent_dispatched_total and rci_symptom_improved_downgrade_total
are read through the same shard-grouped aggregation as item 5's breaker
counter. Both are registered in internal/chat/rci/metrics.go under the rci_
prefix scrape_tokens.sh already keeps (KEEP='^(llm_|custom_mcp_|rci_)',
sregym#89), so no scrape change is needed for a run that includes them.
run-20260917-ctl3 predates both: its sregym-tokens.prom carries
rci_evidence_report_gated_total, rci_hypotheses_capped_total and
rci_hypotheses_generated_{bucket,count,sum} and nothing else under the rci_
prefix, so this script reports both series as absent from that archive
rather than printing a false zero.
"""

import argparse
import collections
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import score_run  # noqa: E402

# The prefix, not the sentence. brain-go changed the sentence on 2026-09-18
# ("no hypothesis was confirmed" replaced "none of the evaluated hypotheses
# could be confirmed") and a full-sentence needle read 0 of 63 on a run that
# had 21. The prefix is the stable part: it is the lead every inconclusive
# report opens with, on both the old and the new text.
NEEDLE_INCONCLUSIVE = "Investigation inconclusive"
# brain-go's baseline_noise lead (2026-09-18): the report says the alert
# fired on pre-existing traffic, not a regression. On the benchmark every
# problem has an injected fault, so this lead is always a wrong answer there;
# counting it says how often the new verdict ends a real-fault attempt.
NEEDLE_BASELINE_NOISE = "Alert threshold reached by pre-existing baseline traffic"
NEEDLE_OBSERVED_SIGNALS = "## Observed signals"

_SHARD_RE = re.compile(r"(shard-\d+)")


def _shard_key(path: Path) -> str:
    """Which brain-go process a scrape file came from.

    A shard is one box, and one box runs one brain-go deployment shared by
    every slot on it (score_run.py's _per_problem_tokens docstring records
    this for llm_ counters; nothing in the layout suggests it is different
    for custom_mcp_ or rci_). A file with no shard-N segment in its path
    (a run archived without sharding) is its own group rather than being
    silently dropped.
    """
    m = _SHARD_RE.search(str(path))
    return m.group(1) if m else "default"


def _parse_prom_lines(text: str, metric_name: str):
    """Yield (labels, value) for every sample of metric_name in text.

    labels is the raw text inside "{...}", or "" for an unlabelled series.
    Matches the metric name exactly: a line for a longer name sharing this
    one as a prefix (metric_name + "_created", for instance) is not
    mistaken for a sample, because the character right after the prefix must
    be "{" or a space, nothing else.
    """
    prefix = metric_name
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or not line.startswith(prefix):
            continue
        rest = line[len(prefix) :]
        if rest.startswith("{"):
            end = rest.find("}")
            if end == -1:
                continue
            labels, valpart = rest[1:end], rest[end + 1 :].strip()
        elif rest.startswith(" "):
            labels, valpart = "", rest.strip()
        else:
            continue
        try:
            value = float(valpart)
        except ValueError:
            continue
        yield labels, value


def _label_value(labels: str, key: str) -> str | None:
    """key's value out of a raw label string, or None if key is absent."""
    marker = f'{key}="'
    if marker not in labels:
        return None
    return labels.split(marker, 1)[1].split('"', 1)[0]


def grouped_totals(run_dir: Path, metric_name: str) -> dict[str, float]:
    """metric_name's totals, keyed by its raw label string.

    Per shard: the max value seen for a label set, since one shard's own
    counter is one cumulative series across everything scraped from that
    box (a shard-level sregym-tokens.prom and any per-attempt
    tokens-before/after.prom under it all read the same process).
    Across shards: summed, since that is where one physical brain-go
    process ends and the next begins.

    {} when metric_name appears in no file under run_dir at all, so a
    caller can tell "the counter did not exist in this run's build" from
    "the counter exists and is legitimately zero".
    """
    per_shard: dict[str, dict[str, float]] = {}
    for path in sorted(score_run._token_files(run_dir)):
        text = path.read_text(errors="replace")
        bucket = per_shard.setdefault(_shard_key(path), {})
        for labels, value in _parse_prom_lines(text, metric_name):
            bucket[labels] = max(bucket.get(labels, 0.0), value)

    totals: dict[str, float] = {}
    for labels_map in per_shard.values():
        for labels, value in labels_map.items():
            totals[labels] = totals.get(labels, 0.0) + value

    return totals


def restart_files(run_dir: Path) -> list[Path]:
    """Every mcp_pf_restarts.jsonl under run_dir's log tree.

    archive_run.sh's own sync filters (--include "*.csv" "*.json" "*.prom"
    for results/, "*logs/*" "*.log" for the log tree) do not currently pull
    this file down: it sits at shard-N/slots/M/mcp_pf_restarts.jsonl, with
    no logs/ segment and a .jsonl extension neither pattern matches. A run
    archived with today's archive_run.sh will not have it locally; fetch it
    with a separate `aws s3 cp --recursive --include "*mcp_pf_restarts.jsonl"`
    into the log tree first (score_run.py's _log_roots layout) if this
    returns nothing but the run is known to have port-forward restarts.
    """
    files: list[Path] = []
    for root in score_run._log_roots(run_dir):
        files.extend(root.rglob("mcp_pf_restarts.jsonl"))

    # Resolved before dedup: _log_roots() can return the same directory
    # twice under different unresolved paths (a symlinked temp root is the
    # common case, e.g. macOS's /tmp -> /private/tmp), and Path equality is
    # on the string form, not the inode, so an unresolved dedup misses that
    # and double-counts every restart in it.
    return sorted({f.resolve() for f in files})


def restarts(run_dir: Path) -> dict | None:
    """Port-forward restart events from every mcp_pf_restarts.jsonl found.

    None when no file was found at all, so "zero restarts" and "not
    archived" are never printed as the same thing. A line that is not a JSON
    object is skipped rather than raising: one malformed line should cost
    itself, not the whole count. Not an object, not merely not JSON, because
    json.loads("42") parses fine and hands back an int, which has no .get.
    """
    files = restart_files(run_dir)
    if not files:
        return None

    events: collections.Counter = collections.Counter()
    for path in files:
        for line in path.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue

            if not isinstance(parsed, dict):
                continue

            events[str(parsed.get("event", ""))] += 1

    return {"restart": events.get("restart", 0), "events": dict(events), "files": len(files)}


def contamination(run_dir: Path) -> dict:
    """Item 5: custom_mcp_breaker_trips_total and the port-forward restart
    count, side by side as the one contamination column a readout wants.
    """
    trips = grouped_totals(run_dir, "custom_mcp_breaker_trips_total")

    return {
        "breaker_trips": int(trips.get("", 0.0)) if trips else None,
        "restarts": restarts(run_dir),
    }


def inconclusive(rows: list[dict]) -> dict:
    """Items 1, 2 and 6: the inconclusive subset, how many of those carry an
    Observed signals section, and the judge's mean composite over the
    subset's scored attempts.
    """
    subset = [r for r in rows if NEEDLE_INCONCLUSIVE in score_run._val(r, "Diagnosis.submission")]
    observed = [r for r in subset if NEEDLE_OBSERVED_SIGNALS in score_run._val(r, "Diagnosis.submission")]
    baseline_noise = [r for r in rows if NEEDLE_BASELINE_NOISE in score_run._val(r, "Diagnosis.submission")]

    scores = []
    for row in subset:
        composite = score_run._composite(row)
        if composite is not None:
            scores.append(composite)

    return {
        "attempts": len(rows),
        "inconclusive": len(subset),
        "observed_signals": len(observed),
        "baseline_noise": len(baseline_noise),
        "judge_scored": len(scores),
        "composite_mean": round(sum(scores) / len(scores), 4) if scores else None,
    }


def _bare(totals: dict[str, float]) -> int | None:
    """The unlabelled series of a counter, or None when the counter is absent
    from every file. .get rather than indexing, so a counter that gains a
    label reads as a missing bare series instead of a KeyError."""
    return int(totals.get("", 0.0)) if totals else None


def _by_label(totals: dict[str, float], key: str) -> dict[str, int] | None:
    if not totals:
        return None

    return {
        (_label_value(labels, key) or labels or f"(no {key} label)"): int(value)
        for labels, value in sorted(totals.items())
    }


def new_rci_counters(run_dir: Path) -> dict:
    """Items 3 and 4: the replan counters, the exclusion counter, the
    exhausted-turn counter, dispatch by signal, and the symptom-improved
    downgrade.

    A metric absent from every file in run_dir returns None for that entry,
    not 0: this run's brain-go build may predate the counter, which is a
    different fact from the counter firing zero times.

    All of these are process-wide counters on one box's brain-go, so they
    span the diagnosis and mitigation stages of every attempt on that box.
    Nothing here splits them per stage or per attempt; a rate over attempts
    is not derivable from them.
    """
    dispatched = grouped_totals(run_dir, "rci_signal_agent_dispatched_total")
    downgrade = grouped_totals(run_dir, "rci_symptom_improved_downgrade_total")
    replan_round = grouped_totals(run_dir, "rci_replan_round_total")
    replan_declined = grouped_totals(run_dir, "rci_replan_declined_total")
    unexplaining = grouped_totals(run_dir, "rci_hypotheses_unexplaining_total")
    exhausted = grouped_totals(run_dir, "rci_signal_turn_budget_exhausted_total")

    by_signal = None
    if dispatched:
        by_signal = {
            (_label_value(labels, "signal") or labels or "(no signal label)"): int(value)
            for labels, value in sorted(dispatched.items())
        }

    return {
        "signal_agent_dispatched": by_signal,
        "symptom_improved_downgrade": _bare(downgrade),
        "replan_round": _bare(replan_round),
        "replan_declined": _by_label(replan_declined, "reason"),
        "hypotheses_unexplaining": _bare(unexplaining),
        "turn_budget_exhausted": _by_label(exhausted, "signal"),
    }


def replan_note() -> str:
    """Item 3's answer for an archive whose brain-go predates the replan
    counters (added 2026-09-18): there is nothing to read, so this prints
    why rather than a derived number. See this file's own docstring.
    """
    return (
        "not computed: internal/chat/rci/metrics.go has no replan counter, and "
        "aggregator.go's State.ReplanCount is in-memory only, never written to a "
        "metric, a log line, or the stored investigation record. The one candidate "
        "proxy, the hypothesis-generation node's per-attempt call count in "
        "token-usage-by-node.json, cannot separate a replan round from that node's "
        "own tool-calling loop, so it is not printed as a number here."
    )


def render(name: str, readout: dict) -> str:
    inc, cont, rci = readout["inconclusive"], readout["contamination"], readout["rci_counters"]
    lines = [f"{name}: RCI readout"]

    lines.append("")
    lines.append("1-2. Inconclusive path and Observed signals")
    lines.append(f"  inconclusive:      {inc['inconclusive']} of {inc['attempts']}")
    lines.append(f"  Observed signals:  {inc['observed_signals']} of {inc['inconclusive']} inconclusive")

    lines.append("")
    lines.append("3. Replan round (process-wide counts, both stages, not a per-attempt rate)")
    if rci["replan_round"] is None:
        lines.append(f"  {replan_note()}")
    else:
        lines.append(f"  rci_replan_round_total:              {rci['replan_round']}")
        if rci["replan_declined"] is None:
            lines.append("  rci_replan_declined_total:           not present in this archive")
        else:
            lines.append("  rci_replan_declined_total, by reason:")
            for reason, count in rci["replan_declined"].items():
                lines.append(f"    {reason:<16} {count}")
        if rci["hypotheses_unexplaining"] is None:
            lines.append("  rci_hypotheses_unexplaining_total:   not present in this archive")
        else:
            lines.append(f"  rci_hypotheses_unexplaining_total:   {rci['hypotheses_unexplaining']}")
    lines.append(
        f"  baseline_noise lead in submissions:  {inc['baseline_noise']} of {inc['attempts']} (always wrong on an injected fault)"
    )

    lines.append("")
    lines.append("4. New RCI counters")
    if rci["signal_agent_dispatched"] is None:
        lines.append("  rci_signal_agent_dispatched_total:    not present in this archive")
    else:
        lines.append("  rci_signal_agent_dispatched_total, by signal:")
        for signal, count in rci["signal_agent_dispatched"].items():
            lines.append(f"    {signal:<10} {count}")
    if rci["symptom_improved_downgrade"] is None:
        lines.append("  rci_symptom_improved_downgrade_total: not present in this archive")
    else:
        lines.append(f"  rci_symptom_improved_downgrade_total: {rci['symptom_improved_downgrade']}")
    if rci["turn_budget_exhausted"] is None:
        lines.append("  rci_signal_turn_budget_exhausted_total: not present in this archive")
    else:
        lines.append("  rci_signal_turn_budget_exhausted_total, by signal:")
        for signal, count in rci["turn_budget_exhausted"].items():
            lines.append(f"    {signal:<10} {count}")

    lines.append("")
    lines.append("5. Contamination")
    if cont["breaker_trips"] is None:
        lines.append("  custom_mcp_breaker_trips_total: not archived (no *tokens*.prom found)")
    else:
        lines.append(f"  custom_mcp_breaker_trips_total: {cont['breaker_trips']}")
    if cont["restarts"] is None:
        lines.append("  port-forward restarts:          not archived (no mcp_pf_restarts.jsonl found)")
    else:
        lines.append(f"  port-forward restarts:          {cont['restarts']['restart']}")

    lines.append("")
    lines.append("6. Judge mean composite, inconclusive subset")
    if inc["composite_mean"] is None:
        lines.append("  no scored attempt in the inconclusive subset")
    else:
        lines.append(f"  mean composite: {inc['composite_mean']}  (n={inc['judge_scored']} of {inc['inconclusive']})")

    return "\n".join(lines)


def resolve_run_dir(arg: str) -> Path:
    """arg as a directory if it is one; otherwise as a run id, resolved the
    way archive_run.sh lays runs out: <sibling-brain-go>/sregym-results/<id>,
    falling back to this repo's own sregym-results/<id>. Purely local path
    derivation -- this never touches S3.
    """
    direct = Path(arg)
    if direct.is_dir():
        return direct

    here = Path(__file__).resolve()
    primary = here.parents[2]
    for root in (primary.parent / "brain-go", primary):
        candidate = root / "sregym-results" / arg
        if candidate.is_dir():
            return candidate

    return direct


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run", help="a run id, or an archived run directory (sregym-results/<run-id>)")
    ap.add_argument("--json", action="store_true", help="emit the numbers as JSON")
    args = ap.parse_args(argv[1:])

    run_dir = resolve_run_dir(args.run)
    if not run_dir.is_dir():
        print(f"readout_run: not a directory: {run_dir}", file=sys.stderr)
        return 2

    rows = score_run.load(run_dir)
    if not rows:
        print(f"readout_run: no per-attempt result rows under {run_dir}", file=sys.stderr)
        return 1

    readout = {
        "inconclusive": inconclusive(rows),
        "contamination": contamination(run_dir),
        "rci_counters": new_rci_counters(run_dir),
    }

    if args.json:
        print(json.dumps(readout, indent=2))
    else:
        print(render(run_dir.name, readout))

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
