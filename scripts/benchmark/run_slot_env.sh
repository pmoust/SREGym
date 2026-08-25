#!/usr/bin/env bash
# run_slot_env.sh
# Prints the environment that isolates one SREGym run from the others sharing a
# host, as `export` lines. Usage:
#
#   eval "$(scripts/benchmark/run_slot_env.sh 0)"
#   uv run python main.py ...
#
# Slot 0 reproduces the default ports exactly (8000, 9954, 16443), so adopting
# this changes no port for an existing single-run host. The cluster name and the
# two paths are per-slot at every slot including 0, which is the point of them.
#
# Creates the slot directory as a side effect, since kind will not create the
# parent of the kubeconfig it is told to write.
#
# What each variable isolates, and why the list is not only ports:
#
#   KIND_CLUSTER_NAME  kind names every cluster "kind" by default, so a second
#                      create fails outright.
#   KUBECONFIG         the harness has ~790 bare kubectl invocations and 18
#                      load_kube_config() calls, all resolving the default
#                      current-context. A per-slot file is what makes them all
#                      target their own cluster without touching a call site.
#                      Must be exported before python starts: the kubernetes
#                      client reads KUBECONFIG at module import time
#                      (kubernetes/config/kube_config.py), so setting it later
#                      silently keeps the old cluster.
#   SREGYM_CACHE_DIR   cluster_baseline_state.json is captured per cluster.
#                      Sharing it lets one run compare against another cluster's
#                      baseline, which fails silently rather than loudly.
#   API_PORT           conductor API, host-bound.
#   MCP_SERVER_PORT    host side of the MCP port-forward. The in-cluster service
#                      port stays 9954.
#   K8S_PROXY_PORT     the conductor's filtering proxy, host-bound.
#
# MCP_SERVER_URL is deliberately not set here: main.py derives it from the
# resolved MCP_SERVER_PORT, so setting both invites them to disagree.

set -euo pipefail

SLOT="${1:-}"
STATE_ROOT="${SREGYM_SLOT_ROOT:-${HOME}/.sregym-slots}"

die() {
    echo "run_slot_env.sh: $*" >&2
    exit 1
}

case "$SLOT" in
    "") die "usage: run_slot_env.sh <slot-index>" ;;
    *[!0-9]*) die "slot must be a non-negative integer, got '$SLOT'" ;;
esac

# Strip any leading zeros so "08" is 8 rather than an invalid octal literal.
SLOT=$((10#$SLOT))

# The three port families are 8000, 9954 and 16443. They stay disjoint well
# past any workable slot count; the cap is here so a typo cannot silently
# collide one family with the next.
if [ "$SLOT" -gt 500 ]; then
    die "slot $SLOT is out of range (max 500): the port families would overlap"
fi

SLOT_DIR="${STATE_ROOT}/${SLOT}"

# kind writes the kubeconfig but will not create its parent, and paths.py
# expects the cache dir to be usable.
mkdir -p "${SLOT_DIR}/cache" || die "could not create ${SLOT_DIR}/cache"

cat <<EOF
export KIND_CLUSTER_NAME="sregym-${SLOT}"
export KUBECONFIG="${SLOT_DIR}/kubeconfig"
export SREGYM_CACHE_DIR="${SLOT_DIR}/cache"
export API_PORT="$((8000 + SLOT))"
export MCP_SERVER_PORT="$((9954 + SLOT))"
export K8S_PROXY_PORT="$((16443 + SLOT))"
EOF
