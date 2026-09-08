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

## WARNING: every number this module returns is inflated, by about 2.41x

brain-go runs as ONE shared stack serving all `runs_per_instance` slots on a
box, and its Prometheus counters (what scrape_tokens.sh reads and this module
diffs) are process-wide, not per-slot. Bracketing one attempt between two
scrapes therefore captures whatever every OTHER concurrent slot's attempt
also spent in that same window, not only the bracketed attempt's own spend.

Verified directly on run-20260908-0739-4fc0249e: one attempt's own
tokens-after.prom carries three distinct thread_id values, one per slot on
that box, not one. Measured inflation: this module's delta-based total comes
to 12,764 LLM calls; score_run.py's tokens() on the same run's whole-run
scrape (max within each series, summed across series -- immune to this,
because it does not bracket by time at all) counts 5,303. 12,764 / 5,303 =
2.41x.

Consequence: every per-attempt `tokens_*` and `llm_calls` figure this module
ever produced -- the result CSVs' columns and the archived
token-usage-by-node.json files alike -- is inflated by roughly this factor.
The whole-run total from score_run.py's tokens() is not affected and is the
trustworthy figure. Per-attempt tool-call counts are also unaffected: the
driver counts those off its own SSE stream, not a Prometheus counter, so
concurrent slots cannot leak into them.

Not fixed here. A real fix needs a design decision: the driver already knows
its own thread_id and already writes tool-calls.json, so it could write that
id somewhere token_delta.py can filter a scrape by it before diffing. That is
a separate change, not a documentation one.

Usage:
  token_delta.py snapshot <out.prom>          # capture, via scrape_tokens.sh
  token_delta.py delta <before.prom> <after.prom>   # emit JSON on stdout
  token_delta.py delta-by-node <before.prom> <after.prom>  # same, per node
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


def _totals_by_node(path: Path) -> dict[str, dict[str, float]]:
    """Sum each counter across every label set in one scrape, broken out by
    node.

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
        node = label_map.get("node") or "unknown"
        bucket = out.setdefault(node, {})

        if metric == "llm_calls_total":
            bucket["calls"] = bucket.get("calls", 0.0) + amount
            continue

        token_type = label_map.get("token_type")
        if token_type in TOKEN_TYPES:
            bucket[token_type] = bucket.get(token_type, 0.0) + amount

    return out


def _totals(path: Path) -> dict[str, float]:
    """Sum each counter across every label set in one scrape, node included.

    Summed rather than kept per thread on purpose. The delta is taken around a
    single attempt, so every increment inside it belongs to that attempt
    whatever thread carried it, and summing means a mid-attempt thread change
    cannot lose counts.

    WARNING: "belongs to that attempt" is the assumption this module makes,
    and it is false whenever another slot on the same box is running
    concurrently, which is the normal case. These are process-wide counters,
    not per-slot ones, so a sum here also includes every OTHER concurrent
    slot's spend during the same window. See the module docstring's
    "every number this module returns is inflated" section: measured 2.41x
    on run-20260908-0739-4fc0249e. Use score_run.py's tokens() (the whole-run
    scrape, immune to this) for a trustworthy total.
    """
    out: dict[str, float] = {}
    for node_totals in _totals_by_node(path).values():
        for key, amount in node_totals.items():
            out[key] = out.get(key, 0.0) + amount

    return out


def delta(before: Path, after: Path) -> dict[str, int]:
    """What one attempt spent, in total -- or so its name claims. Never
    negative.

    A counter that goes backwards means the process restarted mid-attempt, and
    its pre-restart total is unrecoverable. Reporting a negative would corrupt
    any sum built from these rows, so the floor is zero and the loss is
    silent-but-bounded rather than poisonous.

    WARNING: this is inflated, by about 2.41x measured on
    run-20260908-0739-4fc0249e. brain-go's counters are process-wide, shared
    by every slot on the box, so bracketing one attempt's wall-clock window
    also captures whatever every other concurrently-running slot spent in
    that same window. This is what main.py writes into the result CSVs'
    per-attempt tokens_*/llm_calls columns and into token-usage-by-node.json:
    every consumer of either is reading the inflated figure, silently.
    score_run.py's tokens() (a whole-run scrape, max within each series) is
    not affected and is the number to trust; see this file's module
    docstring for how the 2.41x was measured.
    """
    start, end = _totals(before), _totals(after)
    keys = ("calls", *TOKEN_TYPES)

    return {k: max(0, int(end.get(k, 0.0) - start.get(k, 0.0))) for k in keys}


def delta_by_node(before: Path, after: Path) -> dict[str, dict[str, int]]:
    """What one attempt spent, broken out by node -- or so its name claims.
    Never negative per node.

    Not summed across node the way delta()'s total is summed across thread:
    see _totals_by_node for why. Same never-negative floor as delta(), applied
    per node independently, so one node's counter resetting cannot corrupt
    another node's figure.

    WARNING: inflated the same way and by roughly the same 2.41x delta() is
    -- see that function's docstring and this module's own docstring. Node
    does not save this: a concurrent slot's calls land on the same node
    labels (hypothesis_gen_agent, diagnosis, and so on) as the bracketed
    attempt's own calls, so splitting by node does not separate one slot's
    spend from another's. This is what main.py archives as each attempt's
    token-usage-by-node.json; treat every number in it as inflated by the
    same factor until the underlying bracketing is fixed.
    """
    start, end = _totals_by_node(before), _totals_by_node(after)
    keys = ("calls", *TOKEN_TYPES)
    nodes = sorted(set(start) | set(end))

    return {
        node: {k: max(0, int(end.get(node, {}).get(k, 0.0) - start.get(node, {}).get(k, 0.0))) for k in keys}
        for node in nodes
    }


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "snapshot":
        return snapshot(Path(argv[2]))

    if len(argv) == 4 and argv[1] == "delta":
        print(json.dumps(delta(Path(argv[2]), Path(argv[3]))))

        return 0

    if len(argv) == 4 and argv[1] == "delta-by-node":
        print(json.dumps(delta_by_node(Path(argv[2]), Path(argv[3]))))

        return 0

    print(__doc__, file=sys.stderr)

    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
