#!/usr/bin/env bash
# Put brain-go's own logs where the run archive can reach them.
#
# brain-go runs under docker compose on the instance, so its logs live in the
# container runtime and die with the box. Everything the archive holds is
# harness-side, which means the agent's behaviour is only ever visible through
# what the harness saw of it.
#
# run-20260902-1556 is why this exists. The driver sent an incident window and
# the agent then cited evidence from before the fault, and the archive cannot
# say which of two things happened: brain-go never parsed the window and used
# its own one-hour default, or it parsed it and the agent queried outside it
# anyway. The derived incident and baseline windows are logged by brain-go and
# nowhere else, so the question is unanswerable off the box (sregym#52).
#
# Usage: dump_brain_go_logs.sh <output-path>

set -uo pipefail

OUT="${1:?usage: dump_brain_go_logs.sh <output-path>}"
BRAIN_GO_DIR="${BRAIN_GO_DIR:-/opt/brain-go}"

if [ ! -d "$BRAIN_GO_DIR" ]; then
  echo "[dump-brain-go-logs] no checkout at $BRAIN_GO_DIR, nothing to dump" >&2
  exit 0
fi

# --no-color because these are read as text, and --timestamps because compose
# interleaves services and the per-line container clock is the only way to line
# an entry up against the conductor's fault-injection time.
#
# --tail is bounded rather than "all": a full run's chat-service log is large,
# this is uploaded on every sync pass, and the recent tail is what a
# post-mortem reads. Raise it if a question needs earlier turns.
if ! docker compose --project-directory "$BRAIN_GO_DIR" \
     --env-file "$BRAIN_GO_DIR/deploy/.env" \
     logs --no-color --timestamps --tail "${BRAIN_GO_LOG_TAIL:-20000}" >"$OUT" 2>&1; then
  # Keep whatever landed. A compose invocation that fails still usually writes
  # the reason, and an empty file is more use than a missing one: it says the
  # dump was attempted and shows why it did not work.
  echo "[dump-brain-go-logs] docker compose logs failed; keeping partial output" >&2
fi
