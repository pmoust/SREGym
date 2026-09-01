#!/usr/bin/env python3
"""Merge a slot's prior result CSVs into the single file main.py --resume takes.

A slot writes one *_ALL_results.csv per results/<timestamp>/ directory, so a
slot that has been restarted has several: three in run-20260831-1752. The
bootstrap used `find -print -quit`, which returned whichever the filesystem
offered first, so a replaced instance silently re-ran the other two thirds of
its completed work.

Concatenation does not work here, for three separate reasons. main.py:463
builds `fieldnames` as the sorted union of the keys present in *that process's*
results, so the header is a property of which problems that process happened to
run:

  - Widths differ across a single slot's own files. The 91 archived
    ALL_results files hold ten distinct headers across seven widths, 11 to 27.
  - The headers are not a superset chain, so no single file's header can serve
    as the merged one. Two of the archived files are both 13 fields wide and
    still incomparable: one has `agent_timeout_seconds` and `timed_out`, the
    other has `Mitigation.success` and `TTM`. The union of that pair is 15.
  - `Diagnosis.reasoning` is free text with embedded commas and newlines, so
    even matched headers cannot be joined by line.

csv.DictReader does not raise on any of that. Extra columns land under a None
key and missing ones read as None, so a mismatched merge yields rows whose
`problem_id` is some other column's value. main.py:274 catches Exception
around the whole resume block, so the visible symptom is not an error: the
slot just re-runs everything.

Dedupes on (problem_id, attempt), newest file winning. Overlap is not
hypothetical -- a restarted slot re-writes earlier problems into its new
ALL_results -- and duplicates are not harmless: main.py:266 decides a problem
is finished by counting rows, `Counter(r["problem_id"]) >= n_attempts`, so two
copies of one attempt make a 2-attempt problem look complete after one.

Prints the merged path on stdout, or nothing when there is no prior result.
Diagnostics go to stderr so the caller can capture the path.

Usage: merge_resume.py <slot-dir>
"""

import csv
import sys
from pathlib import Path


def merge(slot_dir: Path) -> Path | None:
    # Oldest first, so a later file's row overwrites an earlier one for the
    # same (problem_id, attempt). mtime rather than name: the directory names
    # are timestamps and sort correctly today, but the tie-break that actually
    # matters is which write happened last.
    sources = sorted(slot_dir.glob("results/**/*_ALL_results.csv"), key=lambda p: p.stat().st_mtime)
    if not sources:
        print("no prior results", file=sys.stderr)
        return None

    fields: set[str] = set()
    rows: dict[tuple[str, str], dict[str, str]] = {}
    skipped = 0
    no_attempt = 0
    unreadable: list[str] = []
    for path in sources:
        try:
            # errors="replace", not strict. The judge's reasoning is the only
            # free-text column and it has carried non-UTF-8 bytes before;
            # problem_id and attempt are ASCII, so a mangled character in the
            # prose still leaves the row correctly keyed. Failing the merge
            # over one byte re-runs the whole shard.
            with path.open(newline="", encoding="utf-8", errors="replace") as fh:
                file_rows = list(csv.DictReader(fh))
        except (OSError, csv.Error) as exc:
            # Keep going. One unreadable file costs its own problems a re-run;
            # aborting costs every problem in every other file too.
            unreadable.append(f"{path}: {exc}")
            continue

        for row in file_rows:
            pid = (row.get("problem_id") or "").strip()
            if not pid or pid == "problem_id":
                # A row we cannot key is a row we cannot dedupe. Dropping it
                # costs one re-run; keeping it risks inflating an attempt
                # count and skipping work that was never done. A pid of
                # "problem_id" is a header line repeated mid-file, which
                # DictReader hands back as ordinary data.
                skipped += 1
                continue
            # Drop DictReader's restkey. A ragged row parks its surplus
            # fields under None, and a None in fieldnames makes the sort
            # below raise on the str comparison.
            fields.update(k for k in row if k is not None)
            attempt = (row.get("attempt") or "").strip()
            if not attempt:
                # Every row main.py writes has one. Without it, two real
                # attempts at one problem collapse to a single key and one
                # is lost, so report it rather than under-count in silence.
                no_attempt += 1
            rows[(pid, attempt)] = row

    if not rows:
        # An empty resume file is worse than none: main.py would load it, find
        # zero completed problems, and log a successful resume. Returning None
        # makes the caller omit --resume entirely, which is the honest
        # description of what we can offer.
        print("no usable rows recovered", file=sys.stderr)
        for bad in unreadable:
            print(f"WARNING: unreadable, its problems will re-run: {bad}", file=sys.stderr)
        return None

    merged = slot_dir / ".resume-merged.csv"
    fieldnames = sorted(fields)
    with merged.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for key in sorted(rows):
            writer.writerow({k: rows[key].get(k, "") for k in fieldnames})

    notes = []
    if skipped:
        notes.append(f"{skipped} unkeyed row(s) dropped")
    if no_attempt:
        notes.append(f"{no_attempt} row(s) with no attempt, which may have collapsed")
    note = f", {', '.join(notes)}" if notes else ""
    print(
        f"merged {len(sources) - len(unreadable)} of {len(sources)} file(s), "
        f"{len(rows)} unique (problem, attempt) rows, {len(fieldnames)} columns{note}",
        file=sys.stderr,
    )
    for bad in unreadable:
        print(f"WARNING: unreadable, its problems will re-run: {bad}", file=sys.stderr)
    return merged


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: merge_resume.py <slot-dir>", file=sys.stderr)
        return 2

    try:
        merged = merge(Path(argv[1]))
    except Exception as exc:  # noqa: BLE001
        # The call site is `resume_csv=$(merge_resume.py ... || true)`: it keeps
        # stdout and throws the exit code away. An uncaught exception here is
        # therefore not a loud failure. It prints nothing to stdout, the slot
        # resumes from nothing, and re-runs work it had already finished, which
        # is the silent re-run this script exists to stop. Say so on stderr and
        # leave stdout empty on purpose rather than by accident.
        print(f"ERROR: merge failed, this slot will re-run everything: {exc}", file=sys.stderr)
        return 1

    if merged is not None:
        print(merged)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
