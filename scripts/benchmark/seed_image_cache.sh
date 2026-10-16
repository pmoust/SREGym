#!/usr/bin/env bash
# Fill the S3-backed registry cache from scripts/benchmark/image_cache.list,
# once, from a developer machine with AWS credentials for the bucket
# (NOFireAI/sregym#150). A benchmark box never does this itself: it only ever
# serves read-only from what this script already put there (see run_shard.sh
# and registry_mirror.sh's "plain" mode).
#
# For each upstream host present in the list: start a writable, non-proxying
# registry:2 over S3 ("store" mode), `crane copy` every one of that host's
# images from the real upstream straight into it, stop it. Then, as
# verification: start a FRESH, read-only registry:2 over the same bucket
# ("plain" mode, the same container run_shard.sh starts on a benchmark box)
# and `crane validate --remote` every image through that, which fetches the
# manifest and every blob it references, not just the manifest. If an image's
# blobs did not really land in the bucket (or landed under a path the plain
# registry's clients will never ask for), this is where that shows up, not a
# benchmark run.
#
# This used to pull each image through a registry:2 configured as a
# pull-through proxy (REGISTRY_PROXY_REMOTEURL), and verify by re-pulling
# through a plain registry with `docker pull`. Both had real gaps:
# `docker pull` skips a layer already present in the local docker
# store, so a verification pull could report success while the bucket was
# missing a blob a different image happened to share; and registry:2 2.8.3,
# the version this script pulls, has no `proxy.ttl` config key at all (a
# previous version of this script set one, believing it suppressed proxy
# mode's default 7-day cache eviction; it did nothing, silently, since an
# unrecognized key under `proxy:` is not an error). The scheduler state
# behind that eviction lives in the bucket itself, so a stale proxy-mode seed
# left running, or resumed, could have expired and deleted content out from
# under a benchmark run days later. `crane copy` writes directly with no
# proxy and no eviction scheduler involved, and `crane validate --remote`
# reads every blob back from a registry process that never touched the local
# docker store, closing both gaps. Proxy mode is removed from
# registry_mirror.sh entirely (see git history for the previous version).
#
# Needs `crane` (https://github.com/google/go-containerregistry) on PATH:
# `brew install crane`, or `go install
# github.com/google/go-containerregistry/cmd/crane@latest`. Not run as a
# container: the only container image that would need (gcr.io/
# go-containerregistry/crane) is itself an upstream pull, and unlike
# registry:2 and the kind node image (cached in the bucket under
# scripts/benchmark/seed_host_images.sh, since the BOX needs those and must
# never reach Docker Hub) that pull only ever happens on a developer's own
# machine, which has no such restriction. A CLI a developer installs once is
# less machinery than a container this script would otherwise have to pull,
# tag, and invoke correctly on every platform it runs on.
#
# Every copy and validate pass is pinned to --platform linux/amd64, the
# architecture of every benchmark box and kind node (kind-config-x86.yaml):
# seeding from an Apple Silicon Mac with no pin caches only arm64 layers,
# which a benchmark box can never use.
#
# Usage:
#   seed_image_cache.sh --bucket <name> [--region us-east-2] [--list PATH]
#                        [--only <host>]
#
# --only limits both passes to one upstream host (see: registry_mirror.sh
# hosts), for seeding just the host a new image landed under rather than
# paying for every image in the list again.
#
# Needs: crane, and AWS credentials (ambient: profile, SSO, instance role)
# that can get/put/list on the bucket. Does not need a Kubernetes cluster,
# kind, or docker (crane talks to the registries directly over HTTP(S), never
# through a local docker daemon).
#
# Idempotent: re-running is safe. `crane copy` is a no-op push for a blob the
# destination already has, and the verification pass always reads the bucket
# fresh either way.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MIRROR="$SCRIPT_DIR/registry_mirror.sh"
LIST="$SCRIPT_DIR/image_cache.list"
REGION="us-east-2"
BUCKET=""
ONLY_HOST=""

