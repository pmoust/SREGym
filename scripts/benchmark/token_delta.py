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

Usage:
  token_delta.py snapshot <out.prom>          # capture, via scrape_tokens.sh
  token_delta.py delta <before.prom> <after.prom>   # emit JSON on stdout
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


def _totals(path: Path) -> dict[str, float]:
    """Sum each counter across every label set in one scrape.

    Summed rather than kept per thread on purpose. The delta is taken around a
    single attempt, so every increment inside it belongs to that attempt
    whatever thread carried it, and summing means a mid-attempt thread change
    cannot lose counts.
    """
    out: dict[str, float] = {}
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

        if metric == "llm_calls_total":
            out["calls"] = out.get("calls", 0.0) + amount
            continue

        token_type = dict(_LABEL.findall(labels)).get("token_type")
        if token_type in TOKEN_TYPES:
            out[token_type] = out.get(token_type, 0.0) + amount

    return out


def delta(before: Path, after: Path) -> dict[str, int]:
    """What one attempt spent. Never negative.

    A counter that goes backwards means the process restarted mid-attempt, and
    its pre-restart total is unrecoverable. Reporting a negative would corrupt
    any sum built from these rows, so the floor is zero and the loss is
    silent-but-bounded rather than poisonous.
    """
    start, end = _totals(before), _totals(after)
    keys = ("calls", *TOKEN_TYPES)

    return {k: max(0, int(end.get(k, 0.0) - start.get(k, 0.0))) for k in keys}


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "snapshot":
        return snapshot(Path(argv[2]))

    if len(argv) == 4 and argv[1] == "delta":
        print(json.dumps(delta(Path(argv[2]), Path(argv[3]))))

        return 0

    print(__doc__, file=sys.stderr)

    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
