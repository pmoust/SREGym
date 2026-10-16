#!/usr/bin/env bash
# One-time: cache every host-level image in the S3 bucket, pinned by local
# image ID, so a benchmark box never pulls any of them from Docker Hub
# Run once per account from
# a developer machine, same as seed_image_cache.sh, and re-run only when one
# of these images needs to move (a kind node image bump, a deliberate
# registry:2 upgrade, or brain-go's own Dockerfile/compose file changing a
# pinned base or service image tag).
#
# A "host-level" image is one the box's own docker daemon pulls or builds
# from directly, never one a kind node's containerd pulls through
# registry_mirror.sh, so none of these belong in image_cache.list either:
#   - registry2, kindnode-x86: `docker run` directly by the HOST
#     (registry_mirror.sh and `kind create cluster` respectively), before
#     anything can pull through the mirror they serve.
#   - the rest: every brain-go compose service with no "build" key (postgres,
#     localstack, temporal, temporal-ui, dashboard, entry today) plus the
#     base images any build-keyed service's Dockerfile resolves FROM
#     (golang:<GO_VERSION>, alpine:<RUNTIME_IMAGE's tag>). A plain `docker
#     compose build`/`up -d` pulls whichever of these is not already present
#     locally.
# This last group is DERIVED from --brain-go-ref with
# scripts/benchmark/brain_go_images.py, not
# hand-copied from whatever origin/main looked like when this script was
# last edited: a pinned ref's docker/Dockerfile can set a different
# GO_VERSION, and its compose file a different dashboard/entry tag, than
# main does today (confirmed: brain-go's run.tfvars/smoke-pin.tfvars pin
# GO_VERSION=1.26.6 and node:22.9.0-alpine where origin/main was
# GO_VERSION=1.26.9 and node:22.22.0-alpine as of this writing), and a benchmark
# box checks out the pinned ref, not main. Re-run this script, with the same
# --brain-go-ref a run's tfvars pins, whenever that ref moves or
# brain-go's compose/Dockerfile changes a tag under it; setup_brain_go.sh's
# own gate fails loudly (listing every image still absent) rather than
# silently falling through to a Docker Hub pull if a box ever runs ahead of
# a re-seed.
#
# All of them used to be pulled straight from Docker Hub on every benchmark
# box (an earlier version of image_cache.list's header called the first two a
# "known, deliberate gap"). The owner decided there is no exception to "a
# benchmark run never touches Docker Hub", so this script, run_shard.sh's
# load of the first two, and setup_brain_go.sh's load of the rest close that
# gap the same way seed_image_cache.sh closes it for every other image:
# cache once, read many times.
#
# Mechanism: `docker pull` the image once, by tag, from this machine;
# `docker inspect` its local image ID (`.Id`), not a registry digest: a
# registry digest is how a *pull* names an image and is not reconstructable
# from a later `docker load`, while the image ID is, which is why
# load_host_image verifies a loaded image against this and not a digest.
# What the image ID actually is depends on which image store this machine's
# docker runs: a hash of the image's config JSON on the classic
# (graph-driver) store, or the manifest/index digest on the newer containerd
# image store (the default on recent Docker Desktop/Engine). Both are
# locally reproducible and round-trip unchanged through `docker save`/
# `docker load`, which is all this mechanism needs; neither is a full
# content-addressed lock of every layer byte (a known gap, not built here).
# `docker save` it to a tarball; upload
# the tarball to s3://<bucket>/host-images/<name>-<id>.tar and a pointer
# file s3://<bucket>/host-images/<name>.digest holding the full
# "sha256:<id>" string. The pointer is what a box reads first, so it knows
# which tarball to fetch and what to verify the loaded image against.
#
# A box never calls `docker pull` for any of these: scripts/benchmark/
# host_image_lib.sh's load_host_image (used by both run_shard.sh and
# setup_brain_go.sh) reads the pointer, downloads that exact tarball,
# `docker load`s it, and checks the loaded image's own ID against the
# pointer before anything uses it. Every benchmark box boots from a fresh EC2
# instance, so that tag exists locally only because of this load, and
# neither registry_mirror.sh's "docker run registry:2", kind's node
# creation, nor brain-go's own `make up` ever has a reason to contact a
# registry for one of these.
#
# Usage:
#   seed_host_images.sh --bucket <name> --brain-go-ref <ref> \
#                        [--region us-east-2] [--brain-go-repo <path>]
#
# --brain-go-ref is required (a tag, branch, or full SHA brain-go can
# resolve; same shape terraform/variables.tf's brain_go_ref accepts): it is
# what scripts/benchmark/brain_go_images.py derives brain-go's own six-ish
# host images from. --brain-go-repo is a local clone to read it from with a
# temporary `git worktree add --detach`, removed again once the derivation
# finishes; it never pushes, commits, or touches that clone's own branches.
# Defaults to $BRAIN_GO_REPO, then ../brain-go next to this checkout (the
# same sibling-repo convention brain-go's own docker-compose.yml uses for
# ../dashboard).
#
# Needs: docker, git, aws, and AWS credentials for the bucket (ambient:
# profile, SSO, instance role: plain `aws s3 cp`, so no special SSO handling
# is needed here the way registry_mirror.sh needs it for registry:2's own S3
# driver).
#
# Only the x86 kind node image is cached: every benchmark box and kind
# cluster is linux/amd64 (kind-config-x86.yaml; run_shard.sh always calls
# `setup_kind_cluster.sh x86`). kind-config-arm.yaml and its
# jacksonarthurclark/aiopslab-kind-arm image exist for an Apple Silicon
# developer's own local cluster, a path a benchmark box never takes, so
# caching the arm image would be dead weight this script does not add.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REGION="us-east-2"
BUCKET=""
BRAIN_GO_REF=""
# No guessed default: this script's own checkout can be the main sregym
# clone or a worktree nested an extra level or two deeper, so "../brain-go"
# relative to it does not reliably land on a brain-go clone either way.
# --brain-go-repo or $BRAIN_GO_REPO must name one explicitly.
BRAIN_GO_REPO="${BRAIN_GO_REPO:-}"

