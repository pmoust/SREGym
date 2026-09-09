#!/usr/bin/env bash
# Pull a run's logs and results out of S3 before anything deletes them.
#
# Written after losing run-20260829-1047's slot logs: the bucket had to be
# emptied for the next apply (run_id derives from timestamp(), so every plan
# renames the bucket and forces a replace), only the CSVs had been saved, and
# the logs that would have said whether pod-readiness timeouts were new to the
# following run went with it.
#
#   ./scripts/benchmark/archive_run.sh run-20260829-1325
#
# Safe to run repeatedly and while a run is still going: s3 sync only fetches
# what changed, so calling it periodically keeps a live snapshot rather than
# betting on the run ending tidily.
#
# Lives here, in version control, beside the scripts that consume what it
# fetches. It spent its first weeks as an untracked file in a sibling brain-go
# checkout, which meant the one script every documented workflow depends on to
# get results out of S3 existed on a single laptop with no history: a reinstall
# would have erased it and nothing would have recorded that it ever existed.

set -euo pipefail

PREFIX="${1:?usage: archive_run.sh <run-id or prefix>   e.g. run-20260829-1325}"
REGION="${AWS_REGION:-us-east-2}"
# Where sregym-results/ and sregym-run-logs/ get created.
#
# SREGYM_ARCHIVE_ROOT wins when set. Otherwise a sibling brain-go checkout is
# preferred, because every run archived so far lives there and both this
# repo's scripts/benchmark/README.md and score_run.py's own examples point at
# those paths; this repo's root is the fallback when there is no sibling. The
# resolved path is echoed below, so landing somewhere unexpected is visible
# rather than silent.
# A linked git worktree lives outside the primary checkout, so deriving the
# sibling from this script's own path finds worktrees/brain-go, which does not
# exist, instead of the real checkout beside the repo. Ask git for the primary
# worktree and look beside that. Falls back to the path when git is not
# available or this is not a checkout at all.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# --git-common-dir rather than parsing `git worktree list`. That list prints
# "<path> <sha> [branch]" unquoted, so any field-splitting truncates a path
# containing a space, and the suffix is not even a fixed shape: a detached
# worktree prints "(detached HEAD)" and a bare one prints "(bare)" with no sha
# at all. --git-common-dir returns the primary repo's .git directory from
# inside any linked worktree, and its own .git from the primary checkout, so
# its parent is the primary worktree in both cases with nothing to parse.
# Tested for emptiness before dirname, not after: `dirname ""` is "." rather
# than empty, so folding the two into one expression made a missing git (or a
# copy of this script outside any checkout) resolve the archive root to the
# current directory and write there.
GIT_COMMON="$(git -C "$SCRIPT_DIR" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
if [ -n "$GIT_COMMON" ]; then
  PRIMARY="$(dirname "$GIT_COMMON")"
else
  PRIMARY="$(cd "$SCRIPT_DIR/../.." && pwd)"
fi

if [ -n "${SREGYM_ARCHIVE_ROOT:-}" ]; then
  ROOT="$SREGYM_ARCHIVE_ROOT"
elif [ -d "$PRIMARY/../brain-go" ]; then
  ROOT="$(cd "$PRIMARY/../brain-go" && pwd)"
else
  ROOT="$PRIMARY"
fi

# Validated rather than trusted, for both routes into it. A bad
# SREGYM_ARCHIVE_ROOT and a PRIMARY derived outside any checkout both used to
# get as far as mkdir before failing, and the failure named a path nobody had
# chosen. Refusing here says which knob to turn instead.
if [ ! -d "$ROOT" ] || [ ! -w "$ROOT" ]; then
  echo "archive_run: archive root is not a writable directory: $ROOT" >&2
  echo "archive_run: set SREGYM_ARCHIVE_ROOT to where sregym-results/ should live" >&2
  exit 2
fi

# Resolve the bucket by prefix rather than constructing the name. run_id carries
# a random_id hex suffix (run-20260829-1325-a4d231c1), which is easy to omit and
# gives a confusing "no such bucket" when you do. Passing the date part is
# enough. Omitting the argument entirely is not: that would silently archive
# whichever run happened to sort last.
#
# Two layouts, because both exist. Runs up to 2026-09-02 each had their own
# bucket named for the run; from sregym#48 there is one bucket named for the
# account, holding every run under its own <run-id>/ prefix. Try the per-run
# name first, since an older run only exists that way.
# Every aws lookup below tolerates failure, and credentials are checked first.
# Without them a failed call (an expired SSO token is the common one) makes the
# command substitution non-zero, set -e exits before a single line is printed,
# and the script looks like it ran and found nothing to do. That is the worst
# possible failure for a tool whose whole job is "get the data off S3 before it
# is deleted": it is indistinguishable from success.
if ! aws sts get-caller-identity --output text >/dev/null 2>&1; then
  echo "archive_run: no usable AWS credentials for $REGION" >&2
  echo "archive_run: refresh them (e.g. aws sso login) and re-run; nothing was fetched" >&2
  exit 2
fi

# Every match, not head -1. The shared-bucket branch below has always refused
# an ambiguous prefix; this branch did not, so a prefix matching two per-run
# buckets silently archived whichever sorted first and printed an ordinary
# success line. "run-2026" matches two real buckets today, which is exactly the
# "wrong run gets analysed" outcome the note above says to refuse.
BUCKET_MATCHES=$(aws s3api list-buckets --region "$REGION" \
  --query "Buckets[?starts_with(Name, 'sregym-bench-${PREFIX}')].Name" --output text 2>/dev/null | tr '\t' '\n' | grep . || true)