log() { echo "[seed-image-cache] $*" >&2; }
die() { echo "[seed-image-cache] ERROR: $*" >&2; exit 1; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    --bucket) BUCKET="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --list) LIST="$2"; shift 2 ;;
    --only) ONLY_HOST="$2"; shift 2 ;;
    *) die "unknown argument '$1'" ;;
  esac
done

[ -n "$BUCKET" ] || die "usage: $0 --bucket <name> [--region us-east-2] [--list PATH] [--only <host>]"
[ -f "$LIST" ] || die "no image list at $LIST"
command -v crane >/dev/null 2>&1 || die "crane is required (brew install crane, or see this script's header comment)"

# refs_for_host <host> <list-file>: every "<path>:<tag>" line in the list
# belonging to that host, comments and blanks already stripped.
refs_for_host() {
  local host="$1" list="$2"
  grep -v '^[[:space:]]*#' "$list" | grep -v '^[[:space:]]*$' | while IFS= read -r line; do
    line="$(echo "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    [ -n "$line" ] || continue
    if [ "$("$MIRROR" host-for "$line")" = "$host" ]; then
      "$MIRROR" path-for "$line"
    fi
  done
}

hosts="$(bash "$MIRROR" hosts)"
if [ -n "$ONLY_HOST" ]; then
  hosts="$ONLY_HOST"
fi

seed_failures=""
verify_failures=""

log "=== seeding: crane copy from the real upstream into a writable registry over the bucket ==="

for host in $hosts; do
  refs="$(refs_for_host "$host" "$LIST")"
  if [ -z "$refs" ]; then
    log "no images for $host in $LIST, skipping"
    continue
  fi
  count=$(echo "$refs" | wc -l | tr -d ' ')
  log "=== $host: seeding $count image(s) ==="

  port="$(bash "$MIRROR" port "$host")"
  bash "$MIRROR" start-store "$host" "$BUCKET" "$REGION" 127.0.0.1 || die "could not start the $host store registry"

  while IFS= read -r ref; do
    [ -n "$ref" ] || continue
    src="${host}/${ref}"
    dst="127.0.0.1:${port}/${ref}"
    log "copying $src -> $dst"
    if ! crane copy --platform linux/amd64 --insecure "$src" "$dst" >/dev/null 2>&1; then
      log "FAILED to seed $host/$ref"
      seed_failures="$seed_failures $host/$ref"
    fi
  done <<EOF
$refs
EOF

  bash "$MIRROR" stop "$host"
done

log ""
log "=== verification: validating every image (manifest + every blob) through a fresh, read-only registry ==="

for host in $hosts; do
  refs="$(refs_for_host "$host" "$LIST")"
  [ -n "$refs" ] || continue

  port="$(bash "$MIRROR" port "$host")"
  bash "$MIRROR" start-plain "$host" "$BUCKET" "$REGION" 127.0.0.1 || die "could not start the $host verification registry"

  while IFS= read -r ref; do
    [ -n "$ref" ] || continue
    target="127.0.0.1:${port}/${ref}"
    log "validating $target"
    # --remote, not --tarball or a local pull: this has to read the blobs
    # back from the registry process itself, never from a local docker
    # store, or a blob two images share could be missing from the bucket and
    # this would never notice.
    # No --fast: that flag skips downloading and digesting every blob, which
    # is exactly the check this pass exists to do.
    if ! crane validate --remote "$target" --platform linux/amd64 --insecure >/dev/null 2>&1; then
      log "MISSING from the bucket: $host/$ref"
      verify_failures="$verify_failures $host/$ref"
    fi
  done <<EOF
$refs
EOF

  bash "$MIRROR" stop "$host"
done

if [ -n "$seed_failures" ] || [ -n "$verify_failures" ]; then
  log ""
  [ -n "$seed_failures" ] && log "did not seed:$seed_failures"
  [ -n "$verify_failures" ] && log "did not validate (copied but a manifest or blob could not be read back):$verify_failures"
  die "image cache seeding incomplete, see the image names above"
fi

log ""
log "every image in $LIST is seeded and verified in s3://$BUCKET"
log "registry:2 and the kind node image still need scripts/benchmark/seed_host_images.sh (run once, separately; see its header)"
