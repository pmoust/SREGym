#!/usr/bin/env bash
# Record what produced a run's numbers, as one object beside its logs.
#
# Without this a finished run cannot state its own configuration.
# setup_brain_go.sh logs brain_env key names only, deliberately, because that
# log ships to S3 and the mechanism is generic enough to carry a secret. The
# values then die with the instance, and analysis has to quote brain-go's
# compiled defaults as though they were the run's values.
#
# That is what happened to run-20260831-1752: its MaxHypotheses,
# ParallelBatchSize and Temperature are unrecoverable. Temperature is the worst
# of the three to lose, being the largest driver of the run-to-run variance
# that run existed to measure.
#
# Values, not just keys, because this is a different surface from the log: the
# bucket is private and encrypted, and brain_env's own documentation already
# tells operators not to put secrets in it, since the values reach terraform
# state, plan output and user_data regardless.
#
# Usage: write_run_manifest.sh <output-path>
#        write_run_manifest.sh --stages <output-path>
#
# Reads its content from the environment, so the caller decides what is known.
# The second form rewrites only the stage lines of a manifest this script
# already wrote, for the one field that is not knowable when the rest is: the
# bootstrap writes the manifest and only then sources run_shard.sh, which is
# where the tasklist gets generated. Same derivation either way, called from
# one place, so the two forms cannot read a tasklist two different ways.

set -uo pipefail

STAGES_ONLY=0
if [ "${1:-}" = "--stages" ]; then
  STAGES_ONLY=1
  shift
fi

OUT="${1:?usage: write_run_manifest.sh [--stages] <output-path>}"

# Which stages ran, derived rather than asserted. sregym/conductor/tasklist.yml
# selects stages per problem (diagnosis, mitigation, in that order); with no
# such file the conductor runs both. The file is gitignored, so a git clone
# never brings one: scripts/benchmark/write_tasklist.py generates it at shard
# start, and the smoke-test workflow writes a mitigation-only one of its own.
# Anything left on a box would silently change what a run measures, and a run
# that cannot say which stages it graded cannot be compared against one that
# graded more.
#
# A stage still needs its oracle attached: a problem with no mitigation_oracle
# is skipped with "Mitigation oracle is not attached", which cannot be seen
# from here. All 21 sregym-lite problems have one, checked against the
# archived results rather than by reading the registry.
emit_stages() {
  local tasklist="${SREGYM_TASKLIST:-${WORKDIR:-.}/sregym/conductor/tasklist.yml}"
  if [ -f "$tasklist" ]; then
    echo "stages_source=tasklist.yml"
    # Comments stripped first. The generated file carries a header saying where
    # it came from, and a header that happened to name a stage would otherwise
    # be read as that stage having run.
    echo "stages=$(grep -v '^[[:space:]]*#' "$tasklist" | grep -oE '(diagnosis|mitigation)' | sort -u | paste -sd, -)"
    echo "tasklist_sha256=$(shasum -a 256 "$tasklist" 2>/dev/null | cut -d" " -f1)"
  else
    echo "stages_source=conductor-default-no-tasklist"
    echo "stages=diagnosis,mitigation"
  fi
}

if [ "$STAGES_ONLY" = "1" ]; then
  if [ ! -f "$OUT" ]; then
    echo "write_run_manifest.sh --stages: $OUT does not exist" >&2
    exit 1
  fi
  # Written to a sibling and moved, so a failure halfway through leaves the
  # previous manifest intact rather than a truncated one. The refreshed lines
  # land at the end of the file; nothing reads the manifest positionally.
  TMP="$OUT.stages.$$"
  {
    grep -v -E '^(stages|stages_source|tasklist_sha256)=' "$OUT"
    emit_stages
  } >"$TMP" && mv "$TMP" "$OUT"
  exit $?
fi

