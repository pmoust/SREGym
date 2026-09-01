#!/usr/bin/env bash
# Launch a benchmark run from a named scenario.
#
#   ./scripts/benchmark/launch.sh smoke     # two problems, proves the harness
#   ./scripts/benchmark/launch.sh run       # 21 x 3, the scored run
#   ./scripts/benchmark/launch.sh smoke --plan-only
#
# The scenario lives in terraform/<name>.tfvars. This script's other job is
# resolving sregym_ref and brain_go_ref, for the reason terraform/README.md
# gives under "Running": both default to a branch name, and a branch is not a
# pinned input.
#
# A note on `set -e` here, because two bugs in the first version of this file
# came from trusting it.
#
# A substitution's own failure does propagate: ROOT=$(git rev-parse ...) stops
# the script if git fails. What does not propagate is a failure *inside* a
# multi-statement substitution, because bash 3.2 is what macOS ships and it has
# no `inherit_errexit`. So a function that fails partway through $(...) keeps
# going and returns whatever it printed, which is how the first version pinned
# a stale SHA after a failed fetch.
#
# Separately, a grep that legitimately matches nothing fails its whole pipeline
# under pipefail. Both cases are handled explicitly below.

set -euo pipefail

usage() {
  echo "usage: launch.sh <scenario> [--plan-only]" >&2
  echo "       scenario names a terraform/<scenario>.tfvars file" >&2
}

# Parse in a loop rather than by position, so `--plan-only smoke` works as well
# as `smoke --plan-only`. The first version took $1 as the scenario name
# unconditionally and reported "no such scenario: --plan-only".
SCENARIO=""
PLAN_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --plan-only) PLAN_ONLY=1 ;;
    -h | --help)
      usage
      exit 0
      ;;
    -*)
      echo "launch: unknown option: $arg" >&2
      usage
      exit 2
      ;;
    *)
      if [ -n "$SCENARIO" ]; then
        echo "launch: more than one scenario given: $SCENARIO and $arg" >&2
        exit 2
      fi
      SCENARIO="$arg"
      ;;
  esac
done
if [ -z "$SCENARIO" ]; then
  usage
  exit 2
fi

# Follow symlinks before asking git where the root is. `dirname "$0"` alone is
# the symlink's directory, which need not be inside the repo at all.
SELF="$0"
while [ -L "$SELF" ]; do
  link=$(readlink "$SELF")
  case "$link" in
    /*) SELF="$link" ;;
    *) SELF="$(dirname "$SELF")/$link" ;;
  esac
done
ROOT=$(git -C "$(dirname "$SELF")" rev-parse --show-toplevel)
TF="$ROOT/terraform"
VARS="$TF/$SCENARIO.tfvars"

if [ ! -f "$VARS" ]; then
  echo "launch: no such scenario: $SCENARIO" >&2
  echo "launch: available:" >&2
  for f in "$TF"/*.tfvars; do
    [ -f "$f" ] || continue
    b=$(basename "$f" .tfvars)
    printf '  %-8s %s\n' "$b" "$(grep -m1 '^# ' "$f" | sed 's/^# //' || true)" >&2
  done
  exit 1
fi

# brain-go lives beside the main checkout. From a linked worktree that is not
# $ROOT/.., so try the main checkout's parent as well before giving up.
if [ -z "${BRAIN_GO_DIR:-}" ]; then
  MAIN_ROOT=$(dirname "$(git -C "$ROOT" rev-parse --git-common-dir)")
  for candidate in "$ROOT/../brain-go" "$MAIN_ROOT/../brain-go"; do
    if [ -d "$candidate/.git" ]; then
      BRAIN_GO_DIR=$(cd "$candidate" && pwd)
      break
    fi
  done
fi
if [ -z "${BRAIN_GO_DIR:-}" ]; then
  echo "launch: cannot find the brain-go checkout beside this one. Set BRAIN_GO_DIR." >&2
  exit 1
fi

resolve() {
  local dir="$1" name="$2" sha
  # Checked explicitly. Relying on set -e here is what let the first version
  # pin a stale SHA after an offline fetch: the fetch failed, rev-parse
  # succeeded against the cached ref, the 40-char check passed because a stale
  # SHA is still 40 chars, and the run pinned last week's code silently.
  if ! git -C "$dir" fetch -q origin main; then
    echo "launch: cannot fetch origin/main for $name in $dir" >&2
    echo "launch: refusing to pin a possibly stale ref" >&2
    return 1
  fi
  if ! sha=$(git -C "$dir" rev-parse origin/main 2>/dev/null); then
    echo "launch: $name has no origin/main in $dir" >&2
    return 1
  fi
  if [ ${#sha} -ne 40 ]; then
    echo "launch: $name resolved to something that is not a full SHA: $sha" >&2
    return 1
  fi
  printf '%s\n' "$sha"
}

# `|| exit 1` rather than trusting errexit through the substitution.
SREGYM_SHA=$(resolve "$ROOT" sregym) || exit 1
BRAIN_GO_SHA=$(resolve "$BRAIN_GO_DIR" brain-go) || exit 1

echo "launch: scenario   $SCENARIO  ($VARS)"
echo "launch: sregym     $SREGYM_SHA"
echo "launch: brain-go   $BRAIN_GO_SHA"

# `|| true` on both: a scenario that sets none of these keys, or leaves
# problems unset, is legitimate. Without it grep's empty-match exit 1 takes the
# whole script down under pipefail, which is what made `launch.sh run` die
# before planning while `launch.sh smoke` worked.
grep -E '^[[:space:]]*(instance_count|runs_per_instance|n_attempts|use_spot|max_runtime_hours|agent_timeout_seconds|noise)' "$VARS" \
  | sed 's/#.*//; s/[[:space:]]*$//; s/^/launch:   /' || true
