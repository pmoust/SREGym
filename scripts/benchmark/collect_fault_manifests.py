#!/usr/bin/env python3
"""Collect a run's per-attempt fault specs into one manifest array.

main.py writes fault-spec.json into every published attempt directory. The
offline scorer takes a single JSON array, so this walks a results tree and
concatenates them, keeping one entry per problem.

    python scripts/benchmark/collect_fault_manifests.py results/0918_1200 -o manifests.json

Attempts of the same problem inject the same fault, so the entries collapse to
one per problem. A later attempt wins, because it is the one whose injected_at
matches the histories most likely to be scored.
"""

import argparse
import json
import sys
from pathlib import Path


def collect(root: Path) -> tuple[list[dict], list[Path]]:
    """Return manifests keyed by problem, plus the specs that named no component."""
    by_problem: dict[str, dict] = {}
    unlabeled: list[Path] = []

    for path in sorted(root.rglob("fault-spec.json")):
        try:
            spec = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            print(f"skipping {path}: {exc}", file=sys.stderr)
            continue

        problem = spec.get("problem")
        if not problem:
            unlabeled.append(path)
            continue
        if not spec.get("component"):
            unlabeled.append(path)

        previous = by_problem.get(problem)
        if previous is None or spec.get("attempt", 0) >= previous.get("attempt", 0):
            by_problem[problem] = spec

    manifests = [
        {
            "problem": spec["problem"],
            "component": spec.get("component", ""),
            "namespace": spec.get("namespace", ""),
            "mechanism": spec.get("mechanism", spec["problem"]),
            "injected_at": spec.get("injected_at"),
        }
        for _, spec in sorted(by_problem.items())
    ]
    return manifests, unlabeled


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results_dir", type=Path, help="a run directory under results/")
    parser.add_argument("-o", "--output", type=Path, help="write here instead of stdout")
    args = parser.parse_args()

    if not args.results_dir.is_dir():
        print(f"not a directory: {args.results_dir}", file=sys.stderr)
        return 1

    manifests, unlabeled = collect(args.results_dir)
    if not manifests:
        print(f"no fault-spec.json under {args.results_dir}", file=sys.stderr)
        return 1

    for path in unlabeled:
        print(f"warning: no component in {path}", file=sys.stderr)
    print(f"{len(manifests)} problems, {len(unlabeled)} unlabeled", file=sys.stderr)

    text = json.dumps(manifests, indent=2) + "\n"
    if args.output:
        args.output.write_text(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