if [ "$(printf '%s' "$BUCKET_MATCHES" | grep -c .)" -gt 1 ]; then
  echo "archive_run: ${PREFIX} matches more than one per-run bucket:" >&2
  printf '%s\n' "$BUCKET_MATCHES" | sed 's/^/  /' >&2
  echo "archive_run: pass a longer prefix" >&2
  exit 1
fi

BUCKET=$(printf '%s' "$BUCKET_MATCHES" | head -1)

if [ -n "$BUCKET" ]; then
  # The object prefix inside a per-run bucket is the run id, which is the
  # bucket name minus the fixed wrapper. Both strips are load-bearing: real
  # per-run buckets are named sregym-bench-<run-id>-results.
  RUN_ID="${BUCKET#sregym-bench-}"
  RUN_ID="${RUN_ID%-results}"
else
  # The shared bucket. Resolve the run id from the prefixes inside it, so a
  # date fragment still works as an argument the way it always has.
  BUCKET=$(aws s3api list-buckets --region "$REGION" \
    --query "Buckets[?starts_with(Name, 'sregym-bench-results-')].Name" --output text 2>/dev/null | tr '\t' '\n' | head -1 || true)

  if [ -n "$BUCKET" ]; then
    MATCHES=$(aws s3api list-objects-v2 --bucket "$BUCKET" --region "$REGION" \
      --delimiter / --query "CommonPrefixes[].Prefix" --output text 2>/dev/null \
      | tr '\t' '\n' | tr -d '/' | grep "^${PREFIX}" || true)
    RUN_ID=$(echo "$MATCHES" | head -1)

    # More than one match means the argument was ambiguous. Archiving an
    # arbitrary one of them is how the wrong run gets analysed, so refuse and
    # name them instead.
    if [ "$(echo "$MATCHES" | grep -c .)" -gt 1 ]; then
      echo "archive_run: ${PREFIX} matches more than one run in $BUCKET:" >&2
      echo "$MATCHES" | sed 's/^/  /' >&2
      echo "archive_run: pass a longer prefix" >&2
      exit 1
    fi
  fi
fi

if [ -z "${BUCKET:-}" ] || [ -z "${RUN_ID:-}" ]; then
  echo "archive_run: no run matching ${PREFIX} in $REGION" >&2
  echo "archive_run: buckets present:" >&2
  aws s3api list-buckets --query "Buckets[?starts_with(Name,'sregym-bench-')].Name" --output text 2>/dev/null \
    | tr '\t' '\n' | sed 's/^/  /' >&2 || true
  if [ -n "${BUCKET:-}" ]; then
    echo "archive_run: runs in $BUCKET:" >&2
    aws s3api list-objects-v2 --bucket "$BUCKET" --region "$REGION" --delimiter / \
      --query "CommonPrefixes[].Prefix" --output text 2>/dev/null | tr '\t' '\n' | sed 's/^/  /' >&2 || true
  fi
  exit 1
fi

RESULTS="$ROOT/sregym-results/$RUN_ID"
LOGS="$ROOT/sregym-run-logs/$RUN_ID"
SRC="s3://${BUCKET}/${RUN_ID}/"

mkdir -p "$RESULTS" "$LOGS"

# *.json and *.prom, not only *.csv. The per-attempt token-usage-by-node.json
# and tool-calls.json carry the per-node token and tool-call breakdown, and
# tokens-before/after.prom are the scrapes a delta was computed from. While
# this pulled csv alone, a local archive held the scores but not the evidence
# behind how they were priced: run-20260908-0739 archived zero .json files
# despite writing 63 of each to S3, so anyone following the documented workflow
# concluded the per-node data did not exist.
echo "[archive] results -> $RESULTS"
# The trailing excludes are not redundant. AWS CLI's "*" spans path separators,
# so a bare --include "*.json" also swept kubectl's API-discovery cache (300 of
# 478 objects on run-20260908-0739, nothing to do with token evidence), and
# --include "*.prom" also matched the shard scrapes the logs sync below already
# takes, landing the same bytes in both trees. Later filters win, so excluding
# those two paths after the includes keeps exactly the per-attempt artifacts.
aws s3 sync "$SRC" "$RESULTS/" --region "$REGION" \
  --exclude "*" --include "*.csv" --include "*.json" --include "*.prom" \
  --exclude "*/.kube/*" --exclude "*/logs/*" --only-show-errors

echo "[archive] logs -> $LOGS"
# Two patterns, because the per-attempt trace and the shard log sit at
# different depths. The shard and slot logs live under a literal logs/
# directory, which is all --include "*logs/*" can match. The per-attempt
# trace, sregym_<time>.log, is written directly into run_N/ and anon_*/
# beside that attempt's token-usage-by-node.json, with no logs/ segment
# anywhere in its key, so the folder pattern never saw it: on
# run-20260908-0739, 126 of 179 .log objects, every per-attempt trace in
# the run, stayed in S3. That is the same loss the results sync above was
# fixed for, one file type over. These land here rather than in results/,
# which never includes .log at all: structured scores and evidence go to
# results/, raw traces to logs/.
aws s3 sync "$SRC" "$LOGS/" --region "$REGION" \
  --exclude "*" --include "*logs/*" --include "*.log" --only-show-errors

# Per-attempt files under a run_N directory, which is what a "scored
# problem-run" is. Counting *_ALL_results.csv instead reported 0 for most of a
# live run, because a slot only writes that file when it finishes, and 21 at
# the end of a run that had scored 63.
scored=$(find "$RESULTS" -path "*/run_*" -name "*_results.csv" | wc -l | tr -d ' ')
logs=$(find "$LOGS" -type f | wc -l | tr -d ' ')
echo "[archive] $RUN_ID: $scored scored problem-runs, $logs log files"
