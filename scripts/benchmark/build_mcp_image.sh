#!/usr/bin/env bash
# Build the MCP server image from this checkout and name it after the commit.
#
# The benchmark pod used to run ghcr.io/sregym/sregym-mcp:latest, which is
# upstream's build and not this repo's. That image was built 2026-09-06, so it
# has neither the /healthz route nor the anyio.to_thread.run_sync offload that
# keeps a blocking kubectl call off the single event loop. Every fix this fork
# made to the MCP server was therefore absent from every pod it ran: the 43
# port-forward restarts in run-20260921-1250 are that absence, a
# `kubectl rollout status --timeout=120s` holding the loop while the
# supervisor's probe went unanswered.
#
# The tag is the commit, not "latest", so `kubectl get pod -o jsonpath` names
# the code the pod ran rather than a moving pointer.
#
# Usage:
#   build_mcp_image.sh                build, then print the reference
#   build_mcp_image.sh --print-name   print the reference, build nothing
#
# The build log goes to stderr so stdout carries the reference alone and a
# caller can assign it. Two callers exist: run_shard.sh builds once per box,
# and write_run_manifest.sh asks for the name only, so the manifest cannot
# record a tag nothing was built under.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# A local name with no registry host. Nothing can pull it, which is the point:
# the only copy is the one `kind load docker-image` puts in the node, and a pod
# that cannot find it must fail rather than silently fall back to a registry.
IMAGE_REPO=sregym-mcp

SHA="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null)"
if [ -z "$SHA" ]; then
  echo "build_mcp_image.sh: cannot resolve a commit for $REPO_ROOT" >&2
  exit 1
fi

IMAGE="$IMAGE_REPO:$SHA"

if [ "${1:-}" = "--print-name" ]; then
  echo "$IMAGE"
  exit 0
fi

# mcp_server/Dockerfile's own FROM line. A plain `docker build` with no local
# copy of this tag pulls it from Docker Hub itself, which is the one Docker
# Hub reach a benchmark box would otherwise have outside kind. On a
# benchmark box
# (SREGYM_REGISTRY_MIRROR_ADDR set by run_shard.sh once the registry mirror
# containers are up) this image is pulled through that mirror instead and
# retagged to the bare name the Dockerfile asks for, so the build below finds
# it already present and never contacts Docker Hub. On a developer machine
# (no mirror running) this is a no-op and `docker build` pulls the base image
# normally, same as before.
BASE_IMAGE="python:3.12-slim"
if [ -n "${SREGYM_REGISTRY_MIRROR_ADDR:-}" ]; then
  port="$("$REPO_ROOT/scripts/benchmark/registry_mirror.sh" port docker.io)" || {
    echo "build_mcp_image.sh: could not resolve the docker.io registry mirror port" >&2
    exit 1
  }
  mirror_ref="127.0.0.1:${port}/library/${BASE_IMAGE}"
  if ! docker pull "$mirror_ref" >&2; then
    echo "build_mcp_image.sh: could not pull $BASE_IMAGE through the registry mirror ($mirror_ref); re-run scripts/benchmark/seed_image_cache.sh" >&2
    exit 1
  fi
  if ! docker tag "$mirror_ref" "$BASE_IMAGE" >&2; then
    echo "build_mcp_image.sh: could not tag $mirror_ref as $BASE_IMAGE" >&2
    exit 1
  fi
fi

# The build context is the repo root, not mcp_server/: the Dockerfile copies
# pyproject.toml, uv.lock, sregym/, clients/, scripts/, llm_backend/ and
# logger/ alongside mcp_server/.
if ! docker build -f "$REPO_ROOT/mcp_server/Dockerfile" -t "$IMAGE" "$REPO_ROOT" >&2; then
  echo "build_mcp_image.sh: docker build failed for $IMAGE" >&2
  exit 1
fi

echo "$IMAGE"