# The fully resolved problem id list, not PROBLEM_SET or PROBLEMS_OVERRIDE
# verbatim: a run's provenance should not depend on whether the selection
# was one named set, a mix of sets and bare ids, or a narrowed override, only
# on which ids actually ran. Resolved with the same resolve_problems() that
# run_shard.sh uses, so the two cannot read the same PROBLEM_SET two
# different ways.
#
# This script runs before run_shard.sh (see bootstrap.sh.tftpl) and its own
# caller already tolerates its failure with `|| true`, so a bad PROBLEM_SET
# here must not blank out every other field: only this line goes empty, and
# run_shard.sh is still what hard-stops the run over it.
PROBLEMS_RESOLVED=$(PROBLEM_SET="${PROBLEM_SET:-}" PROBLEMS_OVERRIDE="${PROBLEMS_OVERRIDE:-}" uv run python -c "
import os, sys
from sregym.conductor.problem_sets import resolve_problems

try:
    ids = resolve_problems(os.environ['PROBLEM_SET'])
except ValueError as exc:
    sys.stderr.write(str(exc) + chr(10))
    sys.exit(1)

want = os.environ.get('PROBLEMS_OVERRIDE', '').split()
if want:
    ids = [p for p in ids if p in set(want)]

print(' '.join(ids))
" 2>/dev/null) || PROBLEMS_RESOLVED=""

# What each seeded connection was tagged with, read straight from
# seed_connections.py's ENDPOINTS rather than copied here, so this cannot drift
# from what actually got seeded. This is the harness's own side of the toolset
# a run had: which MCP endpoint became which CUSTOM_MCP connection, and which
# investigation bucket(s) it was registered under.
#
# It is not the resolved per-node toolset. Which tools a given RCI node (state,
# metrics, logs, traces) actually binds on a turn depends on brain-go's own
# logic -- nodeBucket and BuildCustomTools's bucket filter in
# internal/chat/tools/registry.go, and DefaultBucketForKind in
# internal/core/domain/connection.go -- which this script has no view into and
# must not guess at: a copy here would silently drift the first time that logic
# changes. Recovering the per-node toolset for a specific run still means
# reading those three functions at the run's brain_go_sha and checking
# tool-calls.json, the way this field's own reasoning was worked out.
SEEDED_CONNECTION_BUCKETS=$(uv run python -c "
from scripts.benchmark.seed_connections import ENDPOINTS

print(','.join(f'{path}:{\"+\".join(buckets)}' for _, path, buckets, _ in ENDPOINTS))
" 2>/dev/null) || SEEDED_CONNECTION_BUCKETS=""

{
  echo "run_id=${RUN_ID:-}"
  echo "shard_index=${SHARD_INDEX:-}"
  echo "shard_count=${SHARD_COUNT:-}"
  echo "problem_set=${PROBLEM_SET:-}"
  # The resolved list, independent of how selection was expressed. Empty
  # here means resolution failed (see above), not that the whole registry
  # ran: an unresolvable PROBLEM_SET makes run_shard.sh terminate the run
  # with nothing scored.
  echo "problems=$PROBLEMS_RESOLVED"
  echo "n_attempts=${N_ATTEMPTS:-}"
  echo "noise=${NOISE:-}"
  echo "agent_timeout_seconds=${AGENT_TIMEOUT_SECONDS:-}"
  echo "runs_per_instance=${RUNS_PER_INSTANCE:-}"
  # The instance family, because slot count alone does not describe the box
  # the slots ran on. run-20260902-0606 recorded runs_per_instance=3 and
  # nothing else, so recovering that it ran on r7a.24xlarge took a CloudTrail
  # lookup of RunInstances. Two runs at the same slot count on different
  # families are not the same run.
  echo "instance_type=${INSTANCE_TYPE:-}"
  # Where the results went. One bucket now holds every run under its own
  # <run-id>/ prefix (#48), so the bucket name is no longer recoverable from
  # the run id and an archived run should say where it came from.
  echo "results_bucket=${RESULTS_BUCKET:-}"
  echo "agent_model=${AGENT_MODEL:-}"
  echo "judge_model=${JUDGE_MODEL:-}"
  echo "sregym_ref=${SREGYM_REF:-}"
  echo "sregym_sha=${SREGYM_SHA:-}"
  echo "brain_go_ref=${BRAIN_GO_REF:-}"
  echo "brain_go_sha=${BRAIN_GO_SHA:-}"
  # kind:bucket[+bucket](,kind:bucket...) for each CUSTOM_MCP connection this
  # run seeded. See the comment above SEEDED_CONNECTION_BUCKETS for what this
  # does and does not tell a reader about a node's actual bound toolset.
  echo "seeded_connection_buckets=$SEEDED_CONNECTION_BUCKETS"

  # Which stages ran. See emit_stages above; run_shard.sh calls this script a
  # second time with --stages once it has generated the tasklist, because at
  # this point in the bootstrap the file does not exist yet.
  emit_stages
  # One KEY=VALUE per line, exactly as written into brain-go's deploy/.env.
  printf '%s\n' "${BRAIN_ENV_EXTRA:-}"
} >"$OUT"
