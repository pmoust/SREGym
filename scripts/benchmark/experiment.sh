#!/usr/bin/env bash
# The engineer-facing entry point for an ad-hoc experiment. Skeleton stage:
# this only resolves --sregym-ref and --brain-go-ref and prints what it found.
# No cost model, no problem selection, no terraform invocation yet; those
# land in later work on top of this. The point of landing this much on its
# own is that the ref-resolution behavior can be exercised end to end without
# waiting on the rest of the mechanism.
#
#   ./scripts/benchmark/experiment.sh --brain-go-ref my/new-convergence-node
#   ./scripts/benchmark/experiment.sh --sregym-ref my-branch --brain-go-ref main
#
# The resolution and the three checks (does the ref resolve at all, has it
# been pushed, is the worktree that resolved it clean) mirror launch.sh's
# `resolve()` exactly, because they answer the same question: can the fleet,
# which clones from the remote, actually see the code this ref names. The two
# copies exist because launch.sh's version reads the ref from a scenario's
# tfvars while this one reads it off the command line; once this script
# starts wrapping launch.sh instead of standing alone, this copy goes away
# and the ref flows into the generated tfvars for launch.sh to resolve.

set -euo pipefail

usage() {
  echo "usage: experiment.sh [--sregym-ref REF] [--brain-go-ref REF]" >&2
  echo "       Resolves each ref to a full SHA and reports what would be pinned." >&2
  echo "       Neither flag is required; an omitted ref means origin/main." >&2
}

SREGYM_REF=""
BRAIN_GO_REF=""
while [ $# -gt 0 ]; do
  case "$1" in
    --sregym-ref)
      SREGYM_REF="${2:?--sregym-ref needs a value}"
      shift 2
      ;;
    --brain-go-ref)
      BRAIN_GO_REF="${2:?--brain-go-ref needs a value}"
      shift 2
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "experiment: unknown option: $1" >&2
      usage
      exit 2
      ;;
  esac
done

# Same symlink-following as launch.sh: dirname "$0" alone is the symlink's
# own directory, which need not be inside the repo.
SELF="$0"
while [ -L "$SELF" ]; do
  link=$(readlink "$SELF")
  case "$link" in
    /*) SELF="$link" ;;
    *) SELF="$(dirname "$SELF")/$link" ;;
  esac
done
ROOT=$(git -C "$(dirname "$SELF")" rev-parse --show-toplevel)

# brain-go lives beside the main checkout; from a linked worktree that is not
# $ROOT/.., so try the main checkout's parent too before giving up. Same
# discovery as launch.sh.
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
  echo "experiment: cannot find the brain-go checkout beside this one. Set BRAIN_GO_DIR." >&2
  exit 1
fi

resolve() {
  local dir="$1" name="$2" ref="${3:-}" sha

  if [ -z "$ref" ]; then
    if ! git -C "$dir" fetch -q origin main; then
      echo "experiment: cannot fetch origin/main for $name in $dir" >&2
      echo "experiment: refusing to pin a possibly stale ref" >&2
      return 1
    fi
    if ! sha=$(git -C "$dir" rev-parse origin/main 2>/dev/null); then
      echo "experiment: $name has no origin/main in $dir" >&2
      return 1
    fi
    if [ ${#sha} -ne 40 ]; then
      echo "experiment: $name resolved to something that is not a full SHA: $sha" >&2
      return 1
    fi
    printf '%s\n' "$sha"
    return 0
  fi

  # 1. Does it resolve to a commit here at all.
  if ! sha=$(git -C "$dir" rev-parse --verify -q "${ref}^{commit}" 2>/dev/null); then
    echo "experiment: $name ref '$ref' does not resolve to a commit in $dir" >&2
    return 1
  fi

  # 2. Is that commit reachable on origin. `fetch origin` with no refspec
  # updates every refs/remotes/origin/* branch tip, so `branch -r --contains`
  # after it answers the same question the fleet's own clone would hit.
  if ! git -C "$dir" fetch -q origin; then
    echo "experiment: cannot fetch origin for $name in $dir" >&2
    return 1
  fi
  if [ -z "$(git -C "$dir" branch -r --contains "$sha" 2>/dev/null)" ]; then
    echo "experiment: $name ref '$ref' ($sha) is not on origin in $dir" >&2
    echo "experiment: push it first; the fleet clones from the remote and cannot see an unpushed commit" >&2
    return 1
  fi

  # 3. Is the worktree that resolved it clean.
  if [ -n "$(git -C "$dir" status --porcelain)" ]; then
    echo "experiment: $name worktree in $dir is dirty, refusing to pin ref '$ref'" >&2
    echo "experiment: a run whose code exists nowhere is unreproducible; commit or stash first" >&2
    return 1
  fi

  printf '%s\n' "$sha"
}

SREGYM_SHA=$(resolve "$ROOT" sregym "$SREGYM_REF") || exit 1
BRAIN_GO_SHA=$(resolve "$BRAIN_GO_DIR" brain-go "$BRAIN_GO_REF") || exit 1

echo "experiment: sregym     ${SREGYM_SHA}  ${SREGYM_REF:+(ref: $SREGYM_REF)}"
echo "experiment: brain-go   ${BRAIN_GO_SHA}  ${BRAIN_GO_REF:+(ref: $BRAIN_GO_REF)}"
