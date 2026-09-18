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
# A scenario that leaves sregym_ref / brain_go_ref unset (every scenario
# checked in today) gets origin/main, refetched and re-resolved on every
# launch. Set either to something else in the scenario's tfvars, a branch, a
# tag or a SHA, to run an engineer's own code instead: this script resolves
# that ref to a full SHA, refuses it if it does not resolve, was never pushed
# (the fleet clones from the remote and cannot see a local-only commit), or
# the worktree that resolved it is dirty (a run whose code exists nowhere is
# unreproducible), and pins the result exactly like the origin/main case.
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

# A `key = "value"` scalar out of a tfvars file, comment and quotes stripped.
# Same tradeoff the problems-list grep below already makes: good enough for
# this repo's flat tfvars, not a general HCL parser. A commented-out line
# does not match. `[[:space:]]*` before the key allows only whitespace there,
# never `#`, and a match takes everything up to the closing quote so a
# trailing `# ...` comment is discarded with the rest of the line.
tfvars_ref() {
  sed -n "s/^[[:space:]]*$2[[:space:]]*=[[:space:]]*\"\\([^\"]*\\)\".*/\\1/p" "$1" | tail -1
}

resolve() {
  local dir="$1" name="$2" ref="${3:-}" sha

  if [ -z "$ref" ]; then
    # Default: origin/main, refetched every time. Checked explicitly rather
    # than trusted to set -e, for the reason in the file header: a failed
    # fetch under bash 3.2 does not abort a multi-statement substitution, so
    # this used to fall through to rev-parse against a stale cached ref, pass
    # the 40-char check because a stale SHA is still 40 chars, and pin last
    # week's code silently.
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
    return 0
  fi

  # An engineer-supplied ref: the scenario's tfvars sets sregym_ref or
  # brain_go_ref to something other than main. Three checks, in the order
  # they can fail, each named because each fails quietly otherwise.
  #
  # 1. Does it resolve to a commit here at all.
  if ! sha=$(git -C "$dir" rev-parse --verify -q "${ref}^{commit}" 2>/dev/null); then
    echo "launch: $name ref '$ref' does not resolve to a commit in $dir" >&2
    return 1
  fi

  # 2. Is that commit reachable on origin. `fetch origin` with no refspec
  # updates every refs/remotes/origin/* branch tip per the remote's default
  # fetch refspec, so `branch -r --contains` after it is the same reachable
  # check the fleet's own clone would make, not just "did this name exist
  # once." A commit that only lives in this worktree is invisible to the
  # fleet, which clones from the remote, and that failure mode is otherwise a
  # silent one three minutes into boot rather than a named error here.
  if ! git -C "$dir" fetch -q origin; then
    echo "launch: cannot fetch origin for $name in $dir" >&2
    return 1
  fi
  if [ -z "$(git -C "$dir" branch -r --contains "$sha" 2>/dev/null)" ]; then
    echo "launch: $name ref '$ref' ($sha) is not on origin in $dir" >&2
    echo "launch: push it first; the fleet clones from the remote and cannot see an unpushed commit" >&2
    return 1
  fi

  # 3. Is the worktree that resolved it clean, counting tracked files only.
  # A run whose code exists nowhere, neither pinned nor on disk in the form it
  # ran, is unreproducible, and an uncommitted edit to a tracked file is
  # exactly that: the engineer reads their own change on disk and the fleet
  # clones something else.
  #
  # Untracked files are not that. They cannot be part of the commit check 2
  # just proved is on origin, so they cannot reach the fleet or change what
  # runs. Counting them refuses every launch from a checkout holding scratch
  # notes, an .idea/ or a stray .DS_Store, which is what both of these
  # checkouts hold, and the refusal names a reproducibility problem the run
  # does not have.
  if [ -n "$(git -C "$dir" status --porcelain --untracked-files=no)" ]; then
    echo "launch: $name worktree in $dir is dirty, refusing to pin ref '$ref'" >&2
    echo "launch: a run whose code exists nowhere is unreproducible; commit or stash first" >&2
    return 1
  fi

  printf '%s\n' "$sha"
}

SREGYM_REF_OVERRIDE=$(tfvars_ref "$VARS" sregym_ref)
BRAIN_GO_REF_OVERRIDE=$(tfvars_ref "$VARS" brain_go_ref)

# `|| exit 1` rather than trusting errexit through the substitution.
SREGYM_SHA=$(resolve "$ROOT" sregym "$SREGYM_REF_OVERRIDE") || exit 1
BRAIN_GO_SHA=$(resolve "$BRAIN_GO_DIR" brain-go "$BRAIN_GO_REF_OVERRIDE") || exit 1

echo "launch: scenario   $SCENARIO  ($VARS)"
if [ -n "$SREGYM_REF_OVERRIDE" ]; then
  echo "launch: sregym     $SREGYM_SHA  (ref: $SREGYM_REF_OVERRIDE)"
else
  echo "launch: sregym     $SREGYM_SHA"
fi
if [ -n "$BRAIN_GO_REF_OVERRIDE" ]; then
  echo "launch: brain-go   $BRAIN_GO_SHA  (ref: $BRAIN_GO_REF_OVERRIDE)"
else
  echo "launch: brain-go   $BRAIN_GO_SHA"
fi

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
  # "yes" too. The prompt reads [y/N] and that is what people type; matching
  # only the single letter turned a deliberate yes into "not applied", which
  # reads like the script refused.
  y | Y | yes | YES | Yes) ;;
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
