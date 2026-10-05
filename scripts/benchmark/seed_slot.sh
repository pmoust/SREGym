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
# SREGYM_ARM selects what gets registered. Under "mcp" (the default) that is
# the four CUSTOM_MCP endpoints and nothing else, byte for byte what every
# archived run seeded. Under "native" it is brain-go's own Kubernetes,
# Prometheus, Loki and Jaeger connections against the same backends, plus the
# edge API key the in-cluster agent publishes the entity graph with. Under
# "edge" it is the same three telemetry connections and edge API key, minus
# the Kubernetes connection and its kubeconfig. These are mutually exclusive:
# seeding more than one would give every node two servers for the same signal
# and the comparison would measure neither arm.
#
# Usage:
#   scripts/benchmark/seed_slot.sh <customer> <mcp-port> [brain-api-url]
#
# On success, prints three `export` lines to stdout. Capture them and check
# the status before the eval, the way run_shard.sh's eval_exports does, because
# eval of a substitution returns eval's status and hides a failure:
#   if out=$(scripts/benchmark/seed_slot.sh sregym-shard-0-slot-1 9955); then
#     eval "$out"
#   else
#     echo "seeding failed"
#   fi
#
#   BRAIN_API_KEY        the CLI token, for /cli/chat on the chat service
#   BRAIN_SESSION        the owner session cookie, for the api service
#   BRAIN_ADMIN_API_URL  the api service base
#
# The native arm prints a fourth, BRAIN_EDGE_API_KEY, which the caller hands
# to the edge agent's Helm release.
#
# The last two exist because a CLI API key carries no permissions at all
# (authz.go's APIServicePermissions returns nil for APIKeyTypeCLI), so anything
# on the api service authenticates with the session instead. The driver needs
# it to open an investigation per problem, which is what makes a run's results
# persist (#28).
#
# Tracked by NOFireAI/brain-go#2219.

set -euo pipefail

BRAIN_GO_DIR="${BRAIN_GO_DIR:-/opt/brain-go}"
CUSTOMER="${1:?usage: seed_slot.sh <customer> <mcp-port> [brain-api-url]}"
MCP_PORT="${2:?usage: seed_slot.sh <customer> <mcp-port> [brain-api-url]}"
# Deliberately does NOT fall back to $BRAIN_API_URL. That name is already taken
# in this deployment: setup_brain_go.sh exports it pointing at the CHAT service
# (8001), because that is what the driver talks to. Connections are an
# api-service concern -- POST /api/connections and the csrfCookie middleware
# both live there -- and chat answers GET /health with a 200 and no CSRF-TOKEN,
# so inheriting it failed with "no CSRF-TOKEN cookie in the /health response
# (200)", which points at cookies rather than at the wrong port. Take the URL
# as an argument or use the api default; never guess from the environment.
BRAIN_API_URL="${3:-http://127.0.0.1:8000}"
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
# RFC1918 checked because every connection this script seeds, in either arm,
# is built on the host's RFC1918 address. brain-go's guard is not the reason
# for rejecting a public address: it does not refuse one. Under the private
# allowance it still refuses loopback, link-local and CGNAT.
case "$PRIVATE_IP" in
  10.*|192.168.*|172.1[6-9].*|172.2[0-9].*|172.3[0-1].*) ;;
  *) die "resolved '$PRIVATE_IP', which is not RFC1918; every seeded connection expects the host's RFC1918 address" ;;
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
# 1>&2 is not tidiness: this script's stdout is a contract. The caller evals
# it, so anything on stdout other than the export lines at the end is executed
# as shell. brainctl's summary went to stdout and
# eval choked on its parenthesis:
#   eval: syntax error near unexpected token `('
#   eval: `Dev stack ready: account 3 (created), ... 4 automation(s) seeded.'
log "seeding tenant $CUSTOMER"
docker compose --env-file deploy/.env run --rm -T --no-deps \
  -e DEV_API_KEY="$DEV_API_KEY" \
  -e DEV_SESSION="$DEV_SESSION" \
  seed seed dev-stack --customer "$CUSTOMER" 1>&2 \
  || die "brainctl seed dev-stack failed for $CUSTOMER"

# --- where this script's siblings live ---------------------------------------
# Resolved before the arm branch, because both arms call a script beside this
# one and this script has already cd'd into $BRAIN_GO_DIR. --project, not a
# bare `uv run`: that directory is a Go repo with no pyproject.toml, so a
# plain `uv run` resolves its project from the wrong tree and silently falls
# back to a Python with none of this repo's dependencies installed --
# confirmed directly (ModuleNotFoundError: requests, a real, declared
# dependency that uv sync had already installed).
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SREGYM_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# --- seed_mcp: the four CUSTOM_MCP connections -------------------------------
# Unchanged from before the arm flag existed, and it must stay that way: this
# is the arm every other arm is compared against.
seed_mcp() {
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
  local deadline
  deadline=$(( $(date +%s) + READY_TIMEOUT ))
  until [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "${MCP_BASE}/kubectl/sse" 2>/dev/null)" = "200" ]; do
    [ "$(date +%s)" -lt "$deadline" ] || die "SREGym MCP server not reachable at $MCP_BASE"
    sleep 5
  done

  log "registering four CUSTOM_MCP connections"
  uv run --project "$SREGYM_DIR" python "${SCRIPT_DIR}/seed_connections.py" \
    --brain-url "$BRAIN_API_URL" \
    --mcp-base "$MCP_BASE" \
    --session "$DEV_SESSION" \
    --session-id "sregym-${CUSTOMER}" \
    1>&2
}

