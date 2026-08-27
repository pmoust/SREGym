#!/usr/bin/env bash
# Seed one additional slot's tenant (account, API key, session, and MCP
# connections) against an already-running shared brain-go stack.
#
# Safe to call once per concurrent SREGym slot on the same box: each call
# generates its own credentials and its own account, so N calls produce N
# tenants that cannot see each other's connections or entity graph. Reusing
# one raw token across slots is not an option -- brainctl seed dev-stack
# hashes the given raw token and derives the api_keys row's token_id from it
# (see internal/services/devseed/devseed.go's tokenRE), and that column is
# what authentication resolves a bearer token by. Two accounts sharing one
# raw token would collide on that id, not just on the account name.
#
# This does not bring up the stack -- see setup_brain_go.sh for the one-time
# `make up`, which only needs to happen once per instance no matter how many
# slots it runs. Pairs with run_slot_env.sh, which isolates the kind
# cluster/kubeconfig/cache dir and the MCP/proxy/conductor host ports each
# slot's harness process needs; this script only owns the brain-go side.
#
# Usage:
#   scripts/benchmark/seed_slot.sh <customer> <mcp-port> [brain-api-url]
#
# On success, prints `export BRAIN_API_KEY=...` to stdout so a caller can:
#   eval "$(scripts/benchmark/seed_slot.sh sregym-shard-0-slot-1 9955)"
#
# Tracked by NOFireAI/brain-go#2219.

set -euo pipefail

BRAIN_GO_DIR="${BRAIN_GO_DIR:-/opt/brain-go}"
CUSTOMER="${1:?usage: seed_slot.sh <customer> <mcp-port> [brain-api-url]}"
MCP_PORT="${2:?usage: seed_slot.sh <customer> <mcp-port> [brain-api-url]}"
BRAIN_API_URL="${3:-${BRAIN_API_URL:-http://127.0.0.1:8000}}"
READY_TIMEOUT="${BRAIN_GO_READY_TIMEOUT:-300}"

log() { echo "[seed-slot:${CUSTOMER}] $*" >&2; }
die() { echo "[seed-slot:${CUSTOMER}] ERROR: $*" >&2; exit 1; }

# Matches the Makefile's own dashboard-e2e admin-target seeding
# (target_token="cli_$$(openssl rand -hex 16)_$$(openssl rand -hex 16)").
hex() { openssl rand -hex "$1"; }

[ -d "$BRAIN_GO_DIR" ] || die "brain-go checkout not found at $BRAIN_GO_DIR"
cd "$BRAIN_GO_DIR"

# --- the private address the MCP connections must use ------------------------
# Same reasoning as setup_brain_go.sh: the SSRF guard blocks loopback outright
# and brain-go dials from inside a container, where localhost is the
# container's own loopback, so the host's private address is required.
PRIVATE_IP="${SREGYM_PRIVATE_IP:-$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1)}"
[ -n "$PRIVATE_IP" ] || die "could not determine a private IPv4 address; set SREGYM_PRIVATE_IP"
case "$PRIVATE_IP" in
  10.*|192.168.*|172.1[6-9].*|172.2[0-9].*|172.3[0-1].*) ;;
  *) die "resolved '$PRIVATE_IP', which is not RFC1918; allow_private_endpoint would not cover it" ;;
esac
MCP_BASE="http://${PRIVATE_IP}:${MCP_PORT}"
log "private address $PRIVATE_IP, MCP base $MCP_BASE"

# --- mint this slot's own credentials -----------------------------------------
DEV_API_KEY="cli_$(hex 16)_$(hex 16)"
DEV_SESSION="session_$(hex 16)_$(hex 15)"

# --- seed the tenant, bypassing the shared .env entirely ---------------------
# `docker compose run` overrides both the seed service's command and, via -e,
# its environment for this one invocation, so this never touches deploy/.env
# and is safe to run concurrently for other slots doing the same.
#
# --no-deps is load-bearing, not tidy: without it, `run` also resolves seed's
# depends_on (postgres, migrate) and can decide to recreate them if it judges
# their config to differ even slightly from how the running stack started
# them -- confirmed the hard way, this took postgres down for the whole shared
# stack for several seconds while every slot's tworker held connections to it.
# Matches the Makefile's own dashboard-e2e admin-target seeding
# ($(DASHBOARD_E2E_COMPOSE) run --rm -T --no-deps -e ... seed seed dev-stack
# --customer ...), which already established this is safe to run repeatedly
# against an already-running stack. -T disables pseudo-TTY allocation, which
# this needs for unattended/scripted use.
log "seeding tenant $CUSTOMER"
docker compose --env-file deploy/.env run --rm -T --no-deps \
  -e DEV_API_KEY="$DEV_API_KEY" \
  -e DEV_SESSION="$DEV_SESSION" \
  seed seed dev-stack --customer "$CUSTOMER" \
  || die "brainctl seed dev-stack failed for $CUSTOMER"

# --- register the MCP connections --------------------------------------------
# /kubectl/sse is a Server-Sent Events endpoint: it holds the connection open
# to stream rather than answering and closing. setup_brain_go.sh's version of
# this check (`curl -s -o /dev/null --max-time 3 ...` in an until loop) can
# never succeed once the server is actually up: curl reads the 200 status line
# promptly, then blocks on the body, which never ends, so --max-time always
# fires and curl's own exit code is always 28 (timeout) -- confirmed directly,
# five consecutive attempts against a server already proven reachable by a
# plain manual curl all returned http_code=200 and exit 28. The fix is to read
# the status curl captured rather than trust its exit code: a 200 means the
# endpoint answered, whether or not the (infinite) stream ever finished.
log "waiting for the SREGym MCP server at $MCP_BASE"
deadline=$(( $(date +%s) + READY_TIMEOUT ))
until [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "${MCP_BASE}/kubectl/sse" 2>/dev/null)" = "200" ]; do
  [ "$(date +%s)" -lt "$deadline" ] || die "SREGym MCP server not reachable at $MCP_BASE"
  sleep 5
done

log "registering four CUSTOM_MCP connections"
# --project, not a bare `uv run`: this script cd'd into $BRAIN_GO_DIR (a Go
# repo, no pyproject.toml) and never returns to the sregym checkout, so a
# plain `uv run` resolves its project from the wrong directory and silently
# falls back to a Python with none of this repo's dependencies installed --
# confirmed directly (ModuleNotFoundError: requests, a real, declared
# dependency that uv sync had already installed). setup_brain_go.sh has the
# identical bug for the identical reason.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SREGYM_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
uv run --project "$SREGYM_DIR" python "${SCRIPT_DIR}/seed_connections.py" \
  --brain-url "$BRAIN_API_URL" \
  --mcp-base "$MCP_BASE" \
  --session "$DEV_SESSION" \
  --session-id "sregym-${CUSTOMER}" \
  1>&2

log "ready"
printf 'export BRAIN_API_KEY=%q\n' "$DEV_API_KEY"
