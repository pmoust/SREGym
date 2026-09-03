#!/usr/bin/env bash
# Capture brain-go's LLM token counters so a run can be priced.
#
# SREGym's Table 3 reports "# Tokens" per problem alongside success rate, and
# we had no equivalent: the brain_go driver never reads usage back (run_turn
# returns a bare string), brain-go's /cli/chat emits no usage frame, and
# brain_go is absent from atif_converter's SUPPORTED_AGENTS so results/traces.db
# is never written for it. The first two full runs therefore have no token
# figure at all and cannot be priced.
#
# brain-go does record it, as a Prometheus counter:
#
#   llm_tokens_total{provider, model, token_type, account_id, user_id, thread_id}
#
# thread_id is per attempt, but it does NOT identify the problem any more. This
# comment used to say the driver sets it to cli:sregym-<problem_id>-<hex8>, so
# one scrape attributed tokens to problems for free. Since #28 the driver opens
# an investigation and uses the thread brain-go returns, which is
# cli:inv-<uuid>: run-20260903-0558's counters are labelled
# cli:inv-0248e16e-... and cli:inv-fb383882-... and carry no problem id. The
# fallback name is still the old form, so the old attribution works only when
# opening an investigation fails.
#
# So this file is the run total, and per-attempt attribution comes from
# token_delta.py, which brackets each attempt and records the difference on the
# result row (#67). Both are wanted: the totals here survive an attempt whose
# delta was lost, and the deltas say which problem spent what.
#
# Two endpoints, because the work is split across two services and only one of
# them is reachable the obvious way:
#
#   chat     METRICS_PORT=9091, published to 127.0.0.1 by deploy/docker-compose.yml
#   tworker  TWORKER_METRICS_PORT=9092, publishes NO ports at all
#
# The RCI investigation runs as a Temporal workflow, so its llmStream and
# llmComplete activities execute on tworker. That is where most of the tokens
# are, and it is the one with no published port. Rather than change brain-go's
# compose file, this reaches the container over the docker bridge, which works
# because the host is Linux. It would not work on Docker Desktop.
#
# Counters are cumulative for the life of the process, so the last scrape wins
# and calling this repeatedly is free. It is invoked from sync_logs, which runs
# on the 60s watcher and on every exit path.

set -uo pipefail

OUT="${1:-/var/log/sregym-tokens.prom}"
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

emit() { printf '# scrape: %s\n' "$*" >>"$TMP"; }

scrape() {
  local name="$1" url="$2"
  local body
  if body=$(curl -sf --max-time 5 "$url" 2>/dev/null); then
    printf '%s\n' "$body" | grep -E '^llm_(tokens|calls)_total' >>"$TMP" || true
    emit "$name ok ($url)"

    return 0
  fi
  emit "$name unreachable ($url)"

  return 1
}

emit "at $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# chat: published on loopback, so the direct address works.
scrape chat "http://127.0.0.1:9091/metrics" || true

# tworker: no published port. Resolve the container's bridge address instead of
# adding one, so this needs no change to brain-go's compose file.
TW_IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' \
        "$(docker ps -q --filter 'name=tworker' | head -1)" 2>/dev/null | awk '{print $1}')
if [ -n "${TW_IP:-}" ]; then
  scrape tworker "http://${TW_IP}:9092/metrics" || true
else
  emit "tworker container not found; no token counts from the worker fleet"
fi

# Only replace the previous scrape once this one produced something, so a
# transient failure does not destroy the last good capture.
if grep -q '^llm_' "$TMP" 2>/dev/null; then
  mv "$TMP" "$OUT"
  trap - EXIT
else
  # Keep the diagnostic header even with no samples: "we looked and found
  # nothing" and "we never looked" are different failures.
  [ -s "$OUT" ] || mv "$TMP" "$OUT"
  trap - EXIT
fi
