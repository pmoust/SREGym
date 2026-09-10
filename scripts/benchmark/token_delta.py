#!/usr/bin/env python3
"""Token counters for one attempt, as a delta between two scrapes.

brain-go reports LLM usage as cumulative Prometheus counters labelled by
thread_id. The intended way to attribute them per problem was the thread name:
scrape_tokens.sh says the driver sets it to cli:sregym-<problem_id>-<hex8>, so
"one scrape attributes tokens to problems without any per-slot bookkeeping".

That stopped holding. Since #28 the driver opens an investigation and uses the
thread brain-go returns, which is cli:inv-<uuid> and carries no problem id.
run-20260903-0558's counters are labelled cli:inv-0248e16e-... and
cli:inv-fb383882-..., so a scrape alone cannot say which problem spent what.

Bracketing an attempt is a better answer than repairing the name: it does not
depend on a format anyone can change, it survives an attempt whose thread is
reused, and it needs no join. Counters are monotonic per process, so the
difference between two scrapes is exactly what happened in between.

Four token types, all input-side except output. Both providers report cache
reads and writes OUTSIDE the input count (Anthropic's input_tokens excludes
cache_read_input_tokens and cache_creation_input_tokens; Bedrock's inputTokens
excludes cacheReadInputTokens and cacheWriteInputTokens), so the four sum to
real consumption without double counting. There is no cached output: output
tokens are generated every time.

## One slot's spend, not the whole box's

brain-go runs as one shared stack serving every `runs_per_instance` slot on a
box, and its Prometheus counters (what scrape_tokens.sh reads and this module
diffs) are process-wide, not per-slot. A bracketed window therefore holds what
the other slots spent inside it too. On run-20260908-0739-4fc0249e, with three
slots per box, the delta-based total came to 12,764 LLM calls against the 5,303
score_run.py's tokens() counts from the same run's whole-run scrape (max within
each series, summed across series, so it never brackets by time), a factor of
2.41.

Every slot seeds its own brain-go tenant (scripts/benchmark/seed_slot.sh) and
every sample carries that tenant as account_id, so an account id is what keeps
one slot's window clear of the others: shard-3's scrape from that run carries
exactly three account_id values, 2, 3 and 4, one per slot. Pass the attempt's
own account id to delta() or delta_by_node(). Passing none sums every account,
which is the right answer only for a box running one slot.

thread_id is present on the same samples but is not the key to filter on: it
varies inside one attempt (15 distinct values in one shard's scrape), and
keying on a thread name is what broke attribution in the first place.

Usage:
  token_delta.py snapshot <out.prom>          # capture, via scrape_tokens.sh
  token_delta.py delta <before.prom> <after.prom> [--account <id>]
  token_delta.py delta-by-node <before.prom> <after.prom> [--account <id>]

delta and delta-by-node emit JSON on stdout, the second broken out per node.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRAPER = HERE / "scrape_tokens.sh"

# llm_tokens_total{...,token_type="cache_read",...} 2.05674e+06
_SAMPLE = re.compile(r"^(llm_tokens_total|llm_calls_total)\{([^}]*)\}\s+(\S+)")
_LABEL = re.compile(r'(\w+)="([^"]*)"')

TOKEN_TYPES = ("input", "output", "cache_read", "cache_write")


def snapshot(out: Path) -> int:
    """Capture the counters, reusing the scraper that knows the endpoints.

    Not reimplemented here: the worker fleet's metrics port is unpublished and
    scrape_tokens.sh resolves the container's bridge address to reach it. Most
    of a run's tokens are on that endpoint, because the RCI investigation runs
    as a Temporal workflow whose llm activities execute on tworker. A Python
    reimplementation would have to duplicate that discovery and would drift.
    """
    if not SCRAPER.exists():
        return 1

    return subprocess.run(["bash", str(SCRAPER), str(out)], check=False).returncode


def _totals_by_node(path: Path, account_id: str | None = None) -> dict[str, dict[str, float]]:
    """Sum each counter across every label set in one scrape, broken out by
    node, for one account or for all of them.

    Summed across every label except node -- in particular thread, on
    purpose. The delta is taken around a single attempt, so every increment
    inside it belongs to that attempt whatever thread carried it, and summing
    means a mid-attempt thread change cannot lose counts. node is kept apart
    rather than folded into that same sum: a single attempt's hypothesis
    planner or worker can touch several nodes in the course of one turn, and
    collapsing them away would lose exactly the per-node question
    brain-go#2687 added the label to answer. Every real sample carries a node
    value (brain-go defaults an unattributed call to "unknown" rather than
    omitting the label), so grouping by it is safe even against a scrape
    taken before that label existed.

    account_id is the one label that is not summed over, because the slots
    sharing a brain-go process are exactly one account each and their spend is
    not this attempt's. A given account_id keeps only samples carrying it; a
    sample with no account_id label at all is dropped, since nothing says it
    belongs to this account. None or "" keeps every sample.
    """
    out: dict[str, dict[str, float]] = {}
    if not path.exists():
        return out

    for line in path.read_text(errors="replace").splitlines():
        found = _SAMPLE.match(line)
        if not found:
            continue

        metric, labels, value = found.groups()
        try:
            amount = float(value)
        except ValueError:
            continue

        # float() accepts nan and inf, and int(nan) then raises, which would
        # fail the pricing of a run over one malformed sample. A counter is
        # finite by definition, so a non-finite value is a broken scrape.
        if not math.isfinite(amount):
            continue

        label_map = dict(_LABEL.findall(labels))

        if account_id and label_map.get("account_id") != account_id:
            continue

        node = label_map.get("node") or "unknown"
        bucket = out.setdefault(node, {})

        if metric == "llm_calls_total":
            bucket["calls"] = bucket.get("calls", 0.0) + amount
            continue

        token_type = label_map.get("token_type")
        if token_type in TOKEN_TYPES:
            bucket[token_type] = bucket.get(token_type, 0.0) + amount

    return out


def _totals(path: Path, account_id: str | None = None) -> dict[str, float]:
    """Sum each counter across every label set in one scrape, node included.

    Summed rather than kept per thread on purpose. The delta is taken around a
    single attempt, so every increment inside it belongs to that attempt
    whatever thread carried it, and summing means a mid-attempt thread change
    cannot lose counts.

    account_id is passed straight through to _totals_by_node and is the one
    label a sum here can still exclude on; "belongs to that attempt" holds
    only once it does, because the counters are process-wide and every other
    slot on the box increments them in the same window.
    """
    out: dict[str, float] = {}
    for node_totals in _totals_by_node(path, account_id).values():
        for key, amount in node_totals.items():
            out[key] = out.get(key, 0.0) + amount

    return out


def delta(before: Path, after: Path, account_id: str | None = None) -> dict[str, int]:
    """What one attempt spent, in total. Never negative.

    A counter that goes backwards means the process restarted mid-attempt, and
    its pre-restart total is unrecoverable. Reporting a negative would corrupt
    any sum built from these rows, so the floor is zero and the loss is
    silent-but-bounded rather than poisonous.

    account_id is the attempt's own brain-go tenant, and without it the figure
    also carries what the other slots on the box spent in the same window:
    2.41x on run-20260908-0739-4fc0249e, three slots per box. main.py resolves
    it from the slot's session and passes it here, which is what makes the
    result CSVs' per-attempt tokens_*/llm_calls columns this attempt's own.
    """
    start, end = _totals(before, account_id), _totals(after, account_id)
    keys = ("calls", *TOKEN_TYPES)

    return {k: max(0, int(end.get(k, 0.0) - start.get(k, 0.0))) for k in keys}


def delta_by_node(before: Path, after: Path, account_id: str | None = None) -> dict[str, dict[str, int]]:
    """What one attempt spent, broken out by node. Never negative per node.

    Not summed across node the way delta()'s total is summed across thread:
    see _totals_by_node for why. Same never-negative floor as delta(), applied
    per node independently, so one node's counter resetting cannot corrupt
    another node's figure.

    account_id matters here for the same reason it does in delta(), and node
    is no substitute for it: another slot's calls land on the same node labels
    (hypothesis_gen_agent, diagnosis, and so on) as this attempt's own, so
    splitting by node separates the phases of the spend, not the slots. This
    is what main.py archives as each attempt's token-usage-by-node.json.
    """
    start, end = _totals_by_node(before, account_id), _totals_by_node(after, account_id)
    keys = ("calls", *TOKEN_TYPES)
    nodes = sorted(set(start) | set(end))

    return {
        node: {k: max(0, int(end.get(node, {}).get(k, 0.0) - start.get(node, {}).get(k, 0.0))) for k in keys}
        for node in nodes
    }


def _take_account(args: list[str]) -> tuple[list[str], str | None]:
    """Split a trailing `--account <id>` off the argument list.

    Hand-parsed rather than handed to argparse: this module has three
    subcommands and one option between them, and argparse would cost an import
    and a parser build on a path main.py runs twice per attempt.
    """
    if len(args) >= 2 and args[-2] == "--account":
        return args[:-2], args[-1]

    return args, None


def main(argv: list[str]) -> int:
    args, account_id = _take_account(argv[1:])

    # snapshot captures every account the process serves, since it is one
    # scrape of one endpoint, so an --account here would be silently ignored
    # rather than honoured. It falls through to the usage message.
    if len(args) == 2 and args[0] == "snapshot" and account_id is None:
        return snapshot(Path(args[1]))

    if len(args) == 3 and args[0] == "delta":
        print(json.dumps(delta(Path(args[1]), Path(args[2]), account_id)))

        return 0

    if len(args) == 3 and args[0] == "delta-by-node":
        print(json.dumps(delta_by_node(Path(args[1]), Path(args[2]), account_id)))

        return 0

    print(__doc__, file=sys.stderr)

    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