log() { echo "[seed-host-images] $*" >&2; }
die() { echo "[seed-host-images] ERROR: $*" >&2; exit 1; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    --bucket) BUCKET="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --brain-go-ref) BRAIN_GO_REF="$2"; shift 2 ;;
    --brain-go-repo) BRAIN_GO_REPO="$2"; shift 2 ;;
    *) die "unknown argument '$1'" ;;
  esac
done

usage="usage: $0 --bucket <name> --brain-go-ref <ref> --brain-go-repo <path> [--region us-east-2]"
[ -n "$BUCKET" ] || die "$usage"
[ -n "$BRAIN_GO_REF" ] || die "$usage"
[ -n "$BRAIN_GO_REPO" ] || die "$usage (or set \$BRAIN_GO_REPO)"
command -v docker >/dev/null 2>&1 || die "docker is required"
command -v git >/dev/null 2>&1 || die "git is required (to read brain-go's compose/Dockerfile at --brain-go-ref)"
command -v aws >/dev/null 2>&1 || die "aws is required"
[ -d "$BRAIN_GO_REPO/.git" ] || [ -f "$BRAIN_GO_REPO/.git" ] || die "no git checkout of brain-go at $BRAIN_GO_REPO; pass --brain-go-repo or set BRAIN_GO_REPO"

tmpdir="$(mktemp -d -t sregym-seed-host-images.XXXXXX)"
trap 'rm -rf "$tmpdir"' EXIT

# Read-only derivation against --brain-go-ref: a temporary, detached
# worktree (never a branch, never a commit, removed before this script does
# anything else) checked out from the developer's own local clone, then the
# exact same derivation setup_brain_go.sh runs on a benchmark box
# (scripts/benchmark/brain_go_images.py, against `docker compose config`),
# so the seed can never check a different set than the box later gates on.
safe_ref="${BRAIN_GO_REF//\//-}"
bg_worktree="$tmpdir/brain-go-$safe_ref"
log "checking out brain-go $BRAIN_GO_REF from $BRAIN_GO_REPO (read-only worktree) to derive its host images"
git -C "$BRAIN_GO_REPO" worktree add --detach --quiet "$bg_worktree" "$BRAIN_GO_REF" \
  || die "could not check out brain-go $BRAIN_GO_REF from $BRAIN_GO_REPO; fetch it there first"
