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

# The build context is the repo root, not mcp_server/: the Dockerfile copies
# pyproject.toml, uv.lock, sregym/, clients/, scripts/, llm_backend/ and
# logger/ alongside mcp_server/.
if ! docker build -f "$REPO_ROOT/mcp_server/Dockerfile" -t "$IMAGE" "$REPO_ROOT" >&2; then
  echo "build_mcp_image.sh: docker build failed for $IMAGE" >&2
  exit 1
fi

echo "$IMAGE"