PROBLEM_IDS=$(sed -n '/^problems[[:space:]]*=/,/]/p' "$VARS" | grep -oE '"[a-z0-9_]+"' | tr -d '"' | tr '\n' ' ' || true)
if [ -n "${PROBLEM_IDS// /}" ]; then
  echo "launch:   problems             $(printf '%s' "$PROBLEM_IDS" | wc -w | tr -d ' ') named: $PROBLEM_IDS"
else
  echo "launch:   problems             (unset, the whole problem_set)"
fi
echo

# Checked before the plan rather than after the apply. The 16384-byte user_data
# cap is enforced at apply, once the bucket and security group already exist.
if [ -x "$ROOT/scripts/benchmark/measure_user_data.py" ]; then
  "$ROOT/scripts/benchmark/measure_user_data.py" --check | sed 's/^/launch:   /'
fi
echo

PLAN="$TF/$SCENARIO.tfplan"
terraform -chdir="$TF" init -input=false >/dev/null
terraform -chdir="$TF" plan -input=false -out="$PLAN" \
  -var-file="$VARS" \
  -var "sregym_ref=$SREGYM_SHA" \
  -var "brain_go_ref=$BRAIN_GO_SHA"

if [ "$PLAN_ONLY" = "1" ]; then
  echo
  echo "launch: plan only. To apply:"
  echo "  terraform -chdir=$TF apply $PLAN"
  exit 0
fi

echo
printf 'launch: apply this plan? [y/N] '
# `read` fails at EOF, which is every non-interactive invocation. Treated as a
# no rather than left to abort the script with no message.
if ! read -r reply; then
  reply=""
  echo
fi
case "$reply" in
  y | Y) ;;
  *)
    echo "launch: not applied. The plan is at $PLAN"
    echo "launch: for a non-interactive launch, apply that plan directly."
    exit 0
    ;;
esac

terraform -chdir="$TF" apply "$PLAN"

echo
echo "launch: watch it with"
echo "  terraform -chdir=$TF output -raw watch"
echo "launch: count scored problem-runs with"
echo "  terraform -chdir=$TF output -raw progress"
echo "launch: archive results and logs with"
echo "  $BRAIN_GO_DIR/sregym-results/archive_run.sh <run-id-prefix>"
