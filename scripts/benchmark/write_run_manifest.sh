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
# Reads its content from the environment, so the caller decides what is known.

set -uo pipefail

OUT="${1:?usage: write_run_manifest.sh <output-path>}"

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

  # Which stages ran, derived rather than asserted. sregym/conductor/tasklist.yml
  # selects stages per problem (diagnosis, mitigation, in that order); with no
  # such file the conductor runs both. The file is gitignored, so a git clone
  # never brings one, but CI writes a mitigation-only one, and anything left on a
  # box would silently change what a run measures. A run that cannot say which
  # stages it graded cannot be compared against one that graded more.
  #
  # A stage still needs its oracle attached: a problem with no mitigation_oracle
  # is skipped with "Mitigation oracle is not attached", which cannot be seen
  # from here. All 21 sregym-lite problems have one, checked against the
  # archived results rather than by reading the registry.
  TASKLIST="${SREGYM_TASKLIST:-${WORKDIR:-.}/sregym/conductor/tasklist.yml}"
  if [ -f "$TASKLIST" ]; then
    echo "stages_source=tasklist.yml"
    echo "stages=$(grep -oE '(diagnosis|mitigation)' "$TASKLIST" | sort -u | paste -sd, -)"
    echo "tasklist_sha256=$(shasum -a 256 "$TASKLIST" 2>/dev/null | cut -d" " -f1)"
  else
    echo "stages_source=conductor-default-no-tasklist"
    echo "stages=diagnosis,mitigation"
  fi
  # One KEY=VALUE per line, exactly as written into brain-go's deploy/.env.
  printf '%s\n' "${BRAIN_ENV_EXTRA:-}"
} >"$OUT"