cleanup_worktree() {
  git -C "$BRAIN_GO_REPO" worktree remove --force "$bg_worktree" >/dev/null 2>&1 || true
}
trap 'cleanup_worktree; rm -rf "$tmpdir"' EXIT

[ -f "$bg_worktree/deploy/.env.sample" ] || die "brain-go $BRAIN_GO_REF has no deploy/.env.sample; cannot resolve docker compose config"
cp "$bg_worktree/deploy/.env.sample" "$bg_worktree/deploy/.env"

BRAIN_GO_HOST_IMAGES=$(cd "$bg_worktree" && python3 "$SCRIPT_DIR/brain_go_images.py") \
  || die "could not derive brain-go's required host images at $BRAIN_GO_REF; see the error above"
[ -n "$BRAIN_GO_HOST_IMAGES" ] || die "brain_go_images.py derived zero required images for $BRAIN_GO_REF"

cleanup_worktree
trap 'rm -rf "$tmpdir"' EXIT

log "brain-go $BRAIN_GO_REF needs: $(printf '%s' "$BRAIN_GO_HOST_IMAGES" | cut -f2 | tr '\n' ' ')"

# "<name> <ref>", one pair per host image. "ref" is the exact tag a benchmark
# box's own scripts reference: loading a tarball under this same tag is what
# lets those scripts run unmodified once the image is cached. registry2 and
# kindnode-x86 are fixed (unrelated to any brain-go ref); the rest comes
# from the derivation above, one "<name>\t<ref>" per line, converted to the
# space-separated pairs the loop below already expects.
HOST_IMAGES="registry2 registry:2
kindnode-x86 jacksonarthurclark/aiopslab-kind-x86:latest
$(printf '%s\n' "$BRAIN_GO_HOST_IMAGES" | tr '\t' ' ')"

failures=""

# A heredoc, not a pipe, feeds this loop: `echo "$HOST_IMAGES" | while ...`
# would run the loop body in a subshell, and the "failures" accumulated
# inside it would vanish the moment the pipe's subshell exits, leaving the
# check after the loop unable to ever see a failure.
while IFS=' ' read -r name ref; do
  [ -n "$name" ] || continue
  log "=== $name ($ref) ==="

  log "pulling $ref --platform linux/amd64"
  if ! docker pull --platform linux/amd64 "$ref" >/dev/null; then
    log "FAILED to pull $ref"
    failures="$failures $name"
    continue
  fi

  image_id="$(docker inspect --format '{{.Id}}' "$ref")" || {
    log "FAILED to inspect $ref"
    failures="$failures $name"
    continue
  }
  hex="${image_id#sha256:}"

  tar="$tmpdir/$name.tar"
  log "saving $ref -> $tar"
  if ! docker save "$ref" -o "$tar"; then
    log "FAILED to save $ref"
    failures="$failures $name"
    continue
  fi
  size="$(wc -c <"$tar" | tr -d ' ')"

  key="host-images/${name}-${hex}.tar"
  log "uploading $tar (${size} bytes) -> s3://$BUCKET/$key"
  if ! aws s3 cp "$tar" "s3://$BUCKET/$key" --region "$REGION" --only-show-errors; then
    log "FAILED to upload $key"
    failures="$failures $name"
    continue
  fi

  log "pointing s3://$BUCKET/host-images/$name.digest -> $image_id"
  if ! printf '%s\n' "$image_id" | aws s3 cp - "s3://$BUCKET/host-images/$name.digest" --region "$REGION" --only-show-errors; then
    log "FAILED to write the $name.digest pointer"
    failures="$failures $name"
    continue
  fi

  rm -f "$tar"
  log "$name done: $image_id"
done <<EOF
$HOST_IMAGES
EOF

if [ -n "$failures" ]; then
  die "could not seed:$failures"
fi

log ""
log "registry:2, the kind node image, and brain-go $BRAIN_GO_REF's host images are cached in s3://$BUCKET/host-images/"
