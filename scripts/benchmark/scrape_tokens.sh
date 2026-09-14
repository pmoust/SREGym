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
# "Most of the tokens" is in fact all of them, checked four ways against
# run-20260908-0739-4fc0249e and the 2026-09-10 20:12 run (386 .prom files):
#
#   - Only cmd/brain/internal/tworkersvc registers the chat-turn and RCI
#     workflows and the llm activities on the llm-calls task queue. The chat
#     service only starts workflows.
#   - chatsvc wires both WorkflowChat and LocalToolResolver, and
#     internal/chat/server/chat.go routes a cli_mode turn onto the workflow
#     when both are set, which they always are here.
#   - In run-20260908-0739-4fc0249e shard-0 the chat container logs four
#     startup lines for the whole run and nothing per turn, while tworker
#     logs 36 "chat-turn workflow started" and 36 "rci investigation
#     workflow started".
#   - rci_ series can only come from tworker, because rci.ConfigureMetrics is
#     called in tworkersvc and nowhere else. They are in the archives.
#
# Across both runs the chat endpoint contributed 0 llm_ samples and tworker
# contributed 99,349. Do not re-derive this from marker placement alone.
#
# Counters are cumulative for the life of the process, so the last scrape wins
# and calling this repeatedly is free. It is invoked from sync_logs, which runs
# on the 60s watcher and on every exit path.

set -uo pipefail

OUT="${1:-/var/log/sregym-tokens.prom}"
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

# Which samples are kept from each /metrics payload. This is an allowlist, not
# a passthrough: brain-go's registry also serves the Go runtime and process
# collectors, so the payload carries go_*, process_* and promhttp_* series that
# say nothing about a run, and this file is written per shard per attempt.
#
# The cost of an allowlist is that a new counter stays invisible here until its
# prefix is added, and that has already happened once:
# custom_mcp_breaker_trips_total shipped in brain-go so CUSTOM_MCP breaker
# trips could be counted without repeating a 63-attempt run, and every archived
# .prom file written before this pattern widened holds llm_ samples only. Add
# the prefix in the same change that adds the counter.
#
#   llm_         token and call counts, what a run is priced from
#   custom_mcp_  CUSTOM_MCP circuit-breaker trips
#   rci_         RCI investigation counters
KEEP='^(llm_|custom_mcp_|rci_)'

emit() { printf '# scrape: %s\n' "$*" >>"$TMP"; }

# Each target's marker is written BEFORE that target's samples, so the samples
# between one marker and the next belong to the target the marker names.
#
# It used to be written after, and nothing in the file said so. Every reader
# took it for a header, which assigns every sample to the wrong target and
# inverts the whole picture: the chat marker appeared to introduce tworker's
# tens of thousands of llm_ series, and the tworker marker appeared to introduce
# nothing at all. That reading says the chat service does the LLM work and the
# worker fleet does none, which is the opposite of what happens. Files archived
# before this change carry the old order; read a marker there as closing the
# samples above it, not opening the samples below it.
#
# No parser was affected. token_delta.py matches llm_ sample lines and
# score_run.py matches llm_tokens_total{ ; neither looks at a comment. The cost
# was paid entirely by people reading the archive.
#
# The marker also carries the number of samples kept, because "reachable" and
# "had something to report" are different facts and only the first was recorded.
# A target that answers with an exposition carrying none of the allowlisted
# series is a real possibility (a role that never registered the instruments, a
# process restarted mid-run) and it used to look identical to a healthy scrape.
# An answer with no body at all is louder still: a Prometheus handler always
# serves at least the runtime collectors, so zero bytes means the thing on that
# port is not the endpoint we think it is.
scrape() {
  local name="$1" url="$2"
  local body kept

  if ! body=$(curl -sf --max-time 5 "$url" 2>/dev/null); then
    emit "$name unreachable ($url)"

    return 1
  fi

  if [ -z "$body" ]; then
    emit "$name EMPTY BODY ($url); not a Prometheus endpoint"

    return 1
  fi

  kept=$(printf '%s\n' "$body" | grep -Ec "$KEEP") || true
  emit "$name ok ($url) ${kept:-0} samples"
  printf '%s\n' "$body" | grep -E "$KEEP" >>"$TMP" || true

  return 0
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

# Only replace the previous scrape once this one produced token samples, so a
# transient failure does not destroy the last good capture.
#
# This tests ^llm_ rather than $KEEP because the allowlist admits a series that
# is exported whether or not anything happened: custom_mcp_breaker_trips_total
# is a counter with no labels and its child is created at wiring time, so both
# brain-go services serve it at 0 from their first scrape. Testing $KEEP here
# would accept "curl answered" as success, and a scrape that reached only the
# chat service would overwrite a full capture with that one line. The chat
# service has produced 0 token samples in every archived run so far; all of
# them come from tworker.
if grep -q '^llm_' "$TMP" 2>/dev/null; then
  mv "$TMP" "$OUT"
  trap - EXIT
else
  # Keep the diagnostic header even with no samples: "we looked and found
  # nothing" and "we never looked" are different failures.
  [ -s "$OUT" ] || mv "$TMP" "$OUT"
  trap - EXIT
fi
