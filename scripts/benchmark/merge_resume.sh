#!/usr/bin/env bash
# Merge a slot's prior result CSVs into the single file main.py --resume takes.
#
# A slot writes one *_ALL_results.csv per results/<timestamp>/ directory, and a
# slot that has been restarted has several: three in run-20260831-1752. The
# bootstrap used `find -print -quit`, which returned whichever the filesystem
# offered first, so a replaced instance silently re-ran the other two thirds of
# its completed work. The resume path exists so a replacement costs only the
# in-flight problem.
#
# Prints the merged path on stdout, or nothing when there is no prior result.
# Everything else goes to stderr so the caller can capture the path.
#
# Usage: merge_resume.sh <slot-dir>

set -uo pipefail

SLOT_DIR="${1:?usage: merge_resume.sh <slot-dir>}"
MERGED="$SLOT_DIR/.resume-merged.csv"

# Deliberately not mapfile or a bash array: this has to run under whatever bash
# the box has, and mapfile is bash 4+. macOS ships 3.2, so a change tested only
# on the instance would look fine and a change tested only locally would not.
LIST="$SLOT_DIR/.resume-files"
find "$SLOT_DIR/results" -name '*_ALL_results.csv' 2>/dev/null | sort >"$LIST"

count=$(wc -l <"$LIST" | tr -d ' ')
if [ "$count" -eq 0 ]; then
  rm -f "$LIST"
  echo "no prior results" >&2
  exit 0
fi

# Header from the first, data rows from all. Every file is the same schema:
# they are successive writes of one slot's own results.
first=$(head -1 "$LIST")
{
  head -1 "$first"
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    tail -n +2 "$f"
  done <"$LIST"
} >"$MERGED"
rm -f "$LIST"

echo "merged $count file(s), $(( $(wc -l <"$MERGED") - 1 )) rows" >&2
echo "$MERGED"
