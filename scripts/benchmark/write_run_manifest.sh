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

{
  echo "run_id=${RUN_ID:-}"
  echo "shard_index=${SHARD_INDEX:-}"
  echo "shard_count=${SHARD_COUNT:-}"
  echo "problem_set=${PROBLEM_SET:-}"
  echo "n_attempts=${N_ATTEMPTS:-}"
  echo "noise=${NOISE:-}"
  echo "agent_timeout_seconds=${AGENT_TIMEOUT_SECONDS:-}"
  echo "runs_per_instance=${RUNS_PER_INSTANCE:-}"
  echo "agent_model=${AGENT_MODEL:-}"
  echo "judge_model=${JUDGE_MODEL:-}"
  echo "sregym_ref=${SREGYM_REF:-}"
  echo "sregym_sha=${SREGYM_SHA:-}"
  echo "brain_go_ref=${BRAIN_GO_REF:-}"
  echo "brain_go_sha=${BRAIN_GO_SHA:-}"
  # One KEY=VALUE per line, exactly as written into brain-go's deploy/.env.
  printf '%s\n' "${BRAIN_ENV_EXTRA:-}"
} >"$OUT"