# --- seed_native: brain-go's own connectors, plus the edge key ---------------
# Shared by the native and edge arms: both register the same three telemetry
# connections and mint the same edge API key. Native also registers a
# Kubernetes connection, which needs the read-only kubeconfig run_shard.sh
# built; edge seeds nothing for the Kubernetes API, so that step and its
# kubeconfig requirement are skipped.
#
# The three telemetry URLs are the host side of the port-forwards run_shard.sh
# keeps alive to prometheus-server, loki and jaeger-out in the observe
# namespace: the same three backends the MCP endpoints proxy to, reached
# directly instead of through them.
#
# The forwards are not waited on here. Those services belong to the
# application, which the conductor deploys per problem, so at slot-seed time
# they do not exist yet and will not until the first problem starts. What
# proves they are reachable is the per-attempt gate in the pre-fault window,
# which is also the only point at which the answer matters.
seed_native() {
  [ -n "${SREGYM_PROMETHEUS_PORT:-}" ] && [ -n "${SREGYM_LOKI_PORT:-}" ] && [ -n "${SREGYM_JAEGER_PORT:-}" ] \
    || die "the native and edge arms need SREGYM_PROMETHEUS_PORT, SREGYM_LOKI_PORT and SREGYM_JAEGER_PORT from run_slot_env.sh"

  if [ "$ARM" = "native" ]; then
    [ -n "${SREGYM_NATIVE_KUBECONFIG:-}" ] \
      || die "the native arm needs SREGYM_NATIVE_KUBECONFIG, the read-only kubeconfig native_arm.sh wrote"
    [ -f "$SREGYM_NATIVE_KUBECONFIG" ] \
      || die "no read-only kubeconfig at $SREGYM_NATIVE_KUBECONFIG"

    log "registering native connections, Kubernetes included"
    uv run --project "$SREGYM_DIR" python "${SCRIPT_DIR}/seed_native_connections.py" \
      --brain-url "$BRAIN_API_URL" \
      --session "$DEV_SESSION" \
      --prometheus-url "http://${PRIVATE_IP}:${SREGYM_PROMETHEUS_PORT}" \
      --loki-url "http://${PRIVATE_IP}:${SREGYM_LOKI_PORT}" \
      --jaeger-url "http://${PRIVATE_IP}:${SREGYM_JAEGER_PORT}" \
      --kubeconfig-file "$SREGYM_NATIVE_KUBECONFIG" \
      1>&2 \
      || die "native connection seeding failed"
  else
    log "registering native connections, no Kubernetes (edge arm)"
    uv run --project "$SREGYM_DIR" python "${SCRIPT_DIR}/seed_native_connections.py" \
      --brain-url "$BRAIN_API_URL" \
      --session "$DEV_SESSION" \
      --prometheus-url "http://${PRIVATE_IP}:${SREGYM_PROMETHEUS_PORT}" \
      --loki-url "http://${PRIVATE_IP}:${SREGYM_LOKI_PORT}" \
      --jaeger-url "http://${PRIVATE_IP}:${SREGYM_JAEGER_PORT}" \
      1>&2 \
      || die "edge connection seeding failed"
  fi

  log "minting this slot's edge API key"
  EDGE_API_KEY="$(uv run --project "$SREGYM_DIR" python "${SCRIPT_DIR}/mint_edge_key.py" \
    --brain-url "$BRAIN_API_URL" \
    --session "$DEV_SESSION" \
    --name "sregym-edge-${CUSTOMER}")" \
    || die "could not mint the edge API key"
  [ -n "$EDGE_API_KEY" ] || die "the minted edge API key is empty"
}

# --- which arm this slot seeds -----------------------------------------------
# Resolved by arm.py rather than read here, so a typo is a hard stop in one
# place instead of a silent fallback in several.
ARM="$(uv run --project "$SREGYM_DIR" python "${SCRIPT_DIR}/arm.py")" \
  || die "SREGYM_ARM is not a recognised arm; see the message above"
log "arm: $ARM"

if [ "$ARM" = "native" ] || [ "$ARM" = "edge" ]; then
  seed_native
else
  seed_mcp
fi

log "ready"
printf 'export BRAIN_API_KEY=%q\n' "$DEV_API_KEY"
printf 'export BRAIN_SESSION=%q\n' "$DEV_SESSION"
printf 'export BRAIN_ADMIN_API_URL=%q\n' "$BRAIN_API_URL"
if [ -n "${EDGE_API_KEY:-}" ]; then
  printf 'export BRAIN_EDGE_API_KEY=%q\n' "$EDGE_API_KEY"
fi
